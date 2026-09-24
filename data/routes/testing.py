"""Tests "Ecouter cette source" : VU-metre sans lancer la detection.

Un seul test a la fois (comme l'affiche l'interface) : en demarrer un arrete
proprement le precedent. Chaque test porte un jeton renvoye dans ses messages,
s'arrete de lui-meme au bout de TEST_MAX_SECONDS (onglet ferme sans "Arreter")
et n'emet qu'environ 10 niveaux par seconde.
"""

import logging
import threading
import time
import uuid

from flask import Blueprint, jsonify, request

from audio_sources import mic_source, rtsp_source
from audio_utils import level_db
from settings_manager import load_settings
from routes.sources import _resolve_pulse_name, api_error_response, ApiError

testing_bp = Blueprint('testing', __name__)
testing_bp.register_error_handler(Exception, api_error_response)
_socketio = None

TEST_MAX_SECONDS = 120
EMIT_INTERVAL = 0.1


def init_testing(socketio):
    global _socketio
    _socketio = socketio


class _Test:
    def __init__(self, kind, event, ident):
        self.kind = kind
        self.event = event          # mic_level | rtsp_level | vban_level
        self.ident = ident          # champs d'identification ajoutes aux messages
        self.token = uuid.uuid4().hex[:12]
        self.stop_event = threading.Event()
        self.started = time.monotonic()
        self.reader = None
        self.thread = None
        self.on_stop = None
        self._peak = 0.0
        self._last_emit = 0.0

    def emit(self, payload):
        if _socketio:
            _socketio.emit(self.event, {**self.ident, 'token': self.token, **payload})

    def level(self, peak):
        """Agrege les pics et n'emet que toutes les EMIT_INTERVAL s."""
        self._peak = max(self._peak, peak)
        now = time.monotonic()
        if now - self._last_emit < EMIT_INTERVAL:
            return
        self._last_emit = now
        peak, self._peak = min(1.0, self._peak), 0.0
        self.emit({'peak': round(peak, 4), 'db': level_db(peak)})

    def expired(self):
        return time.monotonic() - self.started > TEST_MAX_SECONDS

    def stop(self):
        self.stop_event.set()
        if self.reader is not None:
            self.reader.close()  # debloque un read() en cours
        if self.on_stop:
            try:
                self.on_stop()
            except Exception as e:
                logging.debug(f"Arret du test: {e}")
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)


_lock = threading.Lock()
_current = None


def _replace(test):
    """Arrete le test en cours (en attendant sa fin) et installe le nouveau."""
    global _current
    with _lock:
        previous, _current = _current, test
    if previous is not None:
        previous.stop()


def _finish(test, error=None):
    global _current
    with _lock:
        if _current is test:
            _current = None
    if error and not test.stop_event.is_set():
        test.emit({'error': error})


def _run_process(test, reader, gain_of=lambda: 1.0):
    test.reader = reader
    error = None
    try:
        got = False
        for block in reader.iter_blocks(test.stop_event):
            got = True
            test.level(float(abs(block).max()) * gain_of() if block.size else 0.0)
            if test.expired():
                error = f'test arrêté automatiquement après {TEST_MAX_SECONDS // 60} minutes'
                break
        if error is None and not test.stop_event.is_set():
            detail = getattr(reader, 'last_error', '') or ''
            error = ('le flux ne renvoie plus de son' if got else 'aucun son reçu') + (f' ({detail})' if detail else '')
    except Exception as e:
        error = str(e)
    finally:
        _finish(test, error)
        test.stop_event.set()


def _stop_route(kind):
    token = (request.get_json(silent=True) or {}).get('token')
    with _lock:
        test = _current
    # Un "stop" en retard (ancien jeton, autre type) n'arrete pas le test en cours.
    if test is not None and test.kind == kind and (not token or token == test.token):
        _finish(test)
        test.stop()
    return jsonify({'success': True})


# --- Micro ------------------------------------------------------------------

