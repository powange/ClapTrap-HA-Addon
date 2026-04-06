import json
import os
import time
import logging
from threading import Lock

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, 'settings.json')
SETTINGS_BACKUP = os.path.join(BASE_DIR, 'settings.json.backup')
SETTINGS_TEMP = os.path.join(BASE_DIR, 'settings.json.tmp')

DEFAULT_SETTINGS = {
    "global": {
        "threshold": "0.5",
        "delay": "1.0"
    },
    "microphone": {
        "device_index": "0",
        "audio_source": "default",
        "webhook_url": "",
        "enabled": False
    },
    "rtsp_sources": [],
    "saved_vban_sources": [],
    "vban": {
        "stream_name": "",
        "ip": "0.0.0.0",
        "port": 6980,
        "webhook_url": "",
        "enabled": False
    }
}

_lock = Lock()
_cache = None
_cache_time = 0
_CACHE_TTL = 5  # secondes


def _deep_merge(default, saved):
    """Fusionne récursivement les paramètres par défaut avec les paramètres sauvegardés."""
    merged = default.copy()
    for key, value in saved.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_settings():
    """Charge les paramètres avec cache TTL et gestion d'erreurs."""
    global _cache, _cache_time

    with _lock:
        now = time.time()
        if _cache is not None and (now - _cache_time) < _CACHE_TTL:
            return _cache

        try:
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, 'r') as f:
                    saved = json.load(f)
                _cache = _deep_merge(DEFAULT_SETTINGS, saved)
            else:
                _cache = DEFAULT_SETTINGS.copy()
                with open(SETTINGS_FILE, 'w') as f:
                    json.dump(_cache, f, indent=4)
        except Exception as e:
            logging.error(f"Erreur lors du chargement des paramètres: {e}")
            if _cache is None:
                _cache = DEFAULT_SETTINGS.copy()

        _cache_time = now
        return _cache


def save_settings(new_settings):
    """Sauvegarde les paramètres de manière atomique avec invalidation du cache."""
    global _cache, _cache_time

    with _lock:
        try:
            current = DEFAULT_SETTINGS.copy()
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, 'r') as f:
                    current = _deep_merge(current, json.load(f))

            if 'rtsp_sources' not in new_settings:
                new_settings['rtsp_sources'] = current.get('rtsp_sources', [])

            current.update(new_settings)

            with open(SETTINGS_TEMP, 'w') as f:
                json.dump(current, f, indent=4)

            if os.path.exists(SETTINGS_FILE):
                os.replace(SETTINGS_FILE, SETTINGS_BACKUP)

            os.replace(SETTINGS_TEMP, SETTINGS_FILE)

            # Invalider le cache
            _cache = current
            _cache_time = time.time()

            return True, "Paramètres sauvegardés avec succès"

        except Exception as e:
            return False, f"Erreur lors de la sauvegarde des paramètres: {str(e)}"
