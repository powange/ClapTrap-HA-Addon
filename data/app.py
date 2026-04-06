from flask import Flask, jsonify, request, render_template, send_from_directory
from flask_socketio import SocketIO
from classify import start_detection, stop_detection, is_running
import sounddevice as sd
import json
import requests
from vban_manager import init_vban_detector as init_vban, cleanup_vban_detector, get_vban_detector
import threading
import time
import os
from datetime import datetime
from events import socketio  # Importation de l'instance Socket.IO
import socket
import uuid
from threading import Lock
import logging
import psutil
import secrets
from webhook import WebhookManager

# Configuration du logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Réduire le niveau de log des modules trop verbeux
logging.getLogger('werkzeug').setLevel(logging.WARNING)
logging.getLogger('engineio').setLevel(logging.WARNING)
logging.getLogger('socketio').setLevel(logging.WARNING)
logging.getLogger('engineio.server').setLevel(logging.WARNING)
logging.getLogger('socketio.server').setLevel(logging.WARNING)

class IngressMiddleware:
    """Middleware WSGI pour gérer le préfixe de chemin ingress de Home Assistant."""
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        ingress_path = environ.get('HTTP_X_INGRESS_PATH', '')
        if ingress_path:
            environ['SCRIPT_NAME'] = ingress_path
        return self.app(environ, start_response)

# Configurer Flask pour qu'il soit moins verbeux
app = Flask(__name__)
app.wsgi_app = IngressMiddleware(app.wsgi_app)
app.logger.setLevel(logging.WARNING)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
socketio = SocketIO(app, 
    cors_allowed_origins="*",
    logger=False,  
    engineio_logger=False,  
    ping_timeout=60,
    ping_interval=25,
    async_mode='threading'
)

# Définir le chemin absolu du dossier de l'application
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, 'settings.json')
SETTINGS_BACKUP = os.path.join(BASE_DIR, 'settings.json.backup')
SETTINGS_TEMP = os.path.join(BASE_DIR, 'settings.json.tmp')

def get_audio_input_devices():
    """Récupère les périphériques audio d'entrée via l'API Supervisor HA, avec fallback sur sounddevice."""
    # Essayer l'API Supervisor de Home Assistant
    supervisor_token = os.environ.get('SUPERVISOR_TOKEN')
    if supervisor_token:
        try:
            resp = requests.get(
                'http://supervisor/audio/info',
                headers={'Authorization': f'Bearer {supervisor_token}'},
                timeout=5
            )
            if resp.ok:
                data = resp.json()
                sources = data.get('data', {}).get('audio', {}).get('input', [])
                if sources:
                    return [
                        {'index': source.get('index', idx), 'name': source.get('description', source.get('name', f'Device {idx}'))}
                        for idx, source in enumerate(sources)
                    ]
        except Exception as e:
            logging.warning(f"Impossible de récupérer les sources audio via l'API Supervisor: {e}")

    # Fallback sur sounddevice
    try:
        all_devices = sd.query_devices()
        return [
            {'index': idx, 'name': device['name']}
            for idx, device in enumerate(all_devices)
            if device['max_input_channels'] > 0
        ]
    except Exception as e:
        logging.error(f"Impossible de lister les périphériques audio: {e}")
        return []

# Initialiser le détecteur VBAN
init_vban()

@app.before_request
def before_request():
    """S'assure que le détecteur VBAN est actif avant chaque requête"""
    init_vban()

# Nettoyer lors de l'arrêt
import atexit

@atexit.register
def cleanup():
    """Nettoie les ressources lors de l'arrêt"""
    cleanup_vban_detector()

