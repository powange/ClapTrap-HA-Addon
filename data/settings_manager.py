import copy
import re
import json
import os
import shutil
import time
import logging
from threading import RLock

MAX_NR_LEVEL = 3   # reduction du bruit de fond : niveaux 0 a 3 (denoise.LEVELS)

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
        # Groupes de claps : seuls les pics qui ont la forme d'un clap comptent.
        "clap_shape_check": True,
        "sound_exclusions": [],
        # Arbitrage entre pieces : desactive par defaut (comportement inchange).
        "room_arbitration": False,
        "room_window": 1.0
    },
    "microphone": {
        "device_index": 0,
        "audio_source": "default",
        "webhook_url": "",
        "enabled": False,
        "volume": 100,
        "auto_volume": False,
        "auto_start": False,
        # Installation neuve : pas de carte micro tant que l'utilisateur ne
        # l'a pas ajoute (sinon l'accueil « Ajoutez votre premiere source »
        # ne s'affichait jamais).
        "configured": False,
    },
    "rtsp_sources": [],
    "saved_vban_sources": [],
}

# Champs de source remplaces par les groupes de sons : convertis en groupe
# « Clap » par _ensure_source_groups puis retires (double source de verite :
# acceptes et enregistres, ils n'avaient plus aucun effet).
LEGACY_SOURCE_FIELDS = ('threshold', 'ha_entities', 'sound_whitelist')
# Sons du groupe « Clap » d'une nouvelle source (YAMNet : applaudissement,
# mains, applaudissements de foule).
DEFAULT_CLAP_SOUNDS = {"Clapping": True, "Hands": True, "Applause": True}

# RLock (reentrant) : permet a atomic_update() de tenir le verrou pendant tout
# le cycle load -> mutate -> save, alors que load_settings/save_settings
# reacquierent le meme verrou en interne.
_lock = RLock()
_cache = None
_cache_time = 0
_CACHE_TTL = 5  # secondes
# settings.json et sa sauvegarde illisibles : valeurs par defaut en memoire.
# Les ecritures automatiques (sons entendus, auto-volume) sont alors refusees,
# et la premiere ecriture voulue par l'utilisateur met les fichiers corrompus
# de cote : avant, la premiere ecriture automatique les ecrasait.
_degraded = False
# Migration non enregistrable (/data en lecture seule, disque plein) : version
# migree gardee tant que le fichier n'a pas change (ids VBAN stables), puis
# relue normalement des qu'il change (un cache infini ne le relisait jamais).
_unsaved = None   # (mtime du fichier, reglages migres)


def _deep_merge(default, saved):
    """Fusionne récursivement les paramètres par défaut avec les paramètres sauvegardés.

    Copie profonde des defauts : une copie superficielle partageait les listes
    et dicts de DEFAULT_SETTINGS, que les migrations mutaient ensuite.
    """
    merged = copy.deepcopy(default)
    for key, value in saved.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        elif key in merged and isinstance(merged[key], (dict, list)) and not isinstance(value, type(merged[key])):
            # Section d'un mauvais type (null, texte...) : valeur par defaut. Un
            # « microphone: null » empechait l'add-on de redemarrer.
            logging.warning(f"Réglages : section « {key} » invalide, valeurs par défaut utilisées")
        else:
            merged[key] = value
    return merged


