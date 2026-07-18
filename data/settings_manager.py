import copy
import json
import os
import time
import logging
from threading import RLock

# /data est le volume persistant HA (survit aux mises à jour de l'addon)
PERSISTENT_DIR = '/data'
# Fallback sur le répertoire de l'app pour le dev local
if not os.path.isdir(PERSISTENT_DIR):
    PERSISTENT_DIR = os.path.dirname(os.path.abspath(__file__))

SETTINGS_FILE = os.path.join(PERSISTENT_DIR, 'settings.json')
SETTINGS_BACKUP = os.path.join(PERSISTENT_DIR, 'settings.json.backup')
SETTINGS_TEMP = os.path.join(PERSISTENT_DIR, 'settings.json.tmp')

DEFAULT_SETTINGS = {
    "global": {
        "threshold": 0.5,
        "delay": 1.5,
        "debug": False,
        "peak_cooldown": 0.08,
        "peak_ratio": 3.0,
        "peak_reset": 0.3,
        "sound_exclusions": []
    },
    "microphone": {
        "device_index": 0,
        "audio_source": "default",
        "webhook_url": "",
        "enabled": False,
        "volume": 100,
        "auto_volume": False,
        "auto_start": False,
        "threshold": 0.5,
        "ha_entities": [1, 2],
        "sound_whitelist": {"Clapping": True, "Hands": True, "Applause": True}
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

# RLock (reentrant) : permet a atomic_update() de tenir le verrou pendant tout
# le cycle load -> mutate -> save, alors que load_settings/save_settings
# reacquierent le meme verrou en interne.
_lock = RLock()
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


def _ensure_source_groups(source, default_threshold=0.5):
    """Garantit que la source a un champ `sound_groups`.

    Si absent ou vide, en cree un seul nomme "Clap" derive des champs
    historiques (`sound_whitelist`, `threshold`, `ha_entities`). Permet la
    coexistence des anciennes sources monogroupes et des nouvelles
    multigroupes sans casser l'UI ni les entites HA existantes.
    """
    if not isinstance(source, dict):
        return
    groups = source.get('sound_groups')
    if isinstance(groups, list) and groups:
        # Normalise minimalement chaque groupe (nom + slug obligatoires)
        for idx, g in enumerate(groups):
            if not isinstance(g, dict):
                continue
            g.setdefault('name', f'Groupe {idx + 1}')
            g.setdefault('slug', _slugify(g['name']) or f'group{idx + 1}')
            g.setdefault('sound_whitelist', {})
            g.setdefault('threshold', float(source.get('threshold', default_threshold)))
            g.setdefault('ha_entities', source.get('ha_entities', [1, 2]))
        return

    legacy_whitelist = source.get('sound_whitelist') or {
        "Clapping": True, "Hands": True, "Applause": True
    }
    source['sound_groups'] = [{
        'name': 'Clap',
        'slug': 'clap',
        'sound_whitelist': dict(legacy_whitelist),
        'threshold': float(source.get('threshold', default_threshold)),
        'ha_entities': list(source.get('ha_entities', [1, 2])),
    }]


def _slugify(text):
    """Slug minimaliste compatible MQTT topic / object_id HA."""
    if not text:
        return ''
    s = str(text).lower()
    s = ''.join(c if c.isalnum() else '_' for c in s)
    while '__' in s:
        s = s.replace('__', '_')
    return s.strip('_')


def _apply_group_migrations(settings):
    """Applique la migration des sources monogroupe vers le format multigroupe."""
    try:
        global_threshold = float(settings.get('global', {}).get('threshold', 0.5))
    except (TypeError, ValueError):
        global_threshold = 0.5
    mic = settings.get('microphone')
    if isinstance(mic, dict):
        _ensure_source_groups(mic, default_threshold=global_threshold)
    for src in settings.get('rtsp_sources', []) or []:
        _ensure_source_groups(src, default_threshold=global_threshold)
    for src in settings.get('saved_vban_sources', []) or []:
        _ensure_source_groups(src, default_threshold=global_threshold)
    return settings


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
            _apply_group_migrations(_cache)
        except Exception as e:
            logging.error(f"Erreur lors du chargement des paramètres: {e}")
            if _cache is None:
                _cache = DEFAULT_SETTINGS.copy()
                _apply_group_migrations(_cache)

        _cache_time = now
        return copy.deepcopy(_cache)


def save_settings(new_settings):
    """Sauvegarde les paramètres de manière atomique avec invalidation du cache."""
    global _cache, _cache_time

    with _lock:
        try:
            current = DEFAULT_SETTINGS.copy()
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, 'r') as f:
                    current = _deep_merge(current, json.load(f))

            # Préserver les sources RTSP et VBAN uniquement si la clé est ABSENTE
            # du payload (ex: sauvegarde de réglages globaux qui ne gère pas les
            # sources). Une liste vide EXPLICITE (`[]`) est honorée : c'est ce qui
            # permet de supprimer la dernière source. Avant, `== []` était traité
            # comme "non fourni" et restaurait l'ancienne liste → la source
            # supprimée réapparaissait.
            for key in ['rtsp_sources', 'saved_vban_sources']:
                if key not in new_settings:
                    new_settings[key] = current.get(key, [])

            # Deep merge pour préserver les sous-clés (ex: microphone.pulse_name)
            for key, value in new_settings.items():
                if isinstance(value, dict) and isinstance(current.get(key), dict):
                    current[key] = {**current[key], **value}
                else:
                    current[key] = value

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


# Sentinelle : un mutator qui retourne NO_CHANGE indique "rien a sauvegarder".
NO_CHANGE = object()


def atomic_update(mutator):
    """Applique une modification de settings de facon ATOMIQUE.

    `mutator(settings)` recoit les settings courants (deja charges), les mute
    en place (ou retourne un nouveau dict), et le tout — lecture puis
    ecriture — est realise sous un seul verrou. Evite les pertes d'ecriture
    quand plusieurs threads (routes UI + threads d'arriere-plan "son vu" /
    volume auto) font un read-modify-write concurrent sur settings.json.

    Le mutator peut retourner `NO_CHANGE` pour eviter une reecriture inutile.
    Retourne le tuple (success, message) de save_settings.
    """
    with _lock:  # RLock : load_settings/save_settings reacquierent sans blocage
        try:
            settings = load_settings()
            result = mutator(settings)
            if result is NO_CHANGE:
                return True, "Aucun changement"
            if result is None:
                result = settings
            return save_settings(result)
        except Exception as e:
            logging.error(f"atomic_update a echoue: {e}")
            return False, str(e)
