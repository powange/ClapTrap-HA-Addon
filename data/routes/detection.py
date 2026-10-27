from flask import Blueprint, jsonify
import logging

from classify import stop_detection, get_detection_history
from routes.sources import ApiError, api_error_response

detection_bp = Blueprint('detection', __name__)
# Memes reponses d'erreur que les autres routes : {success: false, error}
# avec le bon code (les erreurs internes partaient en 400 avec str(e)).
detection_bp.register_error_handler(Exception, api_error_response)
_socketio = None


def init_detection(socketio):
    global _socketio
    _socketio = socketio


@detection_bp.route('/api/detection/start', methods=['POST'])
def start_detection_route():
    """Demarre la detection avec les reglages ENREGISTRES.

    Le corps de la requete est ignore : l'UI envoyait son `window.settings`
    tel qu'au chargement de la page, et les modifications faites depuis
    (URL RTSP, seuils, sons coches...) n'etaient pas appliquees.
    """
    from classify import start_from_settings
    try:
        started, sources = start_from_settings(_socketio)
    except (ValueError, TypeError) as e:
        raise ApiError(f'Erreur dans les paramètres : {e}')
    if not sources:
        raise ApiError('Aucune source audio activée')
    if not started:
        raise ApiError('La détection est déjà en cours', 409)
    for s in sources:
        logging.info(f"Source activée: {s['label']}")
    source_display = ' + '.join(s['label'] for s in sources)
    _socketio.emit('detection_status', {'status': 'running', 'source': source_display})
    return jsonify({'success': True, 'source': source_display})


@detection_bp.route('/api/detection/stop', methods=['POST'])
def stop_detection_route():
    # detection_status 'stopped' est emis par la session a sa fin.
    stop_detection()
    return jsonify({'success': True})


@detection_bp.route('/status')
def status():
    from classify import get_status
    from settings_manager import is_degraded
    st = get_status()
    if is_degraded():
        st['settings_degraded'] = True   # l'interface le signale
    return jsonify(st)


@detection_bp.route('/api/detections/history', methods=['GET'])
def detection_history():
    return jsonify(get_detection_history())


@detection_bp.route('/api/detections/near_misses', methods=['GET'])
def detection_near_misses():
    """Gestes qui ont failli declencher (pic sans son reconnu, son reconnu
    sans pic assez fort, nombre de claps sans entite), 50 au plus."""
    from classify import get_near_misses
    return jsonify(get_near_misses())


@detection_bp.route('/api/detections/history', methods=['DELETE'])
def clear_detection_history_route():
    from classify import clear_detection_history
    clear_detection_history()
    return jsonify({'success': True})


FEEDBACK_STEP = 0.01   # seuil releve juste au-dessus du score observe
MAX_RAISED = 0.99


@detection_bp.route('/api/detections/<det_id>/feedback', methods=['POST'])
def detection_feedback(det_id):
    """« Ce n'etait pas ca » sur une detection de l'historique.

    Body: {action, label?}
    - ignore : decoche `label` dans le groupe qui a declenche ;
    - threshold : seuil du groupe juste au-dessus du score observe ;
    - negative : l'empreinte du son appris `label` devient un contre-exemple.
    """
    from classify import feedback_target, mark_feedback
    from routes.sources import _json, _require_source, _apply_live
    from settings_manager import modify_settings
    data = _json()
    action = data.get('action')
    label = data.get('label')
    entry, info = feedback_target(det_id)
    if entry is None:
        raise ApiError('détection introuvable (historique effacé ou add-on redémarré)', 404)
    if {'action': action, 'label': label} in (entry.get('feedback') or []):
        raise ApiError('déjà fait pour cette détection', 409)
    heard = {lab.get('label') for lab in entry.get('labels') or [] if isinstance(lab, dict)}
    result = {}

    def _group(settings):
        src = _require_source(settings, info['kind'], info['source_key'])
        group = next((g for g in src.get('sound_groups') or []
                      if isinstance(g, dict) and g.get('slug') == info['group_slug']), None)
        if group is None:
            raise ApiError('groupe introuvable (supprimé ?)', 404)
        return group

    if action == 'ignore':
        if not isinstance(label, str) or label not in heard:
            raise ApiError('label : un son de cette détection attendu')

        def _mut(settings):
            wl = _group(settings).setdefault('sound_whitelist', {})
            if wl.get(label) is not True:
                raise ApiError('ce son n\'est plus coché dans le groupe', 409)
            wl[label] = False
        modify_settings(_mut)
    elif action == 'threshold':
        score = float(entry.get('score') or 0.0)
        if score >= MAX_RAISED:
            raise ApiError('score déjà au maximum : aucun seuil ne l\'aurait écarté (décochez plutôt le son)', 409)
        new = round(min(MAX_RAISED, score + FEEDBACK_STEP), 2)

        def _mut(settings):
            group = _group(settings)
            if float(group.get('threshold') or 0.0) >= new:
                raise ApiError('le seuil du groupe est déjà à ce niveau ou au-dessus', 409)
            group['threshold'] = new
        modify_settings(_mut)
        result['threshold'] = new
    elif action == 'negative':
        import learned
        fp = info['evidence'].get(label) if isinstance(label, str) else None
        if fp is None:
            raise ApiError('label : un son appris entendu pendant cette détection attendu')
        try:
            result.update(learned.add_negative(label, fp))
        except learned.LearnError as e:
            raise ApiError(str(e), e.status)
    else:
        raise ApiError('action : « ignore », « threshold » ou « negative » attendu')
    if action != 'negative':
        _apply_live()
    result['feedback'] = mark_feedback(det_id, action, label)
    return jsonify(dict(result, success=True))
