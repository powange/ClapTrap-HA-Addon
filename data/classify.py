"""Orchestration de la detection : une DetectionSession par demarrage.

La session porte tout l'etat d'un run (evenement d'arret, threads, detecteurs,
labels deja vus) : l'arreter joint ses threads puis ferme ses classifieurs, et
un redemarrage cree une session neuve. Les fonctions de module (start/stop,
mises a jour en direct) sont l'API utilisee par les routes.
"""

import collections
import logging
import os
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests

from audio_detector import AudioDetector
from clap_logic import default_group, normalize_group
from audio_sources import VbanSource, mic_source, rtsp_source, run_source
from audio_utils import level_db
from ha_entities import source_label, source_entity_key
from settings_manager import load_settings
from url_validator import mask_url_credentials
from vban_manager import get_vban_detector
from webhook import send_webhook_async

warnings.filterwarnings("ignore", category=UserWarning, module="google.protobuf.symbol_database")

MODEL_PATH = "yamnet.tflite"

# Effets de bord d'une detection (evenement HA, MQTT, webhook) : executes hors
# du thread d'inference. 1 worker : conserve l'ordre des evenements.
_side_effects = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clap-effects")
# Connexion HTTP reutilisee vers le Supervisor (une par clap sinon).
_http = requests.Session()
# Sons nouvellement entendus, enregistres par lots (une ecriture toutes les
# SEEN_FLUSH_DELAY s) sur leur propre fil : chaque son reecrivait tout
# settings.json sur le fil des claps, qui attendaient derriere sur carte SD.
SEEN_FLUSH_DELAY = 2.0
_seen_pending = {}     # (kind, source_key) -> {labels}
_seen_lock = threading.Lock()
_seen_timer = None

_session = None
_session_lock = threading.Lock()
_detection_history = collections.deque(maxlen=50)
_history_lock = threading.Lock()
# Gain de chaque source (lu a chaque bloc), modifiable en direct par le
# curseur, par la detection comme par le test du son. Indexe par source_id :
# indexe par URL, deux cameras a la meme URL partageaient leur gain et une URL
# modifiee laissait une entree orpheline. Remis a zero a chaque session.
_live_gains = {}       # {source_id: gain}
_whitelist_lock = threading.Lock()  # mises a jour en direct des listes de sons
# Onglets connectes (Socket.IO) : sans aucun, les retours en direct (niveau,
# scores, sons, ~12 messages/s par source) ne sont pas emis.
_clients = 0
_clients_lock = threading.Lock()


def client_connected(delta):
    global _clients
    with _clients_lock:
        _clients = max(0, _clients + delta)


# --- Construction des sources depuis les settings ------------------------------

def _build_groups_for_source(source_settings, global_threshold):
    """Groupes d'une source au format du detecteur (groupe « Clap » par
    defaut si la source n'en a aucun)."""
    groups = [normalize_group(g, idx, global_threshold)
              for idx, g in enumerate(source_settings.get('sound_groups') or []) if isinstance(g, dict)]
    return groups or [default_group(global_threshold)]


