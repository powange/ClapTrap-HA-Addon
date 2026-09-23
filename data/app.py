import atexit
import logging
import os
import re
import secrets
import signal
import sys
import threading
import time

from flask import Flask, request, render_template
from flask_socketio import SocketIO

from settings_manager import load_settings
from vban_manager import init_vban_detector, cleanup_vban_detector
from audio_utils import get_audio_input_devices

# Configuration du logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Appliquer le niveau de log depuis les settings
try:
    if load_settings().get('global', {}).get('debug', False):
        logging.getLogger().setLevel(logging.DEBUG)
        logging.info("Mode debug active depuis les settings")
except Exception as e:
    logging.warning(f"Lecture des settings au demarrage: {e}")

# PULSE_SERVER est detecte par run.sh (unique endroit).
logging.info(f"PulseAudio PULSE_SERVER={os.environ.get('PULSE_SERVER', 'NON DEFINI')}")

# Réduire le niveau de log des modules trop verbeux
for _name in ('werkzeug', 'engineio', 'socketio', 'engineio.server', 'socketio.server'):
    logging.getLogger(_name).setLevel(logging.WARNING)


class IngressMiddleware:
    """Middleware WSGI pour gérer le préfixe de chemin ingress de Home Assistant."""
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        ingress_path = environ.get('HTTP_X_INGRESS_PATH', '')
        if ingress_path:
            environ['SCRIPT_NAME'] = ingress_path
        return self.app(environ, start_response)


class IngressOnlyMiddleware:
    """Middleware WSGI : n'accepte que les connexions venant de l'ingress HA.

    L'add-on tourne en host_network : sans ce filtre, le port 16045 (UI, API,
    Socket.IO) est joignable sans authentification depuis tout le LAN. Le
    Supervisor proxifie l'ingress depuis 172.30.32.2 : c'est la seule adresse
    acceptee (la boucle locale est celle de l'hote en host_network, donc
    accessible aux autres add-ons). Hors Home Assistant (dev local, pas de
    SUPERVISOR_TOKEN), le filtre est desactive.
    """
    ALLOWED = {'172.30.32.2'}

    def __init__(self, app):
        self.app = app
        self.enabled = bool(os.environ.get('SUPERVISOR_TOKEN'))
        self._rejected = set()

    def __call__(self, environ, start_response):
        remote = environ.get('REMOTE_ADDR', '')
        if remote.startswith('::ffff:'):
            remote = remote[7:]
        if self.enabled and remote not in self.ALLOWED:
            if remote not in self._rejected:
                self._rejected.add(remote)
                logging.warning(f"Acces refuse depuis {remote} : l'interface n'est accessible que via l'ingress Home Assistant")
            start_response('403 Forbidden', [('Content-Type', 'text/plain; charset=utf-8')])
            return [b"ClapTrap n'est accessible que depuis Home Assistant (ingress)."]
        return self.app(environ, start_response)

app = Flask(__name__)
app.wsgi_app = IngressMiddleware(app.wsgi_app)
app.logger.setLevel(logging.WARNING)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0  # Désactiver le cache des fichiers statiques
# Taille maximale d'une requete (import de configuration compris) : 2 Mo.
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024
# Origine non filtree ici : l'ingress protege l'acces. Seul le Supervisor
# (172.30.32.2, cf. IngressOnlyMiddleware) peut joindre le serveur, et il
# n'ouvre l'ingress qu'a une session Home Assistant authentifiee (jeton
# d'ingress imprevisible). L'Origin vue ici est celle de HA, variable selon
# l'adresse utilisee (locale, Nabu Casa, application) : la filtrer casserait
# des acces legitimes sans rien ajouter. CORS_ORIGINS permet de la restreindre.
socketio = SocketIO(app,
    cors_allowed_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    logger=False,
    engineio_logger=False,
    ping_timeout=60,
    ping_interval=25,
    async_mode='threading'
)
# Enveloppe externe (apres SocketIO) : le filtre couvre aussi /socket.io.
app.wsgi_app = IngressOnlyMiddleware(app.wsgi_app)