class VBANSource:
    def __init__(self, name, ip, port, stream_name, webhook_url, enabled=True):
        self.name = name
        self.ip = ip
        self.port = port
        self.stream_name = stream_name
        self.webhook_url = webhook_url
        self.enabled = enabled

    def to_dict(self):
        return {
            "name": self.name,
            "ip": self.ip,
            "port": self.port,
            "stream_name": self.stream_name,
            "webhook_url": self.webhook_url,
            "enabled": self.enabled
        }

    @staticmethod
    def from_dict(data):
        return VBANSource(
            name=data.get("name", ""),
            ip=data.get("ip", ""),
            port=data.get("port", 6980),
            stream_name=data.get("stream_name", ""),
            webhook_url=data.get("webhook_url", ""),
            enabled=data.get("enabled", True)
        )

class Settings:
    def __init__(self):
        self.lock = Lock()
    
    def save_settings(self, new_settings):
        with self.lock:
            with open('settings.json', 'w') as f:
                json.dump(new_settings, f, indent=4)

def save_settings(new_settings):
    """Sauvegarde les paramètres avec une gestion d'erreurs améliorée"""
    try:
        # Charger les paramètres existants ou utiliser la structure par défaut
        default_settings = {
            "global": {
                "threshold": "0.5",
                "delay": "1.0"
            },
            "microphone": {
                "device_index": "0",
                "audio_source": "default",
                "webhook_url": "",
                "enabled": False
            },
            "rtsp_sources": [],
            "saved_vban_sources": [],  # Ajout de la liste des sources VBAN sauvegardées
            "vban": {
                "stream_name": "",
                "ip": "0.0.0.0",
                "port": 6980,
                "webhook_url": "",
                "enabled": False
            }
        }

        # Charger les paramètres existants s'ils existent
        current_settings = default_settings.copy()
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r') as f:
                current_settings.update(json.load(f))

        # Préserver les sources RTSP existantes si elles ne sont pas dans les nouveaux paramètres
        if 'rtsp_sources' not in new_settings:
            new_settings['rtsp_sources'] = current_settings.get('rtsp_sources', [])

        # Mettre à jour avec les nouveaux paramètres
        current_settings.update(new_settings)

        # Sauvegarder dans un fichier temporaire d'abord
        with open(SETTINGS_TEMP, 'w') as f:
            json.dump(current_settings, f, indent=4)

        # Faire une sauvegarde de l'ancien fichier si nécessaire
        if os.path.exists(SETTINGS_FILE):
            os.replace(SETTINGS_FILE, SETTINGS_BACKUP)

        # Renommer le fichier temporaire
        os.replace(SETTINGS_TEMP, SETTINGS_FILE)

        return True, "Paramètres sauvegardés avec succès"

    except Exception as e:
        return False, f"Erreur lors de la sauvegarde des paramètres: {str(e)}"

def load_flux():
    try:
        with open('flux.json', 'r') as f:
            flux = json.load(f)
            print("Flux chargés :", flux)  # Ajout d'un log pour le débogage
            return flux
    except FileNotFoundError:
        return {"audio_streams": []}

def load_settings():
    """Charge les paramètres avec gestion d'erreurs améliorée"""
    default_settings = {
        "global": {
            "threshold": "0.5",
            "delay": "1.0"
        },
        "microphone": {
            "device_index": "0",
            "audio_source": "default",
            "webhook_url": "",
            "enabled": False
        },
        "rtsp_sources": [],
        "saved_vban_sources": [],
        "vban": {
            "stream_name": "",
            "ip": "0.0.0.0",
            "port": 6980,
            "webhook_url": "",
            "enabled": False
        }
    }
    
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r') as f:
                settings = json.load(f)
                
            # Fusionner récursivement les paramètres par défaut avec les paramètres sauvegardés
            def deep_merge(default, saved):
                merged = default.copy()
                for key, value in saved.items():
                    if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
                        merged[key] = deep_merge(merged[key], value)
                    else:
                        merged[key] = value
                return merged
                
            merged_settings = deep_merge(default_settings, settings)
            return merged_settings
            
    except Exception as e:
        print(f"Erreur lors du chargement des paramètres: {str(e)}")
        
    # En cas d'erreur ou si le fichier n'existe pas, créer avec les paramètres par défaut
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(default_settings, f, indent=4)
    
    return default_settings

