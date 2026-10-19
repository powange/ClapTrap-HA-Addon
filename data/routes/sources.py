import json

from flask import Blueprint, jsonify, request
from werkzeug.exceptions import HTTPException
import logging
import threading
import uuid

from settings_manager import (load_settings, modify_settings, SettingsSaveError, with_default_groups,
                              to_bool, to_number, to_clap_counts, to_webhook, to_name, normalize_rtsp_url)
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
        # Meme format JSON que les autres erreurs (413, 404, 405 partaient en HTML).
        return jsonify({'success': False, 'error': e.description or e.name}), e.code
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
    try:
        data = request.get_json(silent=True)
    except RecursionError:
        # JSON tres imbrique ([[[[…) : 400 et non une erreur 500.
        raise ApiError('JSON trop imbriqué')
    if not isinstance(data, dict):
        raise ApiError('Corps JSON attendu')
    return data


# Bornes : un fichier ou un appel d'API demesure ne doit pas gonfler les
# reglages, relus et recopies a chaque lecture (memes valeurs qu'a l'import).
from settings_manager import MAX_SOURCES, MAX_GROUPS, MAX_LABEL, MAX_LABELS  # noqa: E402


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
    """Refuse une modification qui CREE une collision d'entites HA (appele
    dans le mutateur : le disque contient encore l'etat d'avant). Une
    collision heritee ne bloque plus les modifications des autres sources."""
    from ha_entities import find_entity_collision
    clash = find_entity_collision(settings, before=load_settings())
    if clash:
        raise ApiError(f"Deux sources produiraient les mêmes entités Home Assistant (« {clash} ») : "
                       "choisissez un autre nom de groupe ou de source", 409)


def _apply_live():
    """Applique a la detection en cours les reglages enregistres (groupes,
    sons, webhook, gain, exclusions, reglages avances) : une seule entree au
    lieu de six fonctions de mise a jour en direct. Les sources dont la
    capture n'a pas change ne sont pas relancees."""
    try:
        from classify import apply_settings_if_running
        with _restart_lock:
            return apply_settings_if_running()   # None : detection arretee
    except Exception as e:
        logging.warning(f"Réglages non appliqués en direct: {e}")
        return None


def _set_live_gain(source_id, gain):
    """Gain lu par le test du son quand la detection est arretee (sinon
    l'application des reglages le met a jour)."""
    try:
        from classify import update_source_gain
        update_source_gain(source_id, gain)
    except Exception as e:
        logging.warning(f"Gain non appliqué en direct: {e}")


def _after_change(restart):
    """Apres une modification de source : redemarrage de la detection (qui
    resynchronise les entites HA) ou simple synchronisation."""
    if restart:
        _sync_and_apply()
    else:
        _sync_ha_entities()


def _source_effects(source_id, fields, restart, gain=None):
    """Effets en direct d'une modification RTSP/VBAN, en un seul passage :
    avant, gain, application et synchronisation HA s'enchainaient (le gain
    etait applique deux fois, les reglages relus a chaque etape)."""
    if restart or 'name' in fields:
        running = _sync_and_apply()          # entites HA + detection (gain compris)
    elif fields & {'webhook_url', 'gain'}:
        running = _apply_live()
    else:
        return
    if gain is not None and running is None:
        _set_live_gain(source_id, gain)


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
    """after_request des blueprints : ajoute `restart` (resultat de la mise a
    jour de la detection, cf. _note_restart) aux reponses JSON de succes.
    Ne reecrit la reponse que si une mise a jour a eu lieu."""
    from flask import g
    result = g.pop('restart', None)
    if result and response.is_json and response.status_code < 400:
        data = response.get_json(silent=True)
        if isinstance(data, dict):
            data['restart'] = result
            response.set_data(json.dumps(data))
    return response


sources_bp.after_request(add_restart_to_response)


