from flask import Blueprint, jsonify, request
from werkzeug.exceptions import HTTPException
import logging
import threading
import uuid

from settings_manager import (load_settings, modify_settings, SettingsSaveError,
                              to_bool, to_number, to_clap_counts, to_webhook)
from audio_utils import get_audio_input_devices
from vban_manager import get_vban_detector

sources_bp = Blueprint('sources', __name__)
_socketio = None
_restart_lock = threading.Lock()


class ApiError(Exception):
    """Erreur de requete : levee dans un mutator, rien n'est enregistre."""

    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra


def api_error_response(e):
    """Handlers d'erreurs partages par les blueprints qui modifient les settings."""
    if isinstance(e, HTTPException):
        return e
    if isinstance(e, ApiError):
        return jsonify({'success': False, 'error': e.message, **e.extra}), e.status
    if isinstance(e, ValueError):
        return jsonify({'success': False, 'error': str(e)}), 400
    if isinstance(e, SettingsSaveError):
        logging.error(f"Sauvegarde des réglages impossible: {e}")
        return jsonify({'success': False, 'error': f"Réglages non enregistrés : {e}"}), 500
    logging.exception("Erreur dans une route")
    return jsonify({'success': False, 'error': str(e)}), 500


sources_bp.register_error_handler(Exception, api_error_response)


def _json():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError('Corps JSON attendu')
    return data


def _source_id_for(kind, source_key=None):
    """Donne l'ID runtime (utilise par classify) a partir de (kind, source_key).

    Doit coller EXACTEMENT a ce que classify.run_*_source construit :
    - mic  : "mic"
    - rtsp : f"rtsp_{src['id']}"  (jamais l'URL : elle peut contenir des identifiants)
    - vban : f"vban_{id}"
    """
    if kind == 'mic':
        return 'mic'
    if kind == 'rtsp':
        return f"rtsp_{source_key}" if source_key else None
    if kind == 'vban':
        return f"vban_{source_key}"
    return None


def init_sources(socketio):
    global _socketio
    _socketio = socketio


def _sync_ha_entities():
    """Aligne les entites HA sur la configuration courante (ajout, suppression,
    groupes, source activee / desactivee), que la detection tourne ou non.
    Toujours a partir des reglages relus, sous le verrou de ha_entities :
    publier la copie d'une requete pouvait retirer ce qu'une autre venait
    d'ajouter."""
    try:
        from ha_entities import sync_sources
        sync_sources()
    except Exception as e:
        logging.warning(f"Entités HA non synchronisées: {e}")


def _check_entity_collision(settings):
    """Refuse une modification dont les entites HA en ecraseraient d'autres."""
    from ha_entities import find_entity_collision
    clash = find_entity_collision(settings)
    if clash:
        raise ApiError(f"« {clash} » produirait les mêmes entités Home Assistant qu'une autre source : "
                       "choisissez un autre nom", 409)


def _apply_live():
    """Applique a la detection en cours les reglages enregistres (groupes,
    sons, webhook, gain, exclusions, reglages avances) : une seule entree au
    lieu de six fonctions de mise a jour en direct. Les sources dont la
    capture n'a pas change ne sont pas relancees."""
    try:
        from classify import apply_settings_if_running
        with _restart_lock:
            apply_settings_if_running()
    except Exception as e:
        logging.warning(f"Réglages non appliqués en direct: {e}")


def _set_live_gain(source_id, gain):
    """Gain lu en direct aussi par le test du son (detection arretee)."""
    try:
        from classify import update_source_gain
        update_source_gain(source_id, gain)
    except Exception as e:
        logging.warning(f"Gain non appliqué en direct: {e}")


def _after_change(restart):
    """Apres une modification de source : redemarrage de la detection (qui
    resynchronise les entites HA) ou simple synchronisation."""
    if restart:
        _restart_detection_if_running()
    else:
        _sync_ha_entities()


def _note_restart(result):
    """Resultat du redemarrage pour la reponse de la requete en cours
    (`restart` : ok | stopped | failed) : l'interface l'annonce d'apres le
    serveur au lieu de le supposer."""
    try:
        from flask import g, has_request_context
        if has_request_context():
            g.restart = result
    except Exception:
        pass


