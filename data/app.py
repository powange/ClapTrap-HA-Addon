from flask import Flask, jsonify, request, render_template, send_from_directory
from flask_socketio import SocketIO
from classify import start_detection, stop_detection, is_running, get_current_source
import sounddevice as sd
import numpy as np
import json
import requests
from vban_manager import init_vban_detector as init_vban, cleanup_vban_detector, get_vban_detector
import threading
import time
import os
from datetime import datetime
import socket
import uuid
from threading import Lock
import logging
import secrets
from webhook import WebhookManager
from settings_manager import load_settings, save_settings, SETTINGS_FILE

# Configuration du logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Appliquer le niveau de log depuis les settings
try:
    _init_settings = load_settings()
    if _init_settings.get('global', {}).get('debug', False):
        logging.getLogger().setLevel(logging.DEBUG)
        logging.info("Mode debug active depuis les settings")
except Exception:
    pass

# Vérifier la configuration PulseAudio
pulse_server = os.environ.get('PULSE_SERVER', '')
if not pulse_server:
    for pulse_path in ['/run/audio/pulse.sock', '/run/pulse/native', '/run/pulse/pulseaudio.socket', '/var/run/pulse/native']:
        if os.path.exists(pulse_path):
            os.environ['PULSE_SERVER'] = f'unix:{pulse_path}'
            pulse_server = os.environ['PULSE_SERVER']
            break
    else:
        # Fallback : extraire depuis pactl info
        try:
            import subprocess
            result = subprocess.run(['pactl', 'info'], capture_output=True, text=True, timeout=5)
            for line in result.stdout.splitlines():
                if 'Server String:' in line:
                    server_str = line.split(':', 1)[1].strip()
                    if server_str:
                        os.environ['PULSE_SERVER'] = server_str
                        pulse_server = server_str
                    break
        except Exception:
            pass
logging.info(f"PulseAudio PULSE_SERVER={os.environ.get('PULSE_SERVER', 'NON DEFINI')}")

# Réduire le niveau de log des modules trop verbeux
logging.getLogger('werkzeug').setLevel(logging.WARNING)
logging.getLogger('engineio').setLevel(logging.WARNING)
logging.getLogger('socketio').setLevel(logging.WARNING)
logging.getLogger('engineio.server').setLevel(logging.WARNING)
logging.getLogger('socketio.server').setLevel(logging.WARNING)

class IngressMiddleware:
    """Middleware WSGI pour gérer le préfixe de chemin ingress de Home Assistant."""
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        ingress_path = environ.get('HTTP_X_INGRESS_PATH', '')
        if ingress_path:
            environ['SCRIPT_NAME'] = ingress_path
        return self.app(environ, start_response)

# Configurer Flask pour qu'il soit moins verbeux
app = Flask(__name__)
app.wsgi_app = IngressMiddleware(app.wsgi_app)
app.logger.setLevel(logging.WARNING)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0  # Désactiver le cache des fichiers statiques
socketio = SocketIO(app,
    cors_allowed_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    logger=False,
    engineio_logger=False,
    ping_timeout=60,
    ping_interval=25,
    async_mode='threading'
)


from audio_utils import get_audio_input_devices

# Initialiser le détecteur VBAN au démarrage (singleton)
init_vban()

# Appliquer le volume micro sauvegardé au démarrage
def _apply_saved_mic_volume():
    try:
        from audio_utils import set_pulse_volume
        settings = load_settings()
        mic = settings.get('microphone', {})
        pulse_name = mic.get('pulse_name', '')
        volume = mic.get('volume', 100)
        if pulse_name and volume != 100:
            set_pulse_volume(pulse_name, volume)
            logging.info(f"Volume micro restauré à {volume}% pour {pulse_name}")
    except Exception as e:
        logging.debug(f"Impossible de restaurer le volume micro au démarrage: {e}")

_apply_saved_mic_volume()

# Enregistrer les entites HA au demarrage
try:
    from ha_entities import init_entities, register_source, source_entity_key
    _ha_settings = load_settings()
    init_entities(settings=_ha_settings)
    _ha_mic = _ha_settings.get('microphone', {})
    if _ha_mic.get('enabled', False):
        register_source(source_entity_key('mic', _ha_mic),
                        label=_ha_mic.get('audio_source', 'Microphone'),
                        groups=_ha_mic.get('sound_groups'),
                        clap_counts=_ha_mic.get('ha_entities', [1, 2]))
    for _ha_src in _ha_settings.get('rtsp_sources', []):
        if _ha_src.get('enabled', False):
            register_source(source_entity_key('rtsp', _ha_src),
                           label=f"RTSP: {_ha_src.get('name', 'RTSP')}",
                           groups=_ha_src.get('sound_groups'),
                           clap_counts=_ha_src.get('ha_entities', [1, 2]))