def build_sources_from_settings(settings):
    """Liste des sources actives a demarrer."""
    sources = []
    global_threshold = float(settings.get('global', {}).get('threshold', 0.5))
    mic = settings.get('microphone', {})
    if mic.get('enabled', False) and mic.get('configured', True) is not False:
        sources.append({
            'type': 'mic', 'kind': 'mic',
            # Stable : l'index PulseAudio change au rebranchement et cassait les
            # automations qui filtrent claptrap_clap sur source_id.
            'source_id': 'mic',
            'source_key': str(mic.get('device_index', 0)),
            'entity_key': source_entity_key('mic', mic),
            'webhook_url': mic.get('webhook_url', ''),
            'groups': _build_groups_for_source(mic, global_threshold),
            'label': source_label('mic', mic),
            'name': mic.get('audio_source') if mic.get('audio_source') not in (None, '', 'default') else 'Microphone',
        })
    for src in settings.get('rtsp_sources', []):
        if src.get('enabled', False) and src.get('url'):
            url = src['url'] if src['url'].lower().startswith('rtsp') else f"rtsp://{src['url']}"
            sources.append({
                'type': 'rtsp', 'kind': 'rtsp',
                # jamais l'URL dans l'id : elle peut contenir des identifiants
                'source_id': f"rtsp_{src.get('id', '')}",
                'source_key': src.get('id', ''),
                'entity_key': source_entity_key('rtsp', src),
                'rtsp_url': url,
                'webhook_url': src.get('webhook_url', ''),
                'gain': float(src.get('gain', 10)),
                'groups': _build_groups_for_source(src, global_threshold),
                'label': source_label('rtsp', src),
                'name': src.get('name') or 'RTSP',
            })
    for src in settings.get('saved_vban_sources', []):
        if src.get('enabled', False):
            sources.append({
                'type': 'vban', 'kind': 'vban',
                # Id propre a chaque flux : deux flux d'une meme IP (Voicemeeter)
                # partageaient source_id, detecteur, gain et webhook.
                'source_id': f"vban_{src.get('id') or src.get('ip', '')}",
                'source_key': src.get('id') or src.get('ip', ''),
                'entity_key': source_entity_key('vban', src),
                'ip': src.get('ip', ''),
                'stream_name': src.get('stream_name') or src.get('name', ''),
                'webhook_url': src.get('webhook_url', ''),
                'gain': float(src.get('gain', 1)),
                'groups': _build_groups_for_source(src, global_threshold),
                'label': source_label('vban', src),
                'name': src.get('name') or src.get('ip', ''),
            })
    return sources


def detection_params_from_settings(settings):
    """Parametres de detection derives de la section `global` (point unique
    pour l'auto-start, le bouton Demarrer et les redemarrages)."""
    g = settings.get('global') or {}
    return {
        'score_threshold': float(g.get('threshold', 0.5)),
        'delay': float(g.get('delay', 1.5)),
        'peak_cooldown': float(g.get('peak_cooldown', 0.08)),
        'peak_ratio': float(g.get('peak_ratio', 3.0)),
    }


# --- Effets de bord -----------------------------------------------------------

def _run_side_effects(source_id, entity_key, base_payload, score, clap_count, group_slug,
                      group_clap_counts, webhook_url):
    """Entite MQTT + evenement HA + webhook d'une detection (worker dedie).

    Le pulse MQTT passe en premier : il ne doit pas attendre l'API REST (Core
    qui redemarre, Supervisor lent).
    """
    try:
        from ha_entities import on_clap_detected
        on_clap_detected(entity_key, score, clap_count,
                         group_slug=group_slug, group_clap_counts=group_clap_counts)
    except Exception as e:
        logging.warning(f"Entite HA non mise a jour pour {source_id}: {e}")
    token = os.environ.get('SUPERVISOR_TOKEN')
    if token:
        try:
            _http.post('http://supervisor/core/api/events/claptrap_clap',
                       headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
                       json=base_payload, timeout=3)
        except Exception as e:
            logging.warning(f"Evenement HA claptrap_clap non envoye: {e}")
    if webhook_url:
        send_webhook_async(webhook_url, {**base_payload, 'event': 'clap'})


# --- Session -------------------------------------------------------------------

