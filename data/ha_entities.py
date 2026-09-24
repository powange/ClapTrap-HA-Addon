"""Gestion des entites Home Assistant pour ClapTrap (MQTT Discovery).

Le broker MQTT est obligatoire (`services: mqtt:need`) : toutes les entites
sont regroupees dans un appareil "ClapTrap".

Entites par source et par groupe de sons :
- binary_sensor.claptrap_<source>_<groupe>_<N>clap(s) : un par nombre de claps (1-4)

Entite globale :
- binary_sensor.claptrap_detection : ON quand la detection tourne

Disponibilite : chaque entite depend de `claptrap/availability` (LWT du
client : passe a "offline" si l'add-on s'arrete ou plante) et de la
disponibilite de sa source (`claptrap/<source>/availability`), "online"
seulement quand la source ecoute vraiment (detection lancee, flux recu). Une
source desactivee ou arretee garde ses entites, et les personnalisations
faites dans HA (zone, nom, icone) sont conservees.

Toute publication passe par `sync_sources`, sous `_sync_lock`, a partir des
reglages courants : deux requetes qui publiaient chacune leur copie pouvaient
retirer de HA une entite que l'autre venait d'ajouter.
"""

import os
import re
import time
import uuid
import logging
import threading
import json
import requests

MQTT_TOPIC_PREFIX = "homeassistant"
AVAILABILITY_TOPIC = "claptrap/availability"
HA_STATUS_TOPIC = f"{MQTT_TOPIC_PREFIX}/status"
DISCOVERY_WILDCARD = f"{MQTT_TOPIC_PREFIX}/binary_sensor/claptrap/+/config"
MIC_KEY = "mic"
CLAP_PULSE_SECONDS = 2  # duree de l'etat ON d'une entite clap

# Etat
_lock = threading.RLock()
_sync_lock = threading.RLock()  # serialise les alignements sur la configuration
_source_info = {}    # source_key -> {slug, label, groups: {g_slug: {name, clap_counts}}, available}
_listening = set()   # sources qui ecoutent reellement (detection lancee, flux recu)
_detection = {'running': False, 'sources': []}
_mqtt_client = None
_mqtt_connected = threading.Event()
_mqtt_started = False
_discovered = set()   # object_ids claptrap vus (retenus) sur le broker
_expected_keys = None  # sources configurees (cle -> slug), pour le nettoyage des orphelines
_off_timers = {}      # topic -> threading.Timer
_availability_seen = set()  # slugs dont un topic claptrap/<slug>/availability est retenu
_pending_removal = set()    # object_ids a supprimer des que le broker est joignable
_auth_failures = 0
_warned_collisions = set()


def _group_object_id(source_slug, group_slug, n):
    """Construit l'object_id (sans le prefixe claptrap_) pour une entite clap."""
    suffix = f"{n}clap" if n == 1 else f"{n}claps"
    return f"{source_slug}_{group_slug or 'clap'}_{suffix}"


def _get_headers():
    return {
        'Authorization': f"Bearer {os.environ.get('SUPERVISOR_TOKEN', '')}",
        'Content-Type': 'application/json'
    }


def _make_slug(entity_id):
    from settings_manager import ascii_slug
    return ascii_slug(entity_id)


def source_entity_key(source_type, source_data):
    """Identifiant UNIQUE et STABLE d'une source (base des entity_id).

    - mic  : "mic" (un seul micro ; l'index PulseAudio change au rebranchement
             ou au redemarrage et changeait l'entity_id)
    - rtsp : UUID du stream (8 premiers caracteres)
    - vban : nom du flux (plus parlant dans HA), IP a defaut
    """
    if source_type == 'mic':
        return MIC_KEY
    if source_type == 'rtsp':
        stream_id = source_data.get('id', '') or source_data.get('stream_id', '')
        if stream_id:
            return f"rtsp_{stream_id[:8]}"
        return f"rtsp_{_make_slug(source_data.get('name', 'unknown'))}"
    if source_type == 'vban':
        if source_data.get('entity_key'):
            return source_data['entity_key']
        name = (source_data.get('name') or '').strip()
        if name:
            return f"vban_{_make_slug(name)}"
        return f"vban_{_make_slug(source_data.get('ip', '0'))}"
    return f"source_{_make_slug(str(source_data))}"


