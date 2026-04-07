from flask import Blueprint, jsonify, request
import json
import logging
from datetime import datetime
import requests

from settings_manager import load_settings, save_settings, SETTINGS_FILE
from webhook import WebhookManager

settings_bp = Blueprint('settings', __name__)

# Singleton WebhookManager (réutilise le pool de connexions HTTP)
_webhook_manager = WebhookManager()


@settings_bp.route('/save_settings', methods=['POST'])
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
