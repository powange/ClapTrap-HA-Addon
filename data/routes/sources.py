from flask import Blueprint, jsonify, request
import logging
import threading
import uuid

from settings_manager import load_settings, save_settings
from audio_utils import get_audio_input_devices
from vban_manager import get_vban_detector

sources_bp = Blueprint('sources', __name__)
_socketio = None
_restart_lock = threading.Lock()

def _source_id_for(kind, source_key):
    """Donne l'ID runtime (utilise par classify) a partir de (kind, source_key).

    Doit coller EXACTEMENT a ce que classify.run_*_source construit :
    - mic  : f"mic_{device_index}"
    - rtsp : f"rtsp_{src['url']}"  (URL brute, sans reparer le prefix)
    - vban : f"vban_{ip}"
    """
    if kind == 'mic':
        return f"mic_{source_key}"
    if kind == 'rtsp':
        settings = load_settings()
        for s in settings.get('rtsp_sources', []):
            if s.get('id') == source_key:
                return f"rtsp_{s.get('url', '')}"
        return None
    if kind == 'vban':
        return f"vban_{source_key}"
    return None


def init_sources(socketio):
    global _socketio
    _socketio = socketio


def _restart_detection_if_running():
    """Redémarre la détection avec les sources mises à jour si elle tourne."""
    with _restart_lock:
        try:
            from classify import is_running, stop_detection, start_detection, build_sources_from_settings
            if not is_running():
                return
            stop_detection()
            import time
            time.sleep(0.5)  # Laisser les threads se terminer
            settings = load_settings()
            sources = build_sources_from_settings(settings)
            if sources and _socketio:
                global_s = settings.get('global', {})
                start_detection(
                    model="yamnet.tflite", max_results=10,
                    score_threshold=float(global_s.get('threshold', 0.5)),
                    overlapping_factor=0.8, socketio=_socketio,
                    delay=float(global_s.get('delay', 1.5)), sources=sources,
                    peak_cooldown=float(global_s.get('peak_cooldown', 0.08)),
                    peak_ratio=float(global_s.get('peak_ratio', 3.0))
                )
                source_display = ' + '.join(s['label'] for s in sources)
                _socketio.emit('detection_status', {'status': 'running', 'source': source_display})
                logging.info(f"Détection redémarrée avec: {source_display}")
            else:
                _socketio.emit('detection_status', {'status': 'stopped'})
                logging.info("Détection arrêtée: aucune source active")
        except Exception as e:
            logging.error(f"Erreur redémarrage détection: {e}")


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
                    # Redémarrer la détection si elle tourne
                    _restart_detection_if_running()
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
                    # Recréer les entités MQTT immédiatement
                    try:
                        from ha_entities import register_source, source_entity_key
                        entity_id = source_entity_key('rtsp', {'id': stream_id})
                        register_source(entity_id,
                                       label=f"RTSP: {stream.get('name', 'RTSP')}",
                                       clap_counts=data['ha_entities'])
                    except Exception:
                        pass

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
            from ha_entities import unregister_source, source_entity_key
            entity_id = source_entity_key('rtsp', {'id': stream_id})
            unregister_source(entity_id)
        except Exception:
            pass

        # Filtrer pour retirer le stream spécifié
        settings['rtsp_sources'] = [s for s in rtsp_sources if s.get('id') != stream_id]
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
                # Mettre à jour gain s'il est fourni (live sans redémarrage)
                if 'gain' in source:
                    s['gain'] = float(source['gain'])
                    try:
                        from classify import update_vban_gain
                        update_vban_gain(s['ip'], s['gain'])
                    except Exception:
                        pass
                # Mettre à jour ha_entities s'il est fourni
                if 'ha_entities' in source:
                    s['ha_entities'] = source['ha_entities']
                    try:
                        from ha_entities import register_source, source_entity_key
                        register_source(source_entity_key('vban', s),
                                       label=f"VBAN: {s.get('name', 'VBAN')}",
                                       clap_counts=source['ha_entities'])
                    except Exception:
                        pass
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
            # Redemarrer la detection si 'enabled' ou 'threshold' a change,
            # pour que la source soit effectivement (re)ajoutee ou retiree.
            if 'enabled' in source or 'threshold' in source:
                _restart_detection_if_running()
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


