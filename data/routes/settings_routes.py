from flask import Blueprint, jsonify, request
import json
import logging
from datetime import datetime
import requests

from settings_manager import (load_settings, save_settings, modify_settings, normalize_settings,
                              to_bool, to_number, to_room_zones, SettingsSaveError, GLOBAL_LIMITS)
from webhook import send_webhook
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


# Reglages avances : tous les reglages globaux bornes sauf le seuil (par
# groupe dans l'interface), avec les bornes de l'import.
_ADVANCED_KEYS = ('delay', 'peak_cooldown', 'peak_ratio')   # liste explicite (pas tout GLOBAL_LIMITS)


@settings_bp.route('/api/settings/advanced', methods=['PUT'])
def update_advanced_settings():
    data = _json()
    unknown = sorted(set(data) - set(_ADVANCED_KEYS) - {'clap_shape_check'})
    if unknown:
        raise ValueError(f"Champ(s) inconnu(s) : {', '.join(unknown)}")
    values = {key: to_number(data[key], key, *GLOBAL_LIMITS[key]) for key in _ADVANCED_KEYS if key in data}
    if 'clap_shape_check' in data:
        values['clap_shape_check'] = to_bool(data['clap_shape_check'], 'clap_shape_check')

    def _mut(settings):
        settings.setdefault('global', {}).update(values)

    modify_settings(_mut)
    _apply_live()   # detecteurs actifs, sans relance
    return jsonify({'success': True})


@settings_bp.route('/api/settings/rooms', methods=['PUT'])
def update_room_settings():
    """Arbitrage entre pieces : {enabled?, window?, zones?} (fenetre
    d'attente en s ; groupes de sources qui se departagent entre elles)."""
    data = _json()
    unknown = sorted(set(data) - {'enabled', 'window', 'zones'})
    if unknown:
        raise ValueError(f"Champ(s) inconnu(s) : {', '.join(unknown)}")
    values = {}
    if 'enabled' in data:
        values['room_arbitration'] = to_bool(data['enabled'], 'enabled')
    if 'window' in data:
        values['room_window'] = to_number(data['window'], 'window', *GLOBAL_LIMITS['room_window'])
    if 'zones' in data:
        values['room_zones'] = to_room_zones(data['zones'])

    def _mut(settings):
        settings.setdefault('global', {}).update(values)
    modify_settings(_mut)
    _apply_live()   # decision anticipee coupee ou retablie
    return jsonify({'success': True, **values})


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
        'entities': removed,   # « appareil <source> » (toutes ses entites) ou entity_id
        'message': f"Supprimé : {', '.join(removed)}." if removed else 'Aucune entité orpheline.',
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
        from settings_manager import shareable_settings
        data = shareable_settings(data)
        data['_shareable'] = True   # refuse a l'import
    name = 'claptrap-settings-partage.json' if shareable else 'claptrap-settings.json'
    return Response(json.dumps(data, indent=4, ensure_ascii=False), mimetype='application/json',
                    headers={'Content-Disposition': f'attachment; filename={name}'})


def _check_import_collisions(merged, current):
    """Refuse un import dont deux sources produiraient les memes entites HA
    (controle des reglages fusionnes, sous le verrou de l'enregistrement)."""
    from ha_entities import find_entity_collision
    clash = find_entity_collision(merged, before=current)
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
    except RecursionError:
        return jsonify({'success': False, 'error': 'Fichier JSON illisible : trop imbriqué'}), 400
    if isinstance(imported, dict) and imported.pop('_shareable', False):
        # Identifiants et chemins de webhook masques : l'importer cassait
        # cameras et webhooks sans aucune erreur.
        raise ValueError("Ce fichier est un export « sans secrets » (identifiants et webhooks masqués) : "
                         "importez plutôt une sauvegarde complète (« Exporter »)")
    # Validation AVANT ecriture : un seuil "abc" faisait planter float() au
    # demarrage suivant (auto-start en echec silencieux).
    normalize_settings(imported)
    from settings_manager import strip_unknown_keys
    ignored = strip_unknown_keys(imported)
    if ignored:
        logging.warning(f"Import : champs inconnus ignorés : {', '.join(ignored[:20])}"
                        f"{' …' if len(ignored) > 20 else ''}")
    success, msg = save_settings(imported, check=_check_import_collisions)
    if not success:
        raise SettingsSaveError(msg)
    # Appliquer la configuration importee sans attendre un redemarrage manuel.
    level = logging.DEBUG if load_settings().get('global', {}).get('debug') else logging.INFO
    logging.getLogger().setLevel(level)
    # Volume du micro importe : applique tout de suite (la detection ne le
    # reapplique qu'au demarrage du micro).
    settings = load_settings()
    mic = settings.get('microphone') or {}
    if 'microphone' in imported and not mic.get('auto_volume'):
        try:
            from audio_utils import set_pulse_volume
            from routes.sources import _resolve_pulse_name
            pulse = _resolve_pulse_name(settings)   # micro reellement present (pas le nom du fichier)
            if pulse:
                set_pulse_volume(pulse, mic.get('volume', 100))
        except Exception as e:
            logging.warning(f"Volume du micro importé non appliqué : {e}")
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
        send_webhook(url, payload)
    except requests.exceptions.RequestException:
        return failure   # redirection comprise (journalisee par send_webhook)
    return jsonify({'success': True, 'message': 'Test réussi'})


# --- Blueprints Home Assistant (onglet Blueprints, voir blueprints.py) --------

def _blueprints(fn, *args):
    import blueprints
    try:
        return fn(*args)
    except blueprints.BlueprintError as e:
        raise ApiError(str(e), e.status)


@settings_bp.route('/api/blueprints', methods=['GET'])
def list_blueprints():
    import blueprints
    items, error = _blueprints(blueprints.list_blueprints)
    return jsonify({'success': True, 'blueprints': items, 'error': error})


@settings_bp.route('/api/blueprints/install', methods=['POST'])
def install_blueprint():
    import blueprints
    name = _json().get('file')
    if not isinstance(name, str) or not name:
        raise ApiError('file : nom du blueprint attendu')
    return jsonify({'success': True, **_blueprints(blueprints.install, name)})