@app.route('/')
def index():
    settings = load_settings()  # Charge les paramètres depuis le fichier JSON
    input_devices = get_audio_input_devices()  # Obtient la liste des périphériques audio d'entrée
    flux = load_flux()
    
    # Échapper correctement le JSON pour JavaScript
    settings_json = json.dumps(settings).replace("'", "\\'").replace('"', '\\"')
    
    return render_template('index.html',
                         settings=settings,
                         devices=input_devices,
                         flux=flux['audio_streams'],
                         debug=app.debug,
                         settings_json=settings_json,
                         ingress_path=request.script_root)

def verify_settings_saved(new_settings, saved_settings):
    """Vérifie que les paramètres ont été correctement sauvegardés"""
    try:
        # Vérifier les paramètres globaux
        if 'global' in new_settings:
            for field in ['threshold', 'delay', 'chunk_duration', 'buffer_duration']:
                if new_settings['global'].get(field) != saved_settings['global'].get(field):
                    print(f"Différence détectée pour global.{field}:")
                    print(f"  Attendu: {new_settings['global'].get(field)}")
                    print(f"  Sauvegardé: {saved_settings['global'].get(field)}")
                    return False

        # Vérifier les paramètres du microphone
        if 'microphone' in new_settings:
            for field in ['device_index', 'audio_source', 'webhook_url']:
                if new_settings['microphone'].get(field) != saved_settings['microphone'].get(field):
                    print(f"Différence détectée pour microphone.{field}:")
                    print(f"  Attendu: {new_settings['microphone'].get(field)}")
                    print(f"  Sauvegardé: {saved_settings['microphone'].get(field)}")
                    return False

        # Vérifier les sources RTSP si présentes
        if 'rtsp_sources' in new_settings:
            if len(new_settings['rtsp_sources']) != len(saved_settings.get('rtsp_sources', [])):
                print("Différence dans le nombre de sources RTSP")
                return False
            for i, (new_source, saved_source) in enumerate(zip(new_settings['rtsp_sources'], saved_settings['rtsp_sources'])):
                for field in ['name', 'url', 'webhook_url']:
                    if new_source.get(field) != saved_source.get(field):
                        print(f"Différence détectée pour rtsp_sources[{i}].{field}:")
                        print(f"  Attendu: {new_source.get(field)}")
                        print(f"  Sauvegardé: {saved_source.get(field)}")
                        return False

        # Ne pas vérifier les champs à la racine car ils sont maintenant dans les sections appropriées
        print("Tous les paramètres ont été correctement sauvegardés")
        return True
        
    except Exception as e:
        print(f"Erreur lors de la vérification des paramètres: {str(e)}")
        return False

