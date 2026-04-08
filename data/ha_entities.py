"""Gestion des entites Home Assistant pour ClapTrap.

Utilise MQTT Discovery si un broker MQTT est disponible (regroupe les entites
dans un appareil "ClapTrap"). Sinon fallback sur l'API REST du Supervisor.

Entites par source :
- binary_sensor.claptrap_<id>_Nclap(s) : un par nombre de claps configure (1-4)

Entite globale :
- binary_sensor.claptrap_detection : ON quand la detection tourne
"""

import os
import time
import logging
import threading
import json
import requests

SUPERVISOR_URL = "http://supervisor/core/api"
MQTT_TOPIC_PREFIX = "homeassistant"

# State
_source_info = {}  # source_id -> {slug, label, clap_counts}
_source_id_map = {}  # source_id technique -> entity_id
_mqtt_client = None
_mqtt_available = False


def _get_headers():
    token = os.environ.get('SUPERVISOR_TOKEN', '')
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }


def _make_slug(entity_id):
    """Cree un slug stable. Ex: mic_7, rtsp_a1b2c3d4, vban_0."""
    s = entity_id.lower()
    s = ''.join(c if c.isalnum() else '_' for c in s)
    while '__' in s:
        s = s.replace('__', '_')
    return s.strip('_')


def _device_block():
    """Block MQTT device pour regrouper les entites."""
    return {
        "identifiers": ["claptrap"],
        "name": "ClapTrap",
        "manufacturer": "Korben & Les Freres Poulain",
        "model": "ClapTrap Audio Detector",
        "sw_version": _get_version()
    }


def _get_version():
    try:
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'config.yaml')
        if os.path.exists(config_path):
            with open(config_path) as f:
                for line in f:
                    if line.startswith('version:'):
                        return line.split(':', 1)[1].strip().strip('"')
    except Exception:
        pass
    return "unknown"


# ===== MQTT Discovery =====

def _init_mqtt():
    """Tente de se connecter au broker MQTT via l'API Supervisor."""
    global _mqtt_client, _mqtt_available
    if _mqtt_client is not None:
        return _mqtt_available

    try:
        import paho.mqtt.client as mqtt_module
    except ImportError:
        logging.warning("paho-mqtt non installe (rebuild l'addon pour l'installer), fallback API REST")
        _mqtt_available = False
        return False

    # Recuperer les infos MQTT depuis le Supervisor
    try:
        resp = requests.get(
            'http://supervisor/services/mqtt',
            headers=_get_headers(),
            timeout=5
        )
        if not resp.ok:
            logging.debug(f"MQTT service non disponible via Supervisor: {resp.status_code}")
            _mqtt_available = False
            return False

        mqtt_info = resp.json().get('data', {})
        host = mqtt_info.get('host', 'core-mosquitto')
        port = int(mqtt_info.get('port', 1883))
        username = mqtt_info.get('username', '')
        password = mqtt_info.get('password', '')

        # Compatibilite paho-mqtt v1 et v2
        try:
            # paho-mqtt v2.x
            from paho.mqtt.enums import CallbackAPIVersion
            client = mqtt_module.Client(client_id="claptrap_addon", callback_api_version=CallbackAPIVersion.VERSION1)
        except (ImportError, AttributeError):
            # paho-mqtt v1.x
            client = mqtt_module.Client(client_id="claptrap_addon")

        if username:
            client.username_pw_set(username, password)
        client.connect(host, port, keepalive=60)
        client.loop_start()
        _mqtt_client = client
        _mqtt_available = True
        logging.info(f"MQTT connecte a {host}:{port}")
        return True

    except Exception as e:
        logging.warning(f"MQTT non disponible: {e}")
        _mqtt_available = False
        return False


def _mqtt_publish(topic, payload, retain=True):
    """Publie un message MQTT."""
    if _mqtt_client:
        if isinstance(payload, (dict, list)):
            data = json.dumps(payload)
        else:
            data = str(payload)
        _mqtt_client.publish(topic, data, retain=retain)