def add_restart_to_response(response):
    """after_request des blueprints : ajoute `restart` aux reponses JSON."""
    from flask import g
    result = g.pop('restart', None)
    if result and response.is_json and response.status_code < 400:
        data = response.get_json(silent=True)
        if isinstance(data, dict):
            data['restart'] = result
            response.set_data(__import__('json').dumps(data))
    return response


sources_bp.after_request(add_restart_to_response)


def _restart_detection_if_running():
    """Applique les reglages a la detection en cours : seules les sources
    ajoutees, retirees ou modifiees (adresse, micro, flux) sont relancees ; les
    autres gardent leur classifieur (tout etait recree a chaque modification).
    Le nom est historique."""
    _sync_ha_entities()
    with _restart_lock:
        try:
            from classify import apply_settings_if_running, build_sources_from_settings
            settings = load_settings()
            changed = apply_settings_if_running(settings)
            if changed is None:
                return  # detection arretee
            if not build_sources_from_settings(settings):
                # Plus aucune source : la session s'arrete d'elle-meme.
                logging.info("Détection arrêtée: aucune source active")
                _note_restart('stopped')
            elif changed:
                if _socketio:
                    _socketio.emit('detection_status', {'status': 'running'})
                logging.info("Détection mise à jour (sources modifiées relancées)")
                _note_restart('ok')
        except Exception as e:
            logging.error(f"Erreur de mise à jour de la détection: {e}")
            _note_restart('failed')


def _persist_pulse_name(pulse_name):
    def _mut(settings):
        settings.setdefault('microphone', {})['pulse_name'] = pulse_name
    try:
        modify_settings(_mut)
    except Exception as e:
        logging.warning(f"pulse_name non enregistré: {e}")


def _resolve_pulse_name(settings):
    """Résout le pulse_name depuis les settings ou l'API Supervisor.
    Vérifie toujours que le pulse_name correspond à l'audio_source actuel.
    Les corrections sont enregistrees de facon atomique (modify_settings)."""
    mic = settings.get('microphone', {})
    audio_source = mic.get('audio_source', '')
    if not audio_source or audio_source == 'default':
        # Pas de source spécifique, effacer le pulse_name stale
        if mic.get('pulse_name'):
            _persist_pulse_name('')
        return ''

    # Vérifier si le pulse_name en cache correspond au device actuel
    cached_pulse_name = mic.get('pulse_name', '')
    devices = get_audio_input_devices()
    if cached_pulse_name:
        for dev in devices:
            if dev.get('name') == audio_source and dev.get('pulse_name') == cached_pulse_name:
                return cached_pulse_name
        logging.warning(f"pulse_name '{cached_pulse_name}' ne correspond plus à '{audio_source}', re-résolution...")

    for dev in devices:
        if dev.get('name') == audio_source and dev.get('pulse_name'):
            _persist_pulse_name(dev['pulse_name'])
            logging.info(f"pulse_name résolu et sauvegardé: {dev['pulse_name']}")
            return dev['pulse_name']

    logging.warning(f"Impossible de résoudre pulse_name pour '{audio_source}'")
    return ''


def _source_name(value, default=None):
    """Nom affiche d'une source : texte de 1 a 80 caracteres (vide = defaut)."""
    name = str(value if value is not None else '').strip()
    if not name and default:
        return default
    if not name or len(name) > 80:
        raise ApiError('Nom : texte de 1 à 80 caractères attendu')
    return name


def _normalize_rtsp_url(url):
    from settings_manager import normalize_rtsp_url
    return normalize_rtsp_url(url)


def _find_rtsp(settings, stream_id):
    for s in settings.get('rtsp_sources', []):
        if s.get('id') == stream_id:
            return s
    raise ApiError('Stream non trouvé', 404)


def _find_vban_by_id(settings, vban_id):
    for s in settings.get('saved_vban_sources', []) or []:
        if s.get('id') == vban_id:
            return s
    raise ApiError('Source non trouvée', 404)


