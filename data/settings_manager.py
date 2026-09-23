import copy
import json
import os
import shutil
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
    """Fusionne récursivement les paramètres par défaut avec les paramètres sauvegardés.

    Copie profonde des defauts : une copie superficielle partageait les listes
    et dicts de DEFAULT_SETTINGS, que les migrations mutaient ensuite.
    """
    merged = copy.deepcopy(default)
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


def _ensure_vban_ids(settings):
    """Donne un id stable a chaque source VBAN (comme les cameras).
    Retourne True si des ids ont ete ajoutes (a enregistrer une fois)."""
    import uuid
    added = False
    for src in settings.get('saved_vban_sources', []) or []:
        if isinstance(src, dict) and not src.get('id'):
            src['id'] = str(uuid.uuid4())
            added = True
    return added


class SettingsSaveError(Exception):
    """Echec d'ecriture de settings.json (disque plein, /data en lecture seule...)."""


def _write_atomic(data, keep_backup=True):
    """Ecrit settings.json de facon atomique.

    Le fichier principal existe a tout instant : la sauvegarde est une COPIE
    de la version precedente (avant, il etait deplace vers .backup puis
    remplace : une coupure entre les deux laissait zero fichier et le
    redemarrage repartait sur les valeurs par defaut).
    """
    with open(SETTINGS_TEMP, 'w') as f:
        json.dump(data, f, indent=4)
        f.flush()
        os.fsync(f.fileno())
    if keep_backup and os.path.exists(SETTINGS_FILE):
        shutil.copy2(SETTINGS_FILE, SETTINGS_BACKUP)
    os.replace(SETTINGS_TEMP, SETTINGS_FILE)
    try:
        dir_fd = os.open(PERSISTENT_DIR, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def _read_saved():
    """Lit settings.json, ou la sauvegarde s'il est absent ou illisible.

    Retourne (donnees | None, un_fichier_existe).
    """
    any_file = False
    for path in (SETTINGS_FILE, SETTINGS_BACKUP):
        if not os.path.exists(path):
            continue
        any_file = True
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("le contenu n'est pas un objet JSON")
        except Exception as e:
            logging.error(f"Lecture de {path} impossible: {e}")
            continue
        if path == SETTINGS_BACKUP:
            logging.warning("settings.json absent ou corrompu : restauration depuis settings.json.backup")
            try:
                _write_atomic(data, keep_backup=False)
            except Exception as e:
                logging.error(f"Restauration de settings.json impossible: {e}")
        return data, True
    return None, any_file


def load_settings():
    """Charge les paramètres avec cache TTL et gestion d'erreurs.

    Retourne TOUJOURS une copie : les appelants peuvent la modifier sans
    toucher au cache partage (avant, un cache hit renvoyait l'objet du cache
    lui-meme, mute ensuite hors verrou par les routes).
    """
    global _cache, _cache_time

    with _lock:
        now = time.time()
        if _cache is not None and (now - _cache_time) < _CACHE_TTL:
            return copy.deepcopy(_cache)

        saved, any_file = _read_saved()
        if saved is not None:
            merged = _deep_merge(DEFAULT_SETTINGS, saved)
        elif _cache is not None:
            # Fichiers illisibles : garder la derniere version valide en memoire.
            merged = _cache
        else:
            merged = copy.deepcopy(DEFAULT_SETTINGS)
            if not any_file:
                try:
                    _write_atomic(merged, keep_backup=False)
                except Exception as e:
                    logging.error(f"Création de settings.json impossible: {e}")
            else:
                # Ne pas ecraser des fichiers corrompus : l'utilisateur peut
                # encore les recuperer a la main.
                logging.error("settings.json et sa sauvegarde sont illisibles : valeurs par défaut en mémoire")
        _apply_group_migrations(merged)
        if saved is not None and _ensure_vban_ids(merged):
            # Enregistrer tout de suite : des ids regeneres a chaque chargement
            # ne seraient pas stables.
            try:
                _write_atomic(merged)
            except Exception as e:
                logging.error(f"Migration des ids VBAN non enregistrée: {e}")
        _cache = merged
        _cache_time = now
        return copy.deepcopy(_cache)


def save_settings(new_settings):
    """Sauvegarde les paramètres de manière atomique avec invalidation du cache.

    Retourne (succes, message).
    """
    global _cache, _cache_time

    with _lock:
        try:
            current = load_settings()
            new_settings = dict(new_settings)

            # Préserver les sources RTSP et VBAN uniquement si la clé est ABSENTE
            # du payload (ex: sauvegarde de réglages globaux qui ne gère pas les
            # sources). Une liste vide EXPLICITE (`[]`) est honorée : c'est ce qui
            # permet de supprimer la dernière source.
            for key in ['rtsp_sources', 'saved_vban_sources']:
                if key not in new_settings:
                    new_settings[key] = current.get(key, [])

            # Deep merge pour préserver les sous-clés (ex: microphone.pulse_name)
            for key, value in new_settings.items():
                if isinstance(value, dict) and isinstance(current.get(key), dict):
                    current[key] = {**current[key], **value}
                else:
                    current[key] = value

            _write_atomic(current)

            _cache = copy.deepcopy(current)
            _cache_time = time.time()

            return True, "Paramètres sauvegardés avec succès"

        except Exception as e:
            logging.error(f"Sauvegarde des paramètres impossible: {e}")
            return False, f"Erreur lors de la sauvegarde des paramètres: {str(e)}"


def modify_settings(mutator):
    """Lecture -> modification -> ecriture ATOMIQUE, pour les routes.

    `mutator(settings)` modifie `settings` en place et retourne ce que la
    route veut renvoyer. S'il leve une exception, rien n'est ecrit et
    l'exception remonte (la route la transforme en 400/404). Leve
    SettingsSaveError si l'ecriture echoue.
    """
    with _lock:
        settings = load_settings()
        result = mutator(settings)
        ok, message = save_settings(settings)
        if not ok:
            raise SettingsSaveError(message)
        return result


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


# --- Validation ----------------------------------------------------------------

_TRUE = {'true', '1', 'yes', 'on'}
_FALSE = {'false', '0', 'no', 'off', ''}


def to_bool(value, path='valeur'):
    """Convertit en bool ; "false" (chaine) est bien False, contrairement a bool()."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
        return value.strip().lower() in _TRUE
    raise ValueError(f"{path} : booléen attendu")


def to_number(value, path='valeur', lo=None, hi=None, integer=False):
    if isinstance(value, bool):
        raise ValueError(f"{path} : nombre attendu")
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{path} : nombre attendu")
    if num != num or num in (float('inf'), float('-inf')):
        raise ValueError(f"{path} : nombre attendu")
    if lo is not None and num < lo or hi is not None and num > hi:
        raise ValueError(f"{path} : doit être entre {lo} et {hi}")
    return int(round(num)) if integer else num


def to_clap_counts(value, path='ha_entities'):
    """Liste d'entiers 1..4 (nombres de claps exposes a HA)."""
    if not isinstance(value, list):
        raise ValueError(f"{path} : liste attendue")
    out = []
    for n in value:
        if isinstance(n, bool) or not isinstance(n, (int, float, str)):
            raise ValueError(f"{path} : entiers de 1 à 4 attendus")
        try:
            n = int(n)
        except ValueError:
            raise ValueError(f"{path} : entiers de 1 à 4 attendus")
        if not 1 <= n <= 4:
            raise ValueError(f"{path} : entiers de 1 à 4 attendus")
        if n not in out:
            out.append(n)
    return sorted(out)


def to_webhook(value, path='webhook_url'):
    from url_validator import is_valid_url
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ValueError(f"{path} : texte attendu")
    value = value.strip()
    if value and not is_valid_url(value):
        raise ValueError(f"{path} : URL http(s) invalide")
    return value


def _norm_groups(src, path):
    groups = src.get('sound_groups')
    if groups is None:
        return
    if not isinstance(groups, list):
        raise ValueError(f"{path}.sound_groups : liste attendue")
    for i, g in enumerate(groups):
        gp = f"{path}.sound_groups[{i}]"
        if not isinstance(g, dict):
            raise ValueError(f"{gp} : objet attendu")
        if 'threshold' in g:
            g['threshold'] = to_number(g['threshold'], f"{gp}.threshold", 0, 1)
        if 'ha_entities' in g:
            g['ha_entities'] = to_clap_counts(g['ha_entities'], f"{gp}.ha_entities")
        wl = g.get('sound_whitelist')
        if wl is not None:
            if not isinstance(wl, dict):
                raise ValueError(f"{gp}.sound_whitelist : objet attendu")
            g['sound_whitelist'] = {str(k): to_bool(v, f"{gp}.sound_whitelist.{k}") for k, v in wl.items()}


def _norm_source(src, path):
    if not isinstance(src, dict):
        raise ValueError(f"{path} : objet attendu")
    for key in ('enabled', 'auto_start', 'auto_volume', 'configured'):
        if key in src:
            src[key] = to_bool(src[key], f"{path}.{key}")
    if 'threshold' in src:
        src['threshold'] = to_number(src['threshold'], f"{path}.threshold", 0, 1)
    if 'gain' in src:
        src['gain'] = to_number(src['gain'], f"{path}.gain", 0, 100)
    if 'volume' in src:
        src['volume'] = to_number(src['volume'], f"{path}.volume", 0, 150, integer=True)
    if 'ha_entities' in src:
        src['ha_entities'] = to_clap_counts(src['ha_entities'], f"{path}.ha_entities")
    if 'webhook_url' in src:
        src['webhook_url'] = to_webhook(src['webhook_url'], f"{path}.webhook_url")
    wl = src.get('sound_whitelist')
    if wl is not None:
        if not isinstance(wl, dict):
            raise ValueError(f"{path}.sound_whitelist : objet attendu")
        src['sound_whitelist'] = {str(k): to_bool(v, f"{path}.sound_whitelist.{k}") for k, v in wl.items()}
    _norm_groups(src, path)


def normalize_settings(data):
    """Valide et normalise (en place) un dict de settings importe ou envoye
    par l'UI. Convertit les formes courantes ("0.5", "true") et leve
    ValueError avec le chemin du champ fautif sinon.
    """
    if not isinstance(data, dict):
        raise ValueError("Format invalide : objet JSON attendu")
    g = data.get('global')
    if g is not None:
        if not isinstance(g, dict):
            raise ValueError("global : objet attendu")
        limits = {'threshold': (0, 1), 'delay': (0.1, 10), 'peak_cooldown': (0, 2),
                  'peak_ratio': (1, 50), 'peak_reset': (0, 5)}
        for key, (lo, hi) in limits.items():
            if key in g:
                g[key] = to_number(g[key], f"global.{key}", lo, hi)
        if 'debug' in g:
            g['debug'] = to_bool(g['debug'], 'global.debug')
        if 'sound_exclusions' in g:
            if not isinstance(g['sound_exclusions'], list):
                raise ValueError("global.sound_exclusions : liste attendue")
            g['sound_exclusions'] = [str(x) for x in g['sound_exclusions']]
    mic = data.get('microphone')
    if mic is not None:
        _norm_source(mic, 'microphone')
    for key in ('rtsp_sources', 'saved_vban_sources'):
        lst = data.get(key)
        if lst is None:
            continue
        if not isinstance(lst, list):
            raise ValueError(f"{key} : liste attendue")
        for i, src in enumerate(lst):
            _norm_source(src, f"{key}[{i}]")
            if key == 'rtsp_sources':
                if not isinstance(src.get('url', ''), str):
                    raise ValueError(f"{key}[{i}].url : texte attendu")
                src.setdefault('id', str(__import__('uuid').uuid4()))
            else:
                if not isinstance(src.get('ip'), str) or not src.get('ip'):
                    raise ValueError(f"{key}[{i}].ip : adresse attendue")
                src.setdefault('id', str(__import__('uuid').uuid4()))
    return data
