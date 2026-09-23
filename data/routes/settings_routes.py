from flask import Blueprint, jsonify, request
import json
import logging
import os
from datetime import datetime
import requests

from settings_manager import (load_settings, save_settings, modify_settings, normalize_settings,
                              to_bool, to_number, SettingsSaveError, SETTINGS_FILE)
from webhook import WebhookManager
from routes.sources import api_error_response, _restart_detection_if_running

settings_bp = Blueprint('settings', __name__)
settings_bp.register_error_handler(Exception, api_error_response)

# Singleton WebhookManager (réutilise le pool de connexions HTTP)
_webhook_manager = WebhookManager()


@settings_bp.route('/api/settings/save', methods=['POST'])
def save_settings_api():
    settings = request.get_json(silent=True)
    if not settings:
        return jsonify({'error': 'Aucun paramètre fourni'}), 400
    normalize_settings(settings)  # ValueError -> 400 avec le champ fautif
    success, message = save_settings(settings)
    if not success:
        raise SettingsSaveError(message)
    return jsonify({'success': True, 'message': message})


@settings_bp.route('/api/settings/debug', methods=['PUT'])
def toggle_debug():
    data = request.get_json(silent=True) or {}
    enabled = to_bool(data.get('enabled', False), 'enabled')

    def _mut(settings):
        settings.setdefault('global', {})['debug'] = enabled

    modify_settings(_mut)
    # Appliquer immédiatement
    logging.getLogger().setLevel(logging.DEBUG if enabled else logging.INFO)
    logging.info(f"Logs debug {'actives' if enabled else 'desactives'}")
    return jsonify({'success': True, 'debug': enabled})


_ADVANCED_LIMITS = {'delay': (0.1, 10), 'peak_cooldown': (0, 2),
                    'peak_ratio': (1, 50), 'peak_reset': (0, 5)}


@settings_bp.route('/api/settings/advanced', methods=['PUT'])
def update_advanced_settings():
    data = request.get_json(silent=True) or {}
    values = {key: to_number(data[key], key, lo, hi)
              for key, (lo, hi) in _ADVANCED_LIMITS.items() if key in data}

    def _mut(settings):
        settings.setdefault('global', {}).update(values)

    modify_settings(_mut)
    # Appliquer en temps réel sur les detectors actifs
    try:
        from classify import update_advanced_params
        update_advanced_params(
            peak_cooldown=values.get('peak_cooldown'),
            peak_ratio=values.get('peak_ratio'),
            delay=values.get('delay'),
            peak_reset=values.get('peak_reset'),
        )
    except Exception as e:
        logging.warning(f"Réglages avancés non appliqués en direct: {e}")
    return jsonify({'success': True})


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
            imported = json.loads(request.files['file'].read().decode('utf-8'))
        else:
            imported = request.get_json(silent=True)
    except (ValueError, UnicodeDecodeError) as e:
        return jsonify({'error': f'Fichier JSON illisible : {e}'}), 400
    # Validation AVANT ecriture : un seuil "abc" faisait planter float() au
    # demarrage suivant (auto-start en echec silencieux).
    normalize_settings(imported)
    success, msg = save_settings(imported)
    if not success:
        raise SettingsSaveError(msg)
    # Appliquer la configuration importee sans attendre un redemarrage manuel.
    level = logging.DEBUG if load_settings().get('global', {}).get('debug') else logging.INFO
    logging.getLogger().setLevel(level)
    _restart_detection_if_running()
    return jsonify({'success': True, 'message': msg})


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