@sources_bp.route('/api/audio-sources', methods=['GET'])
def get_audio_sources():
    audio_sources = [
        {**device, 'type': 'microphone'}
        for device in get_audio_input_devices()
    ]
    return jsonify(audio_sources)


# --- RTSP --------------------------------------------------------------------

@sources_bp.route('/api/rtsp/stream', methods=['POST'])
def add_rtsp_stream():
    data = _json()
    new_stream = {
        'id': str(uuid.uuid4()),
        'name': _source_name(data.get('name'), 'Caméra'),
        'url': _normalize_rtsp_url(data.get('url', '')),
        'webhook_url': to_webhook(data.get('webhook_url', '')),
        'enabled': to_bool(data.get('enabled', True), 'enabled'),
        'gain': to_number(data.get('gain', 10), 'gain', 0, 100, integer=True),
    }

    def _mut(settings):
        # Doublon : un assistant ferme pendant l'ajout faisait recommencer
        # l'utilisateur, et la camera existait deux fois.
        if new_stream['url'] and any(s.get('url') == new_stream['url'] for s in settings.get('rtsp_sources', [])):
            raise ApiError('Cette caméra (même URL) est déjà ajoutée', 409)
        settings.setdefault('rtsp_sources', []).append(new_stream)

    modify_settings(_mut)
    # Une source active avec URL doit etre ecoutee tout de suite ; une source
    # desactivee est publiee (indisponible) sans redemarrage.
    _after_change(new_stream['enabled'] and bool(new_stream['url']))
    return jsonify({'success': True, 'stream': new_stream})


def _update_rtsp(stream_id, data):
    state = {}

    def _mut(settings):
        stream = _find_rtsp(settings, stream_id)
        url_changed = enabled_changed = False
        if 'url' in data:
            new_url = _normalize_rtsp_url(data['url'])
            url_changed = new_url != stream.get('url')
            stream['url'] = new_url
        if 'name' in data:
            stream['name'] = _source_name(data['name'], 'Caméra')
        if 'webhook_url' in data:
            stream['webhook_url'] = to_webhook(data['webhook_url'])
        if 'enabled' in data:
            new_enabled = to_bool(data['enabled'], 'enabled')
            enabled_changed = new_enabled != stream.get('enabled')
            stream['enabled'] = new_enabled
        if 'gain' in data:
            stream['gain'] = to_number(data['gain'], 'gain', 0, 100, integer=True)
        # Redemarrage si la source est (des)activee, ou si l'URL d'une source
        # active change (ffmpeg doit se reconnecter a la nouvelle URL).
        state['restart'] = enabled_changed or (url_changed and stream.get('enabled', False))
        return dict(stream)

    stream = modify_settings(_mut)

    # Effets en direct, APRES l'enregistrement (un redemarrage relit le disque).
    if 'gain' in data:
        _set_live_gain(_source_id_for('rtsp', stream_id), stream['gain'])
    if 'webhook_url' in data or 'gain' in data:
        _apply_live()
    if state.get('restart') or 'name' in data:
        # Une seule synchronisation HA : la relance la fait deja.
        _after_change(state.get('restart'))
    return stream


@sources_bp.route('/api/rtsp/stream/<stream_id>', methods=['DELETE'])
def delete_rtsp_stream(stream_id):
    def _mut(settings):
        before = settings.get('rtsp_sources', [])
        settings['rtsp_sources'] = [s for s in before if s.get('id') != stream_id]
        if len(settings['rtsp_sources']) == len(before):
            raise ApiError('Stream non trouvé', 404)
        return next(s for s in before if s.get('id') == stream_id).get('enabled', False)

    # Source active : redemarrer, sinon ffmpeg continue sur le flux supprime.
    _after_change(modify_settings(_mut))
    return jsonify({'success': True})


# --- VBAN --------------------------------------------------------------------

