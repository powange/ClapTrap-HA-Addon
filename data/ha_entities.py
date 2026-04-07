"""Gestion des entités Home Assistant pour ClapTrap.

Crée et met à jour des entités HA via l'API REST du Supervisor :
- binary_sensor.claptrap_<source> : ON quand un clap est détecté (auto-OFF après 2s)
- sensor.claptrap_<source>_clap_count : nombre de claps du dernier événement
- binary_sensor.claptrap_detection : ON quand la détection tourne
"""

import os
import time
import logging
import threading
import requests

SUPERVISOR_URL = "http://supervisor/core/api"


def _get_headers():
    token = os.environ.get('SUPERVISOR_TOKEN', '')
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }


def _sanitize_source_id(source_id):
    """Convertit un source_id en identifiant HA valide (lowercase, underscores)."""
    s = source_id.lower()
    # Remove protocol prefixes
    for prefix in ['rtsp_rtsp://', 'rtsp_', 'vban_']:
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    # Keep only alphanumeric and underscores
    s = ''.join(c if c.isalnum() else '_' for c in s)
    # Remove consecutive underscores and trim
    while '__' in s:
        s = s.replace('__', '_')
    s = s.strip('_')
    # Prefix with source type
    if source_id.startswith('mic_'):
        return f"mic_{s}" if not s.startswith('mic') else s
    elif source_id.startswith('rtsp_'):
        return f"rtsp_{s}"
    elif source_id.startswith('vban_'):
        return f"vban_{s}"
    return s


def _set_state(entity_id, state, attributes=None):
    """Met à jour l'état d'une entité HA."""
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
        logging.debug(f"Erreur mise à jour entité {entity_id}: {e}")


def update_detection_state(running, sources=None):
    """Met à jour l'entité binary_sensor.claptrap_detection."""
    attrs = {
        'friendly_name': 'ClapTrap Detection',
        'icon': 'mdi:ear-hearing',
        'sources': sources or []
    }
    _set_state('binary_sensor.claptrap_detection', 'on' if running else 'off', attrs)


def on_clap_detected(source_id, score, clap_count):
    """Appelé quand un clap est détecté. Met à jour les entités de la source."""
    safe_id = _sanitize_source_id(source_id)

    # binary_sensor ON
    _set_state(f'binary_sensor.claptrap_{safe_id}', 'on', {
        'friendly_name': f'ClapTrap {safe_id}',
        'icon': 'mdi:hand-clap',
        'source_id': source_id,
        'score': round(score, 3),
        'clap_count': clap_count,
        'last_detection': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'device_class': 'sound'
    })

    # sensor clap_count
    _set_state(f'sensor.claptrap_{safe_id}_clap_count', str(clap_count), {
        'friendly_name': f'ClapTrap {safe_id} Clap Count',
        'icon': 'mdi:counter',
        'source_id': source_id,
        'score': round(score, 3),
        'unit_of_measurement': 'claps'
    })

    # Auto-OFF after 2 seconds
    def _auto_off():
        time.sleep(2)
        _set_state(f'binary_sensor.claptrap_{safe_id}', 'off', {
            'friendly_name': f'ClapTrap {safe_id}',
            'icon': 'mdi:hand-clap',
            'source_id': source_id,
            'device_class': 'sound'
        })
    threading.Thread(target=_auto_off, daemon=True).start()


def register_source(source_id):
    """Initialise les entités pour une source (état OFF)."""
    safe_id = _sanitize_source_id(source_id)
    _set_state(f'binary_sensor.claptrap_{safe_id}', 'off', {
        'friendly_name': f'ClapTrap {safe_id}',
        'icon': 'mdi:hand-clap',
        'source_id': source_id,
        'device_class': 'sound'
    })
    _set_state(f'sensor.claptrap_{safe_id}_clap_count', '0', {
        'friendly_name': f'ClapTrap {safe_id} Clap Count',
        'icon': 'mdi:counter',
        'source_id': source_id,
        'unit_of_measurement': 'claps'
    })
    logging.info(f"Entités HA enregistrées pour {source_id} -> claptrap_{safe_id}")
