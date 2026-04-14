from flask import Blueprint, jsonify, request
import json
import logging
import os
from datetime import datetime
import requests

from settings_manager import load_settings, save_settings, SETTINGS_FILE
from webhook import WebhookManager

settings_bp = Blueprint('settings', __name__)

# Singleton WebhookManager (réutilise le pool de connexions HTTP)
_webhook_manager = WebhookManager()


@settings_bp.route('/api/settings/save', methods=['POST'])
def save_settings_api():
    try:
        settings = request.json
        if not settings:
            return jsonify({'error': 'Aucun paramètre fourni'}), 400

        success, message = save_settings(settings)
        if success:
            # Hot-reload du serveur Wyoming si la section a change
            if 'wyoming' in settings:
                try:
                    from wyoming_server import restart_wyoming
                    # _socketio_ref a deja ete capture au boot dans app.py
                    restart_wyoming(None)
                except Exception as exc:
                    logging.warning(f"Wyoming hot-reload failed: {exc}")
            return jsonify({'success': True, 'message': message})
        else:
            return jsonify({'error': message}), 400

    except Exception as e:
        return jsonify({'error': str(e)}), 400


@settings_bp.route('/api/settings/debug', methods=['PUT'])
def toggle_debug():
    try:
        data = request.get_json()
        enabled = bool(data.get('enabled', False))
        settings = load_settings()
        settings['global']['debug'] = enabled
        save_settings(settings)
        # Appliquer immédiatement
        level = logging.DEBUG if enabled else logging.INFO
        logging.getLogger().setLevel(level)
        logging.info(f"Logs debug {'actives' if enabled else 'desactives'}")
        return jsonify({'success': True, 'debug': enabled})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@settings_bp.route('/api/settings/advanced', methods=['PUT'])
def update_advanced_settings():
    try:
        data = request.get_json()
        settings = load_settings()
        for key in ['delay', 'peak_cooldown', 'peak_ratio']:
            if key in data:
                settings['global'][key] = float(data[key])
        save_settings(settings)

        # Appliquer en temps réel sur les detectors actifs
        try:
            from classify import update_advanced_params
            update_advanced_params(
                peak_cooldown=data.get('peak_cooldown'),
                peak_ratio=data.get('peak_ratio'),
                delay=data.get('delay')
            )
        except Exception:
            pass

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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
            file = request.files['file']
            imported = json.loads(file.read().decode('utf-8'))
        else:
            imported = request.get_json()
        if not isinstance(imported, dict):
            return jsonify({'error': 'Format invalide'}), 400
        success, msg = save_settings(imported)
        return jsonify({'success': success, 'message': msg})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@settings_bp.route('/api/wyoming/discover-targets', methods=['GET'])