@sources_bp.route('/api/vban/save', methods=['POST'])
def save_vban_source():
    source = _json()
    if not all(field in source for field in ('name', 'ip', 'port')):
        raise ApiError('Données manquantes pour la source VBAN')
    name = _source_name(source['name'])
    if not name:
        raise ApiError('Nom requis pour la source VBAN')
    from settings_manager import to_ip
    ip = to_ip(str(source['ip']))
    new_source = {
        'id': str(uuid.uuid4()),
        'name': name,
        'ip': ip,
        'port': to_number(source['port'], 'port', 1, 65535, integer=True),
        # Nom du flux VBAN tel qu'emis par l'emetteur (routage des paquets).
        'stream_name': str(source.get('stream_name') or name).strip(),
        'webhook_url': to_webhook(source.get('webhook_url', '')),
        'enabled': to_bool(source.get('enabled', True), 'enabled'),
    }

    def _mut(settings):
        lst = settings.setdefault('saved_vban_sources', [])
        from settings_manager import vban_entity_key
        new_source['entity_key'] = vban_entity_key(new_source, lst)
        if any(s.get('ip') == ip and s.get('name') == name for s in lst):
            raise ApiError('Cette source VBAN existe déjà')
        # Le listener route les paquets par (IP, nom du flux) : deux sources
        # sur le meme flux se voleraient les paquets.
        if any(s.get('ip') == ip and (s.get('stream_name') or s.get('name')) == new_source['stream_name'] for s in lst):
            raise ApiError('Ce flux VBAN (même IP, même nom de flux) est déjà ajouté')
        lst.append(new_source)
        _check_entity_collision(settings)

    modify_settings(_mut)
    _after_change(new_source['enabled'])
    return jsonify({'success': True, 'source': new_source})


@sources_bp.route('/api/vban/<vban_id>', methods=['DELETE'])
def remove_vban_source(vban_id):
    def _mut(settings):
        src = _find_vban_by_id(settings, vban_id)
        settings['saved_vban_sources'] = [s for s in settings['saved_vban_sources'] if s is not src]
        return bool(src.get('enabled'))

    # Source desactivee : rien a redemarrer (toutes les sources etaient
    # coupees quelques secondes pour rien).
    _after_change(modify_settings(_mut))
    return jsonify({'success': True})


def _update_vban(vban_id, source):
    state = {}

    def _mut(settings):
        s = _find_vban_by_id(settings, vban_id)
        restart = False
        if 'name' in source:
            s['name'] = _source_name(source['name'])
        if 'webhook_url' in source:
            s['webhook_url'] = to_webhook(source['webhook_url'])
        if 'enabled' in source:
            new_enabled = to_bool(source['enabled'], 'enabled')
            restart = new_enabled != s.get('enabled')
            s['enabled'] = new_enabled
        if 'gain' in source:
            s['gain'] = to_number(source['gain'], 'gain', 0, 100, integer=True)
        state['restart'] = restart
        return dict(s)

    s = modify_settings(_mut)
    if 'gain' in source:
        _set_live_gain(_source_id_for('vban', s['id']), s['gain'])
    if 'webhook_url' in source or 'gain' in source:
        _apply_live()
    if state.get('restart') or 'name' in source:
        _after_change(state.get('restart'))   # nom affiche des entites / relance
    return s


@sources_bp.route('/refresh_vban_sources')
def refresh_vban_sources():
    detector = get_vban_detector()
    if not detector:
        return jsonify({"sources": []})
    return jsonify({"sources": detector.get_sources(timeout=1.0)})


# --- Exclusions globales -----------------------------------------------------

@sources_bp.route('/api/sound_exclusions', methods=['GET'])
def get_sound_exclusions():
    """Retourne les exclusions globales et la liste des labels connus."""
    settings = load_settings()
    excluded = list(settings.get('global', {}).get('sound_exclusions', []) or [])
    try:
        from classify import get_all_known_labels
        known = get_all_known_labels()
    except Exception:
        known = []
    # Les exclus ne doivent pas reapparaitre dans les "autres sons"
    available = sorted([l for l in known if l not in excluded])
    return jsonify({'excluded': sorted(excluded), 'available': available})


