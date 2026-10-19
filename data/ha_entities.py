"""Gestion des entites Home Assistant pour ClapTrap (MQTT Discovery).

Broker MQTT : celui de l'add-on Mosquitto (service du Supervisor), ou un
broker externe renseigne dans les options de l'add-on (`mqtt_host`...).

Decouverte par appareil : un seul message retenu par appareil
(`homeassistant/device/claptrap/<appareil>/config`), avec toutes ses entites
dans `cmps` : « ClapTrap » (`hub` : detection, configuration, derniere
commande) et un appareil par source (claps, reglages, diagnostic), rattache
a « ClapTrap ». Les anciennes configurations par entite (6.77 et avant) sont
migrees a la connexion (`migrate_discovery`), sans perte d'historique ni de
personnalisation (memes unique_id).

Entites par source et par groupe de sons :
- binary_sensor.claptrap_<source>_<groupe>_<N>clap(s) : un par nombre de claps (1-4)

Entite globale :
- binary_sensor.claptrap_detection : ON quand la detection tourne

Entites de reglage et de diagnostic, et commandes MQTT : voir ha_control.py.

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
import logging
import threading
import json
import requests

MQTT_TOPIC_PREFIX = "homeassistant"
AVAILABILITY_TOPIC = "claptrap/availability"
HA_STATUS_TOPIC = f"{MQTT_TOPIC_PREFIX}/status"
DISCOVERY_WILDCARD = f"{MQTT_TOPIC_PREFIX}/+/claptrap/+/config"   # appareils et anciennes entites
HUB = 'hub'   # appareil « ClapTrap » (aucune source n'a ce slug : mic, rtsp_…, vban_…)
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
_legacy = {}          # ancien topic de config par entite vu sur le broker -> ses topics d'etat
_seen_devices = {}    # appareil claptrap retenu sur le broker -> ses topics d'etat
_device_cmps = {}     # appareil -> {composant: plateforme} publie (ou retenu) : retraits
_discovery_ready = threading.Event()   # configurations retenues recues : publication possible
_session = 0          # numero de connexion (une reconnexion pendant l'attente arrete l'ancienne)
MIGRATE_CLEAR_DELAY = 1.0   # s entre la configuration d'un appareil et l'effacement des anciennes
_expected_keys = None  # sources configurees (cle -> slug), pour le nettoyage des orphelines
_off_timers = {}      # topic -> threading.Timer
_alerts_on = set()    # capteurs d'alerte allumes (republication : etat garde)
_availability_seen = set()  # slugs dont un topic claptrap/<slug>/availability est retenu
_pending_devices = set()    # appareils a supprimer des que le broker est joignable
_pending_topics = set()     # topics d'etat retenus a vider de meme
_auth_failures = 0
_fresh_cycles = 0           # recreations du client sans connexion reussie (attente croissante)
_fail_streak_warned = -1e9  # dernier avertissement d'une serie d'echecs (instant monotone)
FAIL_WARN_INTERVAL = 600    # s : un echec de connexion au journal, puis un rappel toutes les 10 min
_warned_collisions = set()  # (cle, source en conflit) deja signales
_shutting_down = False      # arret propre : « offline » voulu, ne pas le corriger
OPTIONS_FILE = '/data/options.json'   # options de l'add-on (onglet Configuration)
DISCOVERY_QUIET = 3.0       # s sans config retenue recue : toutes sont arrivees
DISCOVERY_MAX_WAIT = 30.0   # s au plus (broker charge)
_last_discovery = 0.0       # derniere config retenue recue (instant monotone)


def event_type(n):
    """« 1clap », « 2claps »… : suffixe des entites clap et type
    d'evenement de l'entite event du groupe (une seule regle)."""
    return f"{n}clap{'s' if n > 1 else ''}"


def _group_object_id(source_slug, group_slug, n):
    """Construit l'object_id (sans le prefixe claptrap_) pour une entite clap."""
    return f"{source_slug}_{group_slug or 'clap'}_{event_type(n)}"


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


_ingress_url = None   # lien vers l'interface (bouton « Visiter » de la fiche appareil)


def _fetch_ingress_url():
    """homeassistant://hassio/ingress/<slug> d'apres le Supervisor (slug avec
    le prefixe du depot). None hors de Home Assistant."""
    global _ingress_url
    if _ingress_url is None and os.environ.get('SUPERVISOR_TOKEN'):
        try:
            resp = requests.get('http://supervisor/addons/self/info', headers=_get_headers(), timeout=5)
            slug = resp.json().get('data', {}).get('slug') if resp.ok else None
            if slug:
                _ingress_url = f'homeassistant://hassio/ingress/{slug}'
        except Exception as e:
            logging.debug(f"Lien vers l'interface indisponible: {e}")
    return _ingress_url


def _device_block():
    device = {
        "identifiers": ["claptrap"],
        "name": "ClapTrap",
        "manufacturer": "Korben & Les Freres Poulain",
        "model": "ClapTrap Audio Detector",
        "sw_version": _get_version(),
    }
    if _ingress_url:
        device["configuration_url"] = _ingress_url
    return device


