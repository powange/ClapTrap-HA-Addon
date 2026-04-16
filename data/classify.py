import time
import requests
import ffmpeg
import logging
import numpy as np
import sounddevice as sd
from mediapipe.tasks import python
from mediapipe.tasks.python.components import containers
from mediapipe.tasks.python import audio
from flask_socketio import SocketIO
import json
import warnings
import wave
import os
import collections
import sys
import threading
from vban_manager import get_vban_detector
from audio_detector import AudioDetector
from settings_manager import load_settings
from webhook import send_webhook_async

warnings.filterwarnings("ignore", category=UserWarning, module="google.protobuf.symbol_database")


DEFAULT_SOUND_WHITELIST = {"Clapping": True, "Hands": True, "Applause": True}


def build_sources_from_settings(settings):
    """Construit la liste des sources depuis les settings."""
    sources = []
    global_threshold = float(settings.get('global', {}).get('threshold', 0.5))
    mic = settings.get('microphone', {})
    if mic.get('enabled', False):
        mic_name = mic.get('audio_source', 'default')
        sources.append({
            'type': 'mic', 'audio_source': mic_name,
            'source_key': str(mic.get('device_index', 0)),
            'webhook_url': mic.get('webhook_url', ''),
            'threshold': float(mic.get('threshold', global_threshold)),
            'ha_entities': mic.get('ha_entities', [1, 2]),
            'sound_whitelist': dict(mic.get('sound_whitelist') or DEFAULT_SOUND_WHITELIST),
            'label': f'Micro: {mic_name}' if mic_name != 'default' else 'Microphone'
        })
    for src in settings.get('rtsp_sources', []):
        if src.get('enabled', False) and src.get('url'):
            url = src['url'] if src['url'].startswith('rtsp') else f"rtsp://{src['url']}"
            sources.append({
                'type': 'rtsp', 'stream_id': src.get('id', ''),
                'source_key': src.get('id', ''),
                'audio_source': url, 'rtsp_url': src['url'],
                'webhook_url': src.get('webhook_url', ''),
                'gain': src.get('gain', 10),
                'threshold': float(src.get('threshold', global_threshold)),
                'ha_entities': src.get('ha_entities', [1, 2]),
                'sound_whitelist': dict(src.get('sound_whitelist') or DEFAULT_SOUND_WHITELIST),
                'label': f'RTSP: {src.get("name", src["url"][:30])}'
            })
    for src in settings.get('saved_vban_sources', []):
        if src.get('enabled', True):
            sources.append({
                'type': 'vban', 'audio_source': f"vban://{src['ip']}",
                'source_key': src.get('ip', ''),
                'webhook_url': src.get('webhook_url', ''),
                'threshold': float(src.get('threshold', global_threshold)),
                'gain': float(src.get('gain', 1)),
                'ha_entities': src.get('ha_entities', [1, 2]),
                'sound_whitelist': dict(src.get('sound_whitelist') or DEFAULT_SOUND_WHITELIST),
                'label': f'VBAN: {src.get("name", src["ip"])}'
            })
    return sources

# Variables globales
detection_running = False
_detection_lock = threading.Lock()
classifier = None
record = None
model = "yamnet.tflite"
current_audio_source = None
_socketio = None

_detection_history = collections.deque(maxlen=50)
_history_lock = threading.Lock()
_rtsp_gains = {}  # {rtsp_url: volume_float} — modifiable en temps réel
_vban_gains = {}  # {vban_ip: volume_float} — modifiable en temps réel
_active_detectors = []  # Liste des AudioDetector actifs (pour mise à jour en temps réel)
_active_detectors_lock = threading.Lock()
# Registre source_id -> AudioDetector actif, pour la mise à jour live de la whitelist
_detectors_by_source_id = {}
# Ensemble des labels déjà "vus" pour chaque source (évite les emits en boucle)
_seen_labels_by_source = {}
# Mapping source_id -> (kind, source_key) pour savoir où écrire dans les settings
_source_info_by_id = {}


def reload_settings():
    return load_settings()


