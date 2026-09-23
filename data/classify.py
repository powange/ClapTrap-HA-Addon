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
from audio_sources import VbanSource, mic_source, rtsp_source, run_source
from ha_entities import source_label, source_entity_key
from settings_manager import load_settings, clap_counts_of
from url_validator import mask_url_credentials
from vban_manager import get_vban_detector
from webhook import send_webhook_async

warnings.filterwarnings("ignore", category=UserWarning, module="google.protobuf.symbol_database")

MODEL_PATH = "yamnet.tflite"
DEFAULT_SOUND_WHITELIST = {"Clapping": True, "Hands": True, "Applause": True}

# Effets de bord d'une detection (evenement HA, MQTT, webhook) : executes hors
# du thread d'inference. 1 worker : conserve l'ordre des evenements.
_side_effects = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clap-effects")

_session = None
_session_lock = threading.Lock()
_detection_history = collections.deque(maxlen=50)
_history_lock = threading.Lock()
# Reglages modifiables en direct (lus a chaque bloc / clap), conserves entre
# les sessions pour que le test RTSP et les routes puissent les lire.
_rtsp_gains = {}       # {rtsp_url: gain}
_vban_gains = {}       # {source_id VBAN: gain}
_whitelist_lock = threading.Lock()  # mises a jour en direct des listes de sons
_source_webhooks = {}  # {source_id: url}


# --- Construction des sources depuis les settings ------------------------------

def _build_groups_for_source(source_settings, global_threshold):
    """Convertit la config d'une source en liste de groupes pour le detecteur.

    Repli retro : sound_whitelist + threshold + ha_entities en un groupe
    "Clap" unique si la source n'a pas (encore) de groupes.
    """
    groups = source_settings.get('sound_groups') or []
    if not groups:
        groups = [{
            'name': 'Clap', 'slug': 'clap',
            'sound_whitelist': dict(source_settings.get('sound_whitelist') or DEFAULT_SOUND_WHITELIST),
            'threshold': float(source_settings.get('threshold', global_threshold)),
            'ha_entities': source_settings.get('ha_entities', [1, 2]),
        }]
    normalised = []
    for idx, g in enumerate(groups):
        if not isinstance(g, dict):
            continue
        normalised.append({
            'name': g.get('name') or f'Groupe {idx + 1}',
            'slug': g.get('slug') or f'group{idx + 1}',
            'whitelist': dict(g.get('sound_whitelist') or g.get('whitelist') or {}),
            'threshold': float(g.get('threshold', global_threshold)),
            'clap_counts': clap_counts_of(g),
        })
    return normalised


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
        })
    for src in settings.get('rtsp_sources', []):
        if src.get('enabled', False) and src.get('url'):
            url = src['url'] if src['url'].startswith('rtsp') else f"rtsp://{src['url']}"
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
        'peak_reset': float(g.get('peak_reset', 0.3)),
    }


# --- Effets de bord -----------------------------------------------------------