def source_label(source_type, src):
    """Libelle lisible d'une source, identique partout (evite de republier
    les entites a chaque demarrage parce que deux libelles different)."""
    if source_type == 'mic':
        name = src.get('audio_source') or 'default'
        return f'Micro: {name}' if name != 'default' else 'Microphone'
    if source_type == 'rtsp':
        return f"RTSP: {src.get('name') or 'RTSP'}"
    return f"VBAN: {src.get('name') or src.get('ip')}"


def _get_version():
    # BUILD_VERSION est passe par le Supervisor au build (cf. Dockerfile).
    return os.environ.get('CLAPTRAP_VERSION') or 'unknown'


def _device_block():
    return {
        "identifiers": ["claptrap"],
        "name": "ClapTrap",
        "manufacturer": "Korben & Les Freres Poulain",
        "model": "ClapTrap Audio Detector",
        "sw_version": _get_version(),
    }


def _origin_block():
    return {"name": "ClapTrap", "sw_version": _get_version(),
            "support_url": "https://github.com/powange/ClapTrap-HA-Addon"}


# ===== MQTT : connexion ======================================================

def _mqtt_publish(topic, payload, retain=False):
    client = _mqtt_client
    if client is None or not _mqtt_connected.is_set():
        return None
    data = json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload)
    try:
        return client.publish(topic, data, retain=retain)
    except Exception as e:
        logging.warning(f"MQTT: publication sur {topic} impossible: {e}")
        return None


def _fetch_broker_info():
    resp = requests.get('http://supervisor/services/mqtt', headers=_get_headers(), timeout=5)
    if not resp.ok:
        raise RuntimeError(f"service MQTT indisponible via le Supervisor (HTTP {resp.status_code})")
    return resp.json().get('data', {})


def _connection_failed(client, reason):
    """Refus du broker ou echec TCP : apres 3 echecs, relire les infos du
    broker (identifiants changes apres reinstallation de Mosquitto, hote ou
    port differents). Elles n'etaient lues qu'une fois : echecs en boucle
    jusqu'au redemarrage de l'add-on."""
    global _auth_failures
    _auth_failures += 1
    logging.warning(f"MQTT: connexion impossible ({reason})")
    if _auth_failures >= 3:
        _auth_failures = 0
        threading.Thread(target=_reconnect_fresh, args=(client,), daemon=True).start()


def _on_connect_fail(client, userdata, *args):
    _connection_failed(client, "broker injoignable")


def _on_connect(client, userdata, flags, rc, *args):
    global _auth_failures
    failed = getattr(rc, 'is_failure', rc != 0)
    if failed:
        _connection_failed(client, f"refus : {rc}")
        return
    _auth_failures = 0
    logging.info("MQTT connecté")
    _mqtt_connected.set()
    client.subscribe(HA_STATUS_TOPIC)
    client.subscribe(DISCOVERY_WILDCARD)
    client.subscribe('claptrap/+/availability')
    # Republication complete : le broker a pu perdre ses messages retenus, et
    # tout ce qui a ete publie pendant la coupure est perdu (QoS 0).
    threading.Thread(target=_after_connect, daemon=True).start()


def _on_disconnect(client, userdata, *args):
    _mqtt_connected.clear()
    logging.warning("MQTT déconnecté, reconnexion automatique...")


def _on_message(client, userdata, msg):
    if msg.topic == HA_STATUS_TOPIC:
        if msg.payload.decode(errors='ignore') == 'online':
            # Message de naissance de HA (redemarrage de HA) : republier.
            logging.info("Home Assistant redémarré : republication des entités")
            threading.Thread(target=republish_all, daemon=True).start()
        return
    a = re.match(r'^claptrap/([^/]+)/availability$', msg.topic)
    if a:
        with _lock:
            if msg.payload:
                _availability_seen.add(a.group(1))
            else:
                _availability_seen.discard(a.group(1))
        return
    m = re.match(r'^homeassistant/binary_sensor/claptrap/([^/]+)/config$', msg.topic)
    if m:
        with _lock:
            if msg.payload:
                _discovered.add(m.group(1))
            else:
                _discovered.discard(m.group(1))


def _after_connect():
    _mqtt_publish(AVAILABILITY_TOPIC, 'online', retain=True)
    with _lock:
        pending = set(_pending_removal)
        _pending_removal.clear()
    for obj in pending:
        _unpublish_object(obj)
    republish_all()
    # Laisser arriver les configs retenues avant de chercher les orphelines.
    time.sleep(3)
    cleanup_orphans()