@sources_bp.route('/api/sound_exclusions', methods=['PUT'])
def update_sound_exclusions():
    """Ajoute ou retire un label des exclusions globales.

    Body: {label: str, excluded: bool}
    """
    data = _json()
    label = str(data.get('label') or '').strip()
    excluded = to_bool(data.get('excluded', False), 'excluded')
    if not label:
        raise ApiError('label requis')

    def _mut(settings):
        g = settings.setdefault('global', {})
        lst = list(g.get('sound_exclusions', []) or [])
        if excluded and label not in lst:
            lst.append(label)
        elif not excluded and label in lst:
            lst.remove(label)
        g['sound_exclusions'] = lst
        return lst

    lst = modify_settings(_mut)
    _apply_live()
    return jsonify({'success': True, 'excluded': sorted(lst)})


# --- Sons et groupes d'une source --------------------------------------------

def _find_source_dict(settings, kind, source_key):
    if kind == 'mic':
        return settings.setdefault('microphone', {})
    if kind == 'rtsp':
        for s in settings.get('rtsp_sources', []):
            if s.get('id') == source_key:
                return s
    if kind == 'vban':
        for s in settings.get('saved_vban_sources', []):
            if s.get('id') == source_key:
                return s
    return None


def _require_source(settings, kind, source_key):
    src = _find_source_dict(settings, kind, source_key)
    if src is None:
        raise ApiError('source introuvable', 404)
    return src


def _require_kind(data, *fields):
    kind = data.get('kind')
    if kind not in ('mic', 'rtsp', 'vban') or any(not data.get(f) for f in fields):
        raise ApiError(' / '.join(('kind',) + fields) + ' requis')
    return kind


def _default_group(groups):
    """Groupe par defaut d'une source : le premier (historiquement "clap").

    Il reste le groupe par defaut meme renomme (son slug suit alors le nom) :
    avant, un renommage suivi d'un PUT sans group_slug recreait un groupe
    "clap" vide.
    """
    return next((g for g in groups if isinstance(g, dict)), None)


@sources_bp.route('/api/source/sound_whitelist/cleanup', methods=['POST'])
def cleanup_source_sound_whitelist():
    """Retire tous les labels non coches (value=false) de la whitelist."""
    data = _json()
    kind = _require_kind(data)
    source_key = data.get('source_key')
    group_slug = data.get('group_slug')
    try:
        # Sons entendus en attente d'enregistrement (par lots de 2 s) : les
        # ecrire avant, sinon ils reapparaissaient juste apres le nettoyage.
        from classify import _flush_sound_seen
        _flush_sound_seen()
    except Exception as e:
        logging.debug(f"Sons en attente non enregistrés: {e}")

    def _mut(settings):
        target = _require_source(settings, kind, source_key)
        groups = target.get('sound_groups', []) or []
        if group_slug:
            target_group = next((g for g in groups
                                 if isinstance(g, dict) and g.get('slug') == group_slug), None)
            if target_group is None:
                raise ApiError('groupe introuvable', 404)
            wl = target_group.get('sound_whitelist') or {}
            kept = {k: v for k, v in wl.items() if v}
            removed = len(wl) - len(kept)
            target_group['sound_whitelist'] = kept
        else:
            removed = 0
            for g in groups:
                if isinstance(g, dict):
                    g_wl = g.get('sound_whitelist') or {}
                    g['sound_whitelist'] = {k: v for k, v in g_wl.items() if v}
                    removed += len(g_wl) - len(g['sound_whitelist'])
        remaining = set()
        for g in groups:
            if isinstance(g, dict):
                remaining.update((g.get('sound_whitelist') or {}).keys())
        return removed, remaining, list(groups)

    removed, remaining_labels, groups = modify_settings(_mut)
    _apply_live()   # groupes et sons « deja vus » du detecteur actif

    return jsonify({'success': True, 'removed': removed, 'remaining': len(remaining_labels)})