def _run_side_effects(source_id, entity_key, base_payload, score, clap_count, group_slug,
                      group_clap_counts, webhook_url):
    """Evenement HA + entite MQTT + webhook d'une detection (worker dedie)."""
    token = os.environ.get('SUPERVISOR_TOKEN')
    if token:
        try:
            requests.post('http://supervisor/core/api/events/claptrap_clap',
                          headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
                          json=base_payload, timeout=3)
        except Exception as e:
            logging.warning(f"Evenement HA claptrap_clap non envoye: {e}")
    try:
        from ha_entities import on_clap_detected
        on_clap_detected(entity_key, score, clap_count,
                         group_slug=group_slug, group_clap_counts=group_clap_counts)
    except Exception as e:
        logging.warning(f"Entite HA non mise a jour pour {source_id}: {e}")
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
        self._threads = []
        self._readers = []
        self._lock = threading.Lock()
        self.detectors = {}   # source_id -> AudioDetector
        self.seen = {}        # source_id -> labels deja presents dans les groupes
        self._supervisor = None
        try:
            self.exclusions = set(load_settings().get('global', {}).get('sound_exclusions') or [])
        except Exception:
            self.exclusions = set()
        self._last_live = {}  # (source_id, event) -> instant du dernier envoi

    def _emit(self, event, payload):
        if self.socketio:
            try:
                self.socketio.emit(event, payload)
            except Exception as e:
                logging.debug(f"socketio {event}: {e}")

    # --- Callbacks du detecteur ---

    def _emit_live(self, source_id, event, payload, interval=0.2):
        """Retours en direct limites a ~5/s par source (niveau, scores)."""
        now = time.monotonic()
        key = (source_id, event)
        if now - self._last_live.get(key, 0) < interval:
            return
        self._last_live[key] = now
        self._emit(event, {'source_id': source_id, **payload})

    def _on_level(self, src, peak, gain_of=None):
        gain = gain_of() if gain_of else 1.0
        level = min(1.0, peak * gain)
        db = max(-60.0, 20 * np.log10(level + 1e-10))
        self._emit_live(src['source_id'], 'source_level', {'peak': round(level, 4), 'db': round(float(db), 1)})

    def _on_detection(self, src, data):
        base_payload = {
            'source_id': src['source_id'],
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
                             _source_webhooks.get(src['source_id'], ''))

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
        # Ecriture disque hors du thread de resultats MediaPipe.
        _side_effects.submit(_persist_sound_seen, src['kind'], src['source_key'], label)
        self._emit('sound_seen', {'source_id': source_id, 'kind': src['kind'],
                                  'source_key': src['source_key'], 'label': label,
                                  'score': float(data.get('score', 0.0))})

    def _create_detector(self, src):
        p = self.params
        groups = list(src.get('groups') or []) or [{
            'name': 'Clap', 'slug': 'clap', 'whitelist': dict(DEFAULT_SOUND_WHITELIST),
            'threshold': p['score_threshold'], 'clap_counts': [1, 2]}]
        det = AudioDetector(MODEL_PATH)
        # 30 labels : un son de groupe classe 11e mais au-dessus de son seuil
        # etait invisible avec 10.
        det.initialize(max_results=30,
                       score_threshold=min(g.get('threshold', p['score_threshold']) for g in groups),
                       clap_window=p['delay'], peak_cooldown=p['peak_cooldown'],
                       peak_ratio=p['peak_ratio'], peak_reset=p['peak_reset'])
        det.set_groups(groups)
        det.set_exclusions(self.exclusions)
        det.configure(
            src['source_id'], label=src['label'],
            detection_callback=lambda d: self._on_detection(src, d),
            labels_callback=lambda labels: self._emit('labels', {'source': src['source_id'], 'detected': labels}),
            sound_seen_callback=lambda d: self._on_sound_seen(src, d),
            scores_callback=lambda scores: self._emit_live(
                src['source_id'], 'group_scores', {'scores': {k: round(v, 3) for k, v in scores.items()}}))
        det.start()
        _source_webhooks[src['source_id']] = src.get('webhook_url') or ''
        with self._lock:
            # L'initialisation de YAMNet peut durer plus que l'attente de
            # l'arret : si l'arret a ete demande entre-temps, fermer ce
            # detecteur ici (sinon il n'etait jamais ferme).
            if self.stop_event.is_set():
                det.stop()
                return None
            self.detectors[src['source_id']] = det
            self.seen[src['source_id']] = {l for g in groups for l in (g.get('whitelist') or {})}
        try:
            from ha_entities import register_source
            register_source(src['entity_key'], label=src['label'],
                            technical_id=src['source_id'], groups=groups)
        except Exception as e:
            logging.warning(f"Entites HA non enregistrees pour {src['source_id']}: {e}")
        return det

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

    def _run_one(self, src):
        det = self._create_detector(src)
        if det is None:
            return  # arret demande pendant l'initialisation
        kind = src['type']
        gain_of = on_status = auto_volume = None
        if kind == 'mic':
            reader, auto_volume = self._prepare_mic()
        elif kind == 'rtsp':
            url = src['rtsp_url']
            _rtsp_gains[url] = src.get('gain', 10.0)
            reader = rtsp_source(url)
            gain_of = lambda: _rtsp_gains.get(url, 10.0)
            stream_id = src['source_key']
            on_status = lambda status, error=None: self._emit(
                'rtsp_status', {'id': stream_id, 'status': status, **({'error': error} if error else {})})
            logging.info(f"RTSP: démarrage capture {mask_url_credentials(url)} (gain={_rtsp_gains[url]}x)")
        else:
            listener = get_vban_detector()
            if listener is None:
                logging.error(f"VBAN: écoute UDP indisponible, source {src['label']} ignorée")
                return
            sid = src['source_id']
            _vban_gains[sid] = src.get('gain', 1.0)
            reader = VbanSource(listener, src['ip'], src['stream_name'])
            gain_of = lambda: _vban_gains.get(sid, 1.0)
        with self._lock:
            self._readers.append(reader)

        def on_block(block):
            peak = float(np.abs(block).max()) if block.size else 0.0
            if auto_volume is not None:
                auto_volume.feed_peak(peak)
            self._on_level(src, peak, gain_of)
            # Bloc brut + gain : le detecteur compte les pics avant le gain et
            # n'amplifie que ce qu'il donne au classifieur.
            det.process_audio(block, gain=gain_of() if gain_of else 1.0)

        # Lecture + relance avec backoff (micro, RTSP et VBAN : meme logique)
        run_source(reader, on_block, self.stop_event, on_status=on_status)

    def _run_guarded(self, src):
        try:
            self._run_one(src)
        except Exception:
            logging.exception(f"Source {src['label']} arrêtée sur erreur")

    # --- Cycle de vie ---

    def _supervise(self):
        logging.info(f"Détection démarrée : {self.label}")
        try:
            from ha_entities import update_detection_state
            update_detection_state(True, [s['label'] for s in self.sources])
        except Exception as e:
            logging.warning(f"Etat HA de la detection non publie: {e}")
        for src in self.sources:
            t = threading.Thread(target=self._run_guarded, args=(src,), daemon=True,
                                 name=f"source-{src['source_id'][:24]}")
            t.start()
            self._threads.append(t)
        while not self.stop_event.wait(0.5):
            if all(not t.is_alive() for t in self._threads):
                logging.warning("Toutes les sources se sont arrêtées")
                break
        self._shutdown()

    def _shutdown(self):
        global _session
        self.stop_event.set()
        with self._lock:
            readers = list(self._readers)
        for r in readers:
            r.close()  # debloque les read() en cours
        for t in self._threads:
            t.join(timeout=3)
        # Threads sortis : les classifieurs peuvent etre fermes sans course.
        with self._lock:
            detectors = list(self.detectors.values())
        for det in detectors:
            det.stop()
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
                    peak_cooldown=0.08, peak_ratio=3.0, peak_reset=0.3):
    global _session
    if not 0 <= score_threshold <= 1:
        raise ValueError("Score threshold must be between 0 and 1.")
    with _session_lock:
        if _session is not None:
            return False
        _session = DetectionSession(sources, {
            'score_threshold': score_threshold, 'delay': delay,
            'peak_cooldown': peak_cooldown, 'peak_ratio': peak_ratio, 'peak_reset': peak_reset,
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


def stop_detection(timeout=10):
    """Arrete la session en cours et attend la fin de ses threads."""
    session = _current()
    if session is not None:
        session.stop(timeout=timeout)
    return True


def _current():
    with _session_lock:
        return _session


def is_running():
    return _current() is not None


def get_current_source():
    s = _current()
    return s.label if s else None


def get_status():
    s = _current()
    if s is None:
        return {'running': False, 'source': None}
    return {'running': True, 'source': s.label, 'since': s.started_at,
            'sources': [src['source_id'] for src in s.sources]}


def get_detection_history():
    with _history_lock:
        return list(_detection_history)


def clear_detection_history():
    with _history_lock:
        _detection_history.clear()


def update_source_webhook(source_id, url):
    """Change le webhook d'une source (pris en compte au prochain clap)."""
    if source_id:
        _source_webhooks[source_id] = url or ''


def update_rtsp_gain(rtsp_url, gain):
    _rtsp_gains[rtsp_url] = float(gain)
    logging.info(f"Volume RTSP mis à jour: {mask_url_credentials(rtsp_url)} -> {gain}x")


def update_vban_gain(source_id, gain):
    _vban_gains[source_id] = float(gain)
    logging.info(f"Volume VBAN mis à jour: {source_id} -> {gain}x")


def get_detector(source_id):
    s = _current()
    return s.detectors.get(source_id) if s else None


def push_groups(source_id, groups):
    """Applique des groupes (format detecteur) a une source en cours."""
    det = get_detector(source_id)
    if det is not None and groups:
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


def remove_source_whitelist_entry(source_id, label):
    det = get_detector(source_id)
    if det is not None:
        with _whitelist_lock:
            groups = det.groups
            for g in groups:
                g['whitelist'].pop(label, None)
            det.set_groups(groups)
    s = _current()
    if s is not None:
        with s._lock:
            s.seen.get(source_id, set()).discard(label)
    return True


def update_global_exclusions(labels):
    s = _current()
    if s is None:
        return
    with s._lock:
        s.exclusions = set(labels or [])
    for det in list(s.detectors.values()):
        det.set_exclusions(labels)


def update_advanced_params(peak_cooldown=None, peak_ratio=None, delay=None, peak_reset=None):
    s = _current()
    detectors = list(s.detectors.values()) if s else []
    for det in detectors:
        det.set_params(window=delay, peak_cooldown=peak_cooldown,
                       peak_ratio=peak_ratio, peak_reset=peak_reset)
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


def _persist_sound_seen(kind, source_key, label):
    """Enregistre un label nouvellement entendu (ecriture atomique)."""
    try:
        from settings_manager import atomic_update, NO_CHANGE

        def _mutate(settings):
            if kind == 'mic':
                target = settings.setdefault('microphone', {})
            else:
                key = 'rtsp_sources' if kind == 'rtsp' else 'saved_vban_sources'
                target = next((s for s in settings.get(key, [])
                               if s.get('id') == source_key or (kind == 'vban' and not s.get('id')
                                                                and s.get('ip') == source_key)), None)
            return settings if target is not None and _ensure_label_in_groups(target, label) else NO_CHANGE

        atomic_update(_mutate)
    except Exception as exc:
        logging.debug(f"_persist_sound_seen({kind},{source_key},{label}) a echoue: {exc}")


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