@app.route('/api/detection/start', methods=['POST'])
def start_detection_route():
    try:
        detection_settings = request.json
        if not detection_settings:
            return jsonify({'error': 'Aucun paramètre fourni'}), 400
            
        # Vérifier la présence des sections requises et initialiser avec des valeurs par défaut si nécessaire
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
            print("Microphone désactivé - aucune capture audio ne sera effectuée")
            
        # Préparer les paramètres pour start_detection avec gestion des valeurs null
        try:
            global_settings = detection_settings.get('global', {})
            if not isinstance(global_settings, dict):
                global_settings = {}
                
            microphone_settings = detection_settings.get('microphone', {})
            if not isinstance(microphone_settings, dict):
                microphone_settings = {}
                
            detection_params = {
                'model': "yamnet.tflite",
                'max_results': 5,
                'score_threshold': float(global_settings.get('threshold', '0.2')),
                'overlapping_factor': 0.8,
                'socketio': socketio,
                'webhook_url': microphone_settings.get('webhook_url') if microphone_enabled else None,
                'delay': float(global_settings.get('delay', '1.0')),
                'audio_source': microphone_settings.get('audio_source') if microphone_enabled else None,
                'rtsp_url': None
            }

            # Check for RTSP sources first
            rtsp_sources = detection_settings.get('rtsp_sources', [])
            for source in rtsp_sources:
                if source.get('enabled', False):
                    detection_params['audio_source'] = f"rtsp://{source['url']}"
                    detection_params['rtsp_url'] = source['url']
                    logging.info(f"Utilisation de la source RTSP: {source.get('name', 'Unknown')} ({source['url']})")
                    break

            # If no RTSP source is enabled, check for VBAN sources
            if not detection_params['audio_source'] and not microphone_enabled:
                # Vérifier d'abord saved_vban_sources
                saved_vban_sources = detection_settings.get('saved_vban_sources', [])
                if saved_vban_sources:
                    # Utiliser la première source VBAN active
                    for source in saved_vban_sources:
                        if source.get('enabled', True):
                            detection_params['audio_source'] = f"vban://{source['ip']}"
                            logging.info(f"Utilisation de la source VBAN: {source['name']} ({source['ip']})")
                            break
                    else:
                        if not any(source.get('enabled', True) for source in saved_vban_sources):
                            logging.info("Aucune source VBAN active n'est activée")
                else:
                    logging.info("Aucune source VBAN configurée")
        except (ValueError, TypeError) as e:
            return jsonify({'error': f'Erreur dans les paramètres : {str(e)}'}), 400
        
        # Démarrer la détection
        if start_detection(**detection_params):
            return jsonify({'success': True})
        else:
            return jsonify({'error': 'Impossible de démarrer la détection'}), 400
            
    except Exception as e:
        print(f"Erreur lors du démarrage de la détection: {str(e)}")
        return jsonify({'error': str(e)}), 400

@app.route('/api/detection/stop', methods=['POST'])
def stop_detection_route():
    try:
        # Arrêter la détection
        if stop_detection():
            # Émettre un événement de statut avant d'arrter
            socketio.emit('detection_status', {'status': 'stopped'})
            return jsonify({'success': True})
        else:
            return jsonify({'error': 'Impossible d\'arrêter la détection'}), 400
    except Exception as e:
        print(f"Erreur lors de l'arrêt de la détection: {str(e)}")
        return jsonify({'error': str(e)}), 400

@app.route('/api/webhook/test', methods=['POST'])
def test_webhook():
    try:
        data = request.get_json()
        if not data or 'url' not in data:
            return jsonify({'error': 'URL manquante'}), 400

        url = data['url']
        source = data.get('source', 'test')

        # Créer les données de test
        test_data = {
            'event': 'test',
            'source': source,
            'timestamp': datetime.now().isoformat(),
            'test': True
        }

        try:
            # Utiliser le WebhookManager pour envoyer la requête
            webhook_manager = WebhookManager()
            response = webhook_manager.send_webhook(url, test_data)
            return jsonify({'success': True, 'message': 'Test réussi'})
            
        except requests.exceptions.RequestException as e:
            error_message = str(e)
            if hasattr(e.response, 'text'):
                error_message = f"{error_message}: {e.response.text}"
            return jsonify({'error': f'Échec du test: {error_message}'}), 500

    except Exception as e:
        return jsonify({'error': f'Erreur: {str(e)}'}), 500

@app.route('/refresh_vban')
def refresh_vban():
    try:
        print("Récupération des sources VBAN...")  # Debug log
        
        # Utiliser l'instance globale
        sources = init_vban().get_active_sources()
        print(f"Sources trouvées: {sources}")  # Debug log
        
        # Formater les sources pour l'interface
        formatted_sources = []
        for source in sources:
            formatted_sources.append({
                'name': source.stream_name,
                'ip': source.ip,
                'port': source.port,
                'channels': source.channels,
                'sample_rate': source.sample_rate,
                'id': f"vban_{source.ip}_{source.port}"
            })
        
        print(f"Sources formatées: {formatted_sources}")  # Debug log
        return jsonify({'sources': formatted_sources})
    except Exception as e:
        print(f"Erreur lors de la rcupération des sources VBAN: {str(e)}")  # Debug log
        return jsonify({'error': str(e)}), 400