@sources_bp.route('/api/source/sound_whitelist', methods=['PUT'])
def update_source_sound_whitelist():
    """Active/desactive un label dans un groupe de la source.

    Body: {kind, source_key, label, enabled, group_slug?}
    `group_slug` optionnel : si absent, cible le groupe par defaut (le premier).
    Refuse l'activation si le label est deja active dans un autre groupe
    de la meme source (regle d'exclusivite).
    """
    data = _json()
    kind = _require_kind(data, 'label')
    source_key = data.get('source_key')
    label = data.get('label')
    if not isinstance(label, str) or not label or len(label) > 200:
        raise ApiError('label : nom de son attendu')
    enabled = to_bool(data.get('enabled', False), 'enabled')

    def _mut(settings):
        source_dict = _require_source(settings, kind, source_key)
        groups = source_dict.setdefault('sound_groups', [])
        default = _default_group(groups)
        target_slug = data.get('group_slug') or (default.get('slug') if default else 'clap')
        target_group = next((g for g in groups if isinstance(g, dict) and g.get('slug') == target_slug), None)
        if target_group is None:
            if groups:
                raise ApiError('groupe introuvable', 404)
            target_group = {
                'slug': target_slug, 'name': target_slug.capitalize(),
                'sound_whitelist': {}, 'threshold': 0.5, 'ha_entities': [1, 2],
            }
            groups.append(target_group)
            default = target_group
        if enabled:
            for g in groups:
                if g is target_group or not isinstance(g, dict):
                    continue
                if (g.get('sound_whitelist') or {}).get(label) is True:
                    raise ApiError(
                        f'Le son "{label}" est deja active dans le groupe "{g.get("name", g.get("slug"))}"',
                        409, conflict_group=g.get('slug'))
        target_group.setdefault('sound_whitelist', {})[label] = enabled
        return target_slug

    target_slug = modify_settings(_mut)
    _apply_live()
    return jsonify({'success': True, 'group_slug': target_slug})


def _slugify_group(name, existing_slugs):
    """Genere un slug unique a partir du nom."""
    from settings_manager import ascii_slug
    base = ascii_slug(name) or 'groupe'
    slug = base
    i = 2
    while slug in existing_slugs:
        slug = f"{base}{i}"
        i += 1
    return slug


@sources_bp.route('/api/source/sound_groups', methods=['POST'])
def create_source_sound_group():
    """Cree un nouveau groupe pour une source.

    Body: {kind, source_key, name, threshold?, ha_entities?}
    """
    data = _json()
    kind = _require_kind(data, 'name')
    source_key = data.get('source_key')
    name = str(data.get('name') or '').strip()
    if not name:
        raise ApiError('kind / name requis')

    def _mut(settings):
        src = _require_source(settings, kind, source_key)
        groups = src.setdefault('sound_groups', [])
        existing_slugs = {g.get('slug') for g in groups if isinstance(g, dict)}
        # Le nouveau groupe propose tous les sons deja entendus sur la source
        # (non coches) : sinon il restait vide, un son deja "vu" ailleurs n'y
        # etant jamais ajoute.
        known = set()
        for g in groups:
            if isinstance(g, dict):
                known.update((g.get('sound_whitelist') or {}).keys())
        new_group = {
            'slug': _slugify_group(name, existing_slugs), 'name': name,
            'sound_whitelist': {label: False for label in sorted(known)},
            'threshold': to_number(data.get('threshold', 0.5), 'threshold', 0, 1),
            # [] = aucune entite (un `or` le remplacait par 1 et 2 claps)
            'ha_entities': to_clap_counts(data['ha_entities'] if data.get('ha_entities') is not None else [1, 2]),
        }
        groups.append(new_group)
        _check_entity_collision(settings)
        return new_group, dict(src)

    new_group, src = modify_settings(_mut)
    _sync_ha_entities()
    _apply_live()
    return jsonify({'success': True, 'group': new_group})


