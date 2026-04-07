"""Gestion des entites Home Assistant pour ClapTrap.

Utilise MQTT Discovery si un broker MQTT est disponible (regroupe les entites
dans un appareil "ClapTrap"). Sinon fallback sur l'API REST du Supervisor.

Entites par source :
- binary_sensor.claptrap_<id> : ON quand un clap est detecte (auto-OFF apres 2s)
- sensor.claptrap_<id>_clap_count : nombre de claps du dernier evenement

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
_source_info = {}  # source_id -> {slug, label}
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
        logging.debug("paho-mqtt non installe, fallback API REST")
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
        logging.debug(f"MQTT non disponible: {e}")
        _mqtt_available = False
        return False


def _mqtt_publish(topic, payload, retain=True):
    """Publie un message MQTT."""
    if _mqtt_client:
        _mqtt_client.publish(topic, json.dumps(payload), retain=retain)


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


# ===== Public API =====

def init_entities():
    """Initialise le systeme d'entites (tente MQTT, sinon REST)."""
    _init_mqtt()
    if _mqtt_available:
        logging.info("Entites HA via MQTT Discovery (appareil ClapTrap)")
    else:
        logging.info("Entites HA via API REST (pas d'appareil, MQTT non disponible)")


def register_source(source_id, label=None):
    """Enregistre les entites pour une source."""
    display_name = label or source_id
    slug = _make_slug(source_id)
    _source_info[source_id] = {'slug': slug, 'label': display_name}

    if _mqtt_available:
        # Binary sensor (clap detected)
        _register_mqtt_entity('binary_sensor', slug, {
            'name': display_name,
            'unique_id': f'claptrap_{slug}',
            'object_id': f'claptrap_{slug}',
            'state_topic': f'claptrap/{slug}/state',
            'device_class': 'sound',
            'icon': 'mdi:hand-clap',
            'payload_on': 'ON',
            'payload_off': 'OFF',
            'json_attributes_topic': f'claptrap/{slug}/attributes',
            'device': _device_block()
        })
        # Sensor (clap count)
        _register_mqtt_entity('sensor', f'{slug}_clap_count', {
            'name': f'{display_name} Clap Count',
            'unique_id': f'claptrap_{slug}_clap_count',
            'object_id': f'claptrap_{slug}_clap_count',
            'state_topic': f'claptrap/{slug}/clap_count',
            'icon': 'mdi:counter',
            'unit_of_measurement': 'claps',
            'device': _device_block()
        })
        # Init states
        _mqtt_publish(f'claptrap/{slug}/state', 'OFF')
        _mqtt_publish(f'claptrap/{slug}/clap_count', '0')
    else:
        _set_rest_state(f'binary_sensor.claptrap_{slug}', 'off', {
            'friendly_name': f'ClapTrap {display_name}',
            'icon': 'mdi:hand-clap',
            'device_class': 'sound'
        })
        _set_rest_state(f'sensor.claptrap_{slug}_clap_count', '0', {
            'friendly_name': f'ClapTrap {display_name} Clap Count',
            'icon': 'mdi:counter',
            'unit_of_measurement': 'claps'
        })

    logging.info(f"Entite HA: claptrap_{slug} ({display_name})")


def unregister_source(source_id):
    """Supprime les entites d'une source."""
    info = _source_info.pop(source_id, None)
    if not info:
        return
    slug = info['slug']
    if _mqtt_available:
        _unregister_mqtt_entity('binary_sensor', slug)
        _unregister_mqtt_entity('sensor', f'{slug}_clap_count')
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
    else:
        _set_rest_state('binary_sensor.claptrap_detection', 'on' if running else 'off', {
            'friendly_name': 'ClapTrap Detection',
            'icon': 'mdi:ear-hearing',
            'sources': sources or []
        })


def on_clap_detected(source_id, score, clap_count):
    """Appele quand un clap est detecte."""
    info = _source_info.get(source_id, {})
    slug = info.get('slug', source_id)
    display_name = info.get('label', source_id)

    if _mqtt_available:
        _mqtt_publish(f'claptrap/{slug}/state', 'ON')
        _mqtt_publish(f'claptrap/{slug}/clap_count', str(clap_count))
        _mqtt_publish(f'claptrap/{slug}/attributes', {
            'score': round(score, 3),
            'clap_count': clap_count,
            'last_detection': time.strftime('%Y-%m-%dT%H:%M:%S')
        })
        # Auto-OFF
        def _off():
            time.sleep(2)
            _mqtt_publish(f'claptrap/{slug}/state', 'OFF')
        threading.Thread(target=_off, daemon=True).start()
    else:
        _set_rest_state(f'binary_sensor.claptrap_{slug}', 'on', {
            'friendly_name': f'ClapTrap {display_name}',
            'icon': 'mdi:hand-clap',
            'device_class': 'sound',
            'score': round(score, 3),
            'clap_count': clap_count,
            'last_detection': time.strftime('%Y-%m-%dT%H:%M:%S')
        })
        _set_rest_state(f'sensor.claptrap_{slug}_clap_count', str(clap_count), {
            'friendly_name': f'ClapTrap {display_name} Clap Count',
            'icon': 'mdi:counter',
            'unit_of_measurement': 'claps'
        })
        def _off():
            time.sleep(2)
            _set_rest_state(f'binary_sensor.claptrap_{slug}', 'off', {
                'friendly_name': f'ClapTrap {display_name}',
                'icon': 'mdi:hand-clap',
                'device_class': 'sound'
            })
        threading.Thread(target=_off, daemon=True).start()
