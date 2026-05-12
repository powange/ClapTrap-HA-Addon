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
_source_info = {}  # source_id -> {slug, label, groups: {group_slug: {name, clap_counts}}}
_source_id_map = {}  # source_id technique -> entity_id
_mqtt_client = None
_mqtt_available = False


def _group_object_id(source_slug, group_slug, n):
    """Construit l'object_id (sans le prefixe claptrap_) pour une entite clap.

    Tous les groupes (y compris le groupe auto-migre "clap") suivent la meme
    convention `{source}_{group}_{n}clap[s]`. Les automations HA existantes
    qui pointaient vers l'ancien naming `{source}_{n}clap[s]` doivent etre
    mises a jour manuellement.
    """
    suffix = f"{n}clap" if n == 1 else f"{n}claps"
    g = group_slug or 'clap'
    return f"{source_slug}_{g}_{suffix}"


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


def source_entity_key(source_type, source_data):
    """Génère un identifiant UNIQUE et STABLE pour une source.

    C'est LA seule fonction à utiliser pour dériver l'entity_id.
    Basé sur des données qui ne changent jamais :
    - mic: device_index
    - rtsp: UUID du stream (8 premiers chars)
    - vban: IP
    """
    if source_type == 'mic':
        idx = source_data.get('device_index', 0)
        return f"mic_{idx}"
    elif source_type == 'rtsp':
        stream_id = source_data.get('id', '') or source_data.get('stream_id', '')
        if stream_id:
            return f"rtsp_{stream_id[:8]}"
        return f"rtsp_{_make_slug(source_data.get('name', 'unknown'))}"
    elif source_type == 'vban':
        # Privilegier le nom (plus parlant pour l'utilisateur dans HA).
        # Fallback sur l'IP si le nom est absent.
        name = (source_data.get('name') or '').strip()
        if name:
            return f"vban_{_make_slug(name)}"
        ip = source_data.get('ip', '0')
        return f"vban_{_make_slug(ip)}"
    return f"source_{_make_slug(str(source_data))}"


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
        result = _mqtt_client.publish(topic, data, retain=retain)
        return result
    return None


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
    """DEPRECATED: utiliser source_entity_key('rtsp', data) à la place."""
    if isinstance(stream_or_id, dict):
        return source_entity_key('rtsp', stream_or_id)
    return source_entity_key('rtsp', {'id': str(stream_or_id)})


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
    """Supprime TOUTES les entites ClapTrap via entity registry + MQTT Discovery + states."""
    token = os.environ.get('SUPERVISOR_TOKEN', '')
    if not token:
        return
    try:
        # 1. Supprimer via l'entity registry API (suppression definitive)
        resp = requests.get(
            f"{SUPERVISOR_URL}/config/entity_registry",
            headers=_get_headers(),
            timeout=10
        )
        # Lister toutes les entites claptrap via les states
        resp = requests.get(
            f"{SUPERVISOR_URL}/states",
            headers=_get_headers(),
            timeout=10
        )
        if not resp.ok:
            return

        removed = 0
        for state in resp.json():
            entity_id = state.get('entity_id', '')
            if not entity_id.startswith(('binary_sensor.claptrap_', 'sensor.claptrap_')):
                continue

            slug = entity_id.split('.claptrap_', 1)[1] if '.claptrap_' in entity_id else ''
            component = entity_id.split('.')[0]

            # 1. MQTT Discovery via notre client
            if _mqtt_client and slug:
                _unregister_mqtt_entity(component, slug)

            # 2. MQTT Discovery via le service HA
            # Essayer le slug tel quel + variantes (HA ajoute des _ entre les mots)
            slugs_to_try = [slug]
            # Variante sans le suffixe _N (doublons HA)
            import re
            base = re.sub(r'_(\d+)$', '', slug)
            if base != slug:
                slugs_to_try.append(base)
            # Variante sans underscore entre le nombre et "claps" (1_clap -> 1clap)
            for s in list(slugs_to_try):
                fixed = re.sub(r'_(\d)_claps?$', lambda m: f'_{m.group(1)}clap{"s" if int(m.group(1)) > 1 else ""}', s)
                if fixed != s:
                    slugs_to_try.append(fixed)

            for try_slug in set(slugs_to_try):
                try:
                    requests.post(
                        f"{SUPERVISOR_URL}/services/mqtt/publish",
                        headers=_get_headers(),
                        json={
                            'topic': f'{MQTT_TOPIC_PREFIX}/{component}/claptrap/{try_slug}/config',
                            'payload': '',
                            'retain': True
                        },
                        timeout=5
                    )
                except Exception:
                    pass
            logging.info(f"MQTT cleanup: {slug} ({len(slugs_to_try)} variantes)")

            # 3. Supprimer le state
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
            logging.info(f"Nettoyage: {removed} entite(s) ClapTrap nettoyee(s). "
                        "Si des doublons persistent, supprimez-les manuellement dans HA: "
                        "Parametres > Appareils > ClapTrap > clic sur l'entite > Supprimer.")
    except Exception as e:
        logging.debug(f"Erreur nettoyage entites: {e}")