def _sync_and_apply():
    """Synchronise les entites HA puis applique les reglages a la detection
    en cours : seules les sources ajoutees, retirees ou modifiees (adresse,
    micro, flux) sont relancees ; les autres gardent leur classifieur.
    Renvoie None si la detection est arretee."""
    _sync_ha_entities()
    with _restart_lock:
        try:
            from classify import apply_settings_if_running, build_sources_from_settings
            settings = load_settings()
            changed = apply_settings_if_running(settings)
            if changed is None:
                return None  # detection arretee
            if not build_sources_from_settings(settings):
                # Plus aucune source : la session s'arrete d'elle-meme.
                logging.info("Détection arrêtée: aucune source active")
                _note_restart('stopped')
            elif changed:
                if _socketio:
                    _socketio.emit('detection_status', {'status': 'running'})
                logging.info("Détection mise à jour (sources modifiées relancées)")
                _note_restart('ok')
            return changed
        except Exception as e:
            logging.error(f"Erreur de mise à jour de la détection: {e}")
            _note_restart('failed')
            return None


def _persist_pulse_name(pulse_name):
    def _mut(settings):
        settings.setdefault('microphone', {})['pulse_name'] = pulse_name
    try:
        modify_settings(_mut)
    except Exception as e:
        logging.warning(f"pulse_name non enregistré: {e}")


def _find_pulse_name(settings):
    """pulse_name du micro choisi, verifie parmi les micros presents, sans
    rien ecrire (validations, tests du son). Renvoie (pulse_name, a_ecrire) :
    `a_ecrire` vaut la valeur a enregistrer si celle des reglages est perimee,
    sinon None."""
    mic = settings.get('microphone', {})
    audio_source = mic.get('audio_source', '')
    cached = mic.get('pulse_name', '')
    if not audio_source or audio_source == 'default':
        return '', ('' if cached else None)   # pas de micro precis : nom perime efface
    devices = get_audio_input_devices()
    if cached and any(d.get('name') == audio_source and d.get('pulse_name') == cached for d in devices):
        return cached, None
    found = next((d['pulse_name'] for d in devices if d.get('name') == audio_source and d.get('pulse_name')), '')
    if found:
        return found, found
    logging.warning(f"Impossible de résoudre pulse_name pour '{audio_source}'")
    return '', None


def _resolve_pulse_name(settings):
    """Comme _find_pulse_name, en enregistrant la correction (atomique)."""
    pulse, to_save = _find_pulse_name(settings)
    if to_save is not None:
        _persist_pulse_name(to_save)
        if to_save:
            logging.info(f"pulse_name résolu et sauvegardé: {to_save}")
    return pulse


def _rtsp_url_key(url):
    """URL comparee sans tenir compte de la casse du schema (RTSP:// = rtsp://)."""
    scheme, sep, rest = str(url or '').partition('://')
    return f"{scheme.lower()}{sep}{rest}" if sep else str(url or '')