@sources_bp.route('/api/source/sound_groups', methods=['PUT'])
def update_source_sound_group():
    """Met a jour les meta-donnees d'un groupe.

    Body: {kind, source_key, group_slug, name?, threshold?, ha_entities?}
    """
    data = _json()
    kind = _require_kind(data, 'group_slug')
    source_key = data.get('source_key')
    slug = data.get('group_slug')

    def _mut(settings):
        src = _require_source(settings, kind, source_key)
        groups = src.get('sound_groups', []) or []
        target = next((g for g in groups if isinstance(g, dict) and g.get('slug') == slug), None)
        if target is None:
            raise ApiError('groupe introuvable', 404)
        if 'name' in data:
            # Le slug (donc l'entity_id HA) reste STABLE : renommer un groupe
            # ne change que le nom affiche. Avant, l'entity_id suivait le nom
            # et les automations cassaient a chaque renommage.
            target['name'] = str(data['name'] or '').strip() or target.get('name', slug)
        if 'threshold' in data:
            target['threshold'] = to_number(data['threshold'], 'threshold', 0, 1)
        if 'ha_entities' in data:
            target['ha_entities'] = to_clap_counts(data['ha_entities'] or [])
            # Plus de nombres de claps = nouvelles entites : meme controle qu'a
            # la creation (une collision effacait l'entite d'une autre source).
            _check_entity_collision(settings)
        return dict(target)

    target = modify_settings(_mut)
    if 'name' in data or 'ha_entities' in data:
        # Seuls le nom et les nombres de claps changent les entites : ne pas
        # republier toute la configuration MQTT a chaque cran du seuil.
        _sync_ha_entities()
    _apply_live()
    return jsonify({'success': True, 'group': target, 'group_slug': slug})


@sources_bp.route('/api/source/sound_groups', methods=['DELETE'])
def delete_source_sound_group():
    """Supprime un groupe (le groupe par defaut, le premier, est protege).

    Body: {kind, source_key, group_slug}
    """
    data = _json()
    kind = _require_kind(data, 'group_slug')
    source_key = data.get('source_key')
    slug = data.get('group_slug')

    def _mut(settings):
        src = _require_source(settings, kind, source_key)
        groups = src.get('sound_groups', []) or []
        target = next((g for g in groups if isinstance(g, dict) and g.get('slug') == slug), None)
        if target is None:
            raise ApiError('groupe introuvable', 404)
        if target is _default_group(groups):
            raise ApiError('Le groupe par defaut ne peut etre supprime')
        src['sound_groups'] = [g for g in groups if g is not target]
        return dict(src)

    src = modify_settings(_mut)
    _sync_ha_entities()
    _apply_live()
    return jsonify({'success': True})


# --- Microphone --------------------------------------------------------------

def _update_mic(field, value):
    def _mut(settings):
        mic = settings.setdefault('microphone', {})
        mic[field] = value
        return dict(mic)
    return modify_settings(_mut)


@sources_bp.route('/api/microphone', methods=['POST'])
def add_microphone():
    """Affiche a nouveau la source micro (bouton "Ajouter > Microphone")."""
    def _mut(settings):
        mic = settings.setdefault('microphone', {})
        mic['configured'] = True
        return dict(mic)
    mic = modify_settings(_mut)
    _sync_ha_entities()  # publie ses entites (indisponibles tant qu'il est desactive)
    return jsonify({'success': True, 'microphone': mic})


@sources_bp.route('/api/microphone', methods=['DELETE'])
def delete_microphone():
    """Retire la source micro : desactivee et masquee, ses entites HA sont
    supprimees. Avant, "Supprimer" ne touchait que l'etat de la page et le
    micro reapparaissait au rechargement."""
    def _mut(settings):
        mic = settings.setdefault('microphone', {})
        was_enabled = bool(mic.get('enabled'))
        mic['enabled'] = False
        mic['configured'] = False
        return was_enabled
    _after_change(modify_settings(_mut))
    return jsonify({'success': True})


@sources_bp.route('/api/microphone/auto-start', methods=['PUT'])
def toggle_auto_start():
    enabled = to_bool(_json().get('enabled', False), 'enabled')
    _update_mic('auto_start', enabled)
    return jsonify({'success': True, 'auto_start': enabled})


# --- API unifiee -------------------------------------------------------------

_PATCH_FIELDS = {
    # Seuil et entites se reglent par groupe (/api/source/sound_groups) : les
    # champs de source equivalents etaient acceptes sans aucun effet.
    'mic': {'device', 'volume', 'auto_volume', 'webhook_url', 'enabled'},
    'rtsp': {'url', 'name', 'webhook_url', 'enabled', 'gain'},
    # VBAN : `name` n'est que le nom affiche ; le routage utilise stream_name
    # et les entity_id la cle fixee a la creation.
    'vban': {'name', 'webhook_url', 'enabled', 'gain'},
}


