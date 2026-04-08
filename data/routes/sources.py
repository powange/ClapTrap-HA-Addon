from flask import Blueprint, jsonify, request
import logging
import uuid

from settings_manager import load_settings, save_settings
from audio_utils import get_audio_input_devices
from vban_manager import get_vban_detector

sources_bp = Blueprint('sources', __name__)
_socketio = None

def init_sources(socketio):
    global _socketio
    _socketio = socketio


def _resolve_pulse_name(settings):
    """Résout le pulse_name depuis les settings ou l'API Supervisor.
    Vérifie toujours que le pulse_name correspond à l'audio_source actuel."""
    audio_source = settings.get('microphone', {}).get('audio_source', '')
    if not audio_source or audio_source == 'default':
        # Pas de source spécifique, effacer le pulse_name stale
        if settings.get('microphone', {}).get('pulse_name'):
            settings['microphone']['pulse_name'] = ''
            save_settings(settings)
        return ''

    # Vérifier si le pulse_name en cache correspond au device actuel
    cached_pulse_name = settings.get('microphone', {}).get('pulse_name', '')
    if cached_pulse_name:
        # Valider que le device correspond toujours
        devices = get_audio_input_devices()
        for dev in devices:
            if dev.get('name') == audio_source and dev.get('pulse_name') == cached_pulse_name:
                return cached_pulse_name
        # pulse_name stale : ne correspond plus au device sélectionné
        logging.warning(f"pulse_name '{cached_pulse_name}' ne correspond plus à '{audio_source}', re-résolution...")

    # Résoudre depuis l'API Supervisor
    devices = get_audio_input_devices()
    for dev in devices:
        if dev.get('name') == audio_source and dev.get('pulse_name'):
            settings['microphone']['pulse_name'] = dev['pulse_name']
            save_settings(settings)
            logging.info(f"pulse_name résolu et sauvegardé: {dev['pulse_name']}")
            return dev['pulse_name']

    logging.warning(f"Impossible de résoudre pulse_name pour '{audio_source}'")
    return ''


@sources_bp.route('/api/audio-sources', methods=['GET'])
def get_audio_sources():
    try:
        audio_sources = [
            {**device, 'type': 'microphone'}
            for device in get_audio_input_devices()
        ]
        return jsonify(audio_sources)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@sources_bp.route('/api/rtsp/streams', methods=['GET'])
def get_rtsp_streams():
    try:
        settings = load_settings()
        return jsonify(settings.get('rtsp_sources', []))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@sources_bp.route('/api/rtsp/stream', methods=['POST'])
def add_rtsp_stream():
    try:
        data = request.get_json()
        url = data.get('url', '')
        name = data.get('name', '')
        webhook_url = data.get('webhook_url', '')
        enabled = data.get('enabled', True)

        settings = load_settings()
        if 'rtsp_sources' not in settings:
            settings['rtsp_sources'] = []

        # Générer un ID unique pour le stream
        stream_id = str(uuid.uuid4())

        new_stream = {
            'id': stream_id,
            'name': name,
            'url': url,
            'webhook_url': webhook_url,
            'enabled': enabled,
            'gain': int(data.get('gain', 10)),
            'threshold': float(data.get('threshold', 0.5))
        }

        settings['rtsp_sources'].append(new_stream)
        save_settings(settings)

        return jsonify({'success': True, 'stream': new_stream})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@sources_bp.route('/api/rtsp/stream/<stream_id>', methods=['PUT'])
