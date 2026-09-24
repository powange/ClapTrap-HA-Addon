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

    legacy_whitelist = source.get('sound_whitelist') or {
        "Clapping": True, "Hands": True, "Applause": True
    }
    source['sound_groups'] = [{
        'name': 'Clap',
        'slug': 'clap',
        'sound_whitelist': dict(legacy_whitelist),
        'threshold': _safe_float(source.get('threshold'), default_threshold),
        'ha_entities': list(source.get('ha_entities', [1, 2]) or []),
    }]


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
    for key in ('clap_counts', 'ha_entities'):
        if key in group and group[key] is not None:
            return [n for n in group[key] if isinstance(n, int) and 1 <= n <= 4]
    return list(fallback)
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
    global _cache, _cache_time

    with _lock:
        now = time.time()
        if _cache is not None and (now - _cache_time) < _CACHE_TTL:
            return copy.deepcopy(_cache)

        saved, any_file = _read_saved()
        mic_migrated = False
        if saved is not None:
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
                logging.error("settings.json et sa sauvegarde sont illisibles : valeurs par défaut en mémoire")
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
        _cache = merged
        _cache_time = now
        return copy.deepcopy(_cache)


def _commit(settings):
    """Ecrit `settings` tel quel (sous _lock) et met le cache a jour."""
    global _cache, _cache_time
    _write_atomic(settings)
    _cache = copy.deepcopy(settings)
    _cache_time = time.time()


def save_settings(new_settings):
    """Import : chaque section presente dans `new_settings` REMPLACE la
    section enregistree ; les sections absentes sont conservees (un fichier
    partiel, par exemple seulement `global`, n'efface pas les sources).
    Avant, les sections etaient fusionnees : des cles absentes du fichier
    importe restaient.

    Retourne (succes, message).
    """
    with _lock:
        try:
            current = load_settings()
            new_settings = copy.deepcopy(dict(new_settings))
            _migrate_mic_configured(new_settings.get('microphone'))
            for key, value in new_settings.items():
                current[key] = value
            merged = _deep_merge(DEFAULT_SETTINGS, current)
            _apply_group_migrations(merged)
            _ensure_vban_ids(merged)
            _strip_legacy_fields(merged)
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
            result = mutator(settings)
            if result is NO_CHANGE:
                return True, "Aucun changement"
            _commit(settings if result is None else result)
            return True, "Paramètres sauvegardés avec succès"
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
    slugs = set()
    for i, g in enumerate(groups):
        gp = f"{path}.sound_groups[{i}]"
        if not isinstance(g, dict):
            raise ValueError(f"{gp} : objet attendu")
        name = g.get('name')
        if name is not None and (not isinstance(name, str) or len(name) > 80):
            raise ValueError(f"{gp}.name : texte de 80 caractères au plus attendu")
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
    if 'device_index' in src:
        src['device_index'] = to_number(src['device_index'], f"{path}.device_index", 0, 10000, integer=True)
    if 'port' in src:
        src['port'] = to_number(src['port'], f"{path}.port", 1, 65535, integer=True)
    for key in ('name', 'audio_source', 'pulse_name', 'stream_name'):
        if key in src and src[key] is not None and not isinstance(src[key], str):
            raise ValueError(f"{path}.{key} : texte attendu")
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
                  'peak_ratio': (1, 50)}
        # « Fin d'un pic » (6.36 et avant) : remplace par la detection d'attaque.
        g.pop('peak_reset', None)
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
        if not isinstance(lst, list):
            raise ValueError(f"{key} : liste attendue")
        ids, entity_keys = set(), set()
        for i, src in enumerate(lst):
            _norm_source(src, f"{key}[{i}]")
            if key == 'rtsp_sources':
                # Meme controle que l'API : « http://… » devenait « rtsp://http://… »
                src['url'] = normalize_rtsp_url(src.get('url', ''), f"{key}[{i}].url")
            else:
                src['ip'] = to_ip(src.get('ip'), f"{key}[{i}].ip")
                # Cle d'entite en double : recalculee au chargement.
                if src.get('entity_key') in entity_keys:
                    src.pop('entity_key')
                entity_keys.add(src.get('entity_key'))
            # Id absent ou en double (les routes prenaient la premiere source
            # trouvee) : nouvel id.
            if not isinstance(src.get('id'), str) or not src['id'] or src['id'] in ids:
                src['id'] = str(uuid.uuid4())
            ids.add(src['id'])
    return data
