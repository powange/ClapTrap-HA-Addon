from flask import Blueprint, jsonify, request
import logging

from classify import stop_detection, is_running, get_current_source, get_detection_history
from settings_manager import load_settings

detection_bp = Blueprint('detection', __name__)
_socketio = None

def init_detection(socketio):
    global _socketio
    _socketio = socketio


@detection_bp.route('/api/detection/start', methods=['POST'])
def start_detection_route():
    try:
        detection_settings = request.json
        if not detection_settings:
            return jsonify({'error': 'Aucun paramètre fourni'}), 400

        # Compléter avec les settings sauvegardés (le frontend n'envoie pas toujours les RTSP/VBAN)
        saved = load_settings()
        if not detection_settings.get('rtsp_sources'):
            detection_settings['rtsp_sources'] = saved.get('rtsp_sources', [])
        if not detection_settings.get('saved_vban_sources'):
            detection_settings['saved_vban_sources'] = saved.get('saved_vban_sources', [])

        # Sections absentes du payload : reprendre celles enregistrees.
        if not isinstance(detection_settings.get('global'), dict):
            detection_settings['global'] = saved.get('global', {})
        if not isinstance(detection_settings.get('microphone'), dict):
            detection_settings['microphone'] = {'enabled': False}

        # Démarrer la détection multi-source
        from classify import start_from_settings
        try:
            started, sources = start_from_settings(_socketio, detection_settings)
        except (ValueError, TypeError) as e:
            return jsonify({'error': f'Erreur dans les paramètres : {str(e)}'}), 400
        if not sources:
            return jsonify({'error': 'Aucune source audio activée'}), 400
        for s in sources:
            logging.info(f"Source activée: {s['label']}")
        if started:
            source_labels = [s['label'] for s in sources]
            source_display = ' + '.join(source_labels)
            _socketio.emit('detection_status', {'status': 'running', 'source': source_display})
            return jsonify({'success': True, 'source': source_display})
        else:
            return jsonify({'error': 'Impossible de démarrer la détection'}), 400

    except Exception as e:
        logging.error(f"Erreur lors du démarrage de la détection: {str(e)}")
        return jsonify({'error': str(e)}), 400


@detection_bp.route('/api/detection/stop', methods=['POST'])
def stop_detection_route():
    try:
        # Arrêter la détection
        if stop_detection():
            # Émettre un événement de statut avant d'arrter
            _socketio.emit('detection_status', {'status': 'stopped'})
            return jsonify({'success': True})
        else:
            return jsonify({'error': 'Impossible d\'arrêter la détection'}), 400
    except Exception as e:
        logging.error(f"Erreur lors de l'arrêt de la détection: {str(e)}")
        return jsonify({'error': str(e)}), 400


@detection_bp.route('/status')
def status():
    try:
        running = is_running()
        source = get_current_source() if running else None
        return jsonify({'running': running, 'source': source})
    except Exception as e:
        return jsonify({'running': False, 'error': str(e)})


@detection_bp.route('/api/detections/history', methods=['GET'])
def detection_history():
    return jsonify(get_detection_history())