class DetectionSession:
    def __init__(self, sources, params, socketio):
        self.sources = sources
        self.params = params
        self.socketio = socketio
        self.stop_event = threading.Event()
        self.label = ' + '.join(s['label'] for s in sources)
        self.started_at = time.time()
        # Un lanceur par source (signal d'arret, thread, lecteur) : une source
        # ajoutee, retiree ou modifiee est relancee seule, sans recreer les
        # classifieurs YAMNet de toutes les autres.
        self._runners = {}    # source_id -> {'src', 'stop', 'thread', 'reader'}
        self._lock = threading.Lock()
        self.detectors = {}   # source_id -> AudioDetector
        self.seen = {}        # source_id -> labels deja presents dans les groupes
        self._supervisor = None
        try:
            self.exclusions = set(load_settings().get('global', {}).get('sound_exclusions') or [])
        except Exception:
            self.exclusions = set()
        self._last_live = {}  # (source_id, event) -> instant du dernier envoi
        self.source_status = {}  # source_id -> connecting|connected|reconnecting|error
        self.webhooks = {s['source_id']: s.get('webhook_url') or '' for s in sources}
        _live_gains.clear()
        _live_gains.update({s['source_id']: float(s['gain']) for s in sources if 'gain' in s})

    def _set_status(self, source_id, status, error=None):
        self.source_status[source_id] = status
        self._emit('source_status', {'source_id': source_id, 'status': status,
                                     **({'error': error} if error else {})})
        if not self.stop_event.is_set():
            # A l'ecoute seulement quand le flux arrive : pendant une
            # reconnexion (camera debranchee, tentatives tuees par le chien de
            # garde sans message d'erreur), les entites restaient disponibles.
            src = next((s for s in self.sources if s['source_id'] == source_id), None)
            if src:
                self._set_listening(src, status == 'connected')

    @staticmethod
    def _set_listening(src, listening):
        try:
            from ha_entities import set_source_listening
            set_source_listening(src['entity_key'], listening)
        except Exception as e:
            logging.warning(f"Disponibilité HA non publiée pour {src['label']}: {e}")

    def _emit(self, event, payload):
        if self.socketio:
            try:
                self.socketio.emit(event, payload)
            except Exception as e:
                logging.debug(f"socketio {event}: {e}")

    # --- Callbacks du detecteur ---

    def _emit_live(self, source_id, event, payload, interval=0.2):
        """Retours en direct limites a ~5/s par source (niveau, scores), et
        seulement si un onglet est ouvert."""
        if not _clients:
            return
        now = time.monotonic()
        key = (source_id, event)
        if now - self._last_live.get(key, 0) < interval:
            return
        self._last_live[key] = now
        self._emit(event, {'source_id': source_id, **payload})

    def _on_level(self, src, peak, gain_of=None):
        gain = gain_of() if gain_of else 1.0
        level = min(1.0, peak * gain)
        self._emit_live(src['source_id'], 'source_level', {'peak': round(level, 4), 'db': level_db(level)})

    def _on_detection(self, src, data):
        base_payload = {
            'source_id': src['source_id'],
            # Pour filtrer dans une automation sans connaitre l'id interne :
            # meme cle que les entity_id (binary_sensor.claptrap_<entity_key>_...)
            'entity_key': src['entity_key'],
            'source_name': src['name'],
            'timestamp': data['timestamp'],
            'score': data['score'],
            'clap_count': data.get('clap_count', 1),
            'labels': data.get('labels') or [],
            'group_slug': data.get('group_slug', 'clap'),
            'group_name': data.get('group_name', 'Clap'),
            'ignored': bool(data.get('ignored', False)),
        }
        logging.info(f"CLAP{' (ignore)' if base_payload['ignored'] else ''} sur {src['label']} "
                     f"({base_payload['group_name']}): score={data['score']:.2f}, "
                     f"claps={base_payload['clap_count']}")
        self._emit('clap', base_payload)
        with _history_lock:
            _detection_history.appendleft({**base_payload, 'score': round(data['score'], 3)})
        if base_payload['ignored']:
            return  # perdant de l'arbitrage : historique seulement
        _side_effects.submit(_run_side_effects, src['source_id'], src['entity_key'], base_payload,
                             data['score'], base_payload['clap_count'], base_payload['group_slug'],
                             data.get('group_clap_counts') if data.get('group_clap_counts') is not None else [1, 2],
                             self.webhooks.get(src['source_id'], ''))

    def _on_sound_seen(self, src, data):
        label = data.get('label')
        source_id = src['source_id']
        with self._lock:
            seen = self.seen.get(source_id)
            if not label or seen is None or label in seen:
                return  # chemin chaud : deja vu, aucun acces disque
        with self._lock:
            # Exclusions gardees en memoire : un son exclu relisait les reglages
            # (copie profonde) a chaque resultat.
            if label in self.exclusions or label in self.seen[source_id]:
                return
            self.seen[source_id].add(label)
        # Ecriture disque groupee, hors du thread de resultats MediaPipe.
        _queue_sound_seen(src['kind'], src['source_key'], label)
        self._emit('sound_seen', {'source_id': source_id, 'kind': src['kind'],
                                  'source_key': src['source_key'], 'label': label,
                                  'score': float(data.get('score', 0.0))})

    def _create_detector(self, src, stop=None):
        p = self.params
        groups = list(src.get('groups') or []) or [default_group(p['score_threshold'])]
        det = AudioDetector(MODEL_PATH)
        # 30 labels : un son de groupe classe 11e mais au-dessus de son seuil
        # etait invisible avec 10.
        det.initialize(max_results=30,
                       score_threshold=min(g.get('threshold', p['score_threshold']) for g in groups),
                       clap_window=p['delay'], peak_cooldown=p['peak_cooldown'],
                       peak_ratio=p['peak_ratio'])
        det.set_groups(groups)
        det.set_exclusions(self.exclusions)
        det.configure(
            src['source_id'], label=src['label'],
            detection_callback=lambda d: self._on_detection(src, d),
            # Limite a 2/s : un evenement par resultat et par source inondait
            # l'interface (et les lecteurs d'ecran) pendant la detection.
            labels_callback=lambda labels: self._emit_live(
                src['source_id'], 'labels', {'source': src['source_id'], 'detected': labels}, interval=0.5),
            sound_seen_callback=lambda d: self._on_sound_seen(src, d),
            scores_callback=lambda scores: self._emit_live(
                src['source_id'], 'group_scores', {'scores': {k: round(v, 3) for k, v in scores.items()}}))
        det.start()
        with self._lock:
            # L'initialisation de YAMNet peut durer plus que l'attente de
            # l'arret : si l'arret a ete demande entre-temps, fermer ce
            # detecteur ici (sinon il n'etait jamais ferme).
            if self.stop_event.is_set() or (stop is not None and stop.is_set()):
                det.stop()
                return None
            self.detectors[src['source_id']] = det
            self.seen[src['source_id']] = {l for g in groups for l in (g.get('whitelist') or {})}
        self._apply_current_settings(src, det)
        return det

    def _apply_current_settings(self, src, det):
        """Relit les reglages une fois le detecteur enregistre : un son coche
        ou un reglage avance modifie pendant l'initialisation de YAMNet
        (plusieurs secondes sur Pi) n'atteignait aucun detecteur et etait
        ignore jusqu'au redemarrage suivant. Les routes enregistrent AVANT
        d'appliquer en direct : ce qui n'a pas trouve le detecteur est ici."""
        try:
            with _whitelist_lock:  # lecture et application sans mise a jour intercalee
                settings = load_settings()
                current = next((s for s in build_sources_from_settings(settings)
                                if s['source_id'] == src['source_id']), None)
                if current and current['groups']:
                    det.set_groups(current['groups'])
            p = detection_params_from_settings(settings)
            det.set_params(window=p['delay'], peak_cooldown=p['peak_cooldown'], peak_ratio=p['peak_ratio'])
            exclusions = set(settings.get('global', {}).get('sound_exclusions') or [])
            with self._lock:
                self.exclusions = exclusions
            det.set_exclusions(exclusions)
        except Exception as e:
            logging.warning(f"Réglages non relus pour {src['label']}: {e}")

    # --- Sources ---

    def _prepare_mic(self):
        """Resout le device PulseAudio, applique le volume, demarre l'auto-volume."""
        mic = load_settings().get('microphone', {})
        device_name = mic.get('audio_source', 'default')
        pulse_name = mic.get('pulse_name', '')
        if not pulse_name and device_name and device_name != 'default':
            try:
                from audio_utils import get_audio_input_devices
                pulse_name = next((d['pulse_name'] for d in get_audio_input_devices()
                                   if d.get('name') == device_name and d.get('pulse_name')), '')
            except Exception as e:
                logging.warning(f"Micro: nom PulseAudio introuvable pour {device_name}: {e}")
        auto_volume = None
        if pulse_name:
            try:
                from audio_utils import set_pulse_volume
                set_pulse_volume(pulse_name, mic.get('volume', 100))
            except Exception as e:
                logging.warning(f"Micro: volume non appliqué: {e}")
            if mic.get('auto_volume', False) and not self.stop_event.is_set():
                from auto_volume import auto_volume_mgr
                auto_volume_mgr.start(pulse_name, self.socketio)
                auto_volume = auto_volume_mgr
        return mic_source(pulse_name), auto_volume

    def _run_one(self, src, runner):
        det = self._create_detector(src, runner['stop'])
        if det is None:
            return  # arret demande pendant l'initialisation
        kind = src['type']
        sid = src['source_id']
        gain_of = auto_volume = None
        on_status = lambda status, error=None: self._set_status(sid, status, error)
        if kind == 'mic':
            reader, auto_volume = self._prepare_mic()
        elif kind == 'rtsp':
            url = src['rtsp_url']
            reader = rtsp_source(url)
            gain_of = lambda: _live_gains.get(sid, 10.0)
            logging.info(f"RTSP: démarrage capture {mask_url_credentials(url)} (gain={gain_of()}x)")
        else:
            listener = get_vban_detector()
            if listener is None:
                logging.error(f"VBAN: écoute UDP indisponible, source {src['label']} ignorée")
                return
            reader = VbanSource(listener, src['ip'], src['stream_name'],
                                on_idle=lambda idle, msg: on_status('error', msg) if idle else on_status('connected'))
            gain_of = lambda: _live_gains.get(sid, 1.0)
        with self._lock:
            runner['reader'] = reader
            if runner['stop'].is_set():
                return

        def on_block(block):
            # Bloc brut + gain : le detecteur compte les pics avant le gain et
            # n'amplifie que ce qu'il donne au classifieur. Il renvoie le pic du
            # bloc (calcule une seule fois : niveau, auto-volume).
            peak = det.process_audio(block, gain=gain_of() if gain_of else 1.0) or 0.0
            if auto_volume is not None:
                auto_volume.feed_peak(peak)
            self._on_level(src, peak, gain_of)

        # Lecture + relance avec backoff (micro, RTSP et VBAN : meme logique)
        run_source(reader, on_block, runner['stop'], on_status=on_status)

    def _run_guarded(self, src, runner):
        try:
            self._run_one(src, runner)
        except Exception:
            logging.exception(f"Source {src['label']} arrêtée sur erreur")

    def _start_runner(self, src, settings=None):
        runner = {'src': src, 'stop': threading.Event(), 'reader': None,
                  'sig': self._signature(src, settings if settings is not None else load_settings())}
        runner['thread'] = threading.Thread(target=self._run_guarded, args=(src, runner), daemon=True,
                                            name=f"source-{src['source_id'][:24]}")
        with self._lock:
            self._runners[src['source_id']] = runner
            self.webhooks[src['source_id']] = src.get('webhook_url') or ''
            if 'gain' in src:
                _live_gains[src['source_id']] = float(src['gain'])
        runner['thread'].start()

    def _stop_runner(self, source_id):
        """Arrete une source (thread, lecteur, classifieur) sans toucher aux autres."""
        with self._lock:
            runner = self._runners.pop(source_id, None)
        if runner is None:
            return
        runner['stop'].set()
        if runner.get('reader') is not None:
            runner['reader'].close()
        runner['thread'].join(timeout=5)
        with self._lock:
            det = self.detectors.pop(source_id, None)
            self.seen.pop(source_id, None)
            self.source_status.pop(source_id, None)
        if det is not None:
            det.stop()
        if runner['src']['type'] == 'mic':
            try:
                from auto_volume import auto_volume_mgr
                auto_volume_mgr.stop()
            except Exception as e:
                logging.warning(f"Arret de l'auto-volume: {e}")
        self._set_listening(runner['src'], False)

    @staticmethod
    def _signature(src, settings):
        """Ce qui impose de relancer la capture d'une source (le reste :
        groupes, gain, webhook, reglages avances, s'applique en direct)."""
        if src['type'] == 'rtsp':
            return ('rtsp', src['rtsp_url'])
        if src['type'] == 'vban':
            return ('vban', src['ip'], src['stream_name'])
        mic = settings.get('microphone') or {}
        return ('mic', mic.get('device_index'), mic.get('audio_source'), mic.get('pulse_name'),
                bool(mic.get('auto_volume')))

    def apply_settings(self, settings):
        """Aligne la session sur les reglages : sources retirees arretees,
        ajoutees demarrees, modifiees (adresse, micro, flux) relancees seules ;
        les autres gardent leur classifieur et relisent groupes et reglages.
        Retourne True si une source a ete demarree, arretee ou relancee."""
        wanted = {s['source_id']: s for s in build_sources_from_settings(settings)}
        with self._lock:
            current = dict(self._runners)
        changed = False
        for sid, runner in current.items():
            new = wanted.get(sid)
            if new is None or self._signature(new, settings) != runner['sig']:
                self._stop_runner(sid)
                changed = True
        with self._lock:
            running = set(self._runners)
        for sid, src in wanted.items():
            if sid not in running:
                self._start_runner(src, settings)
                changed = True
            else:
                runner = self._runners.get(sid)
                if runner:
                    runner['src'].update(label=src['label'], name=src['name'], entity_key=src['entity_key'])
                    with self._lock:
                        self.webhooks[sid] = src.get('webhook_url') or ''
                    det = self.detectors.get(sid)
                    if det is not None:
                        self._apply_current_settings(runner['src'], det)
        with self._lock:
            self.sources = [r['src'] for r in self._runners.values()]
            self.label = ' + '.join(s['label'] for s in self.sources)
        try:
            from ha_entities import update_detection_state
            update_detection_state(True, [s['label'] for s in self.sources])
        except Exception as e:
            logging.warning(f"Etat HA de la detection non publie: {e}")
        return changed

    # --- Cycle de vie ---

    def _supervise(self):
        logging.info(f"Détection démarrée : {self.label}")
        try:
            from ha_entities import update_detection_state
            update_detection_state(True, [s['label'] for s in self.sources])
        except Exception as e:
            logging.warning(f"Etat HA de la detection non publie: {e}")
        for src in self.sources:
            self._start_runner(src)
        while not self.stop_event.wait(0.5):
            with self._lock:
                threads = [r['thread'] for r in self._runners.values()]
            if all(not t.is_alive() for t in threads):
                logging.warning("Toutes les sources se sont arrêtées" if threads else "Plus aucune source active")
                break
        self._shutdown()

    def _shutdown(self):
        global _session
        self.stop_event.set()
        with self._lock:
            runners = list(self._runners.values())
        for r in runners:
            r['stop'].set()
            if r.get('reader') is not None:
                r['reader'].close()  # debloque les read() en cours
        for r in runners:
            r['thread'].join(timeout=3)
        # Threads sortis : les classifieurs peuvent etre fermes sans course.
        with self._lock:
            detectors = list(self.detectors.values())
        for det in detectors:
            det.stop()
        _flush_sound_seen()
        for src in self.sources:
            self._set_listening(src, False)
        try:
            from auto_volume import auto_volume_mgr
            auto_volume_mgr.stop()
        except Exception as e:
            logging.warning(f"Arret de l'auto-volume: {e}")
        with _session_lock:
            is_current = _session is self
            if is_current:
                _session = None
        if is_current:
            # Gains en direct de cette session : un test du son lance ensuite
            # (apres un import par exemple) doit lire le gain transmis.
            _live_gains.clear()
            try:
                from ha_entities import update_detection_state
                update_detection_state(False)
            except Exception as e:
                logging.warning(f"Etat HA de la detection non publie: {e}")
            # Arret demande ou spontane (toutes les sources mortes) : l'UI doit
            # le savoir dans les deux cas.
            self._emit('detection_status', {'status': 'stopped'})
        logging.info("Détection arrêtée")

    def start(self):
        self._supervisor = threading.Thread(target=self._supervise, daemon=True, name="detection")
        self._supervisor.start()

    def stop(self, timeout=10):
        self.stop_event.set()
        if self._supervisor and self._supervisor is not threading.current_thread():
            self._supervisor.join(timeout=timeout)