@app.route('/clap_detected')  # Added route for clap detection
def clap_detected():
    socketio.emit('clap', {'message': 'Applaudissement détecté!'})
    return "Notification envoyée"

@app.route('/status')
def status():
    try:
        running = is_running()
        return jsonify({'running': running})
    except Exception as e:
        return jsonify({'running': False, 'error': str(e)})

@app.route('/refresh_vban_sources')
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

@app.route('/save_settings', methods=['POST'])
def save_settings_route():
    try:
        settings = request.json
        if not settings:
            return jsonify({'error': 'Aucun paramètre fourni'}), 400
            
        success, message = save_settings(settings)
        if success:
            return jsonify({'message': message})
        else:
            return jsonify({'error': message}), 400
            
    except Exception as e:
        return jsonify({'error': str(e)}), 400

def validate_settings(settings):
    """Valide les paramètres avant la sauvegarde"""
    required_fields = ['threshold', 'delay', 'audio_source']
    
    # Vérifier les champs requis
    if not all(field in settings for field in required_fields):
        return False
        
    # Valider les valeurs
    try:
        threshold = float(settings['threshold'])
        delay = float(settings['delay'])
        
        if not (0 <= threshold <= 1):
            return False
        if delay < 0:
            return False
            
        # Valider l'URL du webhook si présente
        if settings.get('microphone', {}).get('webhook_url'):
            url = settings['microphone']['webhook_url']
            if not url.startswith(('http://', 'https://')):
                return False
                
    except (ValueError, TypeError):
        return False
        
    return True

@socketio.on('clap_detected')
def handle_clap(data):
    print(f"🎯 Clap detected: {data}")  # Debug log
    try:
        socketio.emit('clap', {
            'source_id': 'microphone',
            'timestamp': time.time()
        }, broadcast=True)
        print(f"✅ Clap event emitted")
    except Exception as e:
        print(f"❌ Error emitting clap event: {str(e)}")