except Exception as e:
    logging.warning(f"Init entites HA: {e}")

# Nettoyer lors de l'arrêt
import atexit

@atexit.register
def cleanup():
    """Nettoie les ressources lors de l'arrêt"""
    cleanup_vban_detector()


class VBANSource:
    def __init__(self, name, ip, port, stream_name, webhook_url, enabled=True):
        self.name = name
        self.ip = ip
        self.port = port
        self.stream_name = stream_name
        self.webhook_url = webhook_url
        self.enabled = enabled

    def to_dict(self):
        return {
            "name": self.name,
            "ip": self.ip,
            "port": self.port,
            "stream_name": self.stream_name,
            "webhook_url": self.webhook_url,
            "enabled": self.enabled
        }

    @staticmethod
    def from_dict(data):
        return VBANSource(
            name=data.get("name", ""),
            ip=data.get("ip", ""),
            port=data.get("port", 6980),
            stream_name=data.get("stream_name", ""),
            webhook_url=data.get("webhook_url", ""),
            enabled=data.get("enabled", True)
        )


_flux_cache = None
_flux_cache_time = 0

def load_flux():
    global _flux_cache, _flux_cache_time
    now = time.time()
    if _flux_cache is not None and (now - _flux_cache_time) < 60:
        return _flux_cache
    try:
        with open('flux.json', 'r') as f:
            _flux_cache = json.load(f)
            _flux_cache_time = now
            return _flux_cache
    except FileNotFoundError:
        return {"audio_streams": []}

@app.route('/')
def index():
    settings = load_settings()  # Charge les paramètres depuis le fichier JSON
    input_devices = get_audio_input_devices()  # Obtient la liste des périphériques audio d'entrée
    flux = load_flux()

    return render_template('index.html',
                         settings=settings,
                         devices=input_devices,
                         flux=flux['audio_streams'],
                         debug=app.debug,
                         ingress_path=request.script_root,
                         cache_bust=int(time.time()))

def verify_settings_saved(new_settings, saved_settings):
    """Vérifie que les paramètres ont été correctement sauvegardés"""
    try:
        # Vérifier les paramètres globaux
        if 'global' in new_settings:
            for field in ['threshold', 'delay', 'chunk_duration', 'buffer_duration']:
                if new_settings['global'].get(field) != saved_settings['global'].get(field):
                    logging.debug(f"Différence détectée pour global.{field}:")
                    logging.debug(f"  Attendu: {new_settings['global'].get(field)}")
                    logging.debug(f"  Sauvegardé: {saved_settings['global'].get(field)}")
                    return False

        # Vérifier les paramètres du microphone
        if 'microphone' in new_settings:
            for field in ['device_index', 'audio_source', 'webhook_url']:
                if new_settings['microphone'].get(field) != saved_settings['microphone'].get(field):
                    logging.debug(f"Différence détectée pour microphone.{field}:")
                    logging.debug(f"  Attendu: {new_settings['microphone'].get(field)}")
                    logging.debug(f"  Sauvegardé: {saved_settings['microphone'].get(field)}")
                    return False

        # Vérifier les sources RTSP si présentes
        if 'rtsp_sources' in new_settings:
            if len(new_settings['rtsp_sources']) != len(saved_settings.get('rtsp_sources', [])):
                logging.debug("Différence dans le nombre de sources RTSP")
                return False
            for i, (new_source, saved_source) in enumerate(zip(new_settings['rtsp_sources'], saved_settings['rtsp_sources'])):
                for field in ['name', 'url', 'webhook_url']:
                    if new_source.get(field) != saved_source.get(field):
                        logging.debug(f"Différence détectée pour rtsp_sources[{i}].{field}:")
                        logging.debug(f"  Attendu: {new_source.get(field)}")
                        logging.debug(f"  Sauvegardé: {saved_source.get(field)}")
                        return False

        # Ne pas vérifier les champs à la racine car ils sont maintenant dans les sections appropriées
        logging.debug("Tous les paramètres ont été correctement sauvegardés")
        return True

    except Exception as e:
        logging.error(f"Erreur lors de la vérification des paramètres: {str(e)}")
        return False

@app.route('/clap_detected')  # Added route for clap detection
def clap_detected():
    socketio.emit('clap', {'message': 'Applaudissement détecté!'})
    return "Notification envoyée"

def validate_settings(settings):
    """Valide les paramètres avant la sauvegarde"""
    required_fields = ['threshold', 'delay', 'audio_source']

    # Vérifier les champs requis
    if not all(field in settings for field in required_fields):
        return False

    # Valider les valeurs
    try:
        threshold = float(settings['threshold'])
        delay = float(settings['delay'])

        if not (0 <= threshold <= 1):
            return False
        if delay < 0:
            return False

        # Valider l'URL du webhook si présente
        if settings.get('microphone', {}).get('webhook_url'):
            url = settings['microphone']['webhook_url']
            if not url.startswith(('http://', 'https://')):
                return False

    except (ValueError, TypeError):
        return False

    return True