# --- API de module ---------------------------------------------------------

def start_detection(sources, socketio, score_threshold=0.5, delay=1.5,
                    peak_cooldown=0.08, peak_ratio=3.0):
    global _session
    if not 0 <= score_threshold <= 1:
        raise ValueError("Score threshold must be between 0 and 1.")
    with _session_lock:
        if _session is not None:
            return False
        _session = DetectionSession(sources, {
            'score_threshold': score_threshold, 'delay': delay,
            'peak_cooldown': peak_cooldown, 'peak_ratio': peak_ratio,
        }, socketio)
        session = _session
    session.start()
    return True


def start_from_settings(socketio, settings=None):
    """Demarre la detection a partir des settings (disque par defaut).
    Retourne (demarre, sources). ValueError/TypeError si un reglage est invalide."""
    if settings is None:
        settings = load_settings()
    sources = build_sources_from_settings(settings)
    if not sources:
        return False, []
    return start_detection(sources, socketio, **detection_params_from_settings(settings)), sources


def apply_settings_if_running(settings=None):
    """Applique les reglages a la detection en cours sans tout redemarrer.
    None si elle ne tourne pas, sinon True si une source a change."""
    s = _current()
    if s is None:
        return None
    return s.apply_settings(settings if settings is not None else load_settings())