def _check_rtsp_unique(settings, url, exclude_id=None):
    """Une camera par URL, a l'ajout comme a la modification."""
    if url and any(s.get('id') != exclude_id and _rtsp_url_key(s.get('url')) == _rtsp_url_key(url)
                   for s in settings.get('rtsp_sources') or []):
        raise ApiError('Cette caméra (même URL) est déjà ajoutée', 409)


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
        'name': to_name(data.get('name'), default='Caméra'),
        'url': normalize_rtsp_url(data.get('url', '')),
        'webhook_url': to_webhook(data.get('webhook_url', '')),
        'enabled': to_bool(data.get('enabled', True), 'enabled'),
        'gain': to_number(data.get('gain', 10), 'gain', 0, 100, integer=True),
        'rtsp_tcp': to_bool(data.get('rtsp_tcp', False), 'rtsp_tcp'),
    }
    with_default_groups(new_stream)   # groupe « Clap » deja utilisable

    def _mut(settings):
        # Doublon : un assistant ferme pendant l'ajout faisait recommencer
        # l'utilisateur, et la camera existait deux fois.
        _check_rtsp_unique(settings, new_stream['url'])
        if len(settings.get('rtsp_sources') or []) >= MAX_SOURCES:
            raise ApiError(f'{MAX_SOURCES} caméras au plus')
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
        url_changed = enabled_changed = transport_changed = False
        if 'rtsp_tcp' in data:
            tcp = to_bool(data['rtsp_tcp'], 'rtsp_tcp')
            transport_changed = tcp != bool(stream.get('rtsp_tcp'))
            stream['rtsp_tcp'] = tcp
        if 'url' in data:
            new_url = normalize_rtsp_url(data['url'])
            _check_rtsp_unique(settings, new_url, exclude_id=stream.get('id'))
            url_changed = new_url != stream.get('url')
            stream['url'] = new_url
        if 'name' in data:
            stream['name'] = to_name(data['name'], default='Caméra')
        if 'webhook_url' in data:
            stream['webhook_url'] = to_webhook(data['webhook_url'])
        if 'enabled' in data:
            new_enabled = to_bool(data['enabled'], 'enabled')
            enabled_changed = new_enabled != stream.get('enabled')
            stream['enabled'] = new_enabled
        if 'gain' in data:
            stream['gain'] = to_number(data['gain'], 'gain', 0, 100, integer=True)
        # Redemarrage si la source est (des)activee, ou si l'URL ou le
        # transport d'une source active change (ffmpeg doit se reconnecter).
        state['restart'] = enabled_changed or ((url_changed or transport_changed) and stream.get('enabled', False))
        return dict(stream)

    stream = modify_settings(_mut)

    # Effets en direct, APRES l'enregistrement (un redemarrage relit le disque).
    _source_effects(_source_id_for('rtsp', stream_id), set(data), state.get('restart'), stream.get('gain') if 'gain' in data else None)
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
    name = to_name(source['name'])
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
        'stream_name': to_name(source.get('stream_name') or name),
        'webhook_url': to_webhook(source.get('webhook_url', '')),
        'enabled': to_bool(source.get('enabled', True), 'enabled'),
    }
    with_default_groups(new_source)   # groupe « Clap » deja utilisable

    def _mut(settings):
        lst = settings.setdefault('saved_vban_sources', [])
        from settings_manager import vban_entity_key
        new_source['entity_key'] = vban_entity_key(new_source, lst)
        if len(lst) >= MAX_SOURCES:
            raise ApiError(f'{MAX_SOURCES} flux VBAN au plus')
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
            s['name'] = to_name(source['name'])
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
    _source_effects(_source_id_for('vban', s['id']), set(source), state.get('restart'), s.get('gain') if 'gain' in source else None)
    return s


@sources_bp.route('/refresh_vban_sources')
def refresh_vban_sources():
    detector = get_vban_detector()
    if not detector:
        return jsonify({"sources": []})
    return jsonify({"sources": detector.get_sources(timeout=1.0)})


# --- Exclusions globales -----------------------------------------------------

_YAMNET_LABELS = None