def _mqtt_worker():
    """Connexion au broker, retentee tant qu'elle echoue (Mosquitto peut ne
    pas etre pret au demarrage de l'hote). Ensuite paho gere les reconnexions."""
    global _mqtt_client
    try:
        import paho.mqtt.client as mqtt_module
    except ImportError:
        logging.error("paho-mqtt non installé : entités Home Assistant indisponibles")
        return
    delay = 5
    while True:
        try:
            info = _fetch_broker_info()
            host = info.get('host', 'core-mosquitto')
            port = int(info.get('port', 1883))
            client_id = f"claptrap_{uuid.uuid4().hex[:8]}"
            try:
                from paho.mqtt.enums import CallbackAPIVersion
                client = mqtt_module.Client(callback_api_version=CallbackAPIVersion.VERSION2,
                                            client_id=client_id)
            except (ImportError, AttributeError):
                client = mqtt_module.Client(client_id=client_id)
            if info.get('username'):
                client.username_pw_set(info['username'], info.get('password', ''))
            # LWT : si l'add-on meurt, toutes les entites passent "indisponible".
            client.will_set(AVAILABILITY_TOPIC, 'offline', retain=True)
            client.on_connect = _on_connect
            client.on_connect_fail = _on_connect_fail
            client.on_disconnect = _on_disconnect
            client.on_message = _on_message
            client.reconnect_delay_set(min_delay=1, max_delay=60)
            # Assigne AVANT la connexion : on_connect republie via _mqtt_client.
            _mqtt_client = client
            client.connect_async(host, port, keepalive=60)
            client.loop_start()
            logging.info(f"MQTT: connexion à {host}:{port}...")
            return
        except Exception as e:
            logging.warning(f"MQTT non disponible ({e}), nouvel essai dans {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 60)


def _reconnect_fresh(old_client):
    global _mqtt_client
    try:
        old_client.loop_stop()
        old_client.disconnect()
    except Exception:
        pass
    _mqtt_connected.clear()
    _mqtt_client = None
    _mqtt_worker()


def init_entities(settings=None):
    """Demarre la connexion MQTT (une seule fois) et enregistre les sources."""
    global _mqtt_started
    with _lock:
        if not _mqtt_started:
            _mqtt_started = True
            threading.Thread(target=_mqtt_worker, daemon=True, name="mqtt-connect").start()
    if settings is not None:
        sync_sources(settings)


def shutdown():
    """Arret propre : detection OFF et entites indisponibles."""
    client = _mqtt_client
    if client is None:
        return
    try:
        if _mqtt_connected.is_set():
            client.publish('claptrap/detection/state', 'OFF', retain=True).wait_for_publish(1)
            client.publish(AVAILABILITY_TOPIC, 'offline', retain=True).wait_for_publish(1)
        client.disconnect()
        client.loop_stop()
    except Exception as e:
        logging.debug(f"MQTT: arret: {e}")


# ===== Entites ===============================================================

def _friendly_name(display, g_name, n):
    return f'{display} {g_name} {n} clap{"s" if n > 1 else ""}'


def _entity_config(obj, name, source_slug):
    return {
        'name': name,
        'unique_id': f'claptrap_{obj}',
        # object_id est supprime depuis HA 2026.4 (ignore) : sans
        # default_entity_id, l'entity_id etait derive du nom.
        'default_entity_id': f'binary_sensor.claptrap_{obj}',
        'state_topic': f'claptrap/{obj}/state',
        'availability': [
            {'topic': AVAILABILITY_TOPIC},
            {'topic': f'claptrap/{source_slug}/availability'},
        ],
        'availability_mode': 'all',
        'device_class': 'sound',
        'icon': 'mdi:hand-clap',
        'payload_on': 'ON',
        'payload_off': 'OFF',
        'device': _device_block(),
        'origin': _origin_block(),
    }


def _publish_source(info):
    slug = info['slug']
    for g_slug, g_info in info['groups'].items():
        for n in g_info['clap_counts']:
            obj = _group_object_id(slug, g_slug, n)
            _mqtt_publish(f"{MQTT_TOPIC_PREFIX}/binary_sensor/claptrap/{obj}/config",
                          _entity_config(obj, _friendly_name(info['label'], g_info['name'], n), slug),
                          retain=True)
            _mqtt_publish(f'claptrap/{obj}/state', 'OFF', retain=True)
    _mqtt_publish(f'claptrap/{slug}/availability',
                  'online' if _is_available(info) else 'offline', retain=True)


def _is_available(info):
    return bool(info.get('available', True)) and info.get('key') in _listening


def set_source_listening(entity_key, listening):
    """La source ecoute (flux recu) ou non (detection arretee, erreur) : ses
    entites sont disponibles seulement quand elles peuvent se declencher."""
    with _lock:
        before = entity_key in _listening
        if listening:
            _listening.add(entity_key)
        else:
            _listening.discard(entity_key)
        info = _source_info.get(entity_key)
        if before == bool(listening) or info is None:
            return
        state = 'online' if _is_available(info) else 'offline'
    _mqtt_publish(f"claptrap/{info['slug']}/availability", state, retain=True)


def _unpublish_object(obj):
    _mqtt_publish(f"{MQTT_TOPIC_PREFIX}/binary_sensor/claptrap/{obj}/config", '', retain=True)
    _mqtt_publish(f'claptrap/{obj}/state', '', retain=True)


def _publish_detection():
    _mqtt_publish(f"{MQTT_TOPIC_PREFIX}/binary_sensor/claptrap/detection/config", {
        'name': 'Detection',
        'unique_id': 'claptrap_detection',
        'default_entity_id': 'binary_sensor.claptrap_detection',
        'state_topic': 'claptrap/detection/state',
        'availability_topic': AVAILABILITY_TOPIC,
        'device_class': 'running',
        'entity_category': 'diagnostic',
        'icon': 'mdi:ear-hearing',
        'payload_on': 'ON',
        'payload_off': 'OFF',
        'json_attributes_topic': 'claptrap/detection/attributes',
        'device': _device_block(),
        'origin': _origin_block(),
    }, retain=True)
    _mqtt_publish('claptrap/detection/state', 'ON' if _detection['running'] else 'OFF', retain=True)
    _mqtt_publish('claptrap/detection/attributes', {'sources': _detection['sources']}, retain=True)


def republish_all():
    """Republie la config et l'etat de toutes les entites connues."""
    if not _mqtt_connected.is_set():
        return
    with _lock:
        infos = [dict(i) for i in _source_info.values()]
    _publish_detection()
    for info in infos:
        _publish_source(info)


def _normalise_groups(groups, fallback_clap_counts=None):
    """Normalise une liste de groupes en {slug: {name, clap_counts}}."""
    out = {}
    if isinstance(groups, list) and groups:
        for idx, g in enumerate(groups):
            if not isinstance(g, dict):
                continue
            slug = g.get('slug') or f'group{idx + 1}'
            from settings_manager import clap_counts_of
            out[slug] = {'name': g.get('name') or slug,
                         'clap_counts': clap_counts_of(g, fallback_clap_counts or [1, 2])}
    if not out:
        counts = [n for n in (fallback_clap_counts or [1, 2]) if 1 <= n <= 4]
        out['clap'] = {'name': 'Clap', 'clap_counts': counts}
    return out


def _object_ids(info):
    return {_group_object_id(info['slug'], g_slug, n)
            for g_slug, g in info['groups'].items() for n in g['clap_counts']}


def register_source(source_id, label=None, clap_counts=None, groups=None, available=None):
    """Enregistre (ou met a jour) les entites d'une source.

    Republie simplement la config : HA met a jour l'entite existante (meme
    unique_id) sans la supprimer, ce qui conserve ses personnalisations.
    Seules les entites qui disparaissent (groupe ou nombre de claps retire)
    sont supprimees.
    """
    norm_groups = _normalise_groups(groups, fallback_clap_counts=clap_counts)
    with _lock:
        existing = _source_info.get(source_id)
        info = {
            'key': source_id,
            'slug': _make_slug(source_id),
            'label': label or source_id,
            'groups': norm_groups,
            'available': (existing or {}).get('available', True) if available is None else bool(available),
        }
        if existing == info:
            return
        removed = _object_ids(existing) - _object_ids(info) if existing else set()
        _source_info[source_id] = info
    for obj in removed:
        _unpublish_object(obj)
    _publish_source(info)
    logging.info(f"Entités HA: claptrap_{info['slug']} groupes={list(norm_groups.keys())} ({info['label']})")


def unregister_source(source_id):
    """Supprime toutes les entites d'une source (source supprimee)."""
    with _lock:
        info = _source_info.pop(source_id, None)
    if not info:
        return
    if not _mqtt_connected.is_set():
        # Broker injoignable : sans cela, la suppression etait perdue et les
        # entites restaient pour toujours. Elle sera faite a la reconnexion.
        with _lock:
            _pending_removal.update(_object_ids(info))
    for obj in _object_ids(info):
        _unpublish_object(obj)
    _mqtt_publish(f"claptrap/{info['slug']}/availability", '', retain=True)
    logging.info(f"Entités MQTT supprimées pour {info['slug']}")


def _configured_sources(settings):
    """(cle, libelle, groupes, disponible) de toutes les sources configurees."""
    out = []
    mic = settings.get('microphone') or {}
    # Micro : publie des qu'il est configure, comme les autres sources (un
    # micro desactive n'etait pas publie alors que l'interface affichait deja
    # ses entity_id, et ses groupes supprimes restaient dans HA).
    if mic.get('configured', True) is not False:
        out.append((MIC_KEY, source_label('mic', mic),
                    mic.get('sound_groups'), bool(mic.get('enabled', False))))
    for src in settings.get('rtsp_sources', []) or []:
        out.append((source_entity_key('rtsp', src), source_label('rtsp', src),
                    src.get('sound_groups'), bool(src.get('enabled', False))))
    seen = {}
    for src in settings.get('saved_vban_sources', []) or []:
        key = source_entity_key('vban', src)
        if key in seen:
            if key not in _warned_collisions:
                _warned_collisions.add(key)
                logging.warning(f"VBAN: « {src.get('name')} » et « {seen[key]} » donnent la même entité "
                                f"({key}) : renommez l'une des deux sources")
            continue
        seen[key] = src.get('name')
        out.append((key, source_label('vban', src),
                    src.get('sound_groups'), bool(src.get('enabled', False))))
    # Deux sources qui produiraient le meme object_id (VBAN « Salon » + groupe
    # « Télé clap » et VBAN « Salon Télé » + groupe « Clap ») : la seconde
    # ecraserait l'entite de la premiere. Elle n'est pas publiee.
    claimed, kept = {}, []
    for item in out:
        key, label, groups = item[0], item[1], item[2]
        objs = _object_ids({'slug': _make_slug(key), 'groups': _normalise_groups(groups)})
        clash = next((claimed[o] for o in objs if o in claimed), None)
        if clash:
            if (key, clash) not in _warned_collisions:
                _warned_collisions.add((key, clash))
                logging.warning(f"Entités HA : « {label} » produirait les mêmes entités que « {clash} », "
                                "non publiée : renommez la source ou le groupe")
            continue
        claimed.update({o: label for o in objs})
        kept.append(item)
    return kept


def find_entity_collision(settings):
    """Libelle de la premiere source dont les entites en ecraseraient d'autres
    (None si aucune) : les routes refusent la modification qui la cree."""
    claimed = {}
    for key, label, groups, _ in _configured_sources_raw(settings):
        objs = _object_ids({'slug': _make_slug(key), 'groups': _normalise_groups(groups)})
        for o in objs:
            if o in claimed and claimed[o] != key:
                return label
        claimed.update({o: key for o in objs})
    return None


def _configured_sources_raw(settings):
    mic = settings.get('microphone') or {}
    if mic.get('configured', True) is not False:
        yield MIC_KEY, source_label('mic', mic), mic.get('sound_groups'), None
    for src in settings.get('rtsp_sources', []) or []:
        yield source_entity_key('rtsp', src), source_label('rtsp', src), src.get('sound_groups'), None
    for src in settings.get('saved_vban_sources', []) or []:
        yield source_entity_key('vban', src), source_label('vban', src), src.get('sound_groups'), None


def sync_sources(settings=None):
    """Aligne les entites sur la configuration (courante par defaut) : toutes
    les sources configurees sont enregistrees, les autres supprimees."""
    global _expected_keys
    with _sync_lock:
        if settings is None:
            from settings_manager import load_settings
            settings = load_settings()
        configured = _configured_sources(settings)
        for key, label, groups, available in configured:
            register_source(key, label=label, groups=groups, available=available)
        with _lock:
            _expected_keys = {key for key, *_ in configured}
            stale = [k for k in _source_info if k not in _expected_keys]
        for key in stale:
            unregister_source(key)


def cleanup_orphans():
    """Supprime les entites ClapTrap retenues sur le broker qui ne
    correspondent a aucune source configuree (anciens formats, sources
    supprimees pendant que l'add-on etait arrete). Retourne leur liste."""
    with _lock:
        if _expected_keys is None:
            return []
        keep = {'detection'}
        for info in _source_info.values():
            keep |= _object_ids(info)
        orphans = [obj for obj in _discovered if obj not in keep]
        known_slugs = {info['slug'] for info in _source_info.values()}
        stale_availability = [slug for slug in _availability_seen if slug not in known_slugs]
    for obj in orphans:
        _unpublish_object(obj)
    # Topics de disponibilite retenus des sources disparues : les effacer aussi.
    for slug in stale_availability:
        _mqtt_publish(f'claptrap/{slug}/availability', '', retain=True)
    if orphans:
        logging.info(f"Entités orphelines supprimées: {', '.join(sorted(orphans))}")
    return sorted(orphans)


def entity_ids_for_settings(settings):
    """entity_id de chaque groupe de chaque source configuree, calcules comme
    a la publication : l'interface les affiche tels quels au lieu de les
    recalculer (les deux calculs divergeaient sur les accents).
    Cles : "mic", "rtsp:<id>", "vban:<id>"."""
    from settings_manager import clap_counts_of
    out = {}
    mic = settings.get('microphone') or {}
    items = []
    if mic.get('configured', True) is not False:
        items.append(('mic', 'mic', mic))
    for src in settings.get('rtsp_sources', []) or []:
        items.append((f"rtsp:{src.get('id')}", 'rtsp', src))
    for src in settings.get('saved_vban_sources', []) or []:
        items.append((f"vban:{src.get('id')}", 'vban', src))
    for key, kind, src in items:
        slug = _make_slug(source_entity_key(kind, src))
        out[key] = {g.get('slug'): [f'binary_sensor.claptrap_{_group_object_id(slug, g.get("slug"), n)}'
                                    for n in clap_counts_of(g)]
                    for g in src.get('sound_groups') or [] if isinstance(g, dict) and g.get('slug')}
    return out


def update_detection_state(running, sources=None):
    """Met a jour binary_sensor.claptrap_detection."""
    _detection['running'] = bool(running)
    _detection['sources'] = list(sources or [])
    _mqtt_publish('claptrap/detection/state', 'ON' if running else 'OFF', retain=True)
    _mqtt_publish('claptrap/detection/attributes', {'sources': _detection['sources']}, retain=True)


def _pulse(topic):
    """ON pendant CLAP_PULSE_SECONDS. Un clap pendant qu'on est deja ON
    produit OFF puis ON (une vraie transition, pour que les automations
    `to: "on"` se declenchent a nouveau) et rearme la minuterie : l'ancien
    thread par clap eteignait le 2e clap trop tot."""
    with _lock:
        timer = _off_timers.pop(topic, None)
        if timer is not None:
            timer.cancel()
            _mqtt_publish(topic, 'OFF')
        _mqtt_publish(topic, 'ON')

        def _off():
            with _lock:
                if _off_timers.get(topic) is t:
                    _off_timers.pop(topic, None)
                    _mqtt_publish(topic, 'OFF')
        t = threading.Timer(CLAP_PULSE_SECONDS, _off)
        t.daemon = True
        _off_timers[topic] = t
        t.start()


def on_clap_detected(entity_key, score, clap_count, group_slug='clap', group_clap_counts=None):
    """Appele quand un clap est detecte. Route vers l'entite du bon groupe."""
    with _lock:
        info = _source_info.get(entity_key, {})
    source_slug = info.get('slug', _make_slug(entity_key))
    group_info = (info.get('groups') or {}).get(group_slug, {})
    clap_counts = group_info['clap_counts'] if 'clap_counts' in group_info else (group_clap_counts or [])
    # Au-dela de 4 : bruit pendant la fenetre de comptage, pas « 4 claps ».
    if clap_count not in clap_counts:
        logging.info(f"Clap x{clap_count} sur {source_slug}/{group_slug} : pas d'entité pour ce nombre "
                     f"(configuré : {clap_counts})")
        return
    if not _mqtt_connected.is_set():
        logging.warning(f"MQTT non connecté : clap sur {source_slug}/{group_slug} non publié")
        return
    _pulse(f'claptrap/{_group_object_id(source_slug, group_slug, clap_count)}/state')