def stop_detection(timeout=10):
    """Arrete la session en cours et attend la fin de ses threads."""
    session = _current()
    if session is not None:
        session.stop(timeout=timeout)


def _current():
    with _session_lock:
        return _session


def is_running():
    return _current() is not None


def get_status():
    s = _current()
    if s is None:
        return {'running': False, 'source': None}
    return {'running': True, 'source': s.label, 'since': s.started_at,
            'sources': [src['source_id'] for src in list(s.sources)],
            'source_status': dict(s.source_status)}


def get_detection_history():
    with _history_lock:
        return list(_detection_history)


def clear_detection_history():
    with _history_lock:
        _detection_history.clear()


def update_source_webhook(source_id, url):
    """Change le webhook d'une source (pris en compte au prochain clap)."""
    if source_id:
        s = _current()
        if s is not None:
            s.webhooks[source_id] = url or ''


def update_source_gain(source_id, gain):
    """Gain d'une source RTSP ou VBAN, applique au bloc suivant (detection
    et test du son)."""
    _live_gains[source_id] = float(gain)
    logging.info(f"Gain mis à jour: {source_id} -> {gain}x")


def live_gain(source_id, default):
    return _live_gains.get(source_id, default)


def get_detector(source_id):
    s = _current()
    return s.detectors.get(source_id) if s else None