@sources_bp.route('/api/sounds/known', methods=['GET'])
def known_sounds():
    """Les 521 classes de YAMNet (liste embarquee dans le modele) : la
    recherche d'un groupe propose tous les sons, meme jamais entendus."""
    global _YAMNET_LABELS
    if _YAMNET_LABELS is None:
        import os
        import zipfile
        model = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'yamnet.tflite')
        try:
            with zipfile.ZipFile(model) as z:
                _YAMNET_LABELS = [l for l in z.read('yamnet_label_list.txt').decode('utf-8').splitlines() if l.strip()]
        except (OSError, KeyError, zipfile.BadZipFile) as e:
            logging.warning(f"Liste des sons de YAMNet illisible : {e}")
            return jsonify({'labels': []})
    return jsonify({'labels': _YAMNET_LABELS})


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
    label = data.get('label')
    excluded = to_bool(data.get('excluded', False), 'excluded')
    if not isinstance(label, str) or not label.strip():
        raise ApiError('label : nom de son attendu')
    label = label.strip()
    if excluded and len(label) > MAX_LABEL:
        # Borne a l'ajout seulement : un nom trop long deja enregistre doit
        # pouvoir etre retire.
        raise ApiError(f'label : nom de son de {MAX_LABEL} caractères au plus attendu')

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
    if not isinstance(label, str) or not label or len(label) > MAX_LABEL:
        raise ApiError('label : nom de son attendu')
    enabled = to_bool(data.get('enabled', False), 'enabled')

    def _mut(settings):
        source_dict = _require_source(settings, kind, source_key)
        # Les groupes se creent par /api/source/sound_groups (et chaque source
        # en a un des sa creation) : plus de groupe cree ici a la volee.
        groups = source_dict.get('sound_groups') or []
        default = _default_group(groups)
        target_slug = data.get('group_slug') or (default.get('slug') if default else None)
        target_group = next((g for g in groups if isinstance(g, dict) and g.get('slug') == target_slug), None)
        if target_group is None:
            raise ApiError('groupe introuvable', 404)
        if enabled:
            for g in groups:
                if g is target_group or not isinstance(g, dict):
                    continue
                if (g.get('sound_whitelist') or {}).get(label) is True:
                    raise ApiError(
                        f'Le son "{label}" est deja active dans le groupe "{g.get("name", g.get("slug"))}"',
                        409, conflict_group=g.get('slug'))
        wl = target_group.setdefault('sound_whitelist', {})
        if label not in wl and len(wl) >= MAX_LABELS:
            raise ApiError(f'{MAX_LABELS} sons au plus par groupe')
        wl[label] = enabled
        return target_slug

    target_slug = modify_settings(_mut)
    _apply_live()
    return jsonify({'success': True, 'group_slug': target_slug})


@sources_bp.route('/api/source/sound_groups/usage', methods=['GET'])
def sound_group_usage():
    """Automatisations, scripts et scenes de HA qui utilisent les entites
    d'un groupe (a adapter s'il est renomme). Query : kind, source_key,
    group_slug. `available` : false si HA n'a pas pu etre interroge."""
    import ha_registry
    kind = request.args.get('kind')
    source_key = request.args.get('source_key')
    slug = request.args.get('group_slug')
    settings = load_settings()
    _require_source(settings, kind, source_key)
    ui_key = 'mic' if kind == 'mic' else f'{kind}:{source_key}'
    items = ha_registry.group_usage(settings, ui_key, slug)
    return jsonify({'success': True, 'available': items is not None, 'items': items or []})


def _check_unique_group_name(groups, name, exclude=None):
    """Deux groupes d'une source ne portent pas le meme nom (casse ignoree)."""
    want = name.strip().casefold()
    for g in groups:
        if isinstance(g, dict) and g is not exclude and str(g.get('name') or g.get('slug') or '').strip().casefold() == want:
            raise ApiError(f'Un groupe « {g.get("name") or g.get("slug")} » existe déjà sur cette source', 409)


