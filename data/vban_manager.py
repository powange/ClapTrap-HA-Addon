from vban_listener import VBANDetector
import logging
import threading

DEFAULT_PORT = 6980

# Global VBAN detector instance
vban_detector = None
# Verrou : sans lui, deux requetes Flask concurrentes au demarrage passaient
# toutes deux le test `is None` et creaient DEUX detecteurs (2 sockets + 2
# threads sur le port d'ecoute), le 1er fuyant sans jamais etre arrete.
_vban_lock = threading.Lock()

_warned = set()   # options invalides deja signalees (une fois chacune)


def configured_port():
    """Port UDP d'ecoute VBAN : option `vban_port` de l'add-on (onglet
    Configuration), 6980 par defaut. Un seul socket pour toutes les sources
    (reconnues par leur IP et le nom du flux)."""
    from ha_entities import _addon_options
    value = _addon_options().get('vban_port')
    if value in (None, ''):
        return DEFAULT_PORT
    try:
        port = int(value)
        if isinstance(value, bool) or not 1 <= port <= 65535 or port != float(value):
            raise ValueError
        return port
    except (TypeError, ValueError):
        if repr(value) not in _warned:
            _warned.add(repr(value))
            logging.warning(f"Option vban_port invalide ({value!r}) : port {DEFAULT_PORT} utilisé")
        return DEFAULT_PORT


def listen_port():
    """Port a afficher et a enregistrer : celui du socket ouvert s'il y en a
    un (les options ont pu changer depuis, sans redemarrage), sinon l'option."""
    det = vban_detector
    return det.port if det is not None else configured_port()


def init_vban_detector():
    """Initialize the VBAN detector"""
    global vban_detector
    port = DEFAULT_PORT
    try:
        with _vban_lock:
            if vban_detector is not None:
                return True
            port = configured_port()
            detector = VBANDetector(port=port)
            # bind synchrone : leve une exception si le port est occupe
            detector.start_listening()
            vban_detector = detector
            return True
    except Exception as e:
        logging.warning(f"Écoute VBAN impossible sur le port {port} : {e}")
        return False

def get_vban_detector():
    """Get the global VBAN detector instance"""
    if vban_detector is None:
        if not init_vban_detector():
            return None
    return vban_detector

def cleanup_vban_detector():
    """Clean up VBAN detector resources"""
    global vban_detector
    with _vban_lock:
        if vban_detector:
            try:
                vban_detector.stop_listening()
                logging.debug("Stopping VBAN detector...")
            except Exception as e:
                logging.debug(f"Error stopping VBAN detector: {e}")
            vban_detector = None
