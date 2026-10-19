"""Pilotage de ClapTrap depuis Home Assistant (MQTT).

Permet a HA (tableaux de bord, automatisations, assistant IA via un serveur
MCP) de lire et de modifier les reglages sans passer par l'interface :

Entites de reglage et de diagnostic (MQTT discovery, appareil "ClapTrap") :
- par groupe : number <source>_<groupe>_seuil (seuil en %), switch
  <source>_<groupe>_groupe_actif, sensor <source>_<groupe>_score (dernier
  score du groupe, meme sous le seuil) ;
- par source : switch <source>_source_active, sensor <source>_derniere_classe
  (classe YAMNet la plus probable du dernier son, 3 premieres en attributs),
  sensor <source>_top3 ;
- globales : switch claptrap_detection (demarrer / arreter), sensor
  claptrap_configuration (sources, groupes et references en attributs),
  sensor claptrap_derniere_commande (resultat de la derniere commande).

Commandes JSON sur `claptrap/command` (service mqtt.publish), pour ajouter,
modifier ou supprimer sources, groupes, sons... Le resultat est publie sur
`claptrap/command/result` (capteur « Dernière commande »), secrets masques.
Chaque commande passe par les routes de l'API : memes validations, memes
effets (relance de la detection, entites HA) que depuis l'interface. Les
commandes sont executees une par une, dans l'ordre de reception.
"""

import json
import logging
import queue
import threading
import time

import ha_entities as ha

COMMAND_TOPIC = 'claptrap/command'
RESULT_TOPIC = 'claptrap/command/result'
RESULT_ATTRIBUTES_TOPIC = 'claptrap/command/result/attributs'
CONFIG_TOPIC = 'claptrap/configuration'
DETECTION_SET_TOPIC = 'claptrap/detection/set'
SUBSCRIPTIONS = (COMMAND_TOPIC, DETECTION_SET_TOPIC, 'claptrap/+/source_active/set',
                 'claptrap/+/+/seuil/set', 'claptrap/+/+/groupe_actif/set', 'claptrap/+/+/nom/set')
ANALYSIS_INTERVAL = 2.0   # s entre deux mises a jour des capteurs d'une source
STATES_DELAY = 0.3        # regroupement des republications apres une ecriture

_api = None               # fonction (method, path, body) -> (status, dict)
_notify_ui = None         # previent l'interface ouverte d'un changement venu de HA
_commands = queue.Queue()
_worker = None
_worker_lock = threading.Lock()
_published = {}           # topic -> dernier etat publie (evite les doublons)
_published_lock = threading.Lock()
_analysis_last = {}       # entity_key -> (instant, charge publiee)
_states_timer = None
_states_lock = threading.Lock()

# Aide publiee dans le capteur « Configuration » : un assistant IA y trouve
# la liste des commandes et leurs champs.
ACTIONS = {
    'help': 'Liste des commandes.',
    'get_settings': 'Réglages complets (secrets masqués) et références des sources.',
    'start_detection': 'Démarre la détection.',
    'stop_detection': 'Arrête la détection.',
    'add_rtsp': '{name, url, enabled?, rtsp_tcp?} : ajoute une caméra RTSP.',
    'add_vban': '{name, ip, port?=6980, stream_name?, enabled?} : ajoute un flux VBAN.',
    'add_microphone': '{device?: {name, index, pulse_name}, enabled?} : ajoute le micro.',
    'update_source': '{source, name?, url?, enabled?, gain?, webhook_url?, device?, volume?, auto_volume?, '
                     'rtsp_tcp?(caméra)}.',
    'delete_source': '{source} : supprime une source et ses entités.',
    'add_group': '{source, name, threshold?(0-1), ha_entities?([1-4])} : ajoute un groupe de sons.',
    'update_group': '{source, group, name?, threshold?(0-1), ha_entities?, enabled?, auto_add_sounds?, '
                    'count_mode?("attaques"|"tenus")}.',
    'delete_group': '{source, group} (le premier groupe est protégé).',
    'set_sound': '{source, group, label, enabled} : coche ou décoche une classe YAMNet dans un groupe.',
    'clear_unchecked_sounds': '{source, group} : retire les sons non cochés.',
    'set_exclusion': '{label, excluded} : ignore une classe YAMNet partout.',
    'set_advanced': '{delay?, peak_cooldown?, peak_ratio?} : réglages avancés.',
    'calibration_start': '{source} : commence le calibrage (détection démarrée si besoin, source en sourdine).',
    'calibration_record': '{target, expected?, duration?(s, 4)} : une prise ; jouez le bruit du groupe target '
                          '(existant ou nouveau nom) ou « fond » dès l\'envoi. expected : nombre de pics attendus.',
    'calibration_analyze': 'Propose gain, sons, seuil et comptage de chaque groupe (reconnaissance avant/après).',
    'calibration_apply': '{gain?(true), groups?([noms])} : applique la proposition et termine.',
    'calibration_cancel': 'Abandonne le calibrage sans rien modifier.',
    'calibration_status': 'Calibrage en cours et prises enregistrées.',
}
SOURCE_HELP = ('source : "mic", "rtsp:<id>", "vban:<id>", clé d\'entité (ex. "vban_salon") ou nom ; '
               'group : slug ou nom du groupe.')


