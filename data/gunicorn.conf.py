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


_address = _bind_address()
bind = f"{_address}:16045"
workers = 1
worker_class = 'gthread'
# 1 ou 2 onglets et leurs WebSocket : 16 fils suffisent, et chacun est attendu
# a l'arret.
threads = 16
timeout = 60
# Arret : MQTT (2 s max) puis detection (2 s max) dans worker_exit. s6 laisse
# S6_KILL_GRACETIME (Dockerfile) et le Supervisor `timeout` (config.yaml).
graceful_timeout = 6
accesslog = None
errorlog = '-'
loglevel = 'warning'


def when_ready(server):
    if _address == '0.0.0.0':
        # Le filtre ingress reste actif (403 hors Supervisor), mais le port est
        # de nouveau ouvert sur le reseau : le dire au lieu de le faire en silence.
        server.log.warning(f"Interface {HASSIO_IP} introuvable : écoute sur toutes les interfaces "
                           "(port 16045 joignable depuis le réseau, accès refusé hors ingress)")


def worker_exit(server, worker):
    # Arret propre : detection, MQTT (detection OFF + indisponible), VBAN.
    try:
        import app
        app.cleanup()
    except Exception as exc:
        server.log.warning(f"Arrêt de ClapTrap incomplet : {exc}")
