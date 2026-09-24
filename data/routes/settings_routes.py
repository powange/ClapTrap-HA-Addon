from flask import Blueprint, jsonify, request
import json
import logging
import os
from datetime import datetime
import requests

from settings_manager import (load_settings, save_settings, modify_settings, normalize_settings,
                              to_bool, to_number, SettingsSaveError)
from webhook import send_webhook
from url_validator import mask_webhook_url
from routes.sources import (ApiError, add_restart_to_response, api_error_response, _apply_live, _json,
                           _sync_and_apply)

settings_bp = Blueprint('settings', __name__)
settings_bp.register_error_handler(Exception, api_error_response)
settings_bp.after_request(add_restart_to_response)



@settings_bp.route('/api/settings', methods=['GET'])
def get_settings():
    """Reglages enregistres : l'UI se resynchronise apres une reconnexion
    ou quand un autre onglet a modifie la configuration."""
    return jsonify(load_settings())


@settings_bp.route('/api/settings/debug', methods=['PUT'])
def toggle_debug():
    data = _json()
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
    data = _json()
    unknown = sorted(set(data) - set(_ADVANCED_LIMITS))
    if unknown:
        raise ValueError(f"Champ(s) inconnu(s) : {', '.join(unknown)}")
    values = {key: to_number(data[key], key, lo, hi)
              for key, (lo, hi) in _ADVANCED_LIMITS.items() if key in data}

    def _mut(settings):
        settings.setdefault('global', {}).update(values)

    modify_settings(_mut)
    _apply_live()   # detecteurs actifs, sans relance
    return jsonify({'success': True})


@settings_bp.route('/api/ha/cleanup', methods=['POST'])
def cleanup_ha_entities():
    """Supprime les entites ClapTrap orphelines (aucune source configuree).

    Via MQTT discovery (config vide) : HA supprime reellement l'entite. L'ancien
    bouton ne faisait que marquer TOUTES les entites "unavailable" par l'API
    REST, y compris les actives.
    """
    from ha_entities import cleanup_orphans, sync_sources, _mqtt_connected
    if not _mqtt_connected.is_set():
        # Rien ne serait publie : ne pas annoncer de suppressions.
        return jsonify({'success': False, 'error': 'Broker MQTT injoignable : réessayez une fois connecté'}), 503
    from settings_manager import is_degraded
    if is_degraded():
        return jsonify({'success': False, 'error': 'Réglages illisibles : nettoyage suspendu pour ne supprimer '
                                                   'aucune entité (restaurez une sauvegarde)'}), 409
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


@settings_bp.route('/api/settings/export', methods=['GET'])
def export_settings():
    """Sauvegarde complete, ou `?secrets=0` pour une copie a partager
    (demande d'aide, issue GitHub) : identifiants RTSP et chemins de webhook
    (le secret d'un webhook Home Assistant) masques."""
    from flask import Response
    data = load_settings()
    shareable = request.args.get('secrets', '1') in ('0', 'false', 'no')
    if shareable:
        from url_validator import mask_url_credentials, mask_webhook_url
        sources = [data.get('microphone') or {}] + list(data.get('rtsp_sources') or []) + \
            list(data.get('saved_vban_sources') or [])
        for src in sources:
            if src.get('webhook_url'):
                src['webhook_url'] = mask_webhook_url(src['webhook_url'])
            if src.get('url'):
                src['url'] = mask_url_credentials(src['url'])
        data['_shareable'] = True   # refuse a l'import
    name = 'claptrap-settings-partage.json' if shareable else 'claptrap-settings.json'
    return Response(json.dumps(data, indent=4, ensure_ascii=False), mimetype='application/json',
                    headers={'Content-Disposition': f'attachment; filename={name}'})


def _check_import_collisions(imported):
    """Refuse un import dont deux sources produiraient les memes entites HA."""
    from settings_manager import merge_import
    from ha_entities import find_entity_collision
    # Meme fusion (et memes migrations) que l'enregistrement.
    current = load_settings()
    clash = find_entity_collision(merge_import(current, imported), before=current)
    if clash:
        raise ApiError(f"« {clash} » produirait les mêmes entités Home Assistant qu'une autre source : "
                       "renommez-la dans le fichier avant de l'importer", 409)


@settings_bp.route('/api/settings/import', methods=['POST'])
def import_settings():
    try:
        if 'file' in request.files:
            imported = json.loads(request.files['file'].read().decode('utf-8'))
        else:
            imported = request.get_json(silent=True)
    except (ValueError, UnicodeDecodeError) as e:
        return jsonify({'success': False, 'error': f'Fichier JSON illisible : {e}'}), 400
    if isinstance(imported, dict) and imported.pop('_shareable', False):
        # Identifiants et chemins de webhook masques : l'importer cassait
        # cameras et webhooks sans aucune erreur.
        raise ValueError("Ce fichier est un export « sans secrets » (identifiants et webhooks masqués) : "
                         "importez plutôt une sauvegarde complète (« Exporter »)")
    # Validation AVANT ecriture : un seuil "abc" faisait planter float() au
    # demarrage suivant (auto-start en echec silencieux).
    normalize_settings(imported)
    _check_import_collisions(imported)
    success, msg = save_settings(imported)
    if not success:
        raise SettingsSaveError(msg)
    # Appliquer la configuration importee sans attendre un redemarrage manuel.
    level = logging.DEBUG if load_settings().get('global', {}).get('debug') else logging.INFO
    logging.getLogger().setLevel(level)
    _sync_and_apply()
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
    data = _json()
    url = str(data.get('url') or '').strip()
    if not url:
        return jsonify({'success': False, 'error': 'URL manquante'}), 400
    if not is_valid_url(url):
        return jsonify({'success': False, 'error': 'URL invalide : http:// ou https:// attendu'}), 400
    source_id = str(data.get('source') or 'test')
    try:
        from classify import build_sources_from_settings
        src = next((x for x in build_sources_from_settings(load_settings()) if x['source_id'] == source_id), None)
    except Exception:
        src = None
    payload = {
        'event': 'clap',
        'test': True,
        'source_id': source_id,
        'entity_key': src['entity_key'] if src else 'test',
        'source_name': src['name'] if src else 'Test',
        'timestamp': datetime.now().timestamp(),
        'score': 0.9,
        'clap_count': 1,
        'labels': [{'label': 'Clapping', 'score': 0.9}],
        'group_slug': 'clap',
        'group_name': 'Clap',
        'ignored': False,
    }
    # Un seul message d'echec : distinguer « HTTP 404 » de « injoignable »
    # permettait de sonder le reseau local. Le detail (sans le chemin secret
    # du webhook) est dans le journal de l'add-on.
    failure = jsonify({'success': False,
                       'error': "Le webhook n'a pas été accepté (détail dans le journal de l'add-on)"}), 502
    try:
        response = send_webhook(url, payload, follow_redirects=False)
    except requests.exceptions.RequestException:
        return failure
    if 300 <= response.status_code < 400:
        logging.error(f"Webhook de test vers {mask_webhook_url(url)} : redirection refusée "
                      f"(HTTP {response.status_code})")
        return failure
    return jsonify({'success': True, 'message': 'Test réussi'})