def init(api_caller, notify_ui=None):
    """`api_caller(method, path, body)` -> (code HTTP, reponse JSON) ;
    `notify_ui()` : previent l'interface ouverte qu'un reglage a change."""
    global _api, _notify_ui
    _api, _notify_ui = api_caller, notify_ui
    from settings_manager import add_change_listener
    add_change_listener(schedule_states)


# ===== Entites de reglage ===================================================

def _base_config(component, obj, name, entity_obj=None, **extra):
    """`obj` : unique_id (stable) ; `entity_obj` : entity_id propose (suit le
    nom du groupe, renomme dans le registre de HA par ha_registry). L'appareil
    est porte par le message de decouverte de l'appareil (ha_entities)."""
    cfg = {
        'name': name,
        'unique_id': f'claptrap_{obj}',
        'default_entity_id': f'{component}.claptrap_{entity_obj or obj}',
        'availability_topic': ha.AVAILABILITY_TOPIC,
    }
    cfg.update(extra)
    return cfg


def source_entities(info):
    """[(composant, object_id, config)] des entites de reglage et de
    diagnostic d'une source publiee."""
    slug = info['slug']
    analyse = f'claptrap/{slug}/analyse'
    out = [
        ('switch', f'{slug}_source_active', _base_config(
            'switch', f'{slug}_source_active', 'Source active',
            command_topic=f'claptrap/{slug}/source_active/set', state_topic=f'claptrap/{slug}/source_active',
            payload_on='ON', payload_off='OFF', entity_category='config', icon='mdi:microphone-settings')),
        ('sensor', f'{slug}_derniere_classe', _base_config(
            'sensor', f'{slug}_derniere_classe', 'Dernière classe',
            state_topic=analyse, value_template='{{ value_json.classe }}',
            json_attributes_topic=analyse,
            json_attributes_template='{{ {"score": value_json.score, "top3": value_json.top3, '
                                     '"groupes": value_json.groupes, "heure": value_json.heure, '
                                     '"diagnostic": value_json.diagnostic | default({})} | tojson }}',
            entity_category='diagnostic', icon='mdi:waveform')),
        ('sensor', f'{slug}_top3', _base_config(
            'sensor', f'{slug}_top3', '3 classes probables',
            state_topic=analyse,
            value_template='{{ value_json.top3 | map(attribute="resume") | join(", ") }}',
            entity_category='diagnostic', icon='mdi:format-list-numbered')),
    ]
    for g_slug, g in info['groups'].items():
        base, ebase = f'{slug}_{g_slug}', f"{slug}_{g.get('entity_slug') or g_slug}"
        name = g['name']   # HA prefixe du nom de l'appareil de la source
        topic = f'claptrap/{slug}/{g_slug}'
        out += [
            ('number', f'{base}_seuil', _base_config(
                'number', f'{base}_seuil', f'{name} · seuil', f'{ebase}_seuil',
                command_topic=f'{topic}/seuil/set', state_topic=f'{topic}/seuil',
                min=0, max=100, step=1, unit_of_measurement='%', mode='slider',
                entity_category='config', icon='mdi:tune-vertical')),
            ('switch', f'{base}_groupe_actif', _base_config(
                'switch', f'{base}_groupe_actif', f'{name} · actif', f'{ebase}_groupe_actif',
                command_topic=f'{topic}/groupe_actif/set', state_topic=f'{topic}/groupe_actif',
                payload_on='ON', payload_off='OFF', entity_category='config', icon='mdi:toggle-switch-outline')),
            ('sensor', f'{base}_score', _base_config(
                'sensor', f'{base}_score', f'{name} · score', f'{ebase}_score',
                state_topic=analyse, unit_of_measurement='%',
                value_template=('{% set s = value_json.groupes.get("' + g_slug + '") %}'
                                '{{ (s * 100) | round(0) if s is not none else none }}'),
                entity_category='diagnostic', icon='mdi:gauge')),
            # Renommer le groupe depuis HA (entity_id renommes, historique conserve).
            ('text', f'{base}_nom', _base_config(
                'text', f'{base}_nom', f'{name} · nom', f'{ebase}_nom',
                command_topic=f'{topic}/nom/set', state_topic=f'{topic}/nom',
                min=1, max=80, mode='text', entity_category='config', icon='mdi:rename')),
            *([('event', f'{base}_evenement', _base_config(
                'event', f'{base}_evenement', name, f'{ebase}_evenement',
                # score et sons : attributs (cles autres qu'event_type du message)
                state_topic=f'{topic}/evenement', device_class='button', icon='mdi:hand-clap',
                event_types=[ha.event_type(n) for n in g['clap_counts']]))]
              if g.get('clap_counts') else []),
            ('sensor', f'{base}_sons', _base_config(
                'sensor', f'{base}_sons', f'{name} · sons', f'{ebase}_sons',
                state_topic=f'{topic}/sons', value_template='{{ value_json.nombre }}',
                json_attributes_topic=f'{topic}/sons',
                json_attributes_template='{{ {"sons": value_json.sons, "sons_non_coches": value_json.non_coches, '
                                         '"comptage": value_json.comptage} | tojson }}',
                entity_category='diagnostic', icon='mdi:playlist-music')),
        ]
    return out