# Initialiser le détecteur VBAN au démarrage (singleton)
init_vban_detector()

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

# Entites HA : connexion MQTT en arriere-plan (retentee si le broker n'est
# pas pret) puis enregistrement de TOUTES les sources configurees.
try:
    from ha_entities import init_entities
    init_entities(settings=load_settings())
except Exception as e:
    logging.warning(f"Init entites HA: {e}")


_cleaned = False


@atexit.register
def cleanup():
    """Nettoie les ressources lors de l'arrêt (appele par atexit, par le
    handler SIGTERM en dev et par le hook worker_exit de Gunicorn)."""
    global _cleaned
    if _cleaned:
        return
    _cleaned = True
    # Fermer d'abord la detection (processus ffmpeg/parecord, classifieurs,
    # volume auto persiste), avec des delais courts : le superviseur tue le
    # processus apres quelques secondes.
    try:
        from classify import stop_detection
        stop_detection(timeout=3)
    except Exception as e:
        logging.debug(f"Arret de la detection: {e}")
    try:
        from ha_entities import shutdown as ha_shutdown
        ha_shutdown()
    except Exception as e:
        logging.debug(f"Arret MQTT: {e}")
    cleanup_vban_detector()




@app.route('/')
def index():
    return render_template('index.html',
                           settings=load_settings(),
                           devices=get_audio_input_devices(),
                           debug=app.debug,
                           ingress_path=request.script_root,
                           cache_bust=int(time.time()))


@app.route('/css/<version>/<path:filename>')
def serve_versioned_css(version, filename):
    """Sert le CSS avec la version dans le chemin (bypass service worker cache)."""
    from werkzeug.utils import safe_join
    # safe_join rejette les traversees de repertoire (../) -> None.
    file_path = safe_join(app.static_folder, 'css', filename)
    if not file_path or not os.path.exists(file_path):
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
    from werkzeug.utils import safe_join
    file_path = safe_join(app.static_folder, 'js', 'modules', filename)
    if not file_path or not os.path.exists(file_path):
        # Essayer dans js/ directement
        file_path = safe_join(app.static_folder, 'js', filename)
    if not file_path or not os.path.exists(file_path):
        return 'Not found', 404

    with open(file_path, 'r') as f:
        content = f.read()

    # Réécrire les imports relatifs : ./modules/foo.js ou ./foo.js -> chemin versionné absolu
    base = request.script_root + f'/js/{version}'
    content = re.sub(
        r"""from\s+['"]\.\/modules\/([^'"]+)['"]""",
        lambda m: f"from '{base}/{m.group(1)}'",
        content
    )
    content = re.sub(
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


def _auto_start_if_configured():
    """Demarre la detection au lancement si l'option est activee."""
    if not load_settings().get('microphone', {}).get('auto_start', False):
        return

    def _delayed_auto_start():
        time.sleep(3)
        try:
            logging.info("Auto-start: démarrage automatique de la détection...")
            from classify import start_from_settings
            started, sources = start_from_settings(socketio)
            if started:
                source_display = ' + '.join(s['label'] for s in sources)
                logging.info(f"Auto-start: détection démarrée ({source_display})")
                socketio.emit('detection_status', {'status': 'running', 'source': source_display})
            elif not sources:
                logging.warning("Auto-start: aucune source activée")
            else:
                logging.warning("Auto-start: la détection n'a pas pu démarrer")
        except Exception as e:
            logging.error(f"Auto-start: erreur - {e}")

    threading.Thread(target=_delayed_auto_start, daemon=True).start()


# En production, Gunicorn (worker gthread + simple-websocket, cf.
# gunicorn.conf.py) importe ce module dans son unique worker.
_auto_start_if_configured()


if __name__ == '__main__':
    # Developpement local uniquement : serveur Werkzeug.
    # SIGTERM : sans handler, Python meurt sans executer atexit.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
    try:
        socketio.run(app, host='0.0.0.0', port=16045, debug=False, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()
