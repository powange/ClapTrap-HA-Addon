from flask import Blueprint, jsonify, request
import json
import logging
import os
from datetime import datetime
import requests

from settings_manager import load_settings, save_settings, SETTINGS_FILE
from webhook import WebhookManager

settings_bp = Blueprint('settings', __name__)

# Singleton WebhookManager (réutilise le pool de connexions HTTP)
_webhook_manager = WebhookManager()


@settings_bp.route('/api/settings/save', methods=['POST'])
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


@settings_bp.route('/api/settings/debug', methods=['PUT'])
def toggle_debug():
    try:
        data = request.get_json()
        enabled = bool(data.get('enabled', False))
        settings = load_settings()
        settings['global']['debug'] = enabled
        save_settings(settings)
        # Appliquer immédiatement
        level = logging.DEBUG if enabled else logging.INFO
        logging.getLogger().setLevel(level)
        logging.info(f"Logs debug {'actives' if enabled else 'desactives'}")
        return jsonify({'success': True, 'debug': enabled})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@settings_bp.route('/api/settings/advanced', methods=['PUT'])
def update_advanced_settings():
    try:
        data = request.get_json()
        settings = load_settings()
        for key in ['delay', 'peak_cooldown', 'peak_ratio', 'peak_reset']:
            if key in data:
                settings['global'][key] = float(data[key])
        save_settings(settings)

        # Appliquer en temps réel sur les detectors actifs
        try:
            from classify import update_advanced_params
            update_advanced_params(
                peak_cooldown=data.get('peak_cooldown'),
                peak_ratio=data.get('peak_ratio'),
                delay=data.get('delay'),
                peak_reset=data.get('peak_reset'),
            )
        except Exception:
            pass

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@settings_bp.route('/api/ha/cleanup', methods=['POST'])
def cleanup_ha_entities():
    """Supprime toutes les entites ClapTrap orphelines via l'entity registry."""
    try:
        import requests as req
        token = os.environ.get('SUPERVISOR_TOKEN', '')
        if not token:
            return jsonify({'error': 'SUPERVISOR_TOKEN non disponible'}), 500
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

        # Lister toutes les entites via le registre
        resp = req.get('http://supervisor/core/api/states', headers=headers, timeout=10)
        if not resp.ok:
            return jsonify({'error': f'API states: {resp.status_code}'}), 500

        states = resp.json()
        claptrap_entities = [
            s['entity_id'] for s in states
            if s.get('entity_id', '').startswith(('binary_sensor.claptrap_', 'sensor.claptrap_'))
        ]

        removed = []
        for entity_id in claptrap_entities:
            # Mettre en unavailable pour forcer HA a les considerer comme mortes
            req.post(
                f'http://supervisor/core/api/states/{entity_id}',
                headers=headers,
                json={'state': 'unavailable', 'attributes': {'restored': False}},
                timeout=5
            )
            removed.append(entity_id)

        return jsonify({
            'success': True,
            'removed': len(removed),
            'entities': removed,
            'message': f'{len(removed)} entites marquees unavailable. Redemarrez HA pour les supprimer definitivement.'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@settings_bp.route('/api/ha/entities', methods=['GET'])
def get_ha_entities():
    try:
        from ha_entities import get_entities_info
        return jsonify(get_entities_info())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@settings_bp.route('/api/settings/export', methods=['GET'])
def export_settings():
    from flask import send_file
    return send_file(SETTINGS_FILE, as_attachment=True, download_name='claptrap-settings.json')


@settings_bp.route('/api/settings/import', methods=['POST'])
def import_settings():
    try:
        if 'file' in request.files:
            file = request.files['file']
            imported = json.loads(file.read().decode('utf-8'))
        else:
            imported = request.get_json()
        if not isinstance(imported, dict):
            return jsonify({'error': 'Format invalide'}), 400
        success, msg = save_settings(imported)
        return jsonify({'success': success, 'message': msg})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@settings_bp.route('/api/webhook/test', methods=['POST'])
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
            response = _webhook_manager.send_webhook(url, test_data)
            return jsonify({'success': True, 'message': 'Test réussi'})

        except requests.exceptions.RequestException as e:
            error_message = str(e)
            if hasattr(e.response, 'text'):
                error_message = f"{error_message}: {e.response.text}"
            return jsonify({'error': f'Échec du test: {error_message}'}), 500

    except Exception as e:
        return jsonify({'error': f'Erreur: {str(e)}'}), 500