def source_entity_ids(info):
    """object_id des entites de reglage et de diagnostic d'une source (memes
    que source_entities, sans construire les configurations)."""
    slug = info['slug']
    ids = {f'{slug}_source_active', f'{slug}_derniere_classe', f'{slug}_top3'}
    for g_slug, g in info['groups'].items():
        ids |= {f'{slug}_{g_slug}_{s}' for s in ('seuil', 'groupe_actif', 'score', 'nom', 'sons')}
        if g.get('clap_counts'):
            ids.add(f'{slug}_{g_slug}_evenement')
    return ids


def control_state_topics(info):
    """Topics d'etat retenus des entites de reglage et de diagnostic d'une
    source, a vider quand la source ou le groupe disparait."""
    slug = info['slug']
    topics = {f'claptrap/{slug}/source_active', f'claptrap/{slug}/analyse'}
    for g_slug in info['groups']:
        topics |= {f'claptrap/{slug}/{g_slug}/{t}' for t in ('seuil', 'groupe_actif', 'nom', 'sons')}
    return topics


def clear_topics(topics):
    """Vide des topics retenus (entites retirees) ; ils seront republies si
    la source ou le groupe revient."""
    with _published_lock:
        for topic in topics:
            _published.pop(topic, None)
    for topic in topics:
        ha._mqtt_publish(topic, '', retain=True)


