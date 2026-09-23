from flask import Blueprint, jsonify, request
from werkzeug.exceptions import HTTPException
import logging
import threading
import uuid

from settings_manager import (load_settings, modify_settings, SettingsSaveError, clap_counts_of,
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


def _source_id_for(kind, source_key):
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
    """Aligne les entites HA sur la configuration (ajout, suppression,
    source activee / desactivee), que la detection tourne ou non."""
    try:
        from ha_entities import sync_sources
        sync_sources(load_settings())
    except Exception as e:
        logging.warning(f"Entités HA non synchronisées: {e}")


def _restart_detection_if_running():
    """Redémarre la détection avec les sources mises à jour si elle tourne."""
    _sync_ha_entities()
    with _restart_lock:
        try:
            from classify import is_running, stop_detection, start_from_settings
            if not is_running():
                return
            stop_detection()  # attend la fin des threads de la session
            # Un arret lent (plusieurs sources, Raspberry Pi) ne doit pas etre
            # pris pour un echec : attendre que la session soit vraiment finie.
            import time
            deadline = time.monotonic() + 15
            while is_running() and time.monotonic() < deadline:
                time.sleep(0.2)
            started, sources = start_from_settings(_socketio)
            if started and _socketio:
                source_display = ' + '.join(s['label'] for s in sources)
                _socketio.emit('detection_status', {'status': 'running', 'source': source_display})
                logging.info(f"Détection redémarrée avec: {source_display}")
            elif _socketio:
                _socketio.emit('detection_status', {'status': 'stopped'})
                logging.info("Détection arrêtée: aucune source active" if not sources
                             else "Détection arrêtée: redémarrage impossible")
        except Exception as e:
            logging.error(f"Erreur redémarrage détection: {e}")


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


def _normalize_rtsp_url(url):
    """URL RTSP stockee : texte, prefixe rtsp:// ajoute si absent, aucun autre
    protocole accepte (ffmpeg ouvrirait sinon file://, http://...)."""
    if not isinstance(url, str):
        raise ApiError("URL RTSP : texte attendu")
    url = url.strip()
    if not url:
        return ''
    if '://' in url:
        if not url.lower().startswith(('rtsp://', 'rtsps://')):
            raise ApiError('Seul le protocole rtsp:// est autorisé')
        return url
    return 'rtsp://' + url


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


def _find_vban(settings, ip, name=None, stream_name=None):
    for s in settings.get('saved_vban_sources', []) or []:
        if s.get('ip') != ip:
            continue
        if name is not None and s.get('name') == name:
            return s
        if stream_name is not None and (s.get('stream_name') or s.get('name')) == stream_name:
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

@sources_bp.route('/api/rtsp/streams', methods=['GET'])
def get_rtsp_streams():
    return jsonify(load_settings().get('rtsp_sources', []))


@sources_bp.route('/api/rtsp/stream', methods=['POST'])
def add_rtsp_stream():
    data = _json()
    new_stream = {
        'id': str(uuid.uuid4()),
        'name': str(data.get('name', '') or '').strip(),
        'url': _normalize_rtsp_url(data.get('url', '')),
        'webhook_url': to_webhook(data.get('webhook_url', '')),
        'enabled': to_bool(data.get('enabled', True), 'enabled'),
        'gain': to_number(data.get('gain', 10), 'gain', 0, 100, integer=True),
        'threshold': to_number(data.get('threshold', 0.5), 'threshold', 0, 1),
    }

    def _mut(settings):
        settings.setdefault('rtsp_sources', []).append(new_stream)

    modify_settings(_mut)
    # Une source active avec URL doit etre ecoutee tout de suite.
    if new_stream['enabled'] and new_stream['url']:
        _restart_detection_if_running()
    return jsonify({'success': True, 'stream': new_stream})


@sources_bp.route('/api/rtsp/stream/<stream_id>', methods=['PUT'])
def update_rtsp_stream(stream_id):
    return jsonify({'success': True, 'stream': _update_rtsp(stream_id, _json())})


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
            stream['name'] = str(data['name'] or '').strip()
        if 'webhook_url' in data:
            stream['webhook_url'] = to_webhook(data['webhook_url'])
        if 'enabled' in data:
            new_enabled = to_bool(data['enabled'], 'enabled')
            enabled_changed = new_enabled != stream.get('enabled')
            stream['enabled'] = new_enabled
        if 'threshold' in data:
            stream['threshold'] = to_number(data['threshold'], 'threshold', 0, 1)
        if 'gain' in data:
            stream['gain'] = to_number(data['gain'], 'gain', 0, 100, integer=True)
        if 'ha_entities' in data:
            stream['ha_entities'] = to_clap_counts(data['ha_entities'])
        # Redemarrage si la source est (des)activee, ou si l'URL d'une source
        # active change (ffmpeg doit se reconnecter a la nouvelle URL).
        state['restart'] = enabled_changed or (url_changed and stream.get('enabled', False))
        return dict(stream)

    stream = modify_settings(_mut)

    # Effets en direct, APRES l'enregistrement (un redemarrage relit le disque).
    if 'gain' in data:
        try:
            from classify import update_rtsp_gain
            update_rtsp_gain(stream.get('url', ''), stream['gain'])
        except Exception as e:
            logging.warning(f"Gain RTSP non appliqué en direct: {e}")
    if 'webhook_url' in data:
        from classify import update_source_webhook
        update_source_webhook(_source_id_for('rtsp', stream_id), stream['webhook_url'])
    if 'ha_entities' in data or 'name' in data:
        _refresh_source_entities('rtsp', stream)
    if state.get('restart'):
        _restart_detection_if_running()
    return stream


@sources_bp.route('/api/rtsp/stream/<stream_id>', methods=['DELETE'])
def delete_rtsp_stream(stream_id):
    def _mut(settings):
        before = settings.get('rtsp_sources', [])
        settings['rtsp_sources'] = [s for s in before if s.get('id') != stream_id]
        return len(settings['rtsp_sources']) != len(before)

    removed = modify_settings(_mut)
    try:
        from ha_entities import unregister_source, source_entity_key
        unregister_source(source_entity_key('rtsp', {'id': stream_id}))
    except Exception as e:
        logging.warning(f"Entités HA de la source RTSP non retirées: {e}")
    if removed:
        # Sinon ffmpeg continue sur le flux supprime jusqu'au prochain redemarrage.
        _restart_detection_if_running()
    return jsonify({'success': True})


# --- VBAN --------------------------------------------------------------------

@sources_bp.route('/api/vban/sources', methods=['GET'])
def get_vban_sources():
    detector = get_vban_detector()
    if not detector:
        return jsonify({'error': 'Impossible d\'initialiser la découverte VBAN'}), 500
    return jsonify(detector.get_sources(timeout=1.0))


@sources_bp.route('/api/vban/saved-sources', methods=['GET'])
def get_saved_vban_sources():
    return jsonify(load_settings().get('saved_vban_sources', []))


@sources_bp.route('/api/vban/save', methods=['POST'])
def save_vban_source():
    source = _json()
    if not all(field in source for field in ('name', 'ip', 'port')):
        raise ApiError('Données manquantes pour la source VBAN')
    name = str(source['name']).strip()
    ip = str(source['ip']).strip()
    if not name or not ip:
        raise ApiError('Nom et IP requis pour la source VBAN')
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

    modify_settings(_mut)
    if new_source['enabled']:
        _restart_detection_if_running()
    return jsonify({'success': True, 'source': new_source})


@sources_bp.route('/api/vban/remove', methods=['DELETE'])
def remove_vban_source():
    data = _json()
    ip = data.get('ip')
    stream_name = data.get('stream_name') or data.get('name')
    if not ip or not stream_name:
        raise ApiError('Données manquantes')

    def _mut(settings):
        src = _find_vban(settings, ip, stream_name=stream_name)
        settings['saved_vban_sources'] = [s for s in settings['saved_vban_sources'] if s is not src]
        return dict(src)

    removed = modify_settings(_mut)
    try:
        from ha_entities import unregister_source, source_entity_key
        unregister_source(source_entity_key('vban', removed))
    except Exception as e:
        logging.warning(f"Entités HA de la source VBAN non retirées: {e}")
    _restart_detection_if_running()
    return jsonify({'success': True})


@sources_bp.route('/api/vban/update', methods=['PUT'])
def update_vban_source():
    source = _json()
    if 'ip' not in source or 'name' not in source:
        raise ApiError('Données manquantes')
    s = _find_vban(load_settings(), source['ip'], name=source['name'])
    _update_vban(s['id'], source)
    return jsonify({'success': True})


def _update_vban(vban_id, source):
    state = {}

    def _mut(settings):
        s = _find_vban_by_id(settings, vban_id)
        restart = False
        if 'webhook_url' in source:
            s['webhook_url'] = to_webhook(source['webhook_url'])
        if 'enabled' in source:
            new_enabled = to_bool(source['enabled'], 'enabled')
            restart = new_enabled != s.get('enabled')
            s['enabled'] = new_enabled
        if 'threshold' in source:
            s['threshold'] = to_number(source['threshold'], 'threshold', 0, 1)
        if 'gain' in source:
            s['gain'] = to_number(source['gain'], 'gain', 0, 100)
        if 'ha_entities' in source:
            s['ha_entities'] = to_clap_counts(source['ha_entities'])
        state['restart'] = restart
        return dict(s)

    s = modify_settings(_mut)
    if 'gain' in source:
        try:
            from classify import update_vban_gain
            update_vban_gain(_source_id_for('vban', s['id']), s['gain'])
        except Exception as e:
            logging.warning(f"Gain VBAN non appliqué en direct: {e}")
    if 'webhook_url' in source:
        from classify import update_source_webhook
        update_source_webhook(_source_id_for('vban', s['id']), s['webhook_url'])
    if 'ha_entities' in source:
        _refresh_source_entities('vban', s)
    if state.get('restart'):
        _restart_detection_if_running()
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
    try:
        from classify import update_global_exclusions
        update_global_exclusions(lst)
    except Exception as e:
        logging.warning(f"Exclusions non appliquées en direct: {e}")
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
        # Compat : anciens appels par IP
        for s in settings.get('saved_vban_sources', []):
            if not s.get('id') and s.get('ip') == source_key:
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


def _groups_payload(groups):
    payload = []
    for g in groups or []:
        if not isinstance(g, dict):
            continue
        payload.append({
            'slug': g.get('slug', 'clap'),
            'name': g.get('name', 'Clap'),
            'whitelist': dict(g.get('sound_whitelist') or {}),
            'threshold': float(g.get('threshold', 0.5)),
            'clap_counts': clap_counts_of(g),
        })
    return payload


@sources_bp.route('/api/source/sound_whitelist/cleanup', methods=['POST'])
def cleanup_source_sound_whitelist():
    """Retire tous les labels non coches (value=false) de la whitelist."""
    data = _json()
    kind = _require_kind(data)
    source_key = data.get('source_key')
    group_slug = data.get('group_slug')

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
            if target_group is _default_group(groups):
                legacy = target.get('sound_whitelist') or {}
                target['sound_whitelist'] = {k: v for k, v in legacy.items() if v}
        else:
            # Compat retro : nettoyer tous les groupes + legacy
            wl = target.get('sound_whitelist') or {}
            kept = {k: v for k, v in wl.items() if v}
            removed = len(wl) - len(kept)
            target['sound_whitelist'] = kept
            for g in groups:
                if isinstance(g, dict):
                    g_wl = g.get('sound_whitelist') or {}
                    g['sound_whitelist'] = {k: v for k, v in g_wl.items() if v}
        remaining = set((target.get('sound_whitelist') or {}).keys())
        for g in groups:
            if isinstance(g, dict):
                remaining.update((g.get('sound_whitelist') or {}).keys())
        return removed, remaining, list(groups)

    removed, remaining_labels, groups = modify_settings(_mut)

    # Nettoyer aussi les seen labels du detecteur actif et lui pousser les groupes
    source_id = _source_id_for(kind, source_key)
    if source_id:
        try:
            from classify import set_seen_labels, push_groups
            set_seen_labels(source_id, remaining_labels)
            push_groups(source_id, _groups_payload(groups))
        except Exception as e:
            logging.warning(f"Nettoyage non appliqué au détecteur actif: {e}")

    return jsonify({'success': True, 'removed': removed, 'remaining': len(remaining_labels)})


@sources_bp.route('/api/source/sound_whitelist', methods=['DELETE'])
def delete_source_sound_whitelist_entry():
    """Retire un label de la sound_whitelist d'une source.

    Body: {kind, source_key, label}
    """
    data = _json()
    kind = _require_kind(data, 'label')
    source_key = data.get('source_key')
    label = data.get('label')

    def _mut(settings):
        source_dict = _require_source(settings, kind, source_key)
        source_dict.setdefault('sound_whitelist', {}).pop(label, None)
        for g in source_dict.get('sound_groups', []) or []:
            if isinstance(g, dict):
                (g.get('sound_whitelist') or {}).pop(label, None)

    modify_settings(_mut)
    source_id = _source_id_for(kind, source_key)
    if source_id:
        try:
            from classify import remove_source_whitelist_entry
            remove_source_whitelist_entry(source_id, label)
        except Exception as e:
            logging.warning(f"Retrait non appliqué au détecteur actif: {e}")
    return jsonify({'success': True})


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
        # Synchroniser le legacy (compat UI ancien) pour le groupe par defaut
        if target_group is default:
            source_dict.setdefault('sound_whitelist', {})[label] = enabled
        return target_slug

    target_slug = modify_settings(_mut)
    source_id = _source_id_for(kind, source_key)
    if source_id:
        try:
            from classify import update_source_whitelist
            update_source_whitelist(source_id, label, enabled, group_slug=target_slug)
        except Exception as e:
            logging.warning(f"Whitelist non appliquée au détecteur actif: {e}")
    return jsonify({'success': True, 'group_slug': target_slug})


def _refresh_source_entities(kind, source_dict):
    """Re-enregistre les entites HA pour la source apres modification de groupes."""
    try:
        from ha_entities import register_source, source_entity_key, source_label
        if kind == 'mic' and not source_dict.get('enabled', False):
            return  # micro desactive : pas d'entites a publier
        register_source(source_entity_key(kind, source_dict),
                        label=source_label(kind, source_dict),
                        groups=source_dict.get('sound_groups'))
    except Exception as exc:
        logging.warning(f"Entités HA non rafraîchies ({kind}): {exc}")


def _push_groups_to_detector(kind, source_key, groups):
    """Recharge les groupes dans le detecteur actif si la detection tourne."""
    try:
        source_id = _source_id_for(kind, source_key)
        if not source_id:
            return
        from classify import push_groups
        push_groups(source_id, _groups_payload(groups))
    except Exception as exc:
        logging.warning(f"Groupes non appliqués au détecteur actif: {exc}")


@sources_bp.route('/api/source/sound_groups', methods=['GET'])
def list_source_sound_groups():
    """Liste les groupes d'une source. Query: ?kind=&source_key=."""
    kind = _require_kind(request.args)
    src = _require_source(load_settings(), kind, request.args.get('source_key'))
    return jsonify({'groups': src.get('sound_groups', []) or []})


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
        new_group = {
            'slug': _slugify_group(name, existing_slugs), 'name': name,
            'sound_whitelist': {},
            'threshold': to_number(data.get('threshold', src.get('threshold', 0.5)), 'threshold', 0, 1),
            'ha_entities': to_clap_counts(data.get('ha_entities') or src.get('ha_entities') or [1, 2]),
        }
        groups.append(new_group)
        return new_group, dict(src)

    new_group, src = modify_settings(_mut)
    _refresh_source_entities(kind, src)
    _push_groups_to_detector(kind, source_key, src.get('sound_groups'))
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
        new_slug = slug
        if 'name' in data:
            # Le slug (donc l'entity_id HA) reste STABLE : renommer un groupe
            # ne change que le nom affiche. Avant, l'entity_id suivait le nom
            # et les automations cassaient a chaque renommage.
            target['name'] = str(data['name'] or '').strip() or target.get('name', slug)
        if 'threshold' in data:
            target['threshold'] = to_number(data['threshold'], 'threshold', 0, 1)
        if 'ha_entities' in data:
            target['ha_entities'] = to_clap_counts(data['ha_entities'] or [])
        return dict(target), new_slug, dict(src)

    target, new_slug, src = modify_settings(_mut)
    _refresh_source_entities(kind, src)
    _push_groups_to_detector(kind, source_key, src.get('sound_groups'))
    return jsonify({
        'success': True, 'group': target,
        'group_slug': new_slug,
        'old_slug': slug if new_slug != slug else None,
    })


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
    _refresh_source_entities(kind, src)
    _push_groups_to_detector(kind, source_key, src.get('sound_groups'))
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
    return jsonify({'success': True, 'microphone': modify_settings(_mut)})


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
    was_enabled = modify_settings(_mut)
    try:
        from ha_entities import unregister_source, source_entity_key
        unregister_source(source_entity_key('mic', {}))
    except Exception as e:
        logging.warning(f"Entités HA du micro non retirées: {e}")
    if was_enabled:
        _restart_detection_if_running()
    return jsonify({'success': True})


@sources_bp.route('/api/microphone/device', methods=['PUT'])
def update_microphone_device():
    """Change le micro utilise. N'ecrit QUE les champs du device : avant,
    l'UI renvoyait toute la section microphone telle qu'au chargement de la
    page et ecrasait auto-start, webhook, groupes..."""
    data = _json()
    audio_source = str(data.get('audio_source') or 'default')
    device_index = to_number(data.get('device_index', 0), 'device_index', 0, 10000, integer=True)
    pulse_name = str(data.get('pulse_name') or '')

    def _mut(settings):
        mic = settings.setdefault('microphone', {})
        changed = (mic.get('audio_source'), mic.get('device_index'), mic.get('pulse_name')) != \
            (audio_source, device_index, pulse_name)
        mic['audio_source'] = audio_source
        mic['device_index'] = device_index
        mic['pulse_name'] = pulse_name
        return changed, bool(mic.get('enabled'))

    changed, enabled = modify_settings(_mut)
    if changed and enabled:
        _restart_detection_if_running()
    return jsonify({'success': True})


@sources_bp.route('/api/microphone/webhook', methods=['PUT'])
def update_microphone_webhook():
    webhook_url = to_webhook(_json().get('webhook_url'))
    mic = _update_mic('webhook_url', webhook_url)
    from classify import update_source_webhook
    update_source_webhook(_source_id_for('mic', mic.get('device_index', 0)), webhook_url)
    return jsonify({'success': True})


@sources_bp.route('/api/microphone/threshold', methods=['PUT'])
def update_microphone_threshold():
    threshold = max(0.0, min(1.0, to_number(_json().get('threshold', 0.5), 'threshold')))
    _update_mic('threshold', threshold)
    return jsonify({'success': True, 'threshold': threshold})


@sources_bp.route('/api/microphone/enabled', methods=['PUT'])
def update_microphone_enabled():
    # to_bool : "false" (chaine) valait True avec l'ancien stockage brut.
    enabled = to_bool(_json().get('enabled'), 'enabled')
    _update_mic('enabled', enabled)
    _restart_detection_if_running()
    return jsonify({'success': True})


@sources_bp.route('/api/microphone/auto-start', methods=['PUT'])
def toggle_auto_start():
    enabled = to_bool(_json().get('enabled', False), 'enabled')
    _update_mic('auto_start', enabled)
    return jsonify({'success': True, 'auto_start': enabled})


@sources_bp.route('/api/microphone/volume', methods=['PUT'])
def update_microphone_volume():
    if load_settings().get('microphone', {}).get('auto_volume'):
        raise ApiError('Volume automatique actif : désactivez-le pour régler le volume')
    data = _json()
    volume = int(max(0, min(150, to_number(data.get('volume', 100), 'volume'))))
    mic = _update_mic('volume', volume)

    # Utiliser le pulse_name du frontend si fourni, sinon celui sauvegardé
    pulse_name = data.get('pulse_name') or mic.get('pulse_name', '')
    if pulse_name:
        try:
            from audio_utils import set_pulse_volume
            set_pulse_volume(pulse_name, volume)
            logging.info(f"Volume PulseAudio mis à {volume}% pour {pulse_name}")
        except Exception as e:
            logging.warning(f"Impossible de régler le volume PulseAudio: {e}")
    return jsonify({'success': True, 'volume': volume})


@sources_bp.route('/api/microphone/ha-entities', methods=['PUT'])
def update_microphone_ha_entities():
    ha_entities = to_clap_counts(_json().get('ha_entities', [1, 2]))
    mic = _update_mic('ha_entities', ha_entities)
    _refresh_source_entities('mic', mic)
    return jsonify({'success': True, 'ha_entities': ha_entities})


@sources_bp.route('/api/microphone/auto-volume', methods=['PUT'])
def toggle_auto_volume():
    enabled = to_bool(_json().get('enabled', False), 'enabled')
    _set_auto_volume(enabled)
    return jsonify({'success': True, 'auto_volume': enabled})


def _set_auto_volume(enabled):
    """Le volume automatique vit avec la session de detection, qui lui
    fournit le niveau du micro : on enregistre le reglage et on redemarre la
    detection si elle tourne. Avant, il etait demarre ici sans source de niveau
    (inactif mais bloquant le reglage manuel) puis arrete par la fin de session
    alors que le reglage restait "active"."""
    if enabled and not _resolve_pulse_name(load_settings()):
        raise ApiError('Aucun périphérique PulseAudio trouvé pour le volume automatique')
    _update_mic('auto_volume', enabled)
    if not enabled:
        from auto_volume import auto_volume_mgr
        auto_volume_mgr.stop()
    _restart_detection_if_running()


# --- API unifiee -------------------------------------------------------------

_PATCH_FIELDS = {
    'mic': {'device', 'volume', 'auto_volume', 'webhook_url', 'ha_entities', 'enabled'},
    'rtsp': {'url', 'name', 'webhook_url', 'enabled', 'threshold', 'gain', 'ha_entities'},
    'vban': {'webhook_url', 'enabled', 'threshold', 'gain', 'ha_entities'},
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
        changes['volume'] = int(max(0, min(150, to_number(data['volume'], 'volume'))))
    if 'auto_volume' in data:
        changes['auto_volume'] = to_bool(data['auto_volume'], 'auto_volume')
    if 'webhook_url' in data:
        changes['webhook_url'] = to_webhook(data['webhook_url'])
    if 'ha_entities' in data:
        changes['ha_entities'] = to_clap_counts(data['ha_entities'])
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
        from classify import update_source_webhook
        update_source_webhook(_source_id_for('mic', mic.get('device_index', 0)), changes['webhook_url'])
    if 'ha_entities' in changes:
        _refresh_source_entities('mic', mic)
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
        if '/' in key:  # compat : ancienne cle <ip>/<nom>
            ip, _, name = key.partition('/')
            key = _find_vban(load_settings(), ip, name=name)['id']
        return jsonify({'success': True, 'source': _update_vban(key, data)})
    if kind == 'mic':
        return jsonify({'success': True, 'source': _update_mic_fields(data)})
    raise ApiError('Type de source inconnu', 404)