def _register_mqtt_entity(component, object_id, config):
    """Enregistre une entite via MQTT Discovery."""
    topic = f"{MQTT_TOPIC_PREFIX}/{component}/claptrap/{object_id}/config"
    _mqtt_publish(topic, config)


def _unregister_mqtt_entity(component, object_id):
    """Supprime une entite via MQTT Discovery (payload vide)."""
    topic = f"{MQTT_TOPIC_PREFIX}/{component}/claptrap/{object_id}/config"
    if _mqtt_client:
        _mqtt_client.publish(topic, "", retain=True)


def _set_mqtt_state(topic, payload):
    """Publie l'etat d'une entite."""
    _mqtt_publish(topic, payload, retain=False)


# ===== API REST fallback =====

def _set_rest_state(entity_id, state, attributes=None):
    """Met a jour l'etat d'une entite HA via API REST."""
    token = os.environ.get('SUPERVISOR_TOKEN', '')
    if not token:
        return
    try:
        requests.post(
            f"{SUPERVISOR_URL}/states/{entity_id}",
            headers=_get_headers(),
            json={'state': state, 'attributes': attributes or {}},
            timeout=5
        )
    except Exception as e:
        logging.debug(f"Erreur REST entite {entity_id}: {e}")


def rtsp_entity_id(stream_or_id):
    """Génère l'entity_id pour un flux RTSP depuis son ID ou dict."""
    if isinstance(stream_or_id, dict):
        stream_id = stream_or_id.get('id', '') or stream_or_id.get('stream_id', '')
        name = stream_or_id.get('name', 'unknown')
    else:
        stream_id = str(stream_or_id)
        name = 'unknown'
    return f"rtsp_{stream_id[:8]}" if stream_id else f"rtsp_{name}"


# ===== Public API =====

def _cleanup_old_rest_entities():
    """Supprime les anciennes entites creees via l'API REST (avant MQTT Discovery)."""
    token = os.environ.get('SUPERVISOR_TOKEN', '')
    if not token:
        return
    try:
        # Lister toutes les entites
        resp = requests.get(
            f"{SUPERVISOR_URL}/states",
            headers=_get_headers(),
            timeout=10
        )
        if not resp.ok:
            return
        states = resp.json()
        removed = 0
        for state in states:
            entity_id = state.get('entity_id', '')
            if not entity_id.startswith(('binary_sensor.claptrap_', 'sensor.claptrap_')):
                continue
            # Tenter la suppression via l'entity registry API
            try:
                resp2 = requests.post(
                    f"{SUPERVISOR_URL}/services/homeassistant/remove_entity",
                    headers=_get_headers(),
                    json={'entity_id': entity_id},
                    timeout=5
                )
                if resp2.ok:
                    removed += 1
                    continue
            except Exception:
                pass
            # Fallback : marquer comme unavailable puis supprimer le state
            try:
                requests.post(
                    f"{SUPERVISOR_URL}/states/{entity_id}",
                    headers=_get_headers(),
                    json={'state': 'unavailable', 'attributes': {}},
                    timeout=5
                )
            except Exception:
                pass
        if removed > 0:
            logging.info(f"Nettoyage: {removed} ancienne(s) entite(s) supprimee(s)")
        else:
            old_count = sum(1 for s in states if s.get('entity_id', '').startswith(('binary_sensor.claptrap_', 'sensor.claptrap_')))
            if old_count > 0:
                logging.warning(f"{old_count} ancienne(s) entite(s) ClapTrap ne peuvent pas etre supprimees automatiquement. "
                              "Supprimez-les manuellement : Parametres > Appareils > Entites > chercher 'claptrap' > supprimer celles dans 'Non groupe'.")
    except Exception as e:
        logging.debug(f"Erreur nettoyage entites: {e}")