def global_entities():
    return [
        ('switch', 'detection_active', _base_config(
            'switch', 'detection_active', 'Détection',
            default_entity_id='switch.claptrap_detection',
            command_topic=DETECTION_SET_TOPIC, state_topic='claptrap/detection/state',
            payload_on='ON', payload_off='OFF', icon='mdi:ear-hearing')),
        ('sensor', 'configuration', _base_config(
            'sensor', 'configuration', 'Configuration',
            state_topic=CONFIG_TOPIC, value_template='{{ value_json.resume }}',
            json_attributes_topic=CONFIG_TOPIC,
            json_attributes_template='{{ value_json.details | tojson }}',
            entity_category='diagnostic', icon='mdi:cog')),
        ('sensor', 'derniere_commande', _base_config(
            'sensor', 'derniere_commande', 'Dernière commande',
            state_topic=RESULT_TOPIC,
            value_template="{{ 'ok' if value_json.success else 'erreur' }}",
            json_attributes_topic=RESULT_ATTRIBUTES_TOPIC,
            entity_category='diagnostic', icon='mdi:console')),
    ]


# ===== Etats (reglages -> HA) ===============================================

def _publish_state(topic, payload):
    """Publie un etat retenu s'il a change depuis la derniere publication."""
    data = json.dumps(payload, ensure_ascii=False) if isinstance(payload, (dict, list)) else str(payload)
    with _published_lock:
        if _published.get(topic) == data:
            return
        _published[topic] = data
    ha._mqtt_publish(topic, data, retain=True)


def reset_published():
    """Broker reconnecte ou HA redemarre : tout republier."""
    with _published_lock:
        _published.clear()


def schedule_states():
    """Apres une ecriture des reglages (interface, HA, import) : republier les
    etats un peu plus tard, en une fois (une modification ecrit parfois
    plusieurs fois)."""
    global _states_timer
    with _states_lock:
        if _states_timer is not None:
            return
        _states_timer = threading.Timer(STATES_DELAY, _run_states)
        _states_timer.daemon = True
        _states_timer.start()


def _run_states():
    global _states_timer
    with _states_lock:
        _states_timer = None
    try:
        publish_states()
    except Exception as e:
        logging.warning(f"États des réglages non publiés dans HA: {e}")


def _published_sources(settings):
    """[(ui_key, kind, key, slug, src_dict, label, enabled)] des sources publiees."""
    out = []
    for ui_key, entity_key, label, _groups, enabled, _norm in ha._resolve_sources(settings)[0]:
        kind, _, key = ui_key.partition(':')
        if kind == 'mic':
            key, src = 'mic', settings.get('microphone') or {}
        else:
            lst = settings.get('rtsp_sources' if kind == 'rtsp' else 'saved_vban_sources') or []
            src = next((s for s in lst if str(s.get('id')) == key), {})
        out.append((ui_key, kind, key, ha._make_slug(entity_key), src, label, enabled))
    return out


def publish_states(settings=None):
    if not ha._mqtt_connected.is_set():
        return
    from settings_manager import load_settings
    settings = settings or load_settings()
    sources = _published_sources(settings)
    for ui_key, kind, key, slug, src, label, enabled in sources:
        _publish_state(f'claptrap/{slug}/source_active', 'ON' if enabled else 'OFF')
        for g in src.get('sound_groups') or []:
            if not isinstance(g, dict) or not g.get('slug'):
                continue
            threshold = g.get('threshold', settings.get('global', {}).get('threshold', 0.5))
            _publish_state(f"claptrap/{slug}/{g['slug']}/seuil", round(float(threshold) * 100))
            _publish_state(f"claptrap/{slug}/{g['slug']}/groupe_actif",
                           'OFF' if g.get('enabled', True) is False else 'ON')
            _publish_state(f"claptrap/{slug}/{g['slug']}/nom", g.get('name') or g['slug'])
            wl = g.get('sound_whitelist') or {}
            checked = sorted(label for label, on in wl.items() if on)
            _publish_state(f"claptrap/{slug}/{g['slug']}/sons", {
                'nombre': len(checked), 'sons': checked, 'non_coches': len(wl) - len(checked),
                'comptage': g.get('count_mode', 'attaques')})
    _publish_state(CONFIG_TOPIC, _configuration_payload(settings, sources))


