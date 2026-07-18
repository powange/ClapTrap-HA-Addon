from vban_detector_new import VBANDetector
import time
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
            detector.start_listening()
            # Attendre que le socket soit initialisé
            for _ in range(10):  # Attendre jusqu'à 1 seconde
                if detector._socket is not None:
                    vban_detector = detector
                    logging.debug("VBANDetector initialized and listening")
                    return True
                time.sleep(0.1)
            # Echec d'init : ne pas laisser un detecteur a moitie demarre.
            try:
                detector.stop_listening()
            except Exception:
                pass
            logging.debug("Timeout waiting for VBANDetector to initialize")
            return False
    except Exception as e:
        logging.debug(f"Error initializing VBANDetector: {e}")
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
