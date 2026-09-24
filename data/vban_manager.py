from vban_listener import VBANDetector
import logging
import threading

# Global VBAN detector instance
vban_detector = None
# Verrou : sans lui, deux requetes Flask concurrentes au demarrage passaient
# toutes deux le test `is None` et creaient DEUX detecteurs (2 sockets + 2
# threads sur le port 6980), le 1er fuyant sans jamais etre arrete.
_vban_lock = threading.Lock()

def init_vban_detector():
    """Initialize the VBAN detector"""
    global vban_detector
    try:
        with _vban_lock:
            if vban_detector is not None:
                return True
            detector = VBANDetector()
            # bind synchrone : leve une exception si le port est occupe
            detector.start_listening()
            vban_detector = detector
            return True
    except Exception as e:
        logging.warning(f"Écoute VBAN impossible sur le port 6980 : {e}")
        return False

def get_vban_detector():
    """Get the global VBAN detector instance"""
    global vban_detector
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
