"""Shared audio utility functions.

Extracted from app.py to break the circular import between app.py and classify.py.
"""

import json
import logging
import os
import time

import requests
import sounddevice as sd

_audio_devices_cache = None
_audio_devices_cache_time = 0


def get_audio_input_devices():
    """Recupere les peripheriques audio d'entree via l'API Supervisor HA, avec fallback sur sounddevice. Cache 60s."""
    global _audio_devices_cache, _audio_devices_cache_time
    now = time.time()
    if _audio_devices_cache is not None and (now - _audio_devices_cache_time) < 60:
        return _audio_devices_cache

    # Essayer l'API Supervisor de Home Assistant
    supervisor_token = os.environ.get('SUPERVISOR_TOKEN')
    if supervisor_token:
        try:
            resp = requests.get(
                'http://supervisor/audio/info',
                headers={'Authorization': f'Bearer {supervisor_token}'},
                timeout=5
            )
            if resp.ok:
                data = resp.json()
                logging.info(f"Reponse API audio: {json.dumps(data, indent=2)[:500]}")
                # L'API retourne {"result": "ok", "data": {"audio": {"input": [...]}}}
                audio_data = data.get('data', data)
                sources = audio_data.get('audio', {}).get('input', [])
                if sources:
                    devices = [
                        {
                            'index': source.get('index', idx),
                            'name': source.get('description', source.get('name', f'Device {idx}')),
                            'pulse_name': source.get('name', '')
                        }
                        for idx, source in enumerate(sources)
                    ]
                    logging.info(f"Peripheriques audio detectes via Supervisor: {devices}")
                    _audio_devices_cache = devices
                    _audio_devices_cache_time = now
                    return devices
                else:
                    logging.warning("API Supervisor: aucune source audio d'entree trouvee")
            else:
                logging.warning(f"API Supervisor audio: HTTP {resp.status_code}")
        except Exception as e:
            logging.warning(f"Impossible de recuperer les sources audio via l'API Supervisor: {e}")

    # Fallback sur sounddevice
    try:
        all_devices = sd.query_devices()
        result = [
            {'index': idx, 'name': device['name']}
            for idx, device in enumerate(all_devices)
            if device['max_input_channels'] > 0
        ]
        _audio_devices_cache = result
        _audio_devices_cache_time = now
        return result
    except Exception as e:
        logging.error(f"Impossible de lister les peripheriques audio: {e}")
        return []