@sources_bp.route('/api/source/sound_whitelist', methods=['DELETE'])
def delete_source_sound_whitelist_entry():
    """Retire un label de la sound_whitelist d'une source.

    Body: {kind, source_key, label}
    """
    try:
        data = request.get_json() or {}
        kind = data.get('kind')
        source_key = data.get('source_key')
        label = data.get('label')
        if kind not in ('mic', 'rtsp', 'vban') or not label:
            return jsonify({'error': 'kind / label requis'}), 400

        settings = load_settings()
        target = None
        if kind == 'mic':
            target = settings.setdefault('microphone', {}).setdefault('sound_whitelist', {})
        elif kind == 'rtsp':
            for s in settings.get('rtsp_sources', []):
                if s.get('id') == source_key:
                    target = s.setdefault('sound_whitelist', {})
                    break
        elif kind == 'vban':
            for s in settings.get('saved_vban_sources', []):
                if s.get('ip') == source_key:
                    target = s.setdefault('sound_whitelist', {})
                    break
        if target is None:
            return jsonify({'error': 'source introuvable'}), 404

        target.pop(label, None)
        save_settings(settings)

        source_id = _source_id_for(kind, source_key)
        if source_id:
            try:
                from classify import remove_source_whitelist_entry
                remove_source_whitelist_entry(source_id, label)
            except Exception:
                pass
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@sources_bp.route('/api/source/sound_whitelist', methods=['PUT'])
def update_source_sound_whitelist():
    """Active/desactive un label dans la sound_whitelist d'une source.

    Body: {kind: 'mic'|'rtsp'|'vban', source_key: str, label: str, enabled: bool}
    Applique en live sur le detecteur si la detection tourne.
    """
    try:
        data = request.get_json() or {}
        kind = data.get('kind')
        source_key = data.get('source_key')
        label = data.get('label')
        enabled = bool(data.get('enabled', False))
        if kind not in ('mic', 'rtsp', 'vban') or not label:
            return jsonify({'error': 'kind / label requis'}), 400

        settings = load_settings()
        target = None
        if kind == 'mic':
            target = settings.setdefault('microphone', {}).setdefault('sound_whitelist', {})
        elif kind == 'rtsp':
            for s in settings.get('rtsp_sources', []):
                if s.get('id') == source_key:
                    target = s.setdefault('sound_whitelist', {})
                    break
        elif kind == 'vban':
            for s in settings.get('saved_vban_sources', []):
                if s.get('ip') == source_key:
                    target = s.setdefault('sound_whitelist', {})
                    break
        if target is None:
            return jsonify({'error': 'source introuvable'}), 404

        target[label] = enabled
        save_settings(settings)

        # Mise a jour live du detecteur si la detection tourne
        source_id = _source_id_for(kind, source_key)
        if source_id:
            try:
                from classify import update_source_whitelist
                update_source_whitelist(source_id, label, enabled)
            except Exception:
                pass
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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

        # Redémarrer la détection si elle tourne
        _restart_detection_if_running()

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
                from audio_utils import set_pulse_volume
                set_pulse_volume(pulse_name, volume)
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

        settings = load_settings()
        settings['microphone']['ha_entities'] = ha_entities
        save_settings(settings)

        # Recréer les entités MQTT immédiatement
        try:
            from ha_entities import register_source, source_entity_key
            mic = settings.get('microphone', {})
            register_source(source_entity_key('mic', mic),
                           label=mic.get('audio_source', 'Microphone'),
                           clap_counts=ha_entities)
        except Exception:
            pass

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
