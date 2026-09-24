"""Shared audio utility functions.

Extracted from app.py to break the circular import between app.py and classify.py.
"""

import json
import logging
import os
import subprocess
import threading
import time

import requests

SAMPLE_RATE = 16000   # frequence attendue par YAMNet
BLOCK_SAMPLES = 1600  # 100 ms a 16 kHz : un bloc = un pas de detection


def level_db(peak):
    """Niveau d'un pic (0..1) en dB, borne a -60 dB (VU-metres)."""
    import math
    return round(max(-60.0, 20 * math.log10(min(1.0, max(0.0, peak)) + 1e-10)), 1)


def set_pulse_volume(pulse_name, volume_percent):
    """Règle le volume d'une source PulseAudio via pactl."""
    try:
        subprocess.run(['pactl', 'set-source-volume', pulse_name, f'{volume_percent}%'],
                       capture_output=True, text=True, timeout=5)
    except Exception as e:
        logging.warning(f"pactl set-source-volume: {e}")


def drain_stderr(proc, name, sanitize=None, on_line=None):
    """Lit stderr ligne par ligne et le jette (log DEBUG).

    Un `proc.stderr.read()` accumulait TOUT stderr en memoire jusqu'a l'EOF
    (fuite sur un process qui tourne des semaines).
    """
    def _drain():
        try:
            for raw in iter(proc.stderr.readline, b''):
                line = raw.decode('utf-8', errors='replace').rstrip()
                if sanitize:
                    line = sanitize(line)
                if on_line and line:
                    on_line(line)
                logging.debug(f"{name}: {line}")
        except Exception:
            pass
    threading.Thread(target=_drain, daemon=True).start()


def terminate_process(proc, timeout=2):
    """Termine un subprocess proprement (et le reap pour eviter un zombie)."""
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=timeout)
        except Exception:
            pass

_audio_devices_cache = None
_audio_devices_cache_time = 0


def get_audio_input_devices():
    """Recupere les peripheriques audio d'entree via l'API Supervisor HA, avec repli sur `pactl`. Cache 60s."""
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
                logging.debug(f"Reponse API audio: {json.dumps(data, indent=2)[:500]}")
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
                    logging.debug(f"Peripheriques audio detectes via Supervisor: {devices}")
                    _audio_devices_cache = devices
                    _audio_devices_cache_time = now
                    return devices
                else:
                    logging.warning("API Supervisor: aucune source audio d'entree trouvee")
            else:
                logging.warning(f"API Supervisor audio: HTTP {resp.status_code}")
        except Exception as e:
            logging.warning(f"Impossible de recuperer les sources audio via l'API Supervisor: {e}")

    # Repli : sources PulseAudio via pactl (hors Supervisor, dev local).
    # Remplace sounddevice/PortAudio, qui n'etait utilise que pour ce repli.
    try:
        out = subprocess.run(['pactl', 'list', 'sources', 'short'],
                             capture_output=True, text=True, timeout=5).stdout
        result = []
        for line in out.splitlines():
            cols = line.split('\t')
            if len(cols) >= 2 and not cols[1].endswith('.monitor'):
                result.append({'index': int(cols[0]) if cols[0].isdigit() else len(result),
                               'name': cols[1], 'pulse_name': cols[1]})
        _audio_devices_cache = result
        _audio_devices_cache_time = now
        return result
    except Exception as e:
        logging.error(f"Impossible de lister les peripheriques audio: {e}")
        return []
