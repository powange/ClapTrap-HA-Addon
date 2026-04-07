"""Gestion des entites Home Assistant pour ClapTrap.

Cree et met a jour des entites HA via l'API REST du Supervisor :
- binary_sensor.claptrap_<name> : ON quand un clap est detecte (auto-OFF apres 2s)
- sensor.claptrap_<name>_clap_count : nombre de claps du dernier evenement
- binary_sensor.claptrap_detection : ON quand la detection tourne
"""

import os
import time
import logging
import threading
import requests

SUPERVISOR_URL = "http://supervisor/core/api"

# Mapping source_id -> {label, slug} pour les noms d'entites
_source_labels = {}
_source_counter = {'mic': 0, 'rtsp': 0, 'vban': 0}


def _get_headers():
    token = os.environ.get('SUPERVISOR_TOKEN', '')
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }


def _make_entity_slug(label):
    """Convertit un label lisible en slug HA valide (lowercase, underscores)."""
    # "Micro: LifeCam Cinema" -> "micro_lifecam_cinema"
    # "RTSP: chambre" -> "rtsp_chambre"
    # "chambre" -> "chambre"
    s = label.lower()
    s = ''.join(c if c.isalnum() else '_' for c in s)
    while '__' in s:
        s = s.replace('__', '_')
    return s.strip('_')


def _set_state(entity_id, state, attributes=None):
    """Met a jour l'etat d'une entite HA."""
    token = os.environ.get('SUPERVISOR_TOKEN', '')
    if not token:
        return
    try:
        payload = {
            'state': state,
            'attributes': attributes or {}
        }
        requests.post(
            f"{SUPERVISOR_URL}/states/{entity_id}",
            headers=_get_headers(),
            json=payload,
            timeout=5
        )
    except Exception as e:
        logging.debug(f"Erreur mise a jour entite {entity_id}: {e}")


def update_detection_state(running, sources=None):
    """Met a jour l'entite binary_sensor.claptrap_detection."""
    attrs = {
        'friendly_name': 'ClapTrap Detection',
        'icon': 'mdi:ear-hearing',
        'sources': sources or []
    }
    _set_state('binary_sensor.claptrap_detection', 'on' if running else 'off', attrs)


def register_source(source_id, label=None):
    """Initialise les entites pour une source (etat OFF).

    Args:
        source_id: ID technique (ex: "mic_7", "rtsp_rtsp://...")
        label: Nom lisible (ex: "chambre", "LifeCam Cinema"). Utilise pour le friendly_name.
    """
    # Determiner le type et generer un slug court (mic_1, rtsp_1, rtsp_2...)
    source_type = 'mic' if source_id.startswith('mic') else 'rtsp' if source_id.startswith('rtsp') else 'vban'
    _source_counter[source_type] = _source_counter.get(source_type, 0) + 1
    slug = f"{source_type}_{_source_counter[source_type]}"

    display_name = label or source_id
    _source_labels[source_id] = {'label': display_name, 'slug': slug}

    _set_state(f'binary_sensor.claptrap_{slug}', 'off', {
        'friendly_name': f'ClapTrap {display_name}',
        'icon': 'mdi:hand-clap',
        'source_id': source_id,
        'device_class': 'sound'
    })
    _set_state(f'sensor.claptrap_{slug}_clap_count', '0', {
        'friendly_name': f'ClapTrap {display_name} Clap Count',
        'icon': 'mdi:counter',
        'source_id': source_id,
        'unit_of_measurement': 'claps'
    })
    logging.info(f"Entites HA enregistrees: claptrap_{slug} ({display_name})")


def on_clap_detected(source_id, score, clap_count):
    """Appele quand un clap est detecte. Met a jour les entites de la source."""
    info = _source_labels.get(source_id, {})
    display_name = info.get('label', source_id)
    slug = info.get('slug', _make_entity_slug(display_name))

    # binary_sensor ON
    _set_state(f'binary_sensor.claptrap_{slug}', 'on', {
        'friendly_name': f'ClapTrap {display_name}',
        'icon': 'mdi:hand-clap',
        'source_id': source_id,
        'score': round(score, 3),
        'clap_count': clap_count,
        'last_detection': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'device_class': 'sound'
    })

    # sensor clap_count
    _set_state(f'sensor.claptrap_{slug}_clap_count', str(clap_count), {
        'friendly_name': f'ClapTrap {display_name} Clap Count',
        'icon': 'mdi:counter',
        'source_id': source_id,
        'score': round(score, 3),
        'unit_of_measurement': 'claps'
    })

    # Auto-OFF after 2 seconds
    def _auto_off():
        time.sleep(2)
        _set_state(f'binary_sensor.claptrap_{slug}', 'off', {
            'friendly_name': f'ClapTrap {display_name}',
            'icon': 'mdi:hand-clap',
            'source_id': source_id,
            'device_class': 'sound'
        })
    threading.Thread(target=_auto_off, daemon=True).start()
