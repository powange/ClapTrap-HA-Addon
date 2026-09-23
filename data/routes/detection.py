from flask import Blueprint, jsonify, request
import logging

from classify import stop_detection, is_running, get_current_source, get_detection_history

detection_bp = Blueprint('detection', __name__)
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
    try:
        from classify import start_from_settings
        try:
            started, sources = start_from_settings(_socketio)
        except (ValueError, TypeError) as e:
            return jsonify({'error': f'Erreur dans les paramètres : {str(e)}'}), 400
        if not sources:
            return jsonify({'error': 'Aucune source audio activée'}), 400
        for s in sources:
            logging.info(f"Source activée: {s['label']}")
        if not started:
            return jsonify({'error': 'Impossible de démarrer la détection'}), 400
        source_display = ' + '.join(s['label'] for s in sources)
        _socketio.emit('detection_status', {'status': 'running', 'source': source_display})
        return jsonify({'success': True, 'source': source_display})

    except Exception as e:
        logging.error(f"Erreur lors du démarrage de la détection: {str(e)}")
        return jsonify({'error': str(e)}), 400


@detection_bp.route('/api/detection/stop', methods=['POST'])
def stop_detection_route():
    try:
        # Arrêter la détection
        if stop_detection():
            # detection_status 'stopped' est emis par la session a sa fin
            # (l'emettre aussi ici le doublait).
            return jsonify({'success': True})
        else:
            return jsonify({'error': 'Impossible d\'arrêter la détection'}), 400
    except Exception as e:
        logging.error(f"Erreur lors de l'arrêt de la détection: {str(e)}")
        return jsonify({'error': str(e)}), 400


@detection_bp.route('/status')
def status():
    try:
        from classify import get_status
        return jsonify(get_status())
    except Exception as e:
        return jsonify({'running': False, 'error': str(e)})


@detection_bp.route('/api/detections/history', methods=['GET'])
def detection_history():
    return jsonify(get_detection_history())


@detection_bp.route('/api/detections/history', methods=['DELETE'])
def clear_detection_history_route():
    from classify import clear_detection_history
    clear_detection_history()
    return jsonify({'success': True})