def discover_wyoming_targets():
    """Liste les services Wyoming connus du supervisor (Whisper, Piper, etc.)
    pour aider l'utilisateur a configurer la cible de forward."""
    token = os.environ.get('SUPERVISOR_TOKEN')
    errors = {}
    if not token:
        return jsonify({'targets': [], '_meta': {'error': 'no SUPERVISOR_TOKEN'}})
    try:
        # 1. Liste des services discovery actifs
        items = []
        try:
            r = requests.get(
                'http://supervisor/discovery',
                headers={'Authorization': f'Bearer {token}'},
                timeout=5,
            )
            if r.ok:
                items = r.json().get('data', {}).get('discovery', []) or []
            else:
                errors['discovery'] = f'http {r.status_code}'
        except Exception as exc:
            errors['discovery'] = f'exc {exc!s}'

        # 2. Slug de l'addon courant pour s'auto-exclure
        self_slug = ''
        try:
            r2 = requests.get(
                'http://supervisor/addons/self/info',
                headers={'Authorization': f'Bearer {token}'},
                timeout=5,
            )
            if r2.ok:
                self_slug = r2.json().get('data', {}).get('slug', '')
        except Exception:
            pass

        # 3. Cache des metadata addon (nom court)
        addon_names = {}

        def _addon_name(slug):
            if not slug:
                return ''
            if slug in addon_names:
                return addon_names[slug]
            try:
                rr = requests.get(
                    f'http://supervisor/addons/{slug}/info',
                    headers={'Authorization': f'Bearer {token}'},
                    timeout=3,
                )
                if rr.ok:
                    name = rr.json().get('data', {}).get('name', slug)
                    addon_names[slug] = name
                    return name
            except Exception:
                pass
            addon_names[slug] = slug
            return slug

        targets = []
        seen_keys = set()

        def _add_target(host, port, addon, name=None):
            key = f"{host}:{port}"
            if not host or key in seen_keys:
                return
            seen_keys.add(key)
            targets.append({
                'host': host,
                'port': int(port),
                'addon': addon or '',
                'name': name or _addon_name(addon) or addon or host,
                'uri': f'tcp://{host}:{port}',
            })

        # 4a. Premiere passe : services /discovery actifs
        for it in items:
            if it.get('service') != 'wyoming':
                continue
            addon = it.get('addon', '')
            if addon and self_slug and addon == self_slug:
                continue
            uri = (it.get('config') or {}).get('uri', '')
            if not uri.startswith('tcp://'):
                continue
            rest = uri[6:]
            host, sep, port_s = rest.partition(':')
            try:
                port = int(port_s) if port_s else 10300
            except ValueError:
                port = 10300
            _add_target(host, port, addon)

        # 4b. Fallback 1 : enumerer les addons installes et tenter de lire leur
        # info detaillee (discovery + ports + hostname). Selon le hassio_role
        # /addons/<slug>/info peut etre limite, on tolerera les echecs.
        installed = []
        try:
            r3 = requests.get(
                'http://supervisor/addons',
                headers={'Authorization': f'Bearer {token}'},
                timeout=5,
            )
            if r3.ok:
                installed = r3.json().get('data', {}).get('addons', []) or []
            else:
                errors['addons'] = f'http {r3.status_code}'
        except Exception as exc:
            errors['addons'] = f'exc {exc!s}'

        for ad in installed:
            slug = ad.get('slug', '')
            if not slug or slug == self_slug:
                continue
            try:
                ri = requests.get(
                    f'http://supervisor/addons/{slug}/info',
                    headers={'Authorization': f'Bearer {token}'},
                    timeout=3,
                )
                if not ri.ok:
                    continue
                info = ri.json().get('data', {}) or {}
            except Exception:
                continue

            discovery = info.get('discovery') or []
            if 'wyoming' not in discovery:
                continue

            hostname = info.get('hostname') or slug.replace('_', '-')
            network = info.get('network') or {}
            port = None
            for key, _value in network.items():
                if not key.endswith('/tcp'):
                    continue
                try:
                    port = int(key.split('/')[0])
                    break
                except ValueError:
                    continue
            if port is None:
                continue
            addon_names[slug] = info.get('name', slug)
            _add_target(hostname, port, slug, info.get('name', slug))

        # 4c. Fallback 2 : mapping en dur des addons Wyoming connus (cas ou
        # /addons/<slug>/info ne retourne pas la discovery, ou l'API supervisor
        # est restreinte). On regarde simplement si le slug est installe.
        # Hostname HA = slug avec '_' -> '-'.
        known = {
            # slug : (default_port, friendly_name)
            'core_whisper':            (10300, 'Whisper'),
            'core_faster_whisper':     (10300, 'Faster Whisper'),
            'core_piper':              (10200, 'Piper (TTS)'),
            'core_openwakeword':       (10400, 'openWakeWord'),
            '47701997_whisper':        (10300, 'Whisper'),
            '47701997_piper':          (10200, 'Piper (TTS)'),
            '47701997_openwakeword':   (10400, 'openWakeWord'),
            '47701997_faster-whisper': (10300, 'Faster Whisper'),
        }
        installed_slugs = {ad.get('slug', '') for ad in installed}
        for slug, (port, friendly) in known.items():
            if slug not in installed_slugs:
                continue
            if slug == self_slug:
                continue
            hostname = slug.replace('_', '-')
            _add_target(hostname, port, slug, friendly)

        # 4d. Fallback 3 : interroger HA Core (homeassistant_api) pour lister
        # les config_entries du domaine 'wyoming'. Marche meme si /discovery
        # est restreint cote supervisor.
        ha_entries_count = 0
        try:
            rh = requests.get(
                'http://supervisor/core/api/config/config_entries/entry',
                headers={'Authorization': f'Bearer {token}'},
                timeout=5,
            )
            if not rh.ok:
                errors['ha_core'] = f'http {rh.status_code}'
            if rh.ok:
                entries = rh.json() or []
                for entry in entries:
                    if entry.get('domain') != 'wyoming':
                        continue
                    ha_entries_count += 1
                    title = entry.get('title') or ''
                    # data n'est pas expose par defaut sur cet endpoint ;
                    # le titre Wyoming integration est generalement "Host:Port"
                    if ':' in title:
                        host_part, _, port_part = title.rpartition(':')
                        try:
                            port = int(port_part)
                        except ValueError:
                            continue
                        host = host_part.strip()
                        if host:
                            _add_target(host, port, '', f'HA: {title}')
        except Exception as exc:
            errors['ha_core'] = f'exc {exc!s}'

        return jsonify({
            'targets': targets,
            '_meta': {
                'installed_count': len(installed),
                'discovery_count': len(items),
                'ha_entries_count': ha_entries_count,
                'self_slug': self_slug,
                'errors': errors,
            },
        })
    except Exception as e:
        return jsonify({'targets': [], 'error': str(e)})


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
