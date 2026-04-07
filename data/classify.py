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
from concurrent.futures import ThreadPoolExecutor
from vban_manager import get_vban_detector
from audio_detector import AudioDetector
from settings_manager import load_settings

_webhook_executor = ThreadPoolExecutor(max_workers=2)

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

warnings.filterwarnings("ignore", category=UserWarning, module="google.protobuf.symbol_database")

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
            process.kill()


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
            daemon=True
        )
        detection_thread.start()
        return True

    except Exception as e:
        logging.error(f"Erreur démarrage détection: {e}")
        with _detection_lock:
            detection_running = False
        return False


def run_detection(model, max_results, score_threshold, overlapping_factor, socketio, delay, sources):
    """Exécute la détection multi-source. Un thread par source, un seul detector partagé."""
    global detection_running
    source_threads = []

    try:
        detector = AudioDetector(model, sample_rate=16000, buffer_duration=1.0)
        detector.initialize(max_results=max_results, score_threshold=score_threshold, detection_delay=delay)

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
                    # Event HA natif
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
                    # Historique
                    with _history_lock:
                        _detection_history.appendleft({
                            'source_id': source_name, 'timestamp': detection_data['timestamp'],
                            'score': round(detection_data['score'], 3), 'clap_count': clap_count
                        })
                    # Webhook
                    if webhook_url:
                        _webhook_executor.submit(
                            requests.post, webhook_url,
                            json={'event': 'clap', 'source_id': source_name,
                                  'timestamp': detection_data['timestamp'],
                                  'score': detection_data['score'], 'clap_count': clap_count},
                            timeout=5
                        )
                except Exception as e:
                    logging.error(f"Erreur callback clap {source_name}: {e}")
            return handle_detection

        def create_labels_callback(source_name):
            def handle_labels(labels):
                if socketio:
                    socketio.emit("labels", {"source": source_name, "detected": labels})
            return handle_labels

        # --- Runners par type de source ---

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
                    mic_volume = settings.get('microphone', {}).get('volume', 100)
                    subprocess.run(['pactl', 'set-source-volume', pulse_name, f'{mic_volume}%'],
                                   capture_output=True, text=True, timeout=5)
                except Exception:
                    pass

            source_id = f"mic_{saved_index}"
            detector.add_source(source_id=source_id,
                detection_callback=create_detection_callback(source_id, src.get('webhook_url')),
                labels_callback=create_labels_callback(source_id))

            cmd = ['parecord', '--format=float32le', '--rate=16000', '--channels=1', '--raw']
            if pulse_name:
                cmd.append(f'--device={pulse_name}')
            logging.info(f"Micro: lancement {' '.join(cmd)}")
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            threading.Thread(target=lambda: proc.stderr.read(), daemon=True).start()

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
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except Exception:
                    proc.kill()

        def run_rtsp_source(src):
            rtsp_url = src.get('rtsp_url', src['audio_source'])
            source_id = f"rtsp_{rtsp_url}"
            detector.add_source(source_id=source_id,
                detection_callback=create_detection_callback(source_id, src.get('webhook_url')),
                labels_callback=create_labels_callback(source_id))
            logging.info(f"RTSP: démarrage capture {rtsp_url}")

            reconnect_delay = 1
            while detection_running:
                try:
                    for audio_data in read_audio_from_rtsp(rtsp_url, int(16000 * 0.1)):
                        if not detection_running:
                            break
                        if audio_data is not None:
                            detector.process_audio(audio_data, source_id)
                    if detection_running:
                        logging.warning(f"RTSP {rtsp_url} interrompu, reconnexion dans {reconnect_delay}s...")
                        time.sleep(reconnect_delay)
                        reconnect_delay = min(reconnect_delay * 2, 30)
                except Exception as e:
                    if detection_running:
                        logging.error(f"Erreur RTSP: {e}")
                        time.sleep(reconnect_delay)
                        reconnect_delay = min(reconnect_delay * 2, 30)

        def run_vban_source(src):
            vban_ip = src['audio_source'].replace("vban://", "")
            source_id = f"vban_{vban_ip}"
            detector.add_source(source_id=source_id,
                detection_callback=create_detection_callback(source_id, src.get('webhook_url')),
                labels_callback=create_labels_callback(source_id))
            logging.info(f"VBAN: démarrage capture {vban_ip}")

            vban_det = get_vban_detector()
            def audio_callback(audio_data, timestamp):
                if detection_running and vban_ip in vban_det.get_active_sources():
                    detector.process_audio(audio_data, source_id)
            vban_det.set_audio_callback(audio_callback)

            while detection_running:
                time.sleep(0.5)

        # --- Démarrer le detector puis un thread par source ---
        detector.start()
        logging.info(f"Détection démarrée avec {len(sources)} source(s)")

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

        detector.stop()
        return True

    except Exception as e:
        logging.error(f"Erreur run_detection: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return False
    finally:
        with _detection_lock:
            detection_running = False
        logging.info("run_detection terminé")


def stop_detection():
    """Arrête la détection."""
    global detection_running, classifier, record, current_audio_source, _socketio

    try:
        try:
            from auto_volume import auto_volume_mgr
            auto_volume_mgr.stop()
        except Exception:
            pass

        with _detection_lock:
            detection_running = False

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