def _cleanup_all_claptrap_entities():
    """Supprime TOUTES les entites ClapTrap (MQTT Discovery + entity registry + states)."""
    token = os.environ.get('SUPERVISOR_TOKEN', '')
    if not token:
        return
    try:
        resp = requests.get(
            f"{SUPERVISOR_URL}/states",
            headers=_get_headers(),
            timeout=10
        )
        if not resp.ok:
            return
        states = resp.json()
        removed = 0
        for state in states:
            entity_id = state.get('entity_id', '')
            if not entity_id.startswith(('binary_sensor.claptrap_', 'sensor.claptrap_')):
                continue

            slug = entity_id.split('.claptrap_', 1)[1] if '.claptrap_' in entity_id else ''
            component = entity_id.split('.')[0]

            # 1. Supprimer via MQTT Discovery (payload vide)
            if _mqtt_client and slug:
                _unregister_mqtt_entity(component, slug)

            # 2. Supprimer du state registry (DELETE /api/states/)
            try:
                requests.delete(
                    f"{SUPERVISOR_URL}/states/{entity_id}",
                    headers=_get_headers(),
                    timeout=5
                )
            except Exception:
                pass

            removed += 1

        if removed > 0:
            logging.info(f"Nettoyage: {removed} entite(s) ClapTrap supprimee(s)")
    except Exception as e:
        logging.debug(f"Erreur nettoyage entites: {e}")


def _cleanup_old_mqtt_entities(settings=None):
    """Supprime les anciennes entites MQTT (format pre-v6 : sans suffixe clap)."""
    if not _mqtt_client:
        return
    slugs_to_clean = []
    if settings:
        # Micro
        mic = settings.get('microphone', {})
        if mic.get('enabled', False):
            slugs_to_clean.append(_make_slug('mic_' + str(mic.get('device_index', 0))))
        # RTSP
        for src in settings.get('rtsp_sources', []):
            entity_id = rtsp_entity_id(src)
            slugs_to_clean.append(_make_slug(entity_id))
    # Supprimer les anciens formats pour chaque slug
    for slug in slugs_to_clean:
        _unregister_mqtt_entity('binary_sensor', slug)
        _unregister_mqtt_entity('sensor', f'{slug}_clap_count')
    if slugs_to_clean:
        logging.info(f"Anciennes entites MQTT (pre-v6) nettoyees: {len(slugs_to_clean)} source(s)")


def init_entities(settings=None):
    """Initialise le systeme d'entites (tente MQTT, sinon REST)."""
    _init_mqtt()
    if _mqtt_available:
        _cleanup_old_rest_entities()
        _cleanup_all_claptrap_entities()
        logging.info("Entites HA via MQTT Discovery (appareil ClapTrap)")
    else:
        logging.info("Entites HA via API REST (pas d'appareil, MQTT non disponible)")


def get_entities_info():
    """Retourne les entites HA enregistrees par source."""
    result = {}
    for source_id, info in _source_info.items():
        slug = info['slug']
        clap_counts = info.get('clap_counts', [1, 2])
        result[source_id] = {
            'label': info['label'],
            'entities': [
                f'binary_sensor.claptrap_{slug}_{n}clap{"s" if n > 1 else ""}' for n in clap_counts
            ]
        }
    # Ajouter l'entite globale
    result['_global'] = {
        'label': 'Detection',
        'entities': ['binary_sensor.claptrap_detection']
    }
    return result