@app.route('/api/vban/save', methods=['POST'])
def save_vban_source():
    try:
        source = request.json
        print(f"Réception demande d'ajout source VBAN: {source}")  # Debug log
        
        # Valider les données requises
        required_fields = ['name', 'ip', 'port']
        if not all(field in source for field in required_fields):
            print(f"Champs manquants. Reçu: {source}")  # Debug log
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
            print(f"Source déjà existante: {existing_source}")  # Debug log
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
            print(f"Source VBAN sauvegardée avec succès: {new_source}")  # Debug log
            return jsonify({
                'success': True,
                'source': new_source
            })
        else:
            print(f"Erreur lors de la sauvegarde des paramètres: {message}")  # Debug log
            return jsonify({
                'success': False,
                'error': f"Erreur lors de la sauvegarde: {message}"
            }), 500
            
    except Exception as e:
        print(f"Erreur lors de la sauvegarde de la source VBAN: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/vban/remove', methods=['DELETE'])
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
        print(f"Erreur lors de la suppression de la source VBAN: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/vban/update', methods=['PUT'])
def update_vban_source():
    try:
        source = request.json
        print(f"Mise à jour source VBAN reçue: {source}")  # Debug log
        
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
                source_found = True
                print(f"Source mise à jour: {s}")  # Debug log
                break
                
        if not source_found:
            print(f"Source non trouvée. Sources existantes: {settings['saved_vban_sources']}")  # Debug log
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
        print(f"Erreur lors de la mise à jour de la source VBAN: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/static/js/modules/<path:filename>')
def serve_js_module(filename):
    return send_from_directory('static/js/modules', filename, mimetype='application/javascript')

@app.route('/api/audio-sources', methods=['GET'])
def get_audio_sources():
    try:
        audio_sources = [
            {**device, 'type': 'microphone'}
            for device in get_audio_input_devices()
        ]
        return jsonify(audio_sources)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/rtsp/streams', methods=['GET'])
def get_rtsp_streams():
    try:
        settings = load_settings()
        return jsonify(settings.get('rtsp_sources', []))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/rtsp/stream', methods=['POST'])
def add_rtsp_stream():
    try:
        data = request.get_json()
        url = data.get('url')
        name = data.get('name', '')
        webhook_url = data.get('webhook_url', '')
        enabled = data.get('enabled', True)
        
        if not url:
            return jsonify({'error': 'URL RTSP requise'}), 400
            
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
            'enabled': enabled
        }
        
        settings['rtsp_sources'].append(new_stream)
        save_settings(settings)
        
        return jsonify({'success': True, 'stream': new_stream})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/rtsp/stream/<stream_id>', methods=['PUT'])
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
                
                save_settings(settings)
                return jsonify({'success': True, 'stream': stream})
                
        return jsonify({'error': 'Stream non trouvé'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/rtsp/stream/<stream_id>', methods=['DELETE'])
def delete_rtsp_stream(stream_id):
    try:
        settings = load_settings()
        rtsp_sources = settings.get('rtsp_sources', [])
        
        # Filtrer pour retirer le stream spécifié
        settings['rtsp_sources'] = [s for s in rtsp_sources if s.get('id') != stream_id]
        save_settings(settings)
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/vban/sources', methods=['GET'])
def get_vban_sources():
    try:
        # S'assurer que la découverte VBAN est initialisée
        detector = get_vban_detector()
        if not detector:
            return jsonify({'error': 'Impossible d\'initialiser la découverte VBAN'}), 500
            
        # Utiliser la nouvelle méthode thread-safe pour obtenir les sources
        active_sources = detector.get_sources(timeout=1.0)
        
        print(f"Sources VBAN actives trouvées: {len(active_sources)}")
        print(f"Sources formatées: {active_sources}")
        return jsonify(active_sources)
        
    except Exception as e:
        print(f"Erreur lors de la récupération des sources VBAN: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/vban/saved-sources', methods=['GET'])
def get_saved_vban_sources():
    try:
        settings = load_settings()
        saved_sources = settings.get('saved_vban_sources', [])
        print(f"Sources VBAN sauvegardées trouvées: {len(saved_sources)}")  # Debug log
        print(f"Sources: {saved_sources}")  # Debug log
        return jsonify(saved_sources)
    except Exception as e:
        print(f"Erreur lors de la récupération des sources VBAN sauvegardées: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/settings/save', methods=['POST'])
def save_settings_api():
    try:
        settings = request.json
        if not settings:
            return jsonify({'error': 'Aucun paramètre fourni'}), 400
            
        success, message = save_settings(settings)
        if success:
            return jsonify({'success': True, 'message': message})
        else:
            return jsonify({'error': message}), 400
            
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/microphone/webhook', methods=['PUT'])
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

@app.route('/api/microphone/enabled', methods=['PUT'])
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

@app.route('/api/rtsp/webhook', methods=['PUT'])
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

@app.route('/api/rtsp/enabled', methods=['PUT'])
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

@socketio.on('connect')
def handle_connect():
    print("🔌 Client connecté")

@socketio.on('disconnect')
def handle_disconnect():
    print("🔌 Client déconnecté")

@socketio.on('test_connection')
def handle_test():
    print("🔔 Test de connexion reçu")
    socketio.emit('debug', {'message': 'Test serveur'})

if __name__ == '__main__':
    try:
        # Désactiver le mode debug
        socketio.run(app, host='0.0.0.0', port=16045, debug=False, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        cleanup_vban_detector()
    except Exception as e:
        print(f"Erreur lors du démarrage du serveur: {e}")
        cleanup_vban_detector()