def update_rtsp_stream(stream_id):
    try:
        data = request.get_json()
        settings = load_settings()

        for stream in settings.get('rtsp_sources', []):
            if stream.get('id') == stream_id:
                # Mettre à jour les champs fournis
                if 'url' in data:
                    stream['url'] = data['url']
                if 'name' in data:
                    stream['name'] = data['name']
                if 'webhook_url' in data:
                    stream['webhook_url'] = data['webhook_url']
                if 'enabled' in data:
                    stream['enabled'] = data['enabled']
                if 'threshold' in data:
                    stream['threshold'] = float(data['threshold'])
                if 'gain' in data:
                    stream['gain'] = int(data['gain'])
                    # Appliquer le gain en temps réel si la détection tourne
                    try:
                        from classify import update_rtsp_gain
                        url = stream.get('url', '')
                        rtsp_url = url if url.startswith('rtsp') else f"rtsp://{url}"
                        update_rtsp_gain(rtsp_url, stream['gain'])
                    except Exception:
                        pass
                if 'ha_entities' in data:
                    stream['ha_entities'] = data['ha_entities']

                save_settings(settings)
                return jsonify({'success': True, 'stream': stream})

        return jsonify({'error': 'Stream non trouvé'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@sources_bp.route('/api/rtsp/stream/<stream_id>', methods=['DELETE'])
def delete_rtsp_stream(stream_id):
    try:
        settings = load_settings()
        rtsp_sources = settings.get('rtsp_sources', [])

        # Supprimer les entites HA liees
        try:
            from ha_entities import unregister_source
            entity_id = f"rtsp_{stream_id[:8]}" if stream_id else None
            if entity_id:
                unregister_source(entity_id)
        except Exception:
            pass

        # Filtrer pour retirer le stream spécifié
        settings['rtsp_sources'] = [s for s in rtsp_sources if s.get('id') != stream_id]
        save_settings(settings)

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@sources_bp.route('/api/rtsp/webhook', methods=['PUT'])
def update_rtsp_webhook():
    try:
        data = request.get_json()
        stream_id = data.get('stream_id')
        webhook_url = data.get('webhook_url')

        settings = load_settings()
        for stream in settings.get('rtsp_sources', []):
            if stream.get('id') == stream_id:
                stream['webhook_url'] = webhook_url
                break
        save_settings(settings)

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@sources_bp.route('/api/rtsp/enabled', methods=['PUT'])
def update_rtsp_enabled():
    try:
        data = request.get_json()
        stream_id = data.get('stream_id')
        enabled = data.get('enabled')

        settings = load_settings()
        for stream in settings.get('rtsp_sources', []):
            if stream.get('id') == stream_id:
                stream['enabled'] = enabled
                break
        save_settings(settings)

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@sources_bp.route('/api/vban/sources', methods=['GET'])
def get_vban_sources():
    try:
        # S'assurer que la découverte VBAN est initialisée
        detector = get_vban_detector()
        if not detector:
            return jsonify({'error': 'Impossible d\'initialiser la découverte VBAN'}), 500

        # Utiliser la nouvelle méthode thread-safe pour obtenir les sources
        active_sources = detector.get_sources(timeout=1.0)

        logging.debug(f"Sources VBAN actives trouvées: {len(active_sources)}")
        logging.debug(f"Sources formatées: {active_sources}")
        return jsonify(active_sources)

    except Exception as e:
        logging.error(f"Erreur lors de la récupération des sources VBAN: {str(e)}")
        return jsonify({'error': str(e)}), 500


@sources_bp.route('/api/vban/saved-sources', methods=['GET'])
def get_saved_vban_sources():
    try:
        settings = load_settings()
        saved_sources = settings.get('saved_vban_sources', [])
        logging.debug(f"Sources VBAN sauvegardées trouvées: {len(saved_sources)}")  # Debug log
        logging.debug(f"Sources: {saved_sources}")  # Debug log
        return jsonify(saved_sources)
    except Exception as e:
        logging.error(f"Erreur lors de la récupération des sources VBAN sauvegardées: {str(e)}")
        return jsonify({'error': str(e)}), 500


@sources_bp.route('/api/vban/save', methods=['POST'])
def save_vban_source():
    try:
        source = request.json
        logging.debug(f"Réception demande d'ajout source VBAN: {source}")  # Debug log

        # Valider les données requises
        required_fields = ['name', 'ip', 'port']
        if not all(field in source for field in required_fields):
            logging.debug(f"Champs manquants. Reçu: {source}")  # Debug log
            return jsonify({
                'success': False,
                'error': 'Données manquantes pour la source VBAN'
            }), 400

        # Charger les paramètres actuels
        settings = load_settings()

        # Initialiser la liste si elle n'existe pas
        if 'saved_vban_sources' not in settings:
            settings['saved_vban_sources'] = []

        # Vérifier si la source existe déjà
        existing_source = next(
            (s for s in settings['saved_vban_sources']
             if s['ip'] == source['ip'] and s['name'] == source['name']),
            None
        )

        if existing_source:
            logging.debug(f"Source déjà existante: {existing_source}")  # Debug log
            return jsonify({
                'success': False,
                'error': 'Cette source VBAN existe déjà'
            }), 400

        # Ajouter la nouvelle source
        new_source = {
            'name': source['name'],
            'ip': source['ip'],
            'port': source['port'],
            'stream_name': source['name'],  # Utiliser le nom comme stream_name
            'webhook_url': source.get('webhook_url', ''),
            'enabled': source.get('enabled', True)
        }

        settings['saved_vban_sources'].append(new_source)

        # Sauvegarder immédiatement les paramètres
        success, message = save_settings(settings)

        if success:
            logging.debug(f"Source VBAN sauvegardée avec succès: {new_source}")  # Debug log
            return jsonify({
                'success': True,
                'source': new_source
            })
        else:
            logging.error(f"Erreur lors de la sauvegarde des paramètres: {message}")  # Debug log
            return jsonify({
                'success': False,
                'error': f"Erreur lors de la sauvegarde: {message}"
            }), 500

    except Exception as e:
        logging.error(f"Erreur lors de la sauvegarde de la source VBAN: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@sources_bp.route('/api/vban/remove', methods=['DELETE'])
def remove_vban_source():
    try:
        data = request.json
        if not data or 'ip' not in data or 'stream_name' not in data:
            return jsonify({
                'success': False,
                'error': 'Données manquantes'
            }), 400

        settings = load_settings()

        if 'saved_vban_sources' not in settings:
            return jsonify({
                'success': False,
                'error': 'Aucune source VBAN configurée'
            }), 404

        # Filtrer la source à supprimer
        initial_count = len(settings['saved_vban_sources'])
        settings['saved_vban_sources'] = [
            s for s in settings['saved_vban_sources']
            if not (s['ip'] == data['ip'] and s['stream_name'] == data['stream_name'])
        ]

        if len(settings['saved_vban_sources']) == initial_count:
            return jsonify({
                'success': False,
                'error': 'Source non trouvée'
            }), 404

        # Sauvegarder les modifications
        success, message = save_settings(settings)

        if success:
            return jsonify({'success': True})
        else:
            return jsonify({
                'success': False,
                'error': message
            }), 500

    except Exception as e:
        logging.error(f"Erreur lors de la suppression de la source VBAN: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@sources_bp.route('/api/vban/update', methods=['PUT'])
def update_vban_source():
    try:
        source = request.json
        logging.debug(f"Mise à jour source VBAN reçue: {source}")  # Debug log

        if not source or 'ip' not in source or 'name' not in source:
            return jsonify({
                'success': False,
                'error': 'Données manquantes'
            }), 400

        settings = load_settings()

        if 'saved_vban_sources' not in settings:
            settings['saved_vban_sources'] = []

        # Trouver et mettre à jour la source
        source_found = False
        for s in settings['saved_vban_sources']:
            if s['ip'] == source['ip'] and s['name'] == source['name']:
                # Mettre à jour le webhook_url s'il est fourni
                if 'webhook_url' in source:
                    s['webhook_url'] = source['webhook_url']
                # Mettre à jour enabled s'il est fourni
                if 'enabled' in source:
                    s['enabled'] = source['enabled']
                # Mettre à jour threshold s'il est fourni
                if 'threshold' in source:
                    s['threshold'] = float(source['threshold'])
                # Mettre à jour ha_entities s'il est fourni
                if 'ha_entities' in source:
                    s['ha_entities'] = source['ha_entities']
                source_found = True
                logging.debug(f"Source mise à jour: {s}")  # Debug log
                break

        if not source_found:
            logging.debug(f"Source non trouvée. Sources existantes: {settings['saved_vban_sources']}")  # Debug log
            return jsonify({
                'success': False,
                'error': 'Source non trouvée'
            }), 404

        # Sauvegarder les modifications
        success, message = save_settings(settings)

        if success:
            return jsonify({'success': True})
        else:
            return jsonify({
                'success': False,
                'error': message
            }), 500

    except Exception as e:
        logging.error(f"Erreur lors de la mise à jour de la source VBAN: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@sources_bp.route('/refresh_vban_sources')
def refresh_vban_sources():
    try:
        detector = get_vban_detector()
        if not detector:
            return jsonify({"sources": []})

        active_sources = detector.get_sources(timeout=1.0)
        return jsonify({"sources": active_sources})
    except Exception as e:
        logging.error(f"Erreur lors du rafraîchissement des sources VBAN: {e}")
        return jsonify({"sources": [], "error": str(e)}), 500


@sources_bp.route('/api/microphone/webhook', methods=['PUT'])
def update_microphone_webhook():
    try:
        data = request.get_json()
        webhook_url = data.get('webhook_url')

        settings = load_settings()
        settings['microphone']['webhook_url'] = webhook_url
        save_settings(settings)

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@sources_bp.route('/api/microphone/threshold', methods=['PUT'])
def update_microphone_threshold():
    try:
        data = request.get_json()
        threshold = float(data.get('threshold', 0.5))
        threshold = max(0.0, min(1.0, threshold))

        settings = load_settings()
        settings['microphone']['threshold'] = threshold
        save_settings(settings)

        return jsonify({'success': True, 'threshold': threshold})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@sources_bp.route('/api/microphone/enabled', methods=['PUT'])
def update_microphone_enabled():
    try:
        data = request.get_json()
        enabled = data.get('enabled')

        settings = load_settings()
        settings['microphone']['enabled'] = enabled
        save_settings(settings)

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@sources_bp.route('/api/microphone/auto-start', methods=['PUT'])
def toggle_auto_start():
    try:
        data = request.get_json()
        enabled = bool(data.get('enabled', False))
        settings = load_settings()
        settings['microphone']['auto_start'] = enabled
        save_settings(settings)
        return jsonify({'success': True, 'auto_start': enabled})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@sources_bp.route('/api/microphone/volume', methods=['PUT'])
def update_microphone_volume():
    try:
        from auto_volume import auto_volume_mgr
        if auto_volume_mgr.running:
            return jsonify({'error': 'Auto-volume actif, desactivez-le pour regler manuellement'}), 400

        data = request.get_json()
        volume = int(data.get('volume', 100))
        volume = max(0, min(150, volume))

        settings = load_settings()
        settings['microphone']['volume'] = volume
        save_settings(settings)

        # Utiliser le pulse_name du frontend si fourni, sinon celui sauvegardé
        pulse_name = data.get('pulse_name') or settings.get('microphone', {}).get('pulse_name', '')
        if pulse_name:
            try:
                import subprocess
                subprocess.run(['pactl', 'set-source-volume', pulse_name, f'{volume}%'],
                               capture_output=True, text=True, timeout=5)
                logging.info(f"Volume PulseAudio mis à {volume}% pour {pulse_name}")
            except Exception as e:
                logging.warning(f"Impossible de régler le volume PulseAudio: {e}")

        return jsonify({'success': True, 'volume': volume})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@sources_bp.route('/api/microphone/ha-entities', methods=['PUT'])
def update_microphone_ha_entities():
    try:
        data = request.get_json()
        ha_entities = data.get('ha_entities', [1, 2])
        # Validate: keep only 1-4
        ha_entities = [n for n in ha_entities if isinstance(n, int) and 1 <= n <= 4]
        if not ha_entities:
            ha_entities = [1, 2]

        settings = load_settings()
        settings['microphone']['ha_entities'] = ha_entities
        save_settings(settings)

        return jsonify({'success': True, 'ha_entities': ha_entities})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@sources_bp.route('/api/microphone/auto-volume', methods=['PUT'])
def toggle_auto_volume():
    try:
        data = request.get_json()
        enabled = bool(data.get('enabled', False))

        settings = load_settings()
        settings['microphone']['auto_volume'] = enabled
        save_settings(settings)

        from auto_volume import auto_volume_mgr
        if enabled:
            # Utiliser le pulse_name du frontend si fourni
            pulse_name = data.get('pulse_name') or _resolve_pulse_name(settings)
            if pulse_name:
                auto_volume_mgr.start(pulse_name, _socketio)
            else:
                return jsonify({'error': 'Aucun device PulseAudio trouve'}), 400
        else:
            auto_volume_mgr.stop()

        return jsonify({'success': True, 'auto_volume': enabled})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