def read_audio_from_rtsp(rtsp_url, buffer_size):
    """Lit un flux RTSP audio en continu via ffmpeg."""
    process = None
    try:
        process = (
            ffmpeg
            .input(rtsp_url)
            .output('pipe:', format='f32le', acodec='pcm_f32le', ac=1, ar='16000', buffer_size='64k')
            .run_async(pipe_stdout=True, pipe_stderr=True)
        )
        # Drainer stderr pour éviter le blocage
        import threading
        threading.Thread(target=lambda: process.stderr.read(), daemon=True).start()

        while True:
            in_bytes = process.stdout.read(buffer_size * 4)
            if not in_bytes:
                break
            audio_chunk = np.frombuffer(in_bytes, np.float32)
            if len(audio_chunk) > 0:
                yield audio_chunk
    except Exception as e:
        logging.error(f"Erreur lecture RTSP: {e}")
        yield None
    finally:
        if process:
            try:
                process.kill()
            except Exception:
                pass


def start_detection(model, max_results, score_threshold, overlapping_factor,
                    socketio, delay, sources, **kwargs):
    """Démarre la détection multi-source."""
    global detection_running, current_audio_source, _socketio

    try:
        if (score_threshold < 0) or (score_threshold > 1.0):
            raise ValueError("Score threshold must be between 0 and 1.")

        with _detection_lock:
            if detection_running:
                return False
            detection_running = True

        source_labels = [s['label'] for s in sources]
        logging.info(f"Démarrage détection multi-source: {source_labels}")
        current_audio_source = ' + '.join(source_labels)
        _socketio = socketio

        detection_thread = threading.Thread(
            target=run_detection,
            args=(model, max_results, score_threshold, overlapping_factor, socketio, delay, sources),
            kwargs={'peak_cooldown': kwargs.get('peak_cooldown', 0.08), 'peak_ratio': kwargs.get('peak_ratio', 3.0)},
            daemon=True
        )
        detection_thread.start()
        return True

    except Exception as e:
        logging.error(f"Erreur démarrage détection: {e}")
        with _detection_lock:
            detection_running = False
        return False