@socketio.on('clap_detected')
def handle_clap(data):
    logging.debug(f"🎯 Clap detected: {data}")  # Debug log
    try:
        socketio.emit('clap', {
            'source_id': 'microphone',
            'timestamp': time.time()
        }, broadcast=True)
        logging.debug(f"✅ Clap event emitted")
    except Exception as e:
        logging.debug(f"❌ Error emitting clap event: {str(e)}")

import re as _re

# Version pour le cache bust des modules JS (change à chaque restart)
_js_version = str(int(time.time()))

@app.route('/css/<version>/<path:filename>')
def serve_versioned_css(version, filename):
    """Sert le CSS avec la version dans le chemin (bypass service worker cache)."""
    file_path = os.path.join(app.static_folder, 'css', filename)
    if not os.path.exists(file_path):
        return 'Not found', 404
    with open(file_path, 'r') as f:
        content = f.read()
    response = app.response_class(content, mimetype='text/css')
    response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    return response

@app.route('/js/<version>/<path:filename>')
def serve_versioned_js(version, filename):
    """Sert les modules JS avec la version dans le chemin (bypass service worker cache).
    Réécrit les imports relatifs pour pointer vers le même chemin versionné."""
    file_path = os.path.join(app.static_folder, 'js', 'modules', filename)
    if not os.path.exists(file_path):
        # Essayer dans js/ directement
        file_path = os.path.join(app.static_folder, 'js', filename)
    if not os.path.exists(file_path):
        return 'Not found', 404

    with open(file_path, 'r') as f:
        content = f.read()

    # Réécrire les imports relatifs : ./modules/foo.js ou ./foo.js -> chemin versionné absolu
    base = request.script_root + f'/js/{version}'
    content = _re.sub(
        r"""from\s+['"]\.\/modules\/([^'"]+)['"]""",
        lambda m: f"from '{base}/{m.group(1)}'",
        content
    )
    content = _re.sub(
        r"""from\s+['"]\.\/([^'"]+)['"]""",
        lambda m: f"from '{base}/{m.group(1)}'",
        content
    )

    response = app.response_class(content, mimetype='application/javascript')
    # Le chemin contient la version, donc on peut cacher longtemps
    response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    return response

# --- Register Blueprints ---
from routes.detection import detection_bp, init_detection
from routes.sources import sources_bp, init_sources
from routes.settings_routes import settings_bp
from routes.testing import testing_bp, init_testing

app.register_blueprint(detection_bp)
app.register_blueprint(sources_bp)
app.register_blueprint(settings_bp)
app.register_blueprint(testing_bp)

init_detection(socketio)
init_sources(socketio)
init_testing(socketio)

@socketio.on('connect')
def handle_connect():
    logging.debug("🔌 Client connecté")

@socketio.on('disconnect')
def handle_disconnect():
    logging.debug("🔌 Client déconnecté")

@socketio.on('test_connection')
def handle_test():
    logging.debug("🔔 Test de connexion reçu")
    socketio.emit('debug', {'message': 'Test serveur'})

if __name__ == '__main__':
    # Auto-start detection si configuré
    settings = load_settings()
    if settings.get('microphone', {}).get('auto_start', False):
        def _delayed_auto_start():
            import time as _time
            _time.sleep(3)
            try:
                logging.info("Auto-start: démarrage automatique de la détection...")
                from classify import start_detection, build_sources_from_settings
                s = load_settings()
                global_s = s.get('global', {})
                sources = build_sources_from_settings(s)
                if sources:
                    start_detection(
                        model="yamnet.tflite", max_results=10,
                        score_threshold=float(global_s.get('threshold', 0.5)),
                        overlapping_factor=0.8, socketio=socketio,
                        delay=float(global_s.get('delay', 1.0)), sources=sources
                    )
                    source_display = ' + '.join(s['label'] for s in sources)
                    logging.info(f"Auto-start: détection démarrée ({source_display})")
                    socketio.emit('detection_status', {'status': 'running', 'source': source_display})
                else:
                    logging.warning("Auto-start: aucune source activée")
            except Exception as e:
                logging.error(f"Auto-start: erreur - {e}")

        threading.Thread(target=_delayed_auto_start, daemon=True).start()

    try:
        # Désactiver le mode debug
        socketio.run(app, host='0.0.0.0', port=16045, debug=False, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        cleanup_vban_detector()
    except Exception as e:
        logging.error(f"Erreur lors du démarrage du serveur: {e}")
        cleanup_vban_detector()