def _configuration_payload(settings, sources=None):
    sources = sources if sources is not None else _published_sources(settings)
    entity_ids = ha.entity_ids_for_settings(settings)
    details_sources = []
    n_groups = 0
    for ui_key, kind, key, slug, src, label, enabled in sources:
        groups = []
        for g in src.get('sound_groups') or []:
            if not isinstance(g, dict) or not g.get('slug'):
                continue
            n_groups += 1
            groups.append({
                'group': g['slug'], 'name': g.get('name') or g['slug'],
                'enabled': g.get('enabled', True) is not False,
                'threshold': g.get('threshold'),
                'sounds': sorted(l for l, on in (g.get('sound_whitelist') or {}).items() if on),
                'unchecked_sounds': len([l for l, on in (g.get('sound_whitelist') or {}).items() if not on]),
                'auto_add_sounds': bool(g.get('auto_add_sounds')),
                'count_mode': g.get('count_mode', 'attaques'),
                'ha_entities': (entity_ids.get(ui_key) or {}).get(g['slug'], []),
            })
        details_sources.append({'source': ui_key, 'type': kind, 'name': label, 'entity_key': slug,
                                'enabled': bool(enabled), 'groups': groups})
    g = settings.get('global', {})
    return {
        'resume': f"{len(sources)} source(s), {n_groups} groupe(s)",
        'details': {
            'sources': details_sources,
            'sound_exclusions': g.get('sound_exclusions') or [],
            'advanced': {k: g.get(k) for k in ('delay', 'peak_cooldown', 'peak_ratio')},
            'commandes': (f"mqtt.publish sur {COMMAND_TOPIC} : {{\"action\": ..., \"id\"?: ...}}. {SOURCE_HELP} "
                          f"Résultat dans sensor.claptrap_derniere_commande." if ha.commands_enabled()
                          else "désactivées (option « Commandes MQTT » de l'add-on)"),
            'actions': ACTIONS,
        },
    }


# ===== Diagnostic (detection -> HA) =========================================

def on_analysis(entity_key, top, group_scores, stats=None):
    """Resultat YAMNet d'une source : met a jour ses capteurs de diagnostic.

    Le silence ne remplace pas le dernier son entendu ; au plus une mise a
    jour toutes les ANALYSIS_INTERVAL s par source, et seulement si les
    valeurs (arrondies) changent : l'historique de HA n'est pas inonde.
    """
    if not top or top[0][0] == 'Silence' or not ha._mqtt_connected.is_set():
        return
    with ha._lock:
        info = ha._source_info.get(entity_key)
    if info is None:
        return
    now = time.monotonic()
    last = _analysis_last.get(entity_key)
    if last and now - last[0] < ANALYSIS_INTERVAL:
        return
    payload = {
        'classe': top[0][0],
        'score': round(float(top[0][1]), 2),
        'top3': [{'classe': n, 'score': round(float(s), 2), 'resume': f'{n} {round(float(s) * 100)} %'}
                 for n, s in top],
        'groupes': {k: round(float(v), 2) for k, v in (group_scores or {}).items()},
        # Analyses sautees (Pi surcharge), coupures du flux, duree des analyses.
        'diagnostic': stats or {},
    }
    # Mise a jour seulement si le son change (classes) ou si le score d'un
    # groupe bouge nettement : un bruit de fond qui fluctue de quelques
    # points republiait toutes les 2 s, indefiniment.
    diag = payload['diagnostic']
    key = (tuple(n for n, _ in top), {k: round(v * 10) for k, v in payload['groupes'].items()},
           # nouvelle coupure ou analyse sautee : diagnostic republie aussi
           (diag.get('analyses_sautees'), diag.get('blocs_perdus'), diag.get('coupures')))
    if last and last[1] == key:
        return
    _analysis_last[entity_key] = (now, key)
    ha._mqtt_publish(f"claptrap/{info['slug']}/analyse",
                     dict(payload, heure=time.strftime('%Y-%m-%dT%H:%M:%S%z')), retain=True)


# ===== Commandes (HA -> reglages) ===========================================