def _rename_entity_slug(groups, target):
    """Renommage : les entity_id suivent le nouveau nom (`entity_slug`), le
    slug (unique_id, topics, webhook) ne change pas. Le renommage dans le
    registre de HA (historique et personnalisations conserves) est note dans
    `_renamed_from` (ids encore possibles dans HA, separes par des espaces)
    jusqu'a ce qu'il soit fait. Renvoie True si les entity_id changent."""
    current = target.get('entity_slug') or target['slug']
    # Ids pris par les autres groupes, y compris ceux qu'ils ont encore dans
    # HA (renommage en attente) : deux groupes ne visent jamais le meme id.
    others = set()
    for g in groups:
        if isinstance(g, dict) and g is not target:
            others.add(g.get('entity_slug') or g.get('slug'))
            others.update((g.get('_renamed_from') or '').split())
    new = _slugify_group(target['name'], others)
    if new == current:
        return False
    previous = [s for s in (target.get('_renamed_from') or '').split() if s != new]
    if current not in previous:
        previous.append(current)
    target['_renamed_from'] = ' '.join(previous)
    target['entity_slug'] = new
    return True


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
    if not isinstance(data.get('name'), str):
        raise ApiError('Nom du groupe : texte attendu')
    name = to_name(data['name'])

    def _mut(settings):
        src = _require_source(settings, kind, source_key)
        groups = src.setdefault('sound_groups', [])
        if len(groups) >= MAX_GROUPS:
            raise ApiError(f'{MAX_GROUPS} groupes au plus par source')
        _check_unique_group_name(groups, name)
        existing_slugs = {g.get('slug') for g in groups if isinstance(g, dict)} | \
            {g.get('entity_slug') for g in groups if isinstance(g, dict) and g.get('entity_slug')}
        # Groupe vide : ses sons s'ajoutent en activant « Ajouter les sons
        # entendus » (auto_add_sounds) pendant la detection.
        new_group = {
            'slug': _slugify_group(name, existing_slugs), 'name': name,
            'sound_whitelist': {}, 'auto_add_sounds': False,
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

    Body: {kind, source_key, group_slug, name?, threshold?, ha_entities?,
           auto_add_sounds?, enabled?, count_mode?}
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
            if not isinstance(data['name'], str) or not data['name'].strip():
                raise ApiError('Nom du groupe : texte non vide attendu')
            new_name = to_name(data['name'], default=target.get('name', slug))
            if new_name != target.get('name'):
                # Meme nom renvoye (entite text, assistant) : rien ne change,
                # surtout pas les entity_id.
                _check_unique_group_name(groups, new_name, exclude=target)
                target['name'] = new_name
                state['renamed'] = _rename_entity_slug(groups, target)
        if 'threshold' in data:
            target['threshold'] = to_number(data['threshold'], 'threshold', 0, 1)
        if 'auto_add_sounds' in data:
            target['auto_add_sounds'] = to_bool(data['auto_add_sounds'], 'auto_add_sounds')
        if 'enabled' in data:
            # Groupe desactive : ignore par la detection, entites conservees.
            target['enabled'] = to_bool(data['enabled'], 'enabled')
        if 'count_mode' in data:
            from settings_manager import to_count_mode
            target['count_mode'] = to_count_mode(data['count_mode'])
        if 'ha_entities' in data or state.get('renamed'):
            if 'ha_entities' in data:
                target['ha_entities'] = to_clap_counts(data['ha_entities'] or [])
            # Plus de nombres de claps ou nouveaux entity_id : meme controle
            # qu'a la creation (une collision effacait l'entite d'une autre source).
            _check_entity_collision(settings)
        return dict(target)

    state = {}
    target = modify_settings(_mut)
    if state.get('renamed'):
        # Entites renommees dans le registre de HA (historique conserve).
        import ha_registry
        ha_registry.schedule_reconcile()
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

    modify_settings(_mut)
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
    """Affiche a nouveau la source micro (bouton "Ajouter > Microphone").

    Corps facultatif avec les memes champs que le PATCH (device, enabled...) :
    l'assistant ajoute, regle et active le micro en une requete (une ecriture,
    une synchronisation HA) au lieu de trois.
    """
    data = request.get_json(silent=True)
    if isinstance(data, dict) and data:
        return jsonify({'success': True, 'microphone': _update_mic_fields(data, configured=True)})

    def _mut(settings):
        mic = settings.setdefault('microphone', {})
        mic['configured'] = True
        with_default_groups(mic)
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
    'rtsp': {'url', 'name', 'webhook_url', 'enabled', 'gain', 'rtsp_tcp'},
    # VBAN : `name` n'est que le nom affiche ; le routage utilise stream_name
    # et les entity_id la cle fixee a la creation.
    'vban': {'name', 'webhook_url', 'enabled', 'gain'},
}