def source_device(info):
    """Appareil d'une source (« ClapTrap Salon »), rattache a « ClapTrap » :
    chaque source recoit sa propre piece dans HA. Changer d'appareil ne change
    ni l'entity_id ni l'historique (meme unique_id)."""
    return {
        **_device_block(),
        "identifiers": [f"claptrap_{info['slug']}"],
        "name": f"ClapTrap {info['label']}",
        "model": {'mic': 'Micro', 'rtsp': 'Caméra RTSP', 'vban': 'Flux VBAN'}.get(
            str(info.get('key', '')).split('_', 1)[0], 'Source audio'),
        "via_device": "claptrap",
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
        # QoS 1 pour les messages retenus (configurations, suppressions,
        # disponibilite, etats) : une suppression perdue pendant une
        # micro-coupure laissait une entite orpheline. Impulsions en QoS 0.
        return client.publish(topic, data, qos=1 if retain else 0, retain=retain)
    except Exception as e:
        logging.warning(f"MQTT: publication sur {topic} impossible: {e}")
        return None


def _addon_options():
    try:
        with open(OPTIONS_FILE, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def commands_enabled():
    """Option « Commandes MQTT » de l'add-on (absente : activee)."""
    return _addon_options().get('mqtt_commands') is not False


def _fetch_broker_info():
    """Broker externe (options de l'add-on) s'il est renseigne, sinon celui
    du service MQTT du Supervisor (add-on Mosquitto)."""
    opts = _addon_options()
    host = str(opts.get('mqtt_host') or '').strip()
    if host:
        tls = bool(opts.get('mqtt_tls'))
        return {'host': host, 'port': int(opts.get('mqtt_port') or (8883 if tls else 1883)),
                'username': opts.get('mqtt_username') or '', 'password': opts.get('mqtt_password') or '',
                'ssl': tls, 'tls_insecure': opts.get('mqtt_tls_verify') is False, 'external': True}
    resp = requests.get('http://supervisor/services/mqtt', headers=_get_headers(), timeout=5)
    if not resp.ok:
        raise RuntimeError(f"aucun broker : installez l'add-on Mosquitto ou renseignez « mqtt_host » dans "
                           f"la configuration de ClapTrap (service MQTT : HTTP {resp.status_code})")
    info = dict(resp.json().get('data', {}))
    # Mosquitto en TLS : son certificat vise rarement « core-mosquitto ».
    info['tls_insecure'] = opts.get('mqtt_tls_verify') is False
    return info


def _client_id():
    """Identifiant fixe : a la reconnexion, le broker coupe l'ancienne session
    tout de suite, et son « offline » (LWT) arrive avant le nouveau « online »
    (avec un identifiant aleatoire, il pouvait arriver apres et rendre toutes
    les entites indisponibles)."""
    # Suffixe tire une fois par installation (deux Home Assistant sur le meme
    # broker externe ne se volent pas la session).
    path = os.path.join(os.path.dirname(OPTIONS_FILE), '.mqtt_client_id')
    try:
        with open(path, encoding='utf-8') as f:
            suffix = re.sub(r'[^a-f0-9]', '', f.read())[:12]
    except OSError:
        suffix = ''
    if len(suffix) != 12:
        import secrets
        suffix = secrets.token_hex(6)
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(suffix)
        except OSError:
            pass
    return f"claptrap-{suffix}"


def _connection_failed(client, reason, refused=False):
    """Refus du broker ou echec TCP : apres 3 echecs, relire les infos du
    broker (identifiants changes apres reinstallation de Mosquitto, hote ou
    port differents). Elles n'etaient lues qu'une fois : echecs en boucle
    jusqu'au redemarrage de l'add-on."""
    global _auth_failures, _fail_streak_warned
    _auth_failures += 1
    low = str(reason).lower()
    if 'author' in low or 'password' in low or 'identif' in low:
        reason = (f"{reason} : identifiants refusés par le broker (vérifiez mqtt_username et mqtt_password, "
                  "ou réinstallez l'intégration MQTT si Mosquitto a été réinstallé)")
    # Mauvais mot de passe : un avertissement par echec (~11 000 lignes par
    # jour) ; le premier d'une serie, puis un rappel toutes les 10 min.
    now = time.monotonic()
    if now - _fail_streak_warned >= FAIL_WARN_INTERVAL:
        logging.warning(f"MQTT: connexion impossible ({reason}) ; nouvel essai régulier")
        _fail_streak_warned = now
    else:
        logging.debug(f"MQTT: connexion impossible ({reason})")
    if _auth_failures >= 3:
        _auth_failures = 0
        threading.Thread(target=_reconnect_fresh, args=(client, refused), daemon=True).start()


def _on_connect_fail(client, userdata, *args):
    _connection_failed(client, "broker injoignable")


def _on_connect(client, userdata, flags, rc, *args):
    global _auth_failures
    failed = getattr(rc, 'is_failure', rc != 0)
    if failed:
        _connection_failed(client, f"refus : {rc}", refused=True)
        return
    global _fresh_cycles, _fail_streak_warned
    _auth_failures = 0
    _fresh_cycles = 0
    _fail_streak_warned = -1e9   # prochain echec : de nouveau au journal
    logging.info("MQTT connecté")
    global _session
    _discovery_ready.clear()   # nouvelle session : attendre les configurations retenues
    _session += 1
    _mqtt_connected.set()
    client.subscribe(HA_STATUS_TOPIC)
    client.subscribe(AVAILABILITY_TOPIC)
    client.subscribe(DISCOVERY_WILDCARD)
    client.subscribe('claptrap/+/availability')
    from ha_control import SUBSCRIPTIONS, COMMAND_TOPIC
    commands = commands_enabled()
    for topic in SUBSCRIPTIONS:
        # Option « Commandes MQTT » decochee : le topic qui ajoute ou supprime
        # des sources n'est plus ecoute (les entites de reglage restent).
        if topic != COMMAND_TOPIC or commands:
            client.subscribe(topic)
    if not commands:
        logging.info(f"Commandes MQTT désactivées (option de l'add-on) : {COMMAND_TOPIC} ignoré")
    # Republication complete : le broker a pu perdre ses messages retenus, et
    # tout ce qui a ete publie pendant la coupure est perdu (QoS 0).
    threading.Thread(target=_after_connect, daemon=True).start()


def _on_disconnect(client, userdata, *args):
    was_connected = _mqtt_connected.is_set()
    _mqtt_connected.clear()
    _discovery_ready.clear()
    # Apres un refus (mauvais identifiants), paho appelle aussi ce rappel a
    # chaque essai : seule une vraie deconnexion merite un avertissement.
    if was_connected:
        logging.warning("MQTT déconnecté, reconnexion automatique...")
    else:
        logging.debug("MQTT : connexion refusée ou interrompue avant d'aboutir")


def _on_message(client, userdata, msg):
    global _last_discovery
    if msg.topic.startswith('claptrap/') and (msg.topic.endswith('/set') or msg.topic == 'claptrap/command'):
        from ha_control import handle_message
        handle_message(msg.topic, msg.payload, retained=bool(getattr(msg, 'retain', False)))
        return
    if msg.topic == AVAILABILITY_TOPIC:
        # Message retenu (dernier arret propre, recu a l'abonnement) : deja
        # remplace par le « online » de la connexion. Seul un LWT tardif, en
        # direct, est a corriger.
        if (not getattr(msg, 'retain', False) and msg.payload.decode(errors='ignore') == 'offline'
                and _mqtt_connected.is_set() and not _shutting_down):
            # « offline » d'une ancienne session (LWT tardif) alors qu'on est
            # connecte : retablir, sinon toutes les entites restaient indisponibles.
            logging.info("MQTT : « offline » d'une ancienne session reçu, disponibilité rétablie")
            _mqtt_publish(AVAILABILITY_TOPIC, 'online', retain=True)
        return
    if msg.topic == HA_STATUS_TOPIC:
        if msg.payload.decode(errors='ignore') == 'online':
            # Message de naissance de HA (redemarrage de HA) : republier.
            logging.info("Home Assistant redémarré : republication des entités")
            threading.Thread(target=republish_all, daemon=True).start()
            import ha_registry
            ha_registry.schedule_reconcile(10.0)   # renommages en attente (HA de nouveau joignable)
        return
    a = re.match(r'^claptrap/([^/]+)/availability$', msg.topic)
    if a:
        with _lock:
            if msg.payload:
                _availability_seen.add(a.group(1))
            else:
                _availability_seen.discard(a.group(1))
        return
    m = re.match(r'^homeassistant/([^/]+)/claptrap/([^/]+)/config$', msg.topic)
    if m:
        _last_discovery = time.monotonic()
        _discovery_message(m.group(1), m.group(2), msg.topic, msg.payload)


def _state_topics(cfg):
    return sorted({t for t in (cfg.get('state_topic'), cfg.get('json_attributes_topic'))
                   if isinstance(t, str) and t.startswith('claptrap/')})


def _discovery_message(component, item, topic, payload):
    """Configuration retenue d'un appareil claptrap, ou ancienne configuration
    par entite (a migrer ou a effacer)."""
    try:
        cfg = json.loads(payload) if payload else None
    except ValueError:
        cfg = {}
    if not isinstance(cfg, dict) and cfg is not None:
        cfg = {}
    with _lock:
        if component != 'device':
            # Ancienne configuration, ou {"migrate_discovery": true} reste
            # retenu (migration interrompue) : a migrer ou effacer de meme.
            if cfg is None:
                _legacy.pop(topic, None)
            else:
                _legacy[topic] = _state_topics(cfg)
            return
        if cfg is None:
            _seen_devices.pop(item, None)
            _device_cmps.pop(item, None)
            return
        # Composants reduits a {"p": …} : retraits, sans etat.
        cmps = {cid: expand_component(c) for cid, c in (cfg.get('cmps') or {}).items()
                if isinstance(c, dict) and len(c) > 1 and c.get('p')}
        _seen_devices[item] = sorted({t for c in cmps.values() for t in _state_topics(c)})
        if not _discovery_ready.is_set():
            # Publie par une session precedente (retenu, recu a l'abonnement) :
            # base des retraits du prochain envoi. Nos propres echos, ensuite,
            # ne la modifient plus.
            _device_cmps[item] = {cid: c['p'] for cid, c in cmps.items()}


def _after_connect():
    session = _session
    _mqtt_publish(AVAILABILITY_TOPIC, 'online', retain=True)
    # Lien « Visiter » des appareils : retente a chaque connexion tant qu'il
    # manque (Supervisor lent au demarrage), hors du demarrage de la connexion.
    _fetch_ingress_url()
    with _lock:
        pending, pending_devices = set(_pending_topics), set(_pending_devices)
        _pending_topics.clear()
        _pending_devices.clear()
    for dev in pending_devices:
        _mqtt_publish(_device_topic(dev), '', retain=True)
    from ha_control import clear_topics
    clear_topics(pending)
    # Laisser arriver les configurations retenues (appareils, anciennes
    # entites) avant de publier : la migration doit preceder les nouvelles
    # configurations. Attendre que le flux s'arrete (3 s sans nouvelle), 30 s
    # au plus.
    start = time.monotonic()
    while time.monotonic() - start < DISCOVERY_MAX_WAIT:
        time.sleep(0.5)
        quiet = time.monotonic() - max(start, _last_discovery)
        if quiet >= DISCOVERY_QUIET:
            break
    if not _mqtt_connected.is_set() or session != _session:
        return   # deconnecte ou reconnecte entre-temps : la nouvelle session s'en charge
    _discovery_ready.set()
    republish_all()   # chaque appareil migre ses anciennes entites (_publish_device)
    import ha_registry
    ha_registry.schedule_reconcile(10.0)   # renommages d'entites restes en attente
    if _mqtt_connected.is_set() and session == _session:
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
    last_warning = -1e9
    while True:
        try:
            info = _fetch_broker_info()
            host = info.get('host', 'core-mosquitto')
            port = int(info.get('port', 1883))
            client_id = _client_id()
            try:
                from paho.mqtt.enums import CallbackAPIVersion
                client = mqtt_module.Client(callback_api_version=CallbackAPIVersion.VERSION2,
                                            client_id=client_id)
            except (ImportError, AttributeError):
                client = mqtt_module.Client(client_id=client_id)
            if info.get('username'):
                client.username_pw_set(info['username'], info.get('password', ''))
            if info.get('ssl'):
                # Port TLS (service du Supervisor avec ssl, ou option mqtt_tls) :
                # la connexion partait en clair et echouait en boucle.
                if info.get('tls_insecure'):
                    # Certificat auto-signe (option « Vérifier le certificat »
                    # decochee) : ni chaine ni nom verifies, connexion chiffree.
                    import ssl
                    client.tls_set(cert_reqs=ssl.CERT_NONE)
                    client.tls_insecure_set(True)
                else:
                    client.tls_set()
            # LWT : si l'add-on meurt, toutes les entites passent "indisponible".
            client.will_set(AVAILABILITY_TOPIC, 'offline', qos=1, retain=True)
            client.on_connect = _on_connect
            client.on_connect_fail = _on_connect_fail
            client.on_disconnect = _on_disconnect
            client.on_message = _on_message
            client.reconnect_delay_set(min_delay=1, max_delay=60)
            # Assigne AVANT la connexion : on_connect republie via _mqtt_client.
            _mqtt_client = client
            client.connect_async(host, port, keepalive=60)
            client.loop_start()
            logging.info(f"MQTT: connexion à {host}:{port}{' (TLS)' if info.get('ssl') else ''}"
                         f"{' (broker externe)' if info.get('external') else ''}...")
            return
        except Exception as e:
            # Sans broker (ni Mosquitto ni option) : un avertissement, puis un
            # rappel toutes les 10 min au lieu d'une ligne par minute.
            now = time.monotonic()
            if now - last_warning >= 600:
                logging.warning(f"MQTT non disponible ({e}) : entités Home Assistant indisponibles, "
                                f"nouvel essai régulier")
                last_warning = now
            else:
                logging.debug(f"MQTT non disponible ({e}), nouvel essai dans {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 300)


def _reconnect_fresh(old_client, refused=False):
    """Nouveau client (infos du broker relues) apres 3 echecs. Le nouveau
    client repartait de 1 s : l'attente ne depassait jamais ~8 s. Broker qui
    REFUSE (identifiants) : attente croissante entre deux recreations, jusqu'a
    5 min. Broker injoignable (redemarrage de Mosquitto) : aussitot, pour
    reprendre des son retour."""
    global _mqtt_client, _fresh_cycles
    try:
        old_client.loop_stop()
        old_client.disconnect()
    except Exception:
        pass
    _mqtt_connected.clear()
    _mqtt_client = None
    if refused:
        time.sleep(min(300, 15 * 2 ** _fresh_cycles))
        _fresh_cycles += 1
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
    global _shutting_down
    client = _mqtt_client
    if client is None:
        return
    _shutting_down = True
    try:
        if _mqtt_connected.is_set():
            client.publish('claptrap/detection/state', 'OFF', qos=1, retain=True).wait_for_publish(1)
            client.publish(AVAILABILITY_TOPIC, 'offline', qos=1, retain=True).wait_for_publish(1)
        client.disconnect()
        client.loop_stop()
    except Exception as e:
        logging.debug(f"MQTT: arret: {e}")


# ===== Entites ===============================================================

def _device_topic(dev):
    return f"{MQTT_TOPIC_PREFIX}/device/claptrap/{dev}/config"


# Abreviations de HA (homeassistant/components/mqtt/abbreviations.py, toutes
# presentes en 2025.10) : le message d'une source a 20 groupes faisait 89 Ko.
ABBREVIATIONS = {
    'unique_id': 'uniq_id', 'default_entity_id': 'def_ent_id', 'state_topic': 'stat_t',
    'command_topic': 'cmd_t', 'availability': 'avty', 'availability_mode': 'avty_mode',
    'availability_topic': 'avty_t', 'json_attributes_topic': 'json_attr_t',
    'json_attributes_template': 'json_attr_tpl', 'value_template': 'val_tpl', 'device_class': 'dev_cla',
    'entity_category': 'ent_cat', 'icon': 'ic', 'payload_on': 'pl_on', 'payload_off': 'pl_off',
    'unit_of_measurement': 'unit_of_meas', 'event_types': 'evt_typ',
}
_EXPANSIONS = {v: k for k, v in ABBREVIATIONS.items()}


def _component(platform, cfg):
    """Configuration d'une entite dans `cmps`, abregee (appareil et origine
    sont portes par le message de l'appareil)."""
    c = {ABBREVIATIONS.get(k, k): v for k, v in cfg.items()}
    c['p'] = platform
    return c


def expand_component(c):
    """Composant d'un message retenu, cles completes (abregees ou non)."""
    return {_EXPANSIONS.get(k, k): v for k, v in c.items()}


def _component_ids(info):
    """Composants de l'appareil d'une source, sans construire les configurations."""
    from ha_control import source_entity_ids
    return _object_ids(info) | source_entity_ids(info)


def _hub_components():
    """{composant: (plateforme, config)} de l'appareil « ClapTrap »."""
    from ha_control import global_entities
    cmps = {'detection': ('binary_sensor', _detection_config())}
    cmps.update({obj: (c, cfg) for c, obj, cfg in global_entities()})
    return cmps


def _source_components(info):
    """{composant: (plateforme, config)} de l'appareil d'une source : claps,
    reglages et diagnostic. Le composant porte l'object_id (unique_id sans
    « claptrap_ »), comme les anciens topics par entite."""
    slug = info['slug']
    cmps = {}
    for g_slug, g_info in info['groups'].items():
        if g_info.get('alert'):
            obj = alert_object_id(slug, g_slug)
            cfg = _entity_config(obj, g_info['name'], slug, alert_object_id(slug, g_info.get('entity_slug') or g_slug))
            # Classe du modele (fumee, securite, son) : icone et libelle
            # « Détecté » de HA ; plus d'impulsion de 2 s, l'etat dure.
            cfg['device_class'] = g_info.get('device_class') or 'sound'
            cfg.pop('icon', None)
            cmps[obj] = ('binary_sensor', cfg)
            continue
        for n in g_info['clap_counts']:
            obj = _group_object_id(slug, g_slug, n)
            cmps[obj] = ('binary_sensor', _entity_config(
                obj, _friendly_name(g_info['name'], n), slug,
                _group_object_id(slug, g_info.get('entity_slug') or g_slug, n)))
    from ha_control import source_entities
    cmps.update({obj: (c, cfg) for c, obj, cfg in source_entities(info)})
    return cmps


def _publish_device(dev, device, cmps):
    """Un message retenu pour tout l'appareil. Une entite retiree depuis le
    dernier envoi (groupe, nombre de claps) y figure reduite a sa plateforme :
    HA la supprime. Rien avant la migration (voir _after_connect)."""
    if not _discovery_ready.is_set():
        return
    payload_cmps = {cid: _component(p, cfg) for cid, (p, cfg) in cmps.items()}
    with _lock:
        before = _device_cmps.get(dev, {})
        for cid, platform in before.items():
            if cid not in payload_cmps:
                payload_cmps[cid] = {'p': platform}
        _device_cmps[dev] = {cid: p for cid, (p, _cfg) in cmps.items()}
        # Anciennes configurations par entite (6.78 et avant) de cet appareil.
        legacy = sorted(t for t in _legacy if t.split('/')[3] in cmps)
    # Migration (doc de HA) : {"migrate_discovery": true} sur l'ancien topic
    # AVANT la configuration de l'appareil (HA garde l'entite, son historique
    # et ses reglages, et la rattache a l'appareil au meme unique_id), puis
    # ancien topic vide. A chaque publication, pas seulement a la connexion :
    # une entite pas encore publiee (reglages illisibles au demarrage) est
    # migree quand sa source revient, jamais effacee.
    for topic in legacy:
        _mqtt_publish(topic, {'migrate_discovery': True}, retain=True)
    _mqtt_publish(_device_topic(dev), {'dev': device, 'o': _origin_block(), 'cmps': payload_cmps}, retain=True)
    if legacy:
        logging.info(f"Découverte MQTT : {len(legacy)} entité(s) de « {device.get('name')} » migrée(s) "
                     "vers la découverte par appareil")
        timer = threading.Timer(MIGRATE_CLEAR_DELAY, _clear_legacy, args=(legacy,))
        timer.daemon = True
        timer.start()


def _clear_legacy(topics):
    with _lock:
        for topic in topics:
            _legacy.pop(topic, None)
    for topic in topics:
        _mqtt_publish(topic, '', retain=True)


def _unpublish_device(dev):
    with _lock:
        _device_cmps.pop(dev, None)
    _mqtt_publish(_device_topic(dev), '', retain=True)   # appareil et entites retires de HA


def short_name(label):
    """Nom court d'une source dans HA (« Salon », « Micro ») : le libelle
    complet (« VBAN: Salon », « Micro: alsa_input.usb-… ») donnait des noms
    d'entites longs et techniques, qui changeaient avec le micro."""
    label = str(label or '')
    if label == 'Microphone' or label.startswith('Micro: '):
        return 'Micro'
    for prefix in ('RTSP: ', 'VBAN: '):
        if label.startswith(prefix):
            return label[len(prefix):] or label
    return label


def _friendly_name(g_name, n):
    """Nom de l'entite, sans la source : HA le prefixe du nom de l'appareil
    (« ClapTrap Salon Clap · 2 claps »)."""
    return f'{g_name} · {n} clap{"s" if n > 1 else ""}'


def _entity_config(obj, name, source_slug, entity_obj=None):
    """`obj` (slug du groupe) : unique_id et topics, stables ; `entity_obj`
    (nom du groupe) : entity_id propose a la creation."""
    return {
        'name': name,
        'unique_id': f'claptrap_{obj}',
        # object_id est supprime depuis HA 2026.4 (ignore) : sans
        # default_entity_id, l'entity_id etait derive du nom.
        'default_entity_id': f'binary_sensor.claptrap_{entity_obj or obj}',
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
    }


def _publish_source(info):
    slug = info['slug']
    _publish_device(slug, source_device(info), _source_components(info))
    with _lock:
        for obj in _object_ids(info):
            # Alerte en cours (republication pendant une alarme) : reste allumee.
            _mqtt_publish(f'claptrap/{obj}/state', 'ON' if obj in _alerts_on else 'OFF', retain=True)
    # Sous le verrou, comme set_source_listening : une camera qui se
    # connectait pendant une republication pouvait voir « online » ecrase par
    # un « offline » calcule juste avant, et retenu.
    with _lock:
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
        _mqtt_publish(f"claptrap/{info['slug']}/availability", state, retain=True)   # paho ne bloque pas


def _detection_config():
    return {
        'name': 'Détection en cours',
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
    }


def _publish_detection():
    _publish_device(HUB, _device_block(), _hub_components())
    _mqtt_publish('claptrap/detection/state', 'ON' if _detection['running'] else 'OFF', retain=True)
    _mqtt_publish('claptrap/detection/attributes', {'sources': _detection['sources']}, retain=True)


def republish_all():
    """Republie la config et l'etat de toutes les entites connues."""
    if not _mqtt_connected.is_set():
        return
    # Sous le verrou des synchronisations : une source supprimee pendant la
    # republication (naissance de HA) ressuscitait avec une config retenue.
    with _sync_lock:
        with _lock:
            infos = [dict(i) for i in _source_info.values()]
        _publish_detection()
        for info in infos:
            _publish_source(info)
        import ha_control
        ha_control.reset_published()
        ha_control.publish_states()


def _normalise_groups(groups):
    """Normalise une liste de groupes en {slug: {name, clap_counts}} (groupe
    « Clap » 1 et 2 claps si la source n'en a aucun)."""
    from settings_manager import clap_counts_of
    out = {}
    for idx, g in enumerate(groups if isinstance(groups, list) else []):
        if not isinstance(g, dict):
            continue
        slug = g.get('slug') or f'group{idx + 1}'
        alert = g.get('count_mode') == 'alerte'
        out[slug] = {'name': g.get('name') or slug,
                     # Mode alerte : un capteur « … détecté », ni « N claps » ni event.
                     'clap_counts': [] if alert else clap_counts_of(g),
                     'entity_slug': g.get('entity_slug') or slug}
        if alert:
            out[slug].update(alert=True, device_class=g.get('device_class') or 'sound')
    return out or {'clap': {'name': 'Clap', 'clap_counts': [1, 2], 'entity_slug': 'clap'}}


def _group_objs(source_slug, g_slug, g):
    """Fins d'identifiant des capteurs d'un groupe : un par nombre de claps,
    ou le capteur d'alerte."""
    if g.get('alert'):
        return [alert_object_id(source_slug, g_slug)]
    return [_group_object_id(source_slug, g_slug, n) for n in g['clap_counts']]


def _object_ids(info):
    return {obj for g_slug, g in info['groups'].items() for obj in _group_objs(info['slug'], g_slug, g)}


def alert_object_id(source_slug, group_slug):
    return f'{source_slug}_{group_slug}_alerte'


def state_topics(info):
    """Topics d'etat retenus des entites d'une source (claps, reglages,
    diagnostic), a vider quand l'entite disparait."""
    from ha_control import control_state_topics
    return {f'claptrap/{obj}/state' for obj in _object_ids(info)} | control_state_topics(info)


def group_entity_ids(source_slug, g_slug, g_info, entity_slug=None):
    """[(unique_id, entity_id propose)] de toutes les entites d'un groupe
    (claps, reglages, diagnostic), pour un `entity_slug` donne (par defaut
    celui du groupe). Source unique des formats d'identifiants."""
    es = entity_slug or g_info.get('entity_slug') or g_slug
    g = dict(g_info, entity_slug=es)
    out = [(f'claptrap_{uid}', f'binary_sensor.claptrap_{eid}')
           for uid, eid in zip(_group_objs(source_slug, g_slug, g), _group_objs(source_slug, es, g))]
    from ha_control import source_entities
    for component, obj, cfg in source_entities({'slug': source_slug, 'label': '', 'groups': {g_slug: g}}):
        if obj.startswith(f'{source_slug}_{g_slug}_'):
            out.append((cfg['unique_id'], cfg['default_entity_id']))
    return out


def _entity_object_ids(info):
    """Fins d'entity_id proposees (suivent le nom des groupes)."""
    return {obj for g_slug, g in info['groups'].items()
            for obj in _group_objs(info['slug'], g.get('entity_slug') or g_slug, g)}


def register_source(source_id, label=None, groups=None, available=None):
    """Enregistre (ou met a jour) les entites d'une source.

    Republie simplement la config : HA met a jour l'entite existante (meme
    unique_id) sans la supprimer, ce qui conserve ses personnalisations.
    Seules les entites qui disparaissent (groupe ou nombre de claps retire)
    sont supprimees.
    """
    norm_groups = _normalise_groups(groups)
    with _lock:
        existing = _source_info.get(source_id)
        info = {
            'key': source_id,
            'slug': _make_slug(source_id),
            'label': label or source_id,
            'groups': norm_groups,
            'available': (existing or {}).get('available', True) if available is None else bool(available),
        }
        # Re-ajoutee pendant une coupure du broker : annuler sa suppression en
        # attente (effacee puis recreee a la reconnexion, personnalisations perdues).
        _pending_devices.discard(info['slug'])
        _pending_topics.difference_update(state_topics(info))
        if existing == info:
            return
        # Entites retirees (groupe, nombre de claps) : retirees de l'appareil
        # au prochain envoi ; leurs etats retenus sont vides ici.
        removed_topics = state_topics(existing) - state_topics(info) if existing else set()
        _source_info[source_id] = info
    from ha_control import clear_topics
    clear_topics(removed_topics)
    _publish_source(info)
    logging.info(f"Entités HA: claptrap_{info['slug']} groupes={list(norm_groups.keys())} ({info['label']})")


def unregister_source(source_id, keep_slugs=frozenset(), keep_topics=frozenset(), keep_cmps=frozenset()):
    """Supprime l'appareil d'une source (source supprimee) et ses etats
    retenus, sauf ceux revendiques par une source conservee (meme slug).

    `keep_cmps` : entites de meme identifiant annoncees par une source
    conservee (collision creee hors des routes, fichier modifie a la main).
    HA les a peut-etre creees depuis cet appareil : il est alors garde, reduit
    a ces entites (leurs topics sont communs), au lieu d'etre supprime."""
    with _lock:
        info = _source_info.pop(source_id, None)
    if not info:
        return
    topics = state_topics(info) - set(keep_topics)
    shared = _component_ids(info) & set(keep_cmps)
    if shared:
        cmps = _source_components(info)
        _publish_device(info['slug'], source_device(info), {cid: cmps[cid] for cid in shared})
    remove_device = info['slug'] not in keep_slugs and not shared
    if not _mqtt_connected.is_set():
        # Broker injoignable : sans cela, la suppression etait perdue et les
        # entites restaient pour toujours. Elle sera faite a la reconnexion.
        with _lock:
            if remove_device:
                _pending_devices.add(info['slug'])
            _pending_topics.update(topics)
    if remove_device:
        _unpublish_device(info['slug'])
    from ha_control import clear_topics
    clear_topics(topics)
    if info['slug'] not in keep_slugs:
        _mqtt_publish(f"claptrap/{info['slug']}/availability", '', retain=True)
    logging.info(f"Entités MQTT supprimées pour {info['slug']}")


def _all_sources(settings):
    """(cle d'interface, cle d'entite, libelle, groupes, disponible) de toutes
    les sources configurees, dans l'ordre de publication."""
    mic = settings.get('microphone') or {}
    # Micro : publie des qu'il est configure, comme les autres sources.
    if mic.get('configured', True) is not False:
        yield 'mic', MIC_KEY, source_label('mic', mic), mic.get('sound_groups'), bool(mic.get('enabled', False))
    for kind, lst in (('rtsp', 'rtsp_sources'), ('vban', 'saved_vban_sources')):
        for src in settings.get(lst, []) or []:
            yield (f"{kind}:{src.get('id')}", source_entity_key(kind, src), source_label(kind, src),
                   src.get('sound_groups'), bool(src.get('enabled', False)))


def _resolve_sources(settings):
    """Sources a publier et premiere collision (libelle ou None).

    Une source dont la cle d'entite ou un object_id est deja revendique
    (VBAN « Salon » + groupe « Télé clap » et VBAN « Salon Télé » + groupe
    « Clap ») ecraserait l'entite d'une autre : elle n'est pas publiee, et les
    routes refusent la modification qui la cree. Un seul parcours pour la
    publication, le controle des routes et les entity_id affiches (il y en
    avait trois, avec deux logiques de dedoublonnage).
    """
    claimed, keys, kept, collisions = {}, {}, [], []
    for ui_key, key, label, groups, available in _all_sources(settings):
        norm = _normalise_groups(groups)
        info = {'slug': _make_slug(key), 'groups': norm}
        # unique_id et entity_id : deux groupes renommes peuvent viser le meme id.
        objs = _object_ids(info) | {'id:' + o for o in _entity_object_ids(info)}
        clash = keys.get(key) or next((claimed[o] for o in objs if o in claimed), None)
        if clash:
            collisions.append(label)
            if (key, clash) not in _warned_collisions:
                _warned_collisions.add((key, clash))
                logging.warning(f"Entités HA : « {label} » produirait les mêmes entités que « {clash} », "
                                "non publiée : renommez l'un des groupes (ou supprimez puis recréez la source "
                                "sous un autre nom)")
            continue
        keys[key] = label
        claimed.update({o: label for o in objs})
        kept.append((ui_key, key, label, groups, available, norm))
    return kept, collisions


def _configured_sources(settings):
    """(cle, libelle, groupes, disponible) des sources a publier."""
    return [(key, label, groups, available) for _, key, label, groups, available, _ in _resolve_sources(settings)[0]]


def find_entity_collision(settings, before=None):
    """Libelle de la premiere source dont les entites en ecraseraient
    d'autres (None si aucune). Avec `before` (reglages avant modification),
    seules les NOUVELLES collisions comptent : une collision heritee ne fait
    plus refuser les modifications des autres sources."""
    after = _resolve_sources(settings)[1]
    if before is not None:
        old = set(_resolve_sources(before)[1])
        after = [c for c in after if c not in old]
    return after[0] if after else None


def sync_sources(settings=None):
    """Aligne les entites sur la configuration (courante par defaut) : toutes
    les sources configurees sont enregistrees, les autres supprimees."""
    global _expected_keys
    with _sync_lock:
        if settings is None:
            from settings_manager import load_settings
            settings = load_settings()
        configured = _configured_sources(settings)
        # Nom court dans HA (« Salon »), sauf s'il est porte par deux sources
        # (« RTSP: Salon » et « VBAN: Salon ») : libelle complet alors.
        shorts = [short_name(label) for _k, label, _g, _a in configured]
        for key, label, groups, available in configured:
            display = short_name(label) if shorts.count(short_name(label)) == 1 else label
            register_source(key, label=display, groups=groups, available=available)
        from settings_manager import is_degraded
        if is_degraded():
            # Reglages illisibles (valeurs par defaut, aucune source) : ne rien
            # supprimer, sinon toutes les entites et leurs personnalisations
            # disparaissaient de HA ; le nettoyage des orphelines est suspendu.
            _expected_keys = None
            return
        with _lock:
            _expected_keys = {key for key, *_ in configured}
            stale = [k for k in _source_info if k not in _expected_keys]
            kept = [_source_info[k] for k in _expected_keys if k in _source_info]
            keep_slugs = {i['slug'] for i in kept}
            keep_topics = set().union(*(state_topics(i) for i in kept)) if kept else set()
            keep_cmps = set().union(*(_component_ids(i) for i in kept)) if kept else set()
        for key in stale:
            unregister_source(key, keep_slugs=keep_slugs, keep_topics=keep_topics, keep_cmps=keep_cmps)


def cleanup_orphans():
    """Supprime ce que ClapTrap a laisse sur le broker sans source
    correspondante : appareils de sources supprimees pendant que l'add-on
    etait arrete, anciennes configurations par entite non migrees, etats et
    disponibilites retenus. Retourne les noms retires."""
    with _lock:
        if _expected_keys is None:
            return []
        known_slugs = {info['slug'] for info in _source_info.values()}
        orphan_devices = [d for d in _seen_devices if d != HUB and d not in known_slugs]
        # Anciennes configurations sans entite actuelle (celles d'une entite
        # publiee sont migrees par _publish_device, jamais effacees ici).
        current = set(_hub_components()).union(*(_component_ids(i) for i in _source_info.values()))
        legacy = {t: sts for t, sts in _legacy.items() if t.split('/')[3] not in current} \
            if _discovery_ready.is_set() else {}
        # Etats retenus des orphelins (seuil, nom, sons...) : jamais un topic
        # d'une source gardee ou de l'appareil « ClapTrap ».
        kept_prefixes = tuple(f'claptrap/{slug}/' for slug in known_slugs) + (
            'claptrap/detection/', 'claptrap/command', 'claptrap/configuration')
        kept_states = set().union(*(state_topics(i) for i in _source_info.values()))
        orphan_states = {st for d in orphan_devices for st in _seen_devices.get(d, [])}
        orphan_states |= {st for sts in legacy.values() for st in sts}
        orphan_states = {st for st in orphan_states if not st.startswith(kept_prefixes) and st not in kept_states}
        stale_availability = [slug for slug in _availability_seen if slug not in known_slugs]
    for dev in orphan_devices:
        _unpublish_device(dev)
    for topic in legacy:
        _mqtt_publish(topic, '', retain=True)
    with _lock:
        for topic in legacy:
            _legacy.pop(topic, None)
    for topic in sorted(orphan_states):
        _mqtt_publish(topic, '', retain=True)
    orphans = [f'appareil {d}' for d in orphan_devices] + [f"{t.split('/')[1]}.claptrap_{t.split('/')[3]}" for t in legacy]
    # Topics de disponibilite retenus des sources disparues : les effacer aussi.
    for slug in stale_availability:
        _mqtt_publish(f'claptrap/{slug}/availability', '', retain=True)
    if orphans:
        logging.info(f"Entités orphelines supprimées: {', '.join(sorted(orphans))}")
    return sorted(orphans)


def entity_ids_for_settings(settings):
    """entity_id de chaque groupe des sources publiees, calcules comme a la
    publication (meme parcours) : l'interface les affiche tels quels, et une
    source non publiee (collision) n'en affiche plus.
    Cles : "mic", "rtsp:<id>", "vban:<id>"."""
    out = {}
    for ui_key, key, _, _, _, norm in _resolve_sources(settings)[0]:
        slug = _make_slug(key)
        out[ui_key] = {g_slug: [f'binary_sensor.claptrap_{obj}' for obj in _group_objs(slug, g['entity_slug'], g)]
                       for g_slug, g in norm.items()}
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


def set_alert(entity_key, group_slug, on):
    """Mode alerte : capteur du groupe allume ou eteint (etat retenu)."""
    with _lock:
        info = _source_info.get(entity_key)
        obj = alert_object_id(info['slug'] if info else _make_slug(entity_key), group_slug)
        if not on:
            # Toujours oublie (groupe passe a un autre mode, retire) : sinon
            # republie « ON » au retour du mode alerte, pour toujours.
            _alerts_on.discard(obj)
        if info is None or not (info.get('groups') or {}).get(group_slug, {}).get('alert'):
            logging.info(f"Alerte sur {entity_key}/{group_slug} : pas de capteur d'alerte publié")
            return
        if on:
            _alerts_on.add(obj)
        # Sous le verrou, comme la republication : un « OFF » calcule juste
        # avant ne passe plus apres ce « ON ».
        _mqtt_publish(f'claptrap/{obj}/state', 'ON' if on else 'OFF', retain=True)


def on_clap_detected(entity_key, score, clap_count, group_slug='clap', group_clap_counts=None, labels=None):
    """Appele quand un clap est detecte. Route vers l'entite du bon groupe."""
    with _lock:
        info = _source_info.get(entity_key)
    if info is None:
        # Source non publiee (collision heritee) : ne pas declencher les
        # entites de meme identifiant d'une autre source.
        logging.info(f"Clap sur {entity_key} : source sans entités publiées")
        return
    source_slug = info['slug']
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
    # Entite event du groupe (non retenu : un evenement ne se rejoue pas).
    _mqtt_publish(f'claptrap/{source_slug}/{group_slug}/evenement', {
        'event_type': event_type(clap_count), 'score': round(float(score), 3),
        'sons': [{'classe': l.get('label'), 'score': round(float(l.get('score', 0)), 3)}
                 for l in (labels or []) if isinstance(l, dict)],
    })
    return True   # une entite a ete declenchee
