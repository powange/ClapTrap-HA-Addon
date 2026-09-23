"""Configuration Gunicorn (serveur de production de l'add-on).

Worker "gthread" + simple-websocket : mode recommande par Flask-SocketIO pour
async_mode="threading" (inference audio lourde, pas de green threads).
Un seul worker : l'etat (detection, MQTT, ecoute VBAN) vit dans ce processus.
"""
import socket

HASSIO_IP = '172.30.32.1'  # interface interne de HA (reseau hassio), cible de l'ingress


def _bind_address():
    """Ecoute sur l'interface interne de Home Assistant quand elle existe :
    avec host_network, 0.0.0.0 exposait aussi le port 16045 sur le LAN."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((HASSIO_IP, 0))
        return HASSIO_IP
    except OSError:
        return '0.0.0.0'


bind = f"{_bind_address()}:16045"
workers = 1
worker_class = 'gthread'
threads = 64
timeout = 60
graceful_timeout = 5
accesslog = None
errorlog = '-'
loglevel = 'warning'


def worker_exit(server, worker):
    # Arret propre : detection, MQTT (detection OFF + indisponible), VBAN.
    try:
        import app
        app.cleanup()
    except Exception as exc:
        server.log.warning(f"Arrêt de ClapTrap incomplet : {exc}")