def _check_fields(kind, data):
    unknown = sorted(set(data) - _PATCH_FIELDS[kind])
    if unknown:
        # Avant : ignores en silence avec success: true.
        raise ApiError(f"Champ(s) non modifiable(s) : {', '.join(unknown)}")


def _update_mic_fields(data, configured=False):
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
        for field in ('name', 'pulse_name'):
            if dev.get(field) is not None and not isinstance(dev.get(field), str):
                raise ApiError(f'device.{field} : texte attendu')
        changes['audio_source'] = dev.get('name') or 'default'
        changes['device_index'] = to_number(dev.get('index', 0), 'device.index', 0, 10000, integer=True)
        changes['pulse_name'] = dev.get('pulse_name') or ''
    if 'volume' in data:
        changes['volume'] = to_number(data['volume'], 'volume', 0, 150, integer=True)
    if 'auto_volume' in data:
        changes['auto_volume'] = to_bool(data['auto_volume'], 'auto_volume')
    if 'webhook_url' in data:
        changes['webhook_url'] = to_webhook(data['webhook_url'])
    if 'enabled' in data:
        changes['enabled'] = to_bool(data['enabled'], 'enabled')
    if configured:
        changes['configured'] = True

    current = load_settings().get('microphone', {})
    auto_after = changes.get('auto_volume', current.get('auto_volume', False))
    if 'volume' in changes and auto_after:
        raise ApiError('Volume automatique actif : désactivez-le pour régler le volume')
    if changes.get('auto_volume') and 'device' in data:
        # Peripherique change dans la meme requete : valider sur lui (l'ancien
        # etait verifie), parmi les micros presents, sans rien enregistrer.
        present = [d for d in get_audio_input_devices() if d.get('pulse_name')]
        target_pulse = next((d['pulse_name'] for d in present if d['pulse_name'] == changes['pulse_name']), '') or \
            next((d['pulse_name'] for d in present if d.get('name') == changes['audio_source']), '')
        if not target_pulse:
            raise ApiError('Aucun périphérique PulseAudio trouvé pour le volume automatique')
        changes['pulse_name'] = target_pulse
    elif changes.get('auto_volume'):
        pulse, to_save = _find_pulse_name(load_settings())
        if not pulse:
            raise ApiError('Aucun périphérique PulseAudio trouvé pour le volume automatique')
        if to_save is not None:
            changes['pulse_name'] = to_save   # nom perime corrige, ecrit avec la modification

    def _mut(settings):
        m = settings.setdefault('microphone', {})
        before = dict(m)
        m.update(changes)
        if configured:
            with_default_groups(m)   # micro ajoute par l'assistant : groupe « Clap » utilisable
        return before, dict(m)

    before, mic = modify_settings(_mut)

    # Effets en direct, une fois tout enregistre.
    if 'volume' in changes and mic.get('pulse_name'):
        from audio_utils import set_pulse_volume
        set_pulse_volume(mic['pulse_name'], changes['volume'])
    if changes.get('auto_volume') is False:
        from auto_volume import auto_volume_mgr
        auto_volume_mgr.stop()
    device_changed = any(before.get(k) != mic.get(k) for k in ('audio_source', 'device_index', 'pulse_name'))
    needs_restart = (('enabled' in changes and before.get('enabled') != mic.get('enabled'))
                     or ('auto_volume' in changes and before.get('auto_volume') != mic.get('auto_volume'))
                     or (device_changed and mic.get('enabled')))
    if needs_restart or 'enabled' in changes or device_changed or configured:
        # Une seule synchronisation HA (disponibilite, nom du micro affiche,
        # meme desactive) : la relance la fait deja.
        _after_change(needs_restart)
    elif 'webhook_url' in changes:
        _apply_live()
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