def push_groups(source_id, groups):
    """Applique des groupes (format detecteur) a une source en cours."""
    det = get_detector(source_id)
    if det is not None and groups:
        # Meme verrou que les autres mises a jour en direct et que la relecture
        # des reglages au demarrage : sinon l'une pouvait ecraser l'autre.
        with _whitelist_lock:
            det.set_groups(groups)


def set_seen_labels(source_id, labels):
    s = _current()
    if s is not None:
        with s._lock:
            if source_id in s.seen:
                s.seen[source_id] = set(labels)


def update_source_whitelist(source_id, label, enabled, group_slug=None):
    """Active/desactive un label sur le detecteur en cours (sans redemarrage)."""
    with _whitelist_lock:  # deux requetes simultanees perdaient une modification
        return _update_source_whitelist(source_id, label, enabled, group_slug)


def _update_source_whitelist(source_id, label, enabled, group_slug):
    det = get_detector(source_id)
    if det is None:
        return False
    groups = det.groups
    for g in groups:
        if not group_slug or g['slug'] == group_slug:
            g['whitelist'][label] = bool(enabled)
    s = _current()
    if enabled and s is not None:
        with s._lock:
            s.seen.setdefault(source_id, set()).add(label)
    det.set_groups(groups)
    return True


def update_global_exclusions(labels):
    s = _current()
    if s is None:
        return
    with s._lock:
        s.exclusions = set(labels or [])
    for det in list(s.detectors.values()):
        det.set_exclusions(labels)


