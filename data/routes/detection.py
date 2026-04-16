from flask import Blueprint, jsonify, request
import logging

from classify import start_detection, stop_detection, is_running, get_current_source, get_detection_history
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

        # Vérifier la présence des sections requises
        if 'global' not in detection_settings or detection_settings['global'] is None:
            detection_settings['global'] = {'threshold': '0.2', 'delay': '1.0'}

        if 'microphone' not in detection_settings or detection_settings['microphone'] is None:
            detection_settings['microphone'] = {
                'enabled': False,
                'webhook_url': None,
                'audio_source': None,
                'device_index': '0'
            }

        # Vérifier si le microphone est activé
        microphone_enabled = detection_settings.get('microphone', {})
        if isinstance(microphone_enabled, dict):
            microphone_enabled = microphone_enabled.get('enabled', False)
        else:
            microphone_enabled = False

        if not microphone_enabled:
            logging.debug("Microphone désactivé - aucune capture audio ne sera effectuée")

        # Préparer les paramètres pour start_detection avec gestion des valeurs null
        try:
            global_settings = detection_settings.get('global', {})
            if not isinstance(global_settings, dict):
                global_settings = {}

            microphone_settings = detection_settings.get('microphone', {})
            if not isinstance(microphone_settings, dict):
                microphone_settings = {}

            from classify import build_sources_from_settings
            sources = build_sources_from_settings(detection_settings)
            for s in sources:
                logging.info(f"Source activée: {s['label']}")

            if not sources:
                return jsonify({'error': 'Aucune source audio activée'}), 400

            detection_params = {
                'model': "yamnet.tflite",
                'max_results': 10,
                'score_threshold': float(global_settings.get('threshold', 0.5)),
                'overlapping_factor': 0.8,
                'socketio': _socketio,
                'delay': float(global_settings.get('delay', 1.0)),
                'sources': sources,
                'peak_cooldown': float(global_settings.get('peak_cooldown', 0.08)),
                'peak_ratio': float(global_settings.get('peak_ratio', 3.0)),
                'peak_reset': float(global_settings.get('peak_reset', 0.3)),
            }

        except (ValueError, TypeError) as e:
            return jsonify({'error': f'Erreur dans les paramètres : {str(e)}'}), 400

        # Démarrer la détection multi-source
        if start_detection(**detection_params):
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