def _cleanup_old_mqtt_entities(settings=None):
    """Supprime les anciennes entites MQTT (format pre-v6 : sans suffixe clap)."""
    if not _mqtt_client:
        return
    slugs_to_clean = []
    if settings:
        mic = settings.get('microphone', {})
        if mic.get('enabled', False):
            slugs_to_clean.append(_make_slug(source_entity_key('mic', mic)))
        for src in settings.get('rtsp_sources', []):
            slugs_to_clean.append(_make_slug(source_entity_key('rtsp', src)))
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
    """Retourne les entites HA enregistrees par source (regroupees par groupe)."""
    result = {}
    for source_id, info in _source_info.items():
        source_slug = info['slug']
        groups = info.get('groups', {})
        groups_payload = []
        for g_slug, g_info in groups.items():
            entities = [
                f'binary_sensor.claptrap_{_group_object_id(source_slug, g_slug, n)}'
                for n in g_info.get('clap_counts', [1, 2])
            ]
            groups_payload.append({
                'slug': g_slug,
                'name': g_info.get('name', g_slug),
                'entities': entities,
            })
        result[source_id] = {
            'label': info['label'],
            'groups': groups_payload,
        }
    result['_global'] = {
        'label': 'Detection',
        'entities': ['binary_sensor.claptrap_detection']
    }
    return result


def _normalise_groups(groups, fallback_clap_counts=None):
    """Normalise une liste de groupes en {slug: {name, clap_counts}} compact."""
    out = {}
    if isinstance(groups, list) and groups:
        for idx, g in enumerate(groups):
            if not isinstance(g, dict):
                continue
            slug = g.get('slug') or f'group{idx + 1}'
            name = g.get('name') or slug
            counts = list(g.get('clap_counts') or g.get('ha_entities') or fallback_clap_counts or [1, 2])
            counts = [n for n in counts if 1 <= n <= 4]
            out[slug] = {'name': name, 'clap_counts': counts}
    if not out:
        # Fallback : un seul groupe "clap" avec les clap_counts donnes
        counts = [n for n in (fallback_clap_counts or [1, 2]) if 1 <= n <= 4]
        out['clap'] = {'name': 'Clap', 'clap_counts': counts}
    return out