def run_detection(model, max_results, score_threshold, overlapping_factor, socketio, delay, sources,
                   peak_cooldown=0.08, peak_ratio=3.0):
    """Exécute la détection multi-source. Un classifier par source (isolation complète)."""
    global detection_running
    source_threads = []
    detectors = []  # Pour cleanup

    try:
        def create_detection_callback(source_name, webhook_url=None):
            def handle_detection(detection_data):
                try:
                    clap_count = detection_data.get('clap_count', 1)
                    logging.info(f"CLAP sur {source_name}: score={detection_data['score']:.2f}, claps={clap_count}")
                    if socketio:
                        socketio.emit('clap', {
                            'source_id': source_name, 'timestamp': detection_data['timestamp'],
                            'score': detection_data['score'], 'clap_count': clap_count
                        })
                    supervisor_token = os.environ.get('SUPERVISOR_TOKEN')
                    if supervisor_token:
                        try:
                            requests.post(
                                'http://supervisor/core/api/events/claptrap_clap',
                                headers={'Authorization': f'Bearer {supervisor_token}', 'Content-Type': 'application/json'},
                                json={'source_id': source_name, 'score': detection_data['score'], 'clap_count': clap_count},
                                timeout=3
                            )
                        except Exception:
                            pass
                    # Mettre à jour les entités HA
                    try:
                        from ha_entities import on_clap_detected
                        on_clap_detected(source_name, detection_data['score'], clap_count)
                    except Exception:
                        pass
                    with _history_lock:
                        _detection_history.appendleft({
                            'source_id': source_name, 'timestamp': detection_data['timestamp'],
                            'score': round(detection_data['score'], 3), 'clap_count': clap_count
                        })
                    if webhook_url:
                        send_webhook_async(webhook_url, {
                            'event': 'clap',
                            'source_id': source_name,
                            'timestamp': detection_data['timestamp'],
                            'score': detection_data['score'],
                            'clap_count': clap_count,
                        })
                except Exception as e:
                    logging.error(f"Erreur callback clap {source_name}: {e}")
            return handle_detection

        def create_labels_callback(source_name):
            def handle_labels(labels):
                if socketio:
                    socketio.emit("labels", {"source": source_name, "detected": labels})
            return handle_labels

        def create_detector(source_id, webhook_url, threshold=None, label=None, entity_id=None,
                             clap_counts=None, whitelist=None, kind=None, source_key=None):
            """Crée un AudioDetector dédié pour une source."""
            det = AudioDetector(model, sample_rate=16000, buffer_duration=1.0)
            det.initialize(max_results=max_results, score_threshold=threshold or score_threshold,
                          clap_window=delay, peak_cooldown=peak_cooldown, peak_ratio=peak_ratio)
            det.set_whitelist(whitelist or DEFAULT_SOUND_WHITELIST)
            det.set_sound_seen_callback(_build_sound_seen_handler(source_id))
            det.add_source(source_id=source_id,
                detection_callback=create_detection_callback(source_id, webhook_url),
                labels_callback=create_labels_callback(source_id),
                label=label)
            det.start()
            detectors.append(det)
            with _active_detectors_lock:
                _active_detectors.append(det)
                _detectors_by_source_id[source_id] = det
                _seen_labels_by_source[source_id] = set((whitelist or DEFAULT_SOUND_WHITELIST).keys())
                if kind and source_key is not None:
                    _source_info_by_id[source_id] = (kind, source_key)
            logging.info(f"Classifier dédié créé pour {source_id} ({label})")
            try:
                from ha_entities import register_source
                register_source(entity_id or source_id, label=label, technical_id=source_id, clap_counts=clap_counts)
            except Exception:
                pass
            return det

        # --- Runners par type de source (chacun avec son propre detector) ---

        def run_mic_source(src):
            import subprocess
            from auto_volume import auto_volume_mgr
            settings = reload_settings()
            device_name = settings.get('microphone', {}).get('audio_source', 'default')
            pulse_name = settings.get('microphone', {}).get('pulse_name', '')
            saved_index = int(settings.get('microphone', {}).get('device_index', 0))

            if not pulse_name and device_name and device_name != 'default':
                try:
                    from audio_utils import get_audio_input_devices
                    for dev in get_audio_input_devices():
                        if dev.get('name') == device_name and dev.get('pulse_name'):
                            pulse_name = dev['pulse_name']
                            break
                except Exception:
                    pass

            if pulse_name:
                try:
                    from audio_utils import set_pulse_volume
                    mic_volume = settings.get('microphone', {}).get('volume', 100)
                    set_pulse_volume(pulse_name, mic_volume)
                except Exception:
                    pass

            source_id = f"mic_{saved_index}"
            detector = create_detector(source_id, src.get('webhook_url'),
                threshold=src.get('threshold'), label=src.get('label'),
                clap_counts=src.get('ha_entities', [1, 2]),
                whitelist=src.get('sound_whitelist'),
                kind='mic', source_key=src.get('source_key', str(saved_index)))

            cmd = ['parecord', '--format=float32le', '--rate=16000', '--channels=1',
                   '--raw', '--latency-msec=50']
            if pulse_name:
                cmd.append(f'--device={pulse_name}')
            logging.info(f"Micro: lancement {' '.join(cmd)}")
            from audio_utils import start_process_with_stderr_drain
            proc = start_process_with_stderr_drain(cmd)

            if settings.get('microphone', {}).get('auto_volume', False) and pulse_name:
                auto_volume_mgr.start(pulse_name, socketio)

            block_bytes = 1600 * 4
            try:
                while detection_running:
                    data = proc.stdout.read(block_bytes)
                    if not data:
                        break
                    samples = np.frombuffer(data, dtype=np.float32)
                    auto_volume_mgr.feed_peak(float(np.max(np.abs(samples))))
                    detector.process_audio(samples, source_id)
            finally:
                from audio_utils import terminate_process
                terminate_process(proc)
                detector.stop()

        def run_rtsp_source(src):
            rtsp_url = src.get('rtsp_url', src['audio_source'])
            _rtsp_gains[rtsp_url] = float(src.get('gain', 10))
            source_id = f"rtsp_{rtsp_url}"
            from ha_entities import source_entity_key
            entity_id = source_entity_key('rtsp', src)
            detector = create_detector(source_id, src.get('webhook_url'),
                threshold=src.get('threshold'), label=src.get('label'),
                entity_id=entity_id, clap_counts=src.get('ha_entities', [1, 2]),
                whitelist=src.get('sound_whitelist'),
                kind='rtsp', source_key=src.get('source_key') or src.get('stream_id', ''))
            logging.info(f"RTSP: démarrage capture {rtsp_url} (volume={_rtsp_gains[rtsp_url]}x)")

            if socketio:
                socketio.emit('rtsp_status', {'url': rtsp_url, 'status': 'connecting'})

            reconnect_delay = 1
            while detection_running:
                try:
                    got_data = False
                    for audio_data in read_audio_from_rtsp(rtsp_url, int(16000 * 0.1)):
                        if not detection_running:
                            break
                        if audio_data is not None:
                            if not got_data:
                                got_data = True
                                reconnect_delay = 1  # Reset backoff on first data
                                if socketio:
                                    socketio.emit('rtsp_status', {'url': rtsp_url, 'status': 'connected'})
                            gain = _rtsp_gains.get(rtsp_url, 10)
                            if gain != 1.0:
                                audio_data = np.clip(audio_data * gain, -1.0, 1.0).astype(np.float32)
                            detector.process_audio(audio_data, source_id)
                    if detection_running:
                        if socketio:
                            socketio.emit('rtsp_status', {'url': rtsp_url, 'status': 'reconnecting'})
                        logging.warning(f"RTSP interrompu, reconnexion dans {reconnect_delay}s...")
                        time.sleep(reconnect_delay)
                        reconnect_delay = min(reconnect_delay * 2, 30)
                except Exception as e:
                    if detection_running:
                        if socketio:
                            socketio.emit('rtsp_status', {'url': rtsp_url, 'status': 'error', 'error': str(e)})
                        logging.error(f"Erreur RTSP: {e}")
                        time.sleep(reconnect_delay)
                        reconnect_delay = min(reconnect_delay * 2, 30)
            detector.stop()

        def run_vban_source(src):
            vban_ip = src['audio_source'].replace("vban://", "")
            source_id = f"vban_{vban_ip}"
            _vban_gains[vban_ip] = float(src.get('gain', 1.0))
            detector = create_detector(source_id, src.get('webhook_url'),
                threshold=src.get('threshold'), label=src.get('label'),
                clap_counts=src.get('ha_entities', [1, 2]),
                whitelist=src.get('sound_whitelist'),
                kind='vban', source_key=src.get('source_key') or vban_ip)
            logging.info(f"VBAN: démarrage capture {vban_ip} (gain={_vban_gains[vban_ip]}x)")

            vban_det = get_vban_detector()
            def audio_callback(audio_data, timestamp):
                if detection_running and vban_ip in vban_det.get_active_sources():
                    gain = _vban_gains.get(vban_ip, 1.0)
                    if gain != 1.0:
                        audio_data = np.clip(audio_data * gain, -1.0, 1.0).astype(np.float32)
                    detector.process_audio(audio_data, source_id)
            vban_det.set_audio_callback(audio_callback)

            while detection_running:
                time.sleep(0.5)
            # Detacher le callback audio pour ne pas garder une ref morte
            try:
                vban_det.set_audio_callback(None)
            except Exception:
                pass
            detector.stop()

        # --- Lancer un thread par source ---
        logging.info(f"Détection démarrée avec {len(sources)} source(s) (1 classifier par source)")

        try:
            from ha_entities import init_entities, update_detection_state
            init_entities(settings=reload_settings())
            source_labels = [s['label'] for s in sources]
            update_detection_state(True, source_labels)
        except Exception:
            pass

        runners = {'mic': run_mic_source, 'rtsp': run_rtsp_source, 'vban': run_vban_source}
        for src in sources:
            runner = runners.get(src['type'])
            if runner:
                t = threading.Thread(target=runner, args=(src,), daemon=True)
                t.start()
                source_threads.append(t)
                logging.info(f"Thread démarré pour {src['label']}")

        # Attendre la fin
        while detection_running:
            if all(not t.is_alive() for t in source_threads):
                logging.warning("Toutes les sources se sont arrêtées")
                break
            time.sleep(0.5)

        return True

    except Exception as e:
        logging.error(f"Erreur run_detection: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return False
    finally:
        # Stopper tous les detectors restants
        for det in detectors:
            try:
                det.stop()
            except Exception:
                pass
        try:
            from ha_entities import update_detection_state
            update_detection_state(False)
        except Exception:
            pass
        with _detection_lock:
            detection_running = False
        logging.info("run_detection terminé")


def stop_detection():
    """Arrête la détection."""
    global detection_running, classifier, record, current_audio_source, _socketio
    with _active_detectors_lock:
        _active_detectors.clear()
        _detectors_by_source_id.clear()
        _seen_labels_by_source.clear()
        _source_info_by_id.clear()

    try:
        try:
            from auto_volume import auto_volume_mgr
            auto_volume_mgr.stop()
        except Exception:
            pass

        with _detection_lock:
            detection_running = False

        try:
            from ha_entities import update_detection_state
            update_detection_state(False)
        except Exception:
            pass

        if _socketio:
            _socketio.emit("detection_status", {"status": "stopped"})

        if record:
            record.stop()
            record.close()
            record = None

        if classifier:
            classifier.close()
            classifier = None

        current_audio_source = None
        return True

    except Exception as e:
        logging.error(f"Erreur arrêt détection: {e}")
        return False


def is_running():
    with _detection_lock:
        return detection_running


def get_current_source():
    return current_audio_source


def get_detection_history():
    with _history_lock:
        return list(_detection_history)

def update_rtsp_gain(rtsp_url, gain):
    """Met à jour le gain d'une source RTSP en temps réel."""
    _rtsp_gains[rtsp_url] = float(gain)
    logging.info(f"Volume RTSP mis à jour: {rtsp_url} -> {gain}x")


def update_vban_gain(ip, gain):
    """Met à jour le gain d'une source VBAN en temps réel (pas de redémarrage)."""
    _vban_gains[ip] = float(gain)
    logging.info(f"Volume VBAN mis à jour: {ip} -> {gain}x")


def _persist_sound_seen(kind, source_key, label):
    """Ajoute `label: False` à sound_whitelist de la source `(kind, source_key)`
    dans settings.json si absent. Thread-safe via le lock global du settings_manager."""
    try:
        from settings_manager import save_settings
        settings = load_settings()
        updated = False
        if kind == 'mic':
            mic = settings.setdefault('microphone', {})
            wl = mic.setdefault('sound_whitelist', {})
            if label not in wl:
                wl[label] = False
                updated = True
        elif kind == 'rtsp':
            for s in settings.get('rtsp_sources', []):
                if s.get('id') == source_key:
                    wl = s.setdefault('sound_whitelist', {})
                    if label not in wl:
                        wl[label] = False
                        updated = True
                    break
        elif kind == 'vban':
            for s in settings.get('saved_vban_sources', []):
                if s.get('ip') == source_key:
                    wl = s.setdefault('sound_whitelist', {})
                    if label not in wl:
                        wl[label] = False
                        updated = True
                    break
        if updated:
            save_settings(settings)
    except Exception as exc:
        logging.debug(f"_persist_sound_seen({kind},{source_key},{label}) a echoue: {exc}")


def _build_sound_seen_handler(source_id):
    def _handle(data):
        label = data.get('label') if isinstance(data, dict) else None
        if not label:
            return
        with _active_detectors_lock:
            seen = _seen_labels_by_source.get(source_id)
            if seen is None:
                return
            if label in seen:
                return
            seen.add(label)
            info = _source_info_by_id.get(source_id)
        kind = source_key = None
        if info:
            kind, source_key = info
            _persist_sound_seen(kind, source_key, label)
        try:
            if _socketio:
                _socketio.emit('sound_seen', {
                    'source_id': source_id,
                    'kind': kind,
                    'source_key': source_key,
                    'label': label,
                    'score': float(data.get('score', 0.0)),
                })
        except Exception:
            pass
    return _handle


def update_source_whitelist(source_id, label, enabled):
    """Met à jour la whitelist d'un détecteur actif (sans restart)."""
    with _active_detectors_lock:
        det = _detectors_by_source_id.get(source_id)
        if det is None:
            return False
        wl = dict(det._whitelist or {})
        if enabled:
            wl[label] = True
            _seen_labels_by_source.setdefault(source_id, set()).add(label)
        else:
            wl[label] = False
        det.set_whitelist(wl)
        return True


def remove_source_whitelist_entry(source_id, label):
    """Retire un label de la whitelist d'un détecteur actif et du set 'seen'
    (pour que l'auto-découverte puisse le re-proposer s'il réapparaît)."""
    with _active_detectors_lock:
        det = _detectors_by_source_id.get(source_id)
        if det is not None:
            wl = dict(det._whitelist or {})
            wl.pop(label, None)
            det.set_whitelist(wl)
        seen = _seen_labels_by_source.get(source_id)
        if seen is not None:
            seen.discard(label)
        return True


def update_advanced_params(peak_cooldown=None, peak_ratio=None, delay=None):
    """Met à jour les paramètres avancés sur tous les detectors actifs."""
    with _active_detectors_lock:
        for det in _active_detectors:
            if peak_cooldown is not None:
                det._peak_cooldown = float(peak_cooldown)
            if peak_ratio is not None:
                det._peak_ratio = float(peak_ratio)
            if delay is not None:
                det._clap_window_duration = float(delay)
        logging.info(f"Paramètres avancés mis à jour sur {len(_active_detectors)} detector(s)")
