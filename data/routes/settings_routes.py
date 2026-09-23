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


@settings_bp.route('/api/settings', methods=['GET'])
def get_settings():
    """Reglages enregistres : l'UI se resynchronise apres une reconnexion
    ou quand un autre onglet a modifie la configuration."""
    return jsonify(load_settings())


@settings_bp.route('/api/settings/save', methods=['POST'])
def save_settings_api():
    settings = request.get_json(silent=True)
    if not settings:
        return jsonify({'success': False, 'error': 'Aucun paramètre fourni'}), 400
    normalize_settings(settings)  # ValueError -> 400 avec le champ fautif
    success, message = save_settings(settings)
    if not success:
        raise SettingsSaveError(message)
    return jsonify({'success': True, 'message': message})


@settings_bp.route('/api/settings/debug', methods=['PUT'])
def toggle_debug():
    data = request.get_json(silent=True) or {}
    if 'enabled' not in data:
        raise ValueError("enabled : booléen attendu")  # {} desactivait le journal
    enabled = to_bool(data['enabled'], 'enabled')

    def _mut(settings):
        settings.setdefault('global', {})['debug'] = enabled

    modify_settings(_mut)
    # Appliquer immédiatement
    logging.getLogger().setLevel(logging.DEBUG if enabled else logging.INFO)
    logging.info(f"Logs debug {'actives' if enabled else 'desactives'}")
    return jsonify({'success': True, 'debug': enabled})


_ADVANCED_LIMITS = {'delay': (0.1, 10), 'peak_cooldown': (0, 2), 'peak_ratio': (1, 50)}


@settings_bp.route('/api/settings/advanced', methods=['PUT'])
def update_advanced_settings():
    data = request.get_json(silent=True) or {}
    unknown = sorted(set(data) - set(_ADVANCED_LIMITS))
    if unknown:
        raise ValueError(f"Champ(s) inconnu(s) : {', '.join(unknown)}")
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
        )
    except Exception as e:
        logging.warning(f"Réglages avancés non appliqués en direct: {e}")
    return jsonify({'success': True})


@settings_bp.route('/api/ha/cleanup', methods=['POST'])
def cleanup_ha_entities():
    """Supprime les entites ClapTrap orphelines (aucune source configuree).

    Via MQTT discovery (config vide) : HA supprime reellement l'entite. L'ancien
    bouton ne faisait que marquer TOUTES les entites "unavailable" par l'API
    REST, y compris les actives.
    """
    from ha_entities import cleanup_orphans, sync_sources
    sync_sources(load_settings())
    removed = cleanup_orphans()
    return jsonify({
        'success': True,
        'removed': len(removed),
        'entities': [f'binary_sensor.claptrap_{o}' for o in removed],
        'message': f'{len(removed)} entité(s) orpheline(s) supprimée(s).' if removed
                   else 'Aucune entité orpheline.',
    })


@settings_bp.route('/api/ha/entity-ids', methods=['GET'])
def get_entity_ids():
    from ha_entities import entity_ids_for_settings
    return jsonify(entity_ids_for_settings(load_settings()))


@settings_bp.route('/api/ha/entities', methods=['GET'])
def get_ha_entities():
    try:
        from ha_entities import get_entities_info
        return jsonify(get_entities_info())
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': False, 'error': f'Fichier JSON illisible : {e}'}), 400
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
    """Envoie un webhook de test.

    Meme payload que les vrais claps (plus test: true), pour qu'un test
    reussi garantisse que l'automation fonctionnera. Pas de redirection, et la
    reponse du serveur distant n'est jamais renvoyee au navigateur (la route
    pouvait servir a lire des services internes).
    """
    from url_validator import is_valid_url
    data = request.get_json(silent=True) or {}
    url = str(data.get('url') or '').strip()
    if not url:
        return jsonify({'success': False, 'error': 'URL manquante'}), 400
    if not is_valid_url(url):
        return jsonify({'success': False, 'error': 'URL invalide : http:// ou https:// attendu'}), 400
    payload = {
        'event': 'clap',
        'test': True,
        'source_id': str(data.get('source') or 'test'),
        'timestamp': datetime.now().timestamp(),
        'score': 0.9,
        'clap_count': 1,
        'labels': [{'label': 'Clapping', 'score': 0.9}],
        'group_slug': 'clap',
        'group_name': 'Clap',
        'ignored': False,
    }
    try:
        response = _webhook_manager.send_webhook(url, payload, follow_redirects=False)
    except requests.exceptions.HTTPError as e:
        status = getattr(e.response, 'status_code', '?')
        return jsonify({'success': False, 'error': f'Le serveur a répondu HTTP {status}'}), 502
    except requests.exceptions.RequestException as e:
        return jsonify({'success': False, 'error': f'Serveur injoignable ({type(e).__name__})'}), 502
    if 300 <= response.status_code < 400:
        return jsonify({'success': False, 'error': f'Redirection refusée (HTTP {response.status_code})'}), 502
    return jsonify({'success': True, 'message': 'Test réussi'})