def handle_message(topic, payload, retained=False):
    """Message MQTT sur un topic de commande (appele depuis le fil paho).

    Un seul fil traite les commandes, dans l'ordre : « ajouter une source »
    puis « ajouter un groupe » a cette source ne se croisent pas. Une
    commande retenue sur le broker serait rejouee a chaque reconnexion
    (camera ajoutee en double) : ignoree, et effacee du broker.
    """
    global _worker
    if retained:
        if payload:
            logging.warning(f"Commande retenue ignorée sur {topic} (publiez sans retain)")
            ha._mqtt_publish(topic, '', retain=True)
        return
    if not payload:
        return
    if topic == COMMAND_TOPIC and not ha.commands_enabled():
        logging.warning(f"Commande ignorée sur {topic} : option « Commandes MQTT » décochée")
        return
    _commands.put((topic, payload))
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, daemon=True, name='ha-commands')
            _worker.start()


def _work():
    while True:
        topic, payload = _commands.get()
        _handle(topic, payload)


def _handle(topic, payload):
    text = payload.decode('utf-8', errors='replace') if isinstance(payload, bytes) else str(payload)
    try:
        if topic == COMMAND_TOPIC:
            _run_command(text)
        elif topic == DETECTION_SET_TOPIC:
            _entity_call('detection', _detection_request(text))
        else:
            _entity_command(topic, text)
    except Exception as e:
        # Entite perimee (source renommee, supprimee), valeur non numerique :
        # erreur visible et etat reel republie, comme un refus de l'API.
        logging.warning(f"Commande HA ({topic}) : {e}")
        _entity_failed(topic, 400, f"commande refusée : {e}" if not isinstance(e, KeyError)
                       else f"source ou groupe inconnu ({e})")


def _on(text):
    return text.strip().upper() in ('ON', '1', 'TRUE')


def _detection_request(text):
    return ('POST', '/api/detection/start' if _on(text) else '/api/detection/stop', None)


def _call(method, path, body=None):
    if _api is None:
        raise RuntimeError("API indisponible")
    return _api(method, path, body)


def _entity_call(what, request):
    """Commande issue d'une entite (switch, number) : en cas d'echec, l'etat
    reel est republie (HA revient a la valeur precedente) et l'erreur est
    visible dans « Dernière commande »."""
    status, data = _call(*request)
    if status >= 400:
        _entity_failed(what, status, (data or {}).get('error') or f'HTTP {status}')
        return
    if _notify_ui:
        _notify_ui()
    if what == 'detection':
        # L'etat de la detection est publie par la session : pas de cache.
        ha._mqtt_publish('claptrap/detection/state', 'ON' if ha._detection['running'] else 'OFF', retain=True)
    schedule_states()


def _entity_failed(what, status, error):
    # Valeur refusee (nom deja pris, seuil hors bornes...) : la valeur reelle
    # est republiee, HA revient a l'ancienne.
    logging.warning(f"Commande HA « {what} » refusée : {error} (valeur précédente republiée)")
    _publish_result({'action': what, 'success': False, 'status': status, 'error': error})
    reset_published()
    schedule_states()


def _entity_command(topic, text):
    parts = topic.split('/')
    from settings_manager import load_settings
    settings = load_settings()
    by_slug = {slug: (kind, key, src) for _u, kind, key, slug, src, _l, _e in _published_sources(settings)}
    if len(parts) == 4 and parts[2] == 'source_active':
        kind, key, _src = by_slug[parts[1]]
        _entity_call('source_active', ('PATCH', f'/api/sources/{kind}/{key}', {'enabled': _on(text)}))
        return
    if len(parts) == 5:
        kind, key, _src = by_slug[parts[1]]
        body = {'kind': kind, 'source_key': None if kind == 'mic' else key, 'group_slug': parts[2]}
        if parts[3] == 'seuil':
            body['threshold'] = float(text) / 100
        elif parts[3] == 'groupe_actif':
            body['enabled'] = _on(text)
        elif parts[3] == 'nom':
            body['name'] = text
        else:
            return
        _entity_call(parts[3], ('PUT', '/api/source/sound_groups', body))