def _check_fields(kind, data):
    unknown = sorted(set(data) - _PATCH_FIELDS[kind])
    if unknown:
        # Avant : ignores en silence avec success: true.
        raise ApiError(f"Champ(s) non modifiable(s) : {', '.join(unknown)}")


def _update_mic_fields(data):
    """Champs du micro modifiables via PATCH /api/sources/mic/mic.

    Tout le corps est valide AVANT d'ecrire, puis applique en une seule
    ecriture : avant, jusqu'a 6 ecritures s'enchainaient et une erreur au
    milieu laissait une partie des changements enregistree.
    """
    _check_fields('mic', data)
    changes = {}
    if 'device' in data:
        dev = data['device'] or {}
        if not isinstance(dev, dict):
            raise ApiError('device : objet attendu')
        changes['audio_source'] = str(dev.get('name') or 'default')
        changes['device_index'] = to_number(dev.get('index', 0), 'device.index', 0, 10000, integer=True)
        changes['pulse_name'] = str(dev.get('pulse_name') or '')
    if 'volume' in data:
        changes['volume'] = to_number(data['volume'], 'volume', 0, 150, integer=True)
    if 'auto_volume' in data:
        changes['auto_volume'] = to_bool(data['auto_volume'], 'auto_volume')
    if 'webhook_url' in data:
        changes['webhook_url'] = to_webhook(data['webhook_url'])
    if 'enabled' in data:
        changes['enabled'] = to_bool(data['enabled'], 'enabled')

    current = load_settings().get('microphone', {})
    auto_after = changes.get('auto_volume', current.get('auto_volume', False))
    if 'volume' in changes and auto_after:
        raise ApiError('Volume automatique actif : désactivez-le pour régler le volume')
    if changes.get('auto_volume') and not _resolve_pulse_name(load_settings()):
        raise ApiError('Aucun périphérique PulseAudio trouvé pour le volume automatique')

    def _mut(settings):
        m = settings.setdefault('microphone', {})
        before = dict(m)
        m.update(changes)
        return before, dict(m)

    before, mic = modify_settings(_mut)

    # Effets en direct, une fois tout enregistre.
    if 'volume' in changes and mic.get('pulse_name'):
        from audio_utils import set_pulse_volume
        set_pulse_volume(mic['pulse_name'], changes['volume'])
    if 'webhook_url' in changes:
        _apply_live()
    if 'enabled' in changes:
        _sync_ha_entities()  # disponibilite des entites du micro
    if changes.get('auto_volume') is False:
        from auto_volume import auto_volume_mgr
        auto_volume_mgr.stop()
    device_changed = any(before.get(k) != mic.get(k) for k in ('audio_source', 'device_index', 'pulse_name'))
    needs_restart = (('enabled' in changes and before.get('enabled') != mic.get('enabled'))
                     or ('auto_volume' in changes and before.get('auto_volume') != mic.get('auto_volume'))
                     or (device_changed and mic.get('enabled')))
    if needs_restart:
        _restart_detection_if_running()
    return mic


@sources_bp.route('/api/sources/<kind>/<path:key>', methods=['PATCH'])
def patch_source(kind, key):
    """Modifie une source, quel que soit son type (une seule route pour l'UI).

    - mic  : /api/sources/mic/mic
    - rtsp : /api/sources/rtsp/<id>
    - vban : /api/sources/vban/<id>
    """
    data = _json()
    if kind in ('rtsp', 'vban'):
        _check_fields(kind, data)
    if kind == 'rtsp':
        return jsonify({'success': True, 'source': _update_rtsp(key, data)})
    if kind == 'vban':
        return jsonify({'success': True, 'source': _update_vban(key, data)})
    if kind == 'mic':
        return jsonify({'success': True, 'source': _update_mic_fields(data)})
    raise ApiError('Type de source inconnu', 404)