@testing_bp.route('/api/mic/test/start', methods=['POST'])
def start_mic_test():
    body = request.get_json(silent=True) or {}
    settings = load_settings()
    pulse_name = body.get('pulse_name') or _resolve_pulse_name(settings)
    from auto_volume import auto_volume_mgr
    if pulse_name and not auto_volume_mgr.running:
        # Auto-volume actif : son volume courant n'est enregistre que toutes les
        # 30 s, reappliquer la valeur enregistree le faisait sauter.
        from audio_utils import set_pulse_volume
        set_pulse_volume(pulse_name, settings.get('microphone', {}).get('volume', 100))
    test = _Test('mic', 'mic_level', {})
    _replace(test)
    test.thread = threading.Thread(target=_run_process, args=(test, mic_source(pulse_name)), daemon=True)
    test.thread.start()
    return jsonify({'success': True, 'token': test.token})


@testing_bp.route('/api/mic/test/stop', methods=['POST'])
def stop_mic_test():
    return _stop_route('mic')


# --- RTSP ---------------------------------------------------------------------

@testing_bp.route('/api/rtsp/test/start', methods=['POST'])
def start_rtsp_test():
    data = request.get_json(silent=True) or {}
    rtsp_url = str(data.get('url', '') or '').strip()
    test_id = str(data.get('id', ''))  # renvoye dans rtsp_level : l'UI met a jour la bonne carte
    try:
        initial_gain = float(data.get('gain', 10))
    except (TypeError, ValueError):
        raise ApiError('Gain invalide')
    if not rtsp_url:
        raise ApiError('URL RTSP requise')
    # Securite : n'accepter que rtsp:// (ffmpeg accepterait sinon file://,
    # http://, concat:... : lecture de fichiers locaux, SSRF).
    from settings_manager import normalize_rtsp_url
    rtsp_url = normalize_rtsp_url(rtsp_url)
    # Gain lu en direct (curseur de la carte), jamais ecrit ici.
    from classify import live_gain
    source_id = f"rtsp_{test_id}"
    test = _Test('rtsp', 'rtsp_level', {'id': test_id})
    _replace(test)
    test.thread = threading.Thread(
        target=_run_process,
        args=(test, rtsp_source(rtsp_url), lambda: live_gain(source_id, initial_gain)),
        daemon=True)
    test.thread.start()
    return jsonify({'success': True, 'token': test.token})


@testing_bp.route('/api/rtsp/test/stop', methods=['POST'])
def stop_rtsp_test():
    return _stop_route('rtsp')


# --- VBAN (tap sur l'ecoute UDP partagee) ------------------------------------

@testing_bp.route('/api/vban/test/start', methods=['POST'])
def start_vban_test():
    data = request.get_json(silent=True) or {}
    vban_id = str(data.get('id') or '')
    src = next((s for s in load_settings().get('saved_vban_sources') or [] if s.get('id') == vban_id), None)
    if src is None:
        raise ApiError('Source VBAN introuvable', 404)
    ip = src.get('ip', '')
    from vban_manager import get_vban_detector
    detector = get_vban_detector()
    if detector is None:
        raise ApiError("L'écoute VBAN n'est pas disponible", 500)
    # Gain : celui du curseur s'il a change, sinon celui des reglages, lu une
    # fois (avant : relu a chaque paquet, ~190 fois/s).
    stream_name = src.get('stream_name') or src.get('name') or ''
    saved_gain = float(src.get('gain', 1.0) or 1.0)
    source_id = f"vban_{vban_id}"
    from classify import live_gain

    test = _Test('vban', 'vban_level', {'id': vban_id})
    _replace(test)

    def _tap(peak):
        if test.stop_event.is_set():
            return
        test.level(peak * live_gain(source_id, saved_gain))

    def _clear():
        detector.set_test_tap(None, None)

    def _expire():
        if not test.stop_event.wait(TEST_MAX_SECONDS):
            _finish(test, f'test arrêté automatiquement après {TEST_MAX_SECONDS // 60} minutes')
            test.stop_event.set()
            _clear()

    test.on_stop = _clear
    detector.set_test_tap(ip, _tap, stream_name=stream_name)
    test.thread = threading.Thread(target=_expire, daemon=True)
    test.thread.start()
    logging.info(f"VBAN test: écoute de {ip}/{stream_name}")
    return jsonify({'success': True, 'token': test.token})


@testing_bp.route('/api/vban/test/stop', methods=['POST'])
def stop_vban_test():
    return _stop_route('vban')