def _safe_float(value, default):
    """float() tolerant : un seuil corrompu dans settings.json faisait
    planter TOUS les chargements (donc l'add-on entier)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
            g.setdefault('threshold', _safe_float(source.get('threshold'), default_threshold))
            g.setdefault('ha_entities', source.get('ha_entities', [1, 2]))
        return

    # Source historique (champs monogroupe) : ses sons d'origine. Sinon
    # (source chargee sans groupe : sauvegarde, fichier edite) : groupe vide,
    # rien n'est coche a sa place. Une source AJOUTEE recoit les sons
    # d'applaudissement (with_default_groups).
    if any(k in source for k in LEGACY_SOURCE_FIELDS):
        legacy_whitelist = source.get('sound_whitelist') or dict(DEFAULT_CLAP_SOUNDS)
    else:
        legacy_whitelist = {}
    source['sound_groups'] = [{
        'name': 'Clap',
        'slug': 'clap',
        'sound_whitelist': dict(legacy_whitelist),
        'threshold': _safe_float(source.get('threshold'), default_threshold),
        'ha_entities': list(source.get('ha_entities', [1, 2]) or []),
    }]


def with_default_groups(source):
    """Source ajoutee (interface, assistant, commande) : groupe « Clap » avec
    les sons d'applaudissement deja coches. Il etait vide, et il fallait
    ouvrir les reglages, activer « Ajouter les sons entendus », lancer la
    detection, taper dans ses mains puis cocher avant de detecter quoi que
    ce soit. Un groupe « Clap » vide et seul (micro remis) est complete."""
    groups = source.get('sound_groups')
    if not groups:
        _ensure_source_groups(source)
        groups = source['sound_groups']
    if len(groups) == 1 and not any((groups[0].get('sound_whitelist') or {}).values()):
        groups[0]['sound_whitelist'] = dict(DEFAULT_CLAP_SOUNDS)
    return source


def ascii_slug(text):
    """Slug ASCII [a-z0-9_] : seul format accepte dans les topics MQTT
    discovery et les entity_id de Home Assistant. Les accents sont
    translitteres (« Bébé » -> « bebe ») ; avant, `isalnum()` gardait « é » et
    HA rejetait le topic : l'entite n'etait jamais creee."""
    import unicodedata
    if not text:
        return ''
    s = unicodedata.normalize('NFKD', str(text)).encode('ascii', 'ignore').decode('ascii').lower()
    s = ''.join(c if c.isalnum() else '_' for c in s)
    while '__' in s:
        s = s.replace('__', '_')
    return s.strip('_')


_slugify = ascii_slug


def clap_counts_of(group, fallback=(1, 2)):
    """Nombres de claps exposes a HA pour un groupe. Une liste VIDE veut dire
    « aucune entite » : `a or b or [1, 2]` la remplacait par 1 et 2 claps."""
    if group.get('clap_counts') is None and group.get('ha_entities') is None:
        return list(fallback)
    from clap_logic import normalize_group
    return normalize_group(group)['clap_counts']


_VALID_SLUG = __import__('re').compile(r'^[a-z0-9_]+$')


def _strip_legacy_fields(settings):
    """Retire les champs de source remplaces par les groupes. True si change."""
    changed = False
    sources = [settings.get('microphone')] + list(settings.get('rtsp_sources', []) or []) + \
        list(settings.get('saved_vban_sources', []) or [])
    for src in sources:
        if isinstance(src, dict) and src.get('sound_groups'):
            for key in LEGACY_SOURCE_FIELDS:
                if key in src:
                    del src[key]
                    changed = True
    if 'vban' in settings:  # ancienne section, sans effet depuis longtemps
        del settings['vban']
        changed = True
    if isinstance(settings.get('global'), dict) and 'peak_reset' in settings['global']:
        del settings['global']['peak_reset']  # reglage retire en 6.37
        changed = True
    return changed


def _migrate_mic_configured(mic):
    """Installations d'avant 6.36 : le micro reste affiche s'il a ete utilise
    ou personnalise. True si la cle a ete ajoutee."""
    if not isinstance(mic, dict) or 'configured' in mic:
        return False
    mic['configured'] = bool(mic.get('enabled') or mic.get('sound_groups') or
                             mic.get('audio_source', 'default') not in ('', 'default'))
    return True


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


def vban_entity_key(src, others):
    """Cle d'entite HA d'une source VBAN : son nom, suffixe d'un bout d'id
    si une autre source VBAN porte deja le meme (deux PC qui emettent
    « Stream1 » : la seconde n'avait aucune entite)."""
    base = 'vban_' + (ascii_slug(src.get('name')) or ascii_slug(src.get('ip')) or 'source')
    taken = {o.get('entity_key') for o in others if o is not src}
    if base not in taken:
        return base
    return f"{base}_{ascii_slug(src.get('id', ''))[:4] or 'x'}"


def _ensure_vban_ids(settings):
    """Migrations a enregistrer une seule fois (sinon instables d'un
    chargement a l'autre) : id et cle d'entite de chaque source VBAN, slugs de
    groupe non ASCII. Retourne True si quelque chose a change."""
    import uuid
    changed = False
    vbans = [s for s in settings.get('saved_vban_sources', []) or [] if isinstance(s, dict)]
    for src in vbans:
        if not src.get('id'):
            src['id'] = str(uuid.uuid4())
            changed = True
    for src in vbans:
        if not src.get('entity_key'):
            src['entity_key'] = vban_entity_key(src, vbans)
            changed = True
    for src in vbans:
        # Nom du flux explicite : le nom affiche devient modifiable sans
        # changer le routage des paquets (qui retombait sur `name`).
        if not src.get('stream_name') and src.get('name'):
            src['stream_name'] = src['name']
            changed = True
    sources = [settings.get('microphone')] + list(settings.get('rtsp_sources', []) or []) + vbans
    for src in sources:
        if not isinstance(src, dict):
            continue
        groups = [g for g in src.get('sound_groups') or [] if isinstance(g, dict)]
        used = set()
        for g in groups:
            slug = g.get('slug') or ''
            if not _VALID_SLUG.match(slug) or slug in used:
                base = ascii_slug(slug) or ascii_slug(g.get('name')) or 'groupe'
                new, i = base, 2
                while new in used:
                    new, i = f"{base}{i}", i + 1
                g['slug'] = new
                changed = True
            used.add(g['slug'])
    return changed


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
    global _cache, _cache_time, _degraded, _unsaved

    with _lock:
        now = time.time()
        if _cache is not None and (now - _cache_time) < _CACHE_TTL:
            return copy.deepcopy(_cache)

        if _unsaved is not None and _file_mtime() == _unsaved[0]:
            _cache, _cache_time = _unsaved[1], now
            return copy.deepcopy(_cache)
        _unsaved = None
        saved, any_file = _read_saved()
        mic_migrated = False
        if saved is not None:
            _degraded = False
            mic_migrated = _migrate_mic_configured(saved.get('microphone'))
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
                logging.error("settings.json et sa sauvegarde sont illisibles : valeurs par défaut en mémoire, "
                              "fichiers conservés")
                _degraded = True
        _apply_group_migrations(merged)
        migrated = _ensure_vban_ids(merged)
        migrated = _strip_legacy_fields(merged) or migrated
        if saved is not None and (migrated or mic_migrated):
            # Enregistrer tout de suite : des ids regeneres a chaque chargement
            # ne seraient pas stables.
            try:
                _write_atomic(merged)
            except Exception as e:
                logging.error(f"Migration des réglages non enregistrée: {e}")
                _unsaved = (_file_mtime(), merged)
        _cache = merged
        _cache_time = now
        return copy.deepcopy(_cache)


def _file_mtime():
    try:
        return os.path.getmtime(SETTINGS_FILE)
    except OSError:
        return None


def is_degraded():
    """settings.json et sa sauvegarde illisibles : valeurs par defaut en memoire."""
    return _degraded


# Fonctions appelees apres chaque ecriture (entites de reglage HA). Elles
# doivent rendre la main vite : elles tournent sous le verrou des reglages.
_change_listeners = []


def add_change_listener(fn):
    _change_listeners.append(fn)


def _notify_change():
    for fn in list(_change_listeners):
        try:
            fn()
        except Exception as e:
            logging.debug(f"Notification de changement des réglages: {e}")


def shareable_settings(data):
    """Copie des reglages sans secrets : identifiants RTSP et chemins de
    webhook (le secret d'un webhook Home Assistant) masques."""
    from url_validator import mask_url_credentials, mask_webhook_url
    data = copy.deepcopy(data)
    sources = [data.get('microphone') or {}] + list(data.get('rtsp_sources') or []) + \
        list(data.get('saved_vban_sources') or [])
    for src in sources:
        if src.get('webhook_url'):
            src['webhook_url'] = mask_webhook_url(src['webhook_url'])
        if src.get('url'):
            src['url'] = mask_url_credentials(src['url'])
    return data


def _commit(settings):
    """Ecrit `settings` tel quel (sous _lock) et met le cache a jour."""
    global _cache, _cache_time, _degraded, _unsaved
    _unsaved = None
    prune_room_zones(settings)
    if _degraded:
        stamp = time.strftime('%Y%m%d-%H%M%S')
        for path in (SETTINGS_FILE, SETTINGS_BACKUP):
            if os.path.exists(path):
                os.replace(path, f"{path}.corrompu-{stamp}")
                logging.warning(f"Fichier illisible conservé sous {path}.corrompu-{stamp}")
        _degraded = False
    _write_atomic(settings)
    _cache = copy.deepcopy(settings)
    _notify_change()
    _cache_time = time.time()


# Cles connues (import) : les autres sont ignorees. Avant, toute cle etait
# enregistree telle quelle et recopiee a chaque lecture.
_KNOWN_SECTIONS = ('global', 'microphone', 'rtsp_sources', 'saved_vban_sources')
_GLOBAL_KEYS = {'threshold', 'delay', 'debug', 'peak_cooldown', 'peak_ratio', 'sound_exclusions',
                'room_arbitration', 'room_window', 'room_zones', 'clap_shape_check'}
_SOURCE_KEYS = {
    'microphone': {'device_index', 'audio_source', 'pulse_name', 'webhook_url', 'enabled', 'volume',
                   'auto_volume', 'auto_start', 'configured', 'sound_groups', 'noise_reduction'},
    'rtsp_sources': {'id', 'name', 'url', 'enabled', 'gain', 'webhook_url', 'rtsp_tcp', 'sound_groups',
                     'noise_reduction'},
    'saved_vban_sources': {'id', 'name', 'ip', 'port', 'stream_name', 'enabled', 'gain', 'webhook_url',
                           'entity_key', 'sound_groups', 'noise_reduction'},
}
_GROUP_KEYS = {'slug', 'name', 'threshold', 'ha_entities', 'clap_counts', 'sound_whitelist', 'enabled',
               'auto_add_sounds', 'count_mode', 'entity_slug', '_renamed_from', 'alert_confirm', 'device_class'}


def strip_unknown_keys(data):
    """Retire (en place) les cles inconnues d'un import ; renvoie leurs chemins.
    Les anciens champs de source (seuil, entites, sons) restent : la
    migration les convertit en groupe."""
    removed = []
    for key in [k for k in data if k not in _KNOWN_SECTIONS]:
        removed.append(key)
        data.pop(key)
    g = data.get('global')
    if isinstance(g, dict):
        for key in [k for k in g if k not in _GLOBAL_KEYS]:
            removed.append(f'global.{key}')
            g.pop(key)
    for section, allowed in _SOURCE_KEYS.items():
        value = data.get(section)
        sources = [value] if isinstance(value, dict) else value if isinstance(value, list) else []
        for i, src in enumerate(sources):
            if not isinstance(src, dict):
                continue
            path = section if section == 'microphone' else f'{section}[{i}]'
            for key in [k for k in src if k not in allowed and k not in LEGACY_SOURCE_FIELDS]:
                removed.append(f'{path}.{key}')
                src.pop(key)
            for j, grp in enumerate(src.get('sound_groups') or []):
                if isinstance(grp, dict):
                    for key in [k for k in grp if k not in _GROUP_KEYS]:
                        removed.append(f'{path}.sound_groups[{j}].{key}')
                        grp.pop(key)
    return removed


def merge_import(current, new_settings):
    """Reglages resultant d'un import (sections presentes remplacees,
    migrations appliquees). Utilise par l'enregistrement ET par le controle
    des collisions, qui voyait des reglages differents."""
    current = copy.deepcopy(current)
    new_settings = copy.deepcopy(dict(new_settings))
    _migrate_mic_configured(new_settings.get('microphone'))
    for key, value in new_settings.items():
        current[key] = value
    merged = _deep_merge(DEFAULT_SETTINGS, current)
    _apply_group_migrations(merged)
    _ensure_vban_ids(merged)
    _strip_legacy_fields(merged)
    return merged


def save_settings(new_settings, check=None):
    """Import : chaque section presente dans `new_settings` REMPLACE la
    section enregistree ; les sections absentes sont conservees (un fichier
    partiel, par exemple seulement `global`, n'efface pas les sources).
    Avant, les sections etaient fusionnees : des cles absentes du fichier
    importe restaient.

    `check(merged, current)` controle le resultat avant ecriture, sous le meme
    verrou (une exception annule l'import et remonte) : la fusion n'est faite
    qu'une fois, sur les reglages qui seront ecrits.

    Retourne (succes, message).
    """
    with _lock:
        try:
            current = load_settings()
            merged = merge_import(current, new_settings)
        except Exception as e:
            logging.error(f"Import des paramètres impossible: {e}")
            return False, f"Erreur lors de la sauvegarde des paramètres: {str(e)}"
        if check:
            check(merged, current)   # refus (collision...) : remonte, rien n'est ecrit
        try:
            _commit(merged)
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

    Le dict modifie est ecrit tel quel : avant, il etait refusionne avec les
    reglages relus, et une cle supprimee par le mutateur reapparaissait.
    """
    with _lock:
        settings = load_settings()
        result = mutator(settings)
        try:
            _commit(settings)
        except Exception as e:
            logging.error(f"Sauvegarde des paramètres impossible: {e}")
            raise SettingsSaveError(f"Erreur lors de la sauvegarde des paramètres: {e}")
        return result


# Sentinelle : un mutator qui retourne NO_CHANGE indique "rien a sauvegarder".
NO_CHANGE = object()


def atomic_update(mutator):
    """Applique une modification de settings de facon ATOMIQUE (threads
    d'arriere-plan : sons entendus, volume auto).

    `mutator(settings)` mute les settings courants en place (ou retourne un
    nouveau dict) ; lecture et ecriture sont faites sous un seul verrou. Il
    peut retourner `NO_CHANGE` pour eviter une reecriture inutile.
    Retourne (success, message).
    """
    with _lock:
        try:
            settings = load_settings()
            if _degraded:
                return False, "réglages illisibles : écriture automatique suspendue"
            result = mutator(settings)
            if result is NO_CHANGE:
                return True, "Aucun changement"
            _commit(settings if result is None else result)
            return True, "Paramètres sauvegardés avec succès"
        except Exception as e:
            logging.error(f"atomic_update a echoue: {e}")
            return False, str(e)


# --- Validation ----------------------------------------------------------------

COUNT_MODES = ('attaques', 'tenus', 'alerte')
# Mode « alerte » : classe du capteur dans HA (icone, libelles « Détecté »).
ALERT_DEVICE_CLASSES = ('sound', 'smoke', 'safety', 'problem', 'occupancy')

# Modeles de groupes (ajout d'un groupe) : sons YAMNet deja coches, seuil et
# comptage adaptes. Gestes : capteurs « N claps » ; alarmes : mode alerte,
# capteur allume tant que le son dure (`confirm` : reconnaissances sur les 4
# dernieres analyses, ~2 s, pour l'allumer).
GROUP_PRESETS = {
    'claps': {'name': 'Applaudissements', 'sounds': ['Clapping', 'Hands', 'Applause'],
              'threshold': 0.5, 'ha_entities': [1, 2], 'count_mode': 'attaques'},
    'doigts': {'name': 'Claquement de doigts', 'sounds': ['Finger snapping'],
               'threshold': 0.4, 'ha_entities': [1, 2], 'count_mode': 'attaques'},
    'toc': {'name': 'Toc-toc', 'sounds': ['Knock', 'Tap'],
            'threshold': 0.4, 'ha_entities': [2, 3], 'count_mode': 'attaques'},
    'sifflement': {'name': 'Sifflement', 'sounds': ['Whistling', 'Whistle'],
                   'threshold': 0.4, 'ha_entities': [1], 'count_mode': 'tenus'},
    'fumee': {'name': 'Détecteur de fumée', 'sounds': ['Smoke detector, smoke alarm', 'Fire alarm'],
              'threshold': 0.3, 'count_mode': 'alerte', 'alert_confirm': 3, 'device_class': 'smoke'},
    'verre': {'name': 'Bris de glace', 'sounds': ['Glass', 'Shatter'],
              'threshold': 0.4, 'count_mode': 'alerte', 'alert_confirm': 1, 'device_class': 'safety'},
    'bebe': {'name': 'Pleurs de bébé', 'sounds': ['Baby cry, infant cry', 'Crying, sobbing'],
             'threshold': 0.4, 'count_mode': 'alerte', 'alert_confirm': 3, 'device_class': 'sound'},
    'aboiement': {'name': 'Aboiement', 'sounds': ['Bark', 'Dog'],
                  'threshold': 0.4, 'count_mode': 'alerte', 'alert_confirm': 2, 'device_class': 'sound'},
    'sonnette': {'name': 'Sonnette', 'sounds': ['Doorbell', 'Ding-dong'],
                 'threshold': 0.4, 'count_mode': 'alerte', 'alert_confirm': 1, 'device_class': 'sound'},
    'alarme': {'name': 'Alarme ou sirène', 'sounds': ['Alarm', 'Siren', 'Car alarm', 'Civil defense siren'],
               'threshold': 0.4, 'count_mode': 'alerte', 'alert_confirm': 3, 'device_class': 'safety'},
}
# Bornes d'un import (memes valeurs que les routes)
MAX_SOURCES = 32           # par type
MAX_GROUPS = 20            # par source
MAX_LABELS = 600           # sons d'une liste (YAMNet en connait 521)
MAX_LABEL = 200            # longueur d'un nom de son


def to_count_mode(value, path='count_mode'):
    """Comptage d'un groupe : « attaques » (claps), « tenus » (sifflements) ou
    « alerte » (capteur allume tant que le son dure : alarme, pleurs)."""
    if value not in COUNT_MODES:
        raise ValueError(f"{path} : « attaques », « tenus » ou « alerte » attendu")
    return value


def to_device_class(value, path='device_class'):
    if value not in ALERT_DEVICE_CLASSES:
        raise ValueError(f"{path} : {', '.join(ALERT_DEVICE_CLASSES)} attendu")
    return value


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


def normalize_rtsp_url(url, path='URL RTSP'):
    """URL RTSP stockee : texte, prefixe rtsp:// ajoute si absent, aucun autre
    protocole accepte (ffmpeg ouvrirait sinon file://, http://...)."""
    if not isinstance(url, str):
        raise ValueError(f"{path} : texte attendu")
    url = url.strip()
    if not url:
        return ''
    if '://' in url:
        if not url.lower().startswith(('rtsp://', 'rtsps://')):
            raise ValueError(f"{path} : seul le protocole rtsp:// est autorisé")
        return url
    return 'rtsp://' + url


def to_ip(value, path='ip'):
    """Adresse IPv4/IPv6 (source VBAN)."""
    import ipaddress
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} : adresse attendue")
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        raise ValueError(f"{path} : adresse IP invalide")


def _norm_groups(src, path):
    groups = src.get('sound_groups')
    if groups is None:
        return
    if not isinstance(groups, list):
        raise ValueError(f"{path}.sound_groups : liste attendue")
    if len(groups) > MAX_GROUPS:
        raise ValueError(f"{path}.sound_groups : {MAX_GROUPS} groupes au plus")
    slugs = set()
    for i, g in enumerate(groups):
        gp = f"{path}.sound_groups[{i}]"
        if not isinstance(g, dict):
            raise ValueError(f"{gp} : objet attendu")
        name = g.get('name')
        if name is not None and (not isinstance(name, str) or len(name) > MAX_NAME):
            raise ValueError(f"{gp}.name : texte de {MAX_NAME} caractères au plus attendu")
        # Slug : identifiant MQTT et entity_id, donc ASCII [a-z0-9_] et unique
        # dans la source (/, +, #, accents ou doublons cassaient les topics).
        # Un export ancien peut contenir des slugs accentues : on les convertit
        # plutot que de refuser l'import.
        slug = g.get('slug')
        if slug is not None and not isinstance(slug, str):
            raise ValueError(f"{gp}.slug : texte attendu")
        if not slug or not _VALID_SLUG.match(slug):
            slug = ascii_slug(slug) or ascii_slug(name) or f'group{i + 1}'
        base, n = slug, 2
        while slug in slugs:
            slug, n = f"{base}{n}", n + 1
        slugs.add(slug)
        g['slug'] = slug
        if 'threshold' in g:
            g['threshold'] = to_number(g['threshold'], f"{gp}.threshold", 0, 1)
        if 'clap_counts' in g:
            # Lu en priorite par clap_counts_of : non valide, un import avec
            # "clap_counts": 3 faisait planter la detection a chaque demarrage.
            g['ha_entities'] = to_clap_counts(g.pop('clap_counts'), f"{gp}.clap_counts")
        if 'ha_entities' in g:
            g['ha_entities'] = to_clap_counts(g['ha_entities'], f"{gp}.ha_entities")
        for flag in ('auto_add_sounds', 'enabled'):
            if flag in g:
                g[flag] = to_bool(g[flag], f"{gp}.{flag}")
        if 'count_mode' in g:
            g['count_mode'] = to_count_mode(g['count_mode'], f"{gp}.count_mode")
        if 'alert_confirm' in g:
            g['alert_confirm'] = to_number(g['alert_confirm'], f"{gp}.alert_confirm", 1, 4, integer=True)
        if 'device_class' in g:
            g['device_class'] = to_device_class(g['device_class'], f"{gp}.device_class")
        # Identifiant des entity_id (suit le nom du groupe ; le slug, lui,
        # porte les unique_id et ne change jamais) et renommage HA en attente.
        if 'entity_slug' in g and (not isinstance(g['entity_slug'], str) or not _VALID_SLUG.match(g['entity_slug'])):
            g.pop('entity_slug')
        if '_renamed_from' in g:
            prev = g['_renamed_from'].split() if isinstance(g['_renamed_from'], str) else []
            prev = [p for p in prev if _VALID_SLUG.match(p)]
            if prev:
                g['_renamed_from'] = ' '.join(prev)
            else:
                g.pop('_renamed_from')
        wl = g.get('sound_whitelist')
        if wl is not None:
            if not isinstance(wl, dict):
                raise ValueError(f"{gp}.sound_whitelist : objet attendu")
            if len(wl) > MAX_LABELS or any(len(str(k)) > MAX_LABEL for k in wl):
                raise ValueError(f"{gp}.sound_whitelist : {MAX_LABELS} sons au plus, noms de "
                                 f"{MAX_LABEL} caractères au plus")
            g['sound_whitelist'] = {str(k): to_bool(v, f"{gp}.sound_whitelist.{k}") for k, v in wl.items()}


def _norm_source(src, path):
    if not isinstance(src, dict):
        raise ValueError(f"{path} : objet attendu")
    for key in ('enabled', 'auto_start', 'auto_volume', 'configured', 'rtsp_tcp'):
        if key in src:
            src[key] = to_bool(src[key], f"{path}.{key}")
    if 'threshold' in src:
        src['threshold'] = to_number(src['threshold'], f"{path}.threshold", 0, 1)
    if 'gain' in src:
        src['gain'] = to_number(src['gain'], f"{path}.gain", 0, 100)
    if 'volume' in src:
        src['volume'] = to_number(src['volume'], f"{path}.volume", 0, 150, integer=True)
    if 'noise_reduction' in src:
        src['noise_reduction'] = to_number(src['noise_reduction'], f"{path}.noise_reduction", 0, MAX_NR_LEVEL, integer=True)
    if 'device_index' in src:
        src['device_index'] = to_number(src['device_index'], f"{path}.device_index", 0, 10000, integer=True)
    if 'port' in src:
        src['port'] = to_number(src['port'], f"{path}.port", 1, 65535, integer=True)
    for key in ('name', 'audio_source', 'pulse_name', 'stream_name'):
        if key in src and src[key] is not None and (not isinstance(src[key], str) or len(src[key]) > 200):
            raise ValueError(f"{path}.{key} : texte de 200 caractères au plus attendu")
    if 'ha_entities' in src:
        src['ha_entities'] = to_clap_counts(src['ha_entities'], f"{path}.ha_entities")
    if 'webhook_url' in src:
        src['webhook_url'] = to_webhook(src['webhook_url'], f"{path}.webhook_url")
        if src['webhook_url'].endswith('/…'):
            raise ValueError(f"{path}.webhook_url : adresse masquée (export « sans secrets »)")
    wl = src.get('sound_whitelist')
    if wl is not None:
        if not isinstance(wl, dict):
            raise ValueError(f"{path}.sound_whitelist : objet attendu")
        # Ancien champ de source, converti en groupe : memes bornes.
        if len(wl) > MAX_LABELS or any(len(str(k)) > MAX_LABEL for k in wl):
            raise ValueError(f"{path}.sound_whitelist : {MAX_LABELS} sons au plus, noms de "
                             f"{MAX_LABEL} caractères au plus")
        src['sound_whitelist'] = {str(k): to_bool(v, f"{path}.sound_whitelist.{k}") for k, v in wl.items()}
    _norm_groups(src, path)


# Bornes des reglages globaux (import et onglet Reglages avances).
GLOBAL_LIMITS = {'threshold': (0, 1), 'delay': (0.1, 10), 'peak_cooldown': (0, 2), 'peak_ratio': (1, 50),
                 'room_window': (0.3, 3)}
MAX_NAME = 80   # nom affiche d'une source ou d'un groupe (API)
_SOURCE_ID = re.compile(r'[^/\s]{1,64}')   # segment d'URL des routes (uuid, anciens « vban_<ip>_<port> »)


def rtsp_url_key(url):
    """URL comparee sans tenir compte de la casse du schema (RTSP:// = rtsp://) :
    une camera par adresse, a l'ajout, a la modification et a l'import."""
    scheme, sep, rest = str(url or '').partition('://')
    return f"{scheme.lower()}{sep}{rest}" if sep else str(url or '')


MAX_ROOM_ZONES = 10


def to_room_zones(value, path='zones'):
    """Groupes de sources de l'arbitrage entre pieces : [{name, sources:
    [source_id]}]. Seules les sources d'un meme groupe se departagent ; une
    source n'est que dans un groupe. Liste vide : toutes ensemble."""
    if not isinstance(value, list):
        raise ValueError(f"{path} : liste attendue")
    if len(value) > MAX_ROOM_ZONES:
        raise ValueError(f"{path} : {MAX_ROOM_ZONES} groupes au plus")
    out, seen, names = [], {}, set()
    for i, z in enumerate(value):
        zp = f"{path}[{i}]"
        if not isinstance(z, dict):
            raise ValueError(f"{zp} : objet attendu")
        name = to_name(z.get('name'), f"{zp}.name", default=f"Groupe {i + 1}")
        if len(name) > 40:
            raise ValueError(f"{zp}.name : 40 caractères au plus")
        if name.casefold() in names:
            raise ValueError(f"{zp}.name : « {name} » est déjà utilisé")
        names.add(name.casefold())
        sources = z.get('sources', [])
        if not isinstance(sources, list) or not all(isinstance(s, str) and 0 < len(s) <= 80 for s in sources):
            raise ValueError(f"{zp}.sources : liste d'identifiants de source attendue")
        for sid in sources:
            if sid in seen and seen[sid] != name:
                raise ValueError(f"{zp}.sources : la source {sid} est déjà dans « {seen[sid]} »")
            seen[sid] = name
        out.append({'name': name, 'sources': list(dict.fromkeys(sources))})
    return out


def prune_room_zones(settings):
    """Retire des groupes de sources les sources qui n'existent plus
    (supprimees, ou importees sous un autre identifiant) : un micro supprime
    puis rajoute ne retrouve pas en silence son ancien groupe."""
    g = settings.get('global')
    zones = g.get('room_zones') if isinstance(g, dict) else None
    if not zones:
        return
    mic = settings.get('microphone')
    ids = {'mic'} if isinstance(mic, dict) and mic.get('configured', True) is not False else set()
    ids |= {f"rtsp_{s.get('id')}" for s in settings.get('rtsp_sources') or [] if isinstance(s, dict) and s.get('id')}
    ids |= {f"vban_{s.get('id') or s.get('ip', '')}" for s in settings.get('saved_vban_sources') or [] if isinstance(s, dict)}
    for z in zones:
        if isinstance(z, dict) and isinstance(z.get('sources'), list):
            z['sources'] = [sid for sid in z['sources'] if sid in ids]


def to_name(value, path='Nom', default=None):
    """Nom affiche : texte de 1 a MAX_NAME caracteres (vide = `default`)."""
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{path} : texte attendu")
    name = (value or '').strip()
    if not name and default:
        return default
    if not name or len(name) > MAX_NAME:
        raise ValueError(f"{path} : texte de 1 à {MAX_NAME} caractères attendu")
    return name


def normalize_settings(data):
    """Valide et normalise (en place) un dict de settings importe ou envoye
    par l'UI. Convertit les formes courantes ("0.5", "true") et leve
    ValueError avec le chemin du champ fautif sinon.
    """
    if not isinstance(data, dict):
        raise ValueError("Format invalide : objet JSON attendu")
    # Sections connues : type exact exige (null compris), sinon l'add-on ne
    # redemarrait plus apres un import.
    for key, kind, label in (('global', dict, 'objet'), ('microphone', dict, 'objet'),
                             ('rtsp_sources', list, 'liste'), ('saved_vban_sources', list, 'liste')):
        if key in data and not isinstance(data[key], kind):
            raise ValueError(f"{key} : {label} attendu")
    g = data.get('global')
    if g is not None:
        # « Fin d'un pic » (6.36 et avant) : remplace par la detection d'attaque.
        g.pop('peak_reset', None)
        for key, (lo, hi) in GLOBAL_LIMITS.items():
            if key in g:
                g[key] = to_number(g[key], f"global.{key}", lo, hi)
        for flag in ('debug', 'room_arbitration', 'clap_shape_check'):
            if flag in g:
                g[flag] = to_bool(g[flag], f'global.{flag}')
        if 'room_zones' in g:
            g['room_zones'] = to_room_zones(g['room_zones'], 'global.room_zones')
        if 'sound_exclusions' in g:
            if not isinstance(g['sound_exclusions'], list):
                raise ValueError("global.sound_exclusions : liste attendue")
            # Anciennes sauvegardes : valeurs converties en texte, vides retirees.
            excl = [str(x).strip() for x in g['sound_exclusions'] if x is not None and str(x).strip()]
            if len(excl) > MAX_LABELS:
                raise ValueError(f"global.sound_exclusions : {MAX_LABELS} sons au plus")
            if any(len(x) > MAX_LABEL for x in excl):
                raise ValueError(f"global.sound_exclusions : noms de son de {MAX_LABEL} caractères au plus")
            g['sound_exclusions'] = excl
    mic = data.get('microphone')
    if mic is not None:
        _norm_source(mic, 'microphone')
    legacy = data.get('vban')
    if legacy is not None:
        # Ancienne section, sans effet : on la valide sommairement puis on
        # l'ignore pour ne pas reimporter des valeurs corrompues.
        if not isinstance(legacy, dict):
            raise ValueError("vban : objet attendu")
        data.pop('vban')
    import uuid
    for key in ('rtsp_sources', 'saved_vban_sources'):
        lst = data.get(key)
        if lst is None:
            continue
        if len(lst) > MAX_SOURCES:
            raise ValueError(f"{key} : {MAX_SOURCES} sources au plus")
        ids, entity_keys, streams, urls = set(), set(), set(), set()
        for i, src in enumerate(lst):
            _norm_source(src, f"{key}[{i}]")
            # Memes bornes que l'API : un nom importe plus long ne pouvait plus
            # etre modifie depuis l'interface.
            if isinstance(src.get('name'), str) and len(src['name'].strip()) > MAX_NAME:
                raise ValueError(f"{key}[{i}].name : {MAX_NAME} caractères au plus")
            if key == 'rtsp_sources':
                # Meme controle que l'API : « http://… » devenait « rtsp://http://… »
                src['url'] = normalize_rtsp_url(src.get('url', ''), f"{key}[{i}].url")
                if '***@' in src['url'] or re.search(r'=\*\*\*(&|$)', src['url']):
                    raise ValueError(f"{key}[{i}].url : identifiants masqués (export « sans secrets »)")
                # Une camera par adresse, comme a l'ajout (modifier l'une des
                # deux renvoyait ensuite 409).
                url_key = rtsp_url_key(src['url'])
                if url_key in urls:
                    raise ValueError(f"{key}[{i}].url : caméra en double (même adresse)")
                urls.add(url_key)
            else:
                src['ip'] = to_ip(src.get('ip'), f"{key}[{i}].ip")
                stream = (src.get('stream_name') or src.get('name') or '').strip()
                if (src['ip'], stream) in streams:
                    # Deux sources sur le meme flux se partagent les paquets.
                    raise ValueError(f"{key}[{i}] : flux VBAN {src['ip']}/{stream} en double")
                streams.add((src['ip'], stream))
                # Cle d'entite : meme forme que les entity_id (vban_<slug>),
                # sinon l'entity_key de l'evenement differait des entity_id.
                ek = src.get('entity_key')
                if ek is not None:
                    if not isinstance(ek, str):
                        raise ValueError(f"{key}[{i}].entity_key : texte attendu")
                    ek = ascii_slug(ek)
                    src['entity_key'] = ek if ek.startswith('vban_') else f"vban_{ek}" if ek else None
                    if not src['entity_key']:
                        src.pop('entity_key')
                # Cle d'entite absente ou en double : recalculee au chargement.
                ek = src.get('entity_key')
                if not ek or ek in entity_keys:
                    src.pop('entity_key', None)
                else:
                    entity_keys.add(ek)
            # Id absent, en double (les routes prenaient la premiere source
            # trouvee) ou hors du format des routes (« / » : source impossible
            # a supprimer depuis l'interface) : nouvel id.
            if (not isinstance(src.get('id'), str) or not _SOURCE_ID.fullmatch(src['id'])
                    or src['id'] in ids):
                src['id'] = str(uuid.uuid4())
            ids.add(src['id'])
    return data