def register_source(source_id, label=None, technical_id=None, clap_counts=None, groups=None):
    """Enregistre les entites pour une source (multi-groupes).

    Args:
        source_id: ID pour l'entite (ex: 'rtsp_5cabeef8', 'mic_7')
        label: Nom lisible (ex: 'RTSP: Bureau 2')
        technical_id: ID technique utilise dans les callbacks
        groups: Liste de groupes [{slug, name, clap_counts/ha_entities}].
                Si absent, on retombe sur clap_counts (compat retro) avec un
                seul groupe "Clap".
        clap_counts: DEPRECATED. Utilise par compat retro si `groups` absent.
    """
    norm_groups = _normalise_groups(groups, fallback_clap_counts=clap_counts)
    display_name = label or source_id
    source_slug = _make_slug(source_id)

    existing = _source_info.get(source_id)
    # Skip uniquement si rien n'a change (label, slug, groupes : noms ET counts).
    if (existing and existing['slug'] == source_slug
            and existing.get('label') == display_name
            and existing.get('groups') == norm_groups):
        if technical_id:
            _source_id_map[technical_id] = source_id
        return

    # On collecte les object_ids a supprimer :
    # 1) ceux qui disparaissent (groupe ou clap_count supprime)
    # 2) ceux dont le `name` (friendly name HA) doit changer
    #
    # Pourquoi (2) : HA, via MQTT Discovery, ignore parfois la mise a jour
    # du friendly name si on republie le config avec le meme unique_id.
    # Pour forcer l'update, il faut d'abord publier une config vide (HA
    # supprime l'entite), puis publier la nouvelle config (HA la recree
    # avec le nouveau nom).
    new_obj_ids = set()
    for g_slug, g_info in norm_groups.items():
        for n in g_info['clap_counts']:
            new_obj_ids.add(_group_object_id(source_slug, g_slug, n))

    def _friendly_name(display, g_slug, g_name, n):
        return f'{display} {g_name} {n} clap{"s" if n > 1 else ""}'

    to_unregister = set()
    if existing and existing.get('slug') == source_slug:
        old_display = existing.get('label') or source_id
        old_groups = existing.get('groups') or {}
        # 1) Entites qui disparaissent
        for g_slug, g_info in old_groups.items():
            for n in g_info.get('clap_counts', []):
                obj = _group_object_id(source_slug, g_slug, n)
                if obj not in new_obj_ids:
                    to_unregister.add(obj)
        # 2) Entites avec friendly name modifie
        for g_slug, g_info in norm_groups.items():
            old_g = old_groups.get(g_slug) or {}
            old_g_name = old_g.get('name', g_slug)
            new_g_name = g_info.get('name', g_slug)
            for n in g_info['clap_counts']:
                old_fn = _friendly_name(old_display, g_slug, old_g_name, n)
                new_fn = _friendly_name(display_name, g_slug, new_g_name, n)
                if old_fn != new_fn:
                    to_unregister.add(_group_object_id(source_slug, g_slug, n))
        for obj in to_unregister:
            _unregister_mqtt_entity('binary_sensor', obj)
    else:
        # Premiere registration : pas d'existing concret a nettoyer
        _unregister_all_entities_for_source(existing or {'slug': source_slug, 'groups': {}})

    _source_info[source_id] = {
        'slug': source_slug, 'label': display_name, 'groups': norm_groups,
    }
    if technical_id:
        _source_id_map[technical_id] = source_id

    if _mqtt_available:
        # Si on vient de desinscrire des entites pour un changement de nom,
        # laisser le broker / HA traiter le payload vide avant de republier.
        # Sans ce delai, HA peut coalescer la suppression et la creation, ce
        # qui empeche la mise a jour du friendly name.
        if to_unregister:
            time.sleep(0.5)
        for g_slug, g_info in norm_groups.items():
            g_name = g_info.get('name', g_slug)
            for n in g_info['clap_counts']:
                obj = _group_object_id(source_slug, g_slug, n)
                entity_name = _friendly_name(display_name, g_slug, g_name, n)
                _register_mqtt_entity('binary_sensor', obj, {
                    'name': entity_name,
                    'unique_id': f'claptrap_{obj}',
                    'object_id': f'claptrap_{obj}',
                    'state_topic': f'claptrap/{obj}/state',
                    'device_class': 'sound',
                    'icon': 'mdi:hand-clap',
                    'payload_on': 'ON',
                    'payload_off': 'OFF',
                    'device': _device_block()
                })
                _mqtt_publish(f'claptrap/{obj}/state', 'OFF')

    logging.info(
        f"Entites HA: claptrap_{source_slug} groupes={list(norm_groups.keys())} ({display_name})"
    )


def _unregister_all_entities_for_source(info):
    """Supprime toutes les entites MQTT enregistrees pour `info`."""
    if not _mqtt_available or not info:
        return
    source_slug = info.get('slug')
    if not source_slug:
        return
    for g_slug, g_info in (info.get('groups') or {}).items():
        for n in g_info.get('clap_counts', []):
            _unregister_mqtt_entity('binary_sensor', _group_object_id(source_slug, g_slug, n))


def unregister_source(source_id):
    """Supprime toutes les entites d'une source (tous groupes)."""
    info = _source_info.pop(source_id, None)
    if not info:
        return
    _unregister_all_entities_for_source(info)
    if _mqtt_available:
        logging.info(f"Entites MQTT supprimees pour {info['slug']}")


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


def on_clap_detected(source_id, score, clap_count, group_slug='clap', group_clap_counts=None):
    """Appele quand un clap est detecte. Route vers l'entite du bon groupe."""
    entity_key = _source_id_map.get(source_id, source_id)
    info = _source_info.get(entity_key, {})
    source_slug = info.get('slug', source_id)
    groups = info.get('groups') or {}
    group_info = groups.get(group_slug, {})
    clap_counts = group_info.get('clap_counts') or (group_clap_counts or [1, 2])

    if clap_count > 4:
        clap_count = 4

    logging.info(
        f"on_clap_detected: source={source_id}, group={group_slug}, slug={source_slug}, "
        f"clap_count={clap_count}, configured={clap_counts}, mqtt={_mqtt_available}"
    )

    if clap_count not in clap_counts:
        logging.info(f"on_clap_detected: clap_count {clap_count} pas dans {clap_counts}, ignoré")
        return

    obj = _group_object_id(source_slug, group_slug, clap_count)
    topic = f'claptrap/{obj}/state'

    if _mqtt_available:
        logging.info(f"MQTT publish: {topic} = ON")
        _mqtt_publish(topic, 'ON')
        def _off():
            time.sleep(2)
            _mqtt_publish(topic, 'OFF')
        threading.Thread(target=_off, daemon=True).start()
    else:
        logging.warning(f"MQTT non disponible pour publier {topic}")