def _masked(obj):
    """Copie sans secrets d'une reponse : le resultat est retenu sur le
    broker, lisible par tout abonne (l'ajout d'une camera renvoyait son URL
    avec le mot de passe)."""
    from url_validator import mask_url_credentials, mask_webhook_url
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == 'url' and isinstance(v, str):
                out[k] = mask_url_credentials(v)
            elif k == 'webhook_url' and isinstance(v, str) and v:
                out[k] = mask_webhook_url(v)
            else:
                out[k] = _masked(v)
        return out
    if isinstance(obj, list):
        return [_masked(v) for v in obj]
    return obj


ATTRIBUTES_MAX = 4096   # octets : le recorder de HA refuse les attributs de plus de 16 Ko


def _publish_result(result):
    """Resultat complet sur le topic ; en attributs du capteur, le detail
    seulement s'il reste petit (bilan d'un renommage) : get_settings ou help
    depassaient la limite du recorder de HA."""
    result = _masked(result)
    attributes = result
    if len(json.dumps(result, ensure_ascii=False).encode()) > ATTRIBUTES_MAX:
        attributes = {k: v for k, v in result.items() if k not in ('data', 'actions')}
        attributes['detail'] = f"résultat complet sur le topic {RESULT_TOPIC}"
    ha._mqtt_publish(RESULT_ATTRIBUTES_TOPIC, attributes, retain=True)
    ha._mqtt_publish(RESULT_TOPIC, result, retain=True)


def _run_command(text):
    try:
        cmd = json.loads(text)
    except ValueError:
        cmd = None
    if not isinstance(cmd, dict) or not isinstance(cmd.get('action'), str):
        _publish_result({'success': False, 'error': 'JSON attendu : {"action": "...", ...}', 'actions': ACTIONS})
        return
    action = cmd['action']
    result = {'id': cmd.get('id'), 'action': action}
    try:
        data = execute(action, cmd)
        result.update(success=True, data=data)
        if _notify_ui and action not in ('help', 'get_settings'):
            _notify_ui()
    except CommandError as e:
        result.update(success=False, error=str(e), status=e.status)
    except Exception as e:
        logging.warning(f"Commande HA {action} : {e}")
        result.update(success=False, error=str(e))
    logging.info(f"Commande HA {action} : {'ok' if result['success'] else result.get('error')}")
    _publish_result(result)


class CommandError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _resolve_source(settings, ref):
    """(kind, key, src) d'apres une reference souple (voir SOURCE_HELP)."""
    if not isinstance(ref, str) or not ref.strip():
        raise CommandError('source : référence attendue. ' + SOURCE_HELP)
    want = ref.strip().lower()
    sources = _published_sources(settings)
    # Identifiants d'abord (uniques), puis noms : deux sources du meme nom
    # donnent une erreur plutot que de modifier (ou supprimer) la premiere.
    for exact in (True, False):
        found = []
        for ui_key, kind, key, slug, src, label, _e in sources:
            names = ({ui_key.lower(), slug.lower()} if exact else
                     {str(key).lower(), label.lower(), str(src.get('name') or '').lower()})
            if want in names - {''}:
                found.append((ui_key, kind, key, src))
        if len(found) == 1:
            return found[0][1:]
        if found:
            raise CommandError(f'source « {ref} » ambiguë : précisez parmi '
                               + ', '.join(f[0] for f in found), 409)
    raise CommandError(f'source « {ref} » introuvable. ' + SOURCE_HELP, 404)


def _resolve_group(src, ref):
    if not isinstance(ref, str) or not ref.strip():
        raise CommandError('group : slug ou nom du groupe attendu')
    want = ref.strip().lower()
    for g in src.get('sound_groups') or []:
        if isinstance(g, dict) and want in (str(g.get('slug', '')).lower(), str(g.get('name', '')).lower()):
            return g['slug']
    raise CommandError(f'groupe « {ref} » introuvable', 404)


def _api_or_raise(method, path, body=None):
    status, data = _call(method, path, body)
    if status >= 400:
        raise CommandError((data or {}).get('error') or f'HTTP {status}', status)
    return data