def register_source(source_id, label=None, technical_id=None, clap_counts=None):
    """Enregistre les entites pour une source.

    Args:
        source_id: ID pour l'entite (ex: 'rtsp_5cabeef8', 'mic_7')
        label: Nom lisible (ex: 'RTSP: Bureau 2')
        technical_id: ID technique utilise dans les callbacks (ex: 'rtsp_rtsp://admin:...')
        clap_counts: Liste des nombres de claps pour lesquels creer des entites (1-4)
    """
    if clap_counts is None:
        clap_counts = [1, 2]
    # Filtrer pour garder uniquement 1-4 (liste vide = pas d'entités)
    clap_counts = [n for n in clap_counts if 1 <= n <= 4]

    display_name = label or source_id
    slug = _make_slug(source_id)

    # Si déjà enregistré avec le même slug et les mêmes clap_counts, skip
    existing = _source_info.get(source_id)
    if existing and existing['slug'] == slug and existing.get('clap_counts') == clap_counts:
        # Juste mettre à jour le mapping technique
        if technical_id:
            _source_id_map[technical_id] = source_id
        return

    _source_info[source_id] = {'slug': slug, 'label': display_name, 'clap_counts': clap_counts}
    if technical_id:
        _source_id_map[technical_id] = source_id

    if _mqtt_available:
        # D'abord supprimer TOUTES les entites clap (1-4) pour cette source
        for n in range(1, 5):
            old_slug = f"{slug}_{n}clap" if n == 1 else f"{slug}_{n}claps"
            _unregister_mqtt_entity('binary_sensor', old_slug)

        # Puis creer seulement celles configurees
        for n in clap_counts:
            slug_n = f"{slug}_{n}clap" if n == 1 else f"{slug}_{n}claps"
            _register_mqtt_entity('binary_sensor', slug_n, {
                'name': f'{display_name} {n} clap{"s" if n > 1 else ""}',
                'unique_id': f'claptrap_{slug_n}',
                'object_id': f'claptrap_{slug_n}',
                'state_topic': f'claptrap/{slug_n}/state',
                'device_class': 'sound',
                'icon': 'mdi:hand-clap',
                'payload_on': 'ON',
                'payload_off': 'OFF',
                'device': _device_block()
            })
            _mqtt_publish(f'claptrap/{slug_n}/state', 'OFF')
    # Pas de fallback REST (cree des entites orphelines)

    logging.info(f"Entites HA: claptrap_{slug} clap_counts={clap_counts} ({display_name})")


def unregister_source(source_id):
    """Supprime les entites d'une source."""
    info = _source_info.pop(source_id, None)
    if not info:
        return
    slug = info['slug']
    clap_counts = info.get('clap_counts', [1, 2])
    if _mqtt_available:
        for n in clap_counts:
            slug_n = f"{slug}_{n}clap" if n == 1 else f"{slug}_{n}claps"
            _unregister_mqtt_entity('binary_sensor', slug_n)
        logging.info(f"Entites MQTT supprimees pour {slug}")


def update_detection_state(running, sources=None):
    """Met a jour l'entite binary_sensor.claptrap_detection."""
    if _mqtt_available:
        if not hasattr(update_detection_state, '_registered'):
            _register_mqtt_entity('binary_sensor', 'detection', {
                'name': 'Detection',
                'unique_id': 'claptrap_detection',
                'object_id': 'claptrap_detection',
                'state_topic': 'claptrap/detection/state',
                'icon': 'mdi:ear-hearing',
                'payload_on': 'ON',
                'payload_off': 'OFF',
                'json_attributes_topic': 'claptrap/detection/attributes',
                'device': _device_block()
            })
            update_detection_state._registered = True
        _mqtt_publish('claptrap/detection/state', 'ON' if running else 'OFF')
        _mqtt_publish('claptrap/detection/attributes', {
            'sources': sources or []
        })
    # Pas de fallback REST (cree des entites orphelines sans unique_id)


def on_clap_detected(source_id, score, clap_count):
    """Appele quand un clap est detecte."""
    # Resoudre le source_id technique vers l'entity_id
    entity_key = _source_id_map.get(source_id, source_id)
    info = _source_info.get(entity_key, {})
    slug = info.get('slug', source_id)
    clap_counts = info.get('clap_counts', [1, 2])

    # Cap a 4
    if clap_count > 4:
        clap_count = 4

    # Ignorer si ce clap_count n'est pas configure
    if clap_count not in clap_counts:
        return

    slug_n = f"{slug}_{clap_count}clap" if clap_count == 1 else f"{slug}_{clap_count}claps"

    if _mqtt_available:
        _mqtt_publish(f'claptrap/{slug_n}/state', 'ON')
        # Auto-OFF apres 2s
        def _off():
            time.sleep(2)
            _mqtt_publish(f'claptrap/{slug_n}/state', 'OFF')
        threading.Thread(target=_off, daemon=True).start()