def update_advanced_params(peak_cooldown=None, peak_ratio=None, delay=None):
    s = _current()
    detectors = list(s.detectors.values()) if s else []
    for det in detectors:
        det.set_params(window=delay, peak_cooldown=peak_cooldown, peak_ratio=peak_ratio)
    logging.info(f"Paramètres avancés mis à jour sur {len(detectors)} détecteur(s)")


# --- Sons "vus" (auto-decouverte) -----------------------------------------------

def _ensure_label_in_groups(source_dict, label):
    """Ajoute `label: False` dans la whitelist de chaque groupe de la source.
    Renvoie True si quelque chose a change."""
    if not isinstance(source_dict, dict):
        return False
    groups = source_dict.get('sound_groups')
    if not isinstance(groups, list) or not groups:
        wl = source_dict.setdefault('sound_whitelist', {})
        if label not in wl:
            wl[label] = False
            return True
        return False
    changed = False
    for g in groups:
        if isinstance(g, dict):
            wl = g.setdefault('sound_whitelist', {})
            if label not in wl:
                wl[label] = False
                changed = True
    return changed


def _queue_sound_seen(kind, source_key, label):
    """Programme l'enregistrement d'un son nouvellement entendu (par lots)."""
    global _seen_timer
    with _seen_lock:
        _seen_pending.setdefault((kind, source_key), set()).add(label)
        if _seen_timer is None:
            _seen_timer = threading.Timer(SEEN_FLUSH_DELAY, _flush_sound_seen)
            _seen_timer.daemon = True
            _seen_timer.name = "sound-seen"
            _seen_timer.start()


