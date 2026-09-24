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


@detection_bp.route('/api/detections/history', methods=['DELETE'])
def clear_detection_history_route():
    from classify import clear_detection_history
    clear_detection_history()
    return jsonify({'success': True})