_SOURCE_FIELDS = ('name', 'url', 'enabled', 'gain', 'webhook_url', 'device', 'volume', 'auto_volume', 'rtsp_tcp')
_GROUP_FIELDS = ('name', 'threshold', 'ha_entities', 'enabled', 'auto_add_sounds', 'count_mode')


def _pick(cmd, fields):
    return {k: cmd[k] for k in fields if k in cmd}


def execute(action, cmd):
    """Execute une commande ; retourne les donnees de la reponse."""
    from settings_manager import load_settings, shareable_settings
    if action not in ACTIONS:
        raise CommandError(f'action « {action} » inconnue : ' + ', '.join(ACTIONS))
    if action == 'help':
        return {'actions': ACTIONS, 'references': SOURCE_HELP}
    if action == 'get_settings':
        settings = load_settings()
        return {'settings': shareable_settings(settings),
                'overview': _configuration_payload(settings)['details']['sources']}
    if action == 'start_detection':
        return _api_or_raise('POST', '/api/detection/start')
    if action == 'stop_detection':
        return _api_or_raise('POST', '/api/detection/stop')
    if action == 'add_rtsp':
        return _api_or_raise('POST', '/api/rtsp/stream', _pick(cmd, ('name', 'url', 'enabled', 'rtsp_tcp')))
    if action == 'add_vban':
        body = _pick(cmd, ('name', 'ip', 'port', 'stream_name', 'enabled'))
        body.setdefault('port', 6980)
        return _api_or_raise('POST', '/api/vban/save', body)
    if action == 'add_microphone':
        return _api_or_raise('POST', '/api/microphone', _pick(cmd, ('device', 'enabled')) or None)
    if action == 'set_exclusion':
        return _api_or_raise('PUT', '/api/sound_exclusions', _pick(cmd, ('label', 'excluded')))
    if action == 'set_advanced':
        return _api_or_raise('PUT', '/api/settings/advanced', _pick(cmd, ('delay', 'peak_cooldown', 'peak_ratio')))
    if action == 'calibration_status':
        return _api_or_raise('GET', '/api/calibration')
    if action.startswith('calibration_'):
        fields = {'calibration_start': ('source',), 'calibration_record': ('target', 'expected', 'duration'),
                  'calibration_apply': ('gain', 'groups')}.get(action, ())
        return _api_or_raise('POST', f"/api/calibration/{action[len('calibration_'):]}", _pick(cmd, fields))

    settings = load_settings()
    kind, key, src = _resolve_source(settings, cmd.get('source'))
    source_key = None if kind == 'mic' else key
    if action == 'update_source':
        body = _pick(cmd, _SOURCE_FIELDS)
        if not body:
            raise CommandError('aucun champ à modifier : ' + ', '.join(_SOURCE_FIELDS))
        return _api_or_raise('PATCH', f'/api/sources/{kind}/{key}', body)
    if action == 'delete_source':
        path = {'mic': '/api/microphone', 'rtsp': f'/api/rtsp/stream/{key}', 'vban': f'/api/vban/{key}'}[kind]
        return _api_or_raise('DELETE', path)
    if action == 'add_group':
        body = {'kind': kind, 'source_key': source_key, **_pick(cmd, ('name', 'threshold', 'ha_entities'))}
        return _api_or_raise('POST', '/api/source/sound_groups', body)

    group = _resolve_group(src, cmd.get('group'))
    base = {'kind': kind, 'source_key': source_key, 'group_slug': group}
    if action == 'update_group':
        body = _pick(cmd, _GROUP_FIELDS)
        if not body:
            raise CommandError('aucun champ à modifier : ' + ', '.join(_GROUP_FIELDS))
        return _api_or_raise('PUT', '/api/source/sound_groups', {**base, **body})
    if action == 'delete_group':
        return _api_or_raise('DELETE', '/api/source/sound_groups', base)
    if action == 'set_sound':
        return _api_or_raise('PUT', '/api/source/sound_whitelist', {**base, **_pick(cmd, ('label', 'enabled'))})
    if action == 'clear_unchecked_sounds':
        return _api_or_raise('POST', '/api/source/sound_whitelist/cleanup', base)
    raise CommandError(f'action « {action} » inconnue', 400)