def _flush_sound_seen():
    """Enregistre en une ecriture atomique tous les sons en attente."""
    global _seen_timer
    with _seen_lock:
        if _seen_timer is not None:
            _seen_timer.cancel()
            _seen_timer = None
        pending = {k: set(v) for k, v in _seen_pending.items()}
        _seen_pending.clear()
    if not pending:
        return
    try:
        from settings_manager import atomic_update, NO_CHANGE

        def _mutate(settings):
            changed = False
            for (kind, source_key), labels in pending.items():
                if kind == 'mic':
                    target = settings.setdefault('microphone', {})
                else:
                    key = 'rtsp_sources' if kind == 'rtsp' else 'saved_vban_sources'
                    target = next((s for s in settings.get(key, [])
                                   if s.get('id') == source_key or (kind == 'vban' and not s.get('id')
                                                                    and s.get('ip') == source_key)), None)
                if target is None:
                    continue
                for label in sorted(labels):
                    changed = _ensure_label_in_groups(target, label) or changed
            return settings if changed else NO_CHANGE

        atomic_update(_mutate)
    except Exception as exc:
        logging.warning(f"Sons entendus non enregistrés: {exc}")


def get_all_known_labels():
    """Union des labels deja vus par toutes les sources configurees."""
    settings = load_settings()
    known = set()

    def _collect(source_dict):
        if not isinstance(source_dict, dict):
            return
        groups = source_dict.get('sound_groups')
        if isinstance(groups, list) and groups:
            for g in groups:
                if isinstance(g, dict):
                    known.update((g.get('sound_whitelist') or {}).keys())
        else:
            known.update((source_dict.get('sound_whitelist') or {}).keys())

    for s in settings.get('rtsp_sources', []) or []:
        _collect(s)
    for s in settings.get('saved_vban_sources', []) or []:
        _collect(s)
    _collect(settings.get('microphone', {}) or {})
    return sorted(known)
