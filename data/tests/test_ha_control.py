"""Pilotage depuis Home Assistant : entites de reglage, commandes MQTT, capteurs."""
import json

import pytest

import ha_control
import ha_entities as ha
from helpers import wait_until


@pytest.fixture
def ctl(settings_dir, mqtt, monkeypatch):
    """API reelle (blueprints Flask) branchee sur ha_control, reglages temporaires."""
    from flask import Flask
    import routes.sources as rs
    from routes.detection import detection_bp
    from routes.settings_routes import settings_bp
    monkeypatch.setattr(rs, '_find_pulse_name', lambda s: ('alsa.usb', None))
    app = Flask(__name__)
    for bp in (rs.sources_bp, settings_bp, detection_bp):
        app.register_blueprint(bp)
    client = app.test_client()

    def call(method, path, body=None):
        r = client.open(path, method=method, json=body)
        return r.status_code, r.get_json(silent=True) or {}
    monkeypatch.setattr(ha_control, '_api', call)
    settings_dir.save_settings({
        'microphone': {'configured': True, 'enabled': True, 'sound_groups': [
            {'slug': 'clap', 'name': 'Clap', 'threshold': 0.5, 'ha_entities': [1, 2],
             'sound_whitelist': {'Clapping': True, 'Speech': False}},
            {'slug': 'siffler', 'name': 'Siffler', 'threshold': 0.4, 'ha_entities': [3],
             'sound_whitelist': {'Whistling': True}}]},
        'saved_vban_sources': [{'id': 'v1', 'entity_key': 'vban_atomsalon', 'name': 'AtomSalon', 'ip': '1.1.1.1',
                                'port': 6980, 'stream_name': 'S', 'enabled': True,
                                'webhook_url': 'http://ha:8123/api/webhook/secretid',
                                'sound_groups': [{'slug': 'clap', 'name': 'Clap', 'ha_entities': [1]}]}],
        'rtsp_sources': [{'id': 'abcdef123456', 'name': 'Cam', 'url': 'rtsp://u:motdepasse@h/x', 'enabled': False,
                          'sound_groups': [{'slug': 'clap', 'name': 'Clap', 'ha_entities': [1]}]}],
    })
    ha.sync_sources()
    return settings_dir


def configs(client, component):
    return {t.split('/')[3]: json.loads(p) for t, p, r in client.published
            if t.startswith(f'homeassistant/{component}/') and t.endswith('/config') and p}


def last(client, topic):
    vals = [p for t, p, r in client.published if t == topic]
    return vals[-1] if vals else None


def command(cmd):
    ha_control._handle(ha_control.COMMAND_TOPIC, json.dumps(cmd).encode())


def result(client):
    return json.loads(last(client, ha_control.RESULT_TOPIC))


def test_discovery_of_settings_and_diagnostic_entities(ctl, mqtt):
    numbers, switches, sensors = configs(mqtt, 'number'), configs(mqtt, 'switch'), configs(mqtt, 'sensor')
    assert {'mic_clap_seuil', 'mic_siffler_seuil', 'vban_atomsalon_clap_seuil'} <= set(numbers)
    n = numbers['mic_siffler_seuil']
    assert n['entity_category'] == 'config' and n['command_topic'] == 'claptrap/mic/siffler/seuil/set'
    assert n['default_entity_id'] == 'number.claptrap_mic_siffler_seuil' and n['max'] == 100
    assert {'mic_siffler_groupe_actif', 'vban_atomsalon_source_active'} <= set(switches)
    assert {'mic_siffler_score', 'vban_atomsalon_derniere_classe', 'vban_atomsalon_top3'} <= set(sensors)
    assert sensors['mic_siffler_score']['entity_category'] == 'diagnostic'
    ha.republish_all()
    assert 'detection_active' in configs(mqtt, 'switch') and 'configuration' in configs(mqtt, 'sensor')


def test_states_published_from_settings_and_only_when_changed(ctl, mqtt):
    ha_control.publish_states()
    assert last(mqtt, 'claptrap/mic/siffler/seuil') == '40'
    assert last(mqtt, 'claptrap/mic/siffler/groupe_actif') == 'ON'
    assert last(mqtt, 'claptrap/rtsp_abcdef12/source_active') == 'OFF'
    conf = json.loads(last(mqtt, ha_control.CONFIG_TOPIC))
    vban = next(s for s in conf['details']['sources'] if s['source'] == 'vban:v1')
    assert vban['groups'][0]['ha_entities'] == ['binary_sensor.claptrap_vban_atomsalon_clap_1clap']
    assert 'actions' not in conf['details']                        # deja dans la commande help
    mic = next(s for s in conf['details']['sources'] if s['source'] == 'mic')
    assert mic['groups'][0]['sounds'] == 1                         # nombre seulement (liste : capteur sons)
    mqtt.published.clear()
    ha_control.publish_states()
    assert mqtt.published == []   # rien n'a change


def test_threshold_and_group_switch_from_ha(ctl, mqtt):
    ha_control._handle('claptrap/mic/siffler/seuil/set', b'65')
    groups = ctl.load_settings()['microphone']['sound_groups']
    assert groups[1]['threshold'] == 0.65
    ha_control._handle('claptrap/mic/siffler/groupe_actif/set', b'OFF')
    assert ctl.load_settings()['microphone']['sound_groups'][1]['enabled'] is False
    from classify import build_sources_from_settings
    mic = next(s for s in build_sources_from_settings(ctl.load_settings()) if s['kind'] == 'mic')
    assert [g['slug'] for g in mic['groups']] == ['clap']   # groupe desactive ignore
    ha_control._handle('claptrap/mic/clap/groupe_actif/set', b'OFF')
    mic = next(s for s in build_sources_from_settings(ctl.load_settings()) if s['kind'] == 'mic')
    assert mic['groups'] == []   # tous desactives : pas de groupe « Clap » par defaut
    ha_control.publish_states()
    assert last(mqtt, 'claptrap/mic/siffler/seuil') == '65'
    assert last(mqtt, 'claptrap/mic/siffler/groupe_actif') == 'OFF'


def test_invalid_value_reports_error_and_restores_state(ctl, mqtt):
    ha_control.publish_states()
    ha_control._handle('claptrap/mic/siffler/seuil/set', b'250')
    assert result(mqtt)['success'] is False
    ha_control.publish_states()
    assert last(mqtt, 'claptrap/mic/siffler/seuil') == '40'   # republie apres l'echec


def test_source_switch(ctl, mqtt):
    ha_control._handle('claptrap/rtsp_abcdef12/source_active/set', b'ON')
    assert ctl.load_settings()['rtsp_sources'][0]['enabled'] is True


def test_commands_groups_and_sounds_by_name(ctl, mqtt):
    command({'id': 'a1', 'action': 'add_group', 'source': 'AtomSalon', 'name': 'Siffler', 'ha_entities': [1]})
    r = result(mqtt)
    assert r['success'] and r['id'] == 'a1'
    command({'action': 'update_group', 'source': 'vban_atomsalon', 'group': 'Siffler', 'threshold': 0.3})
    command({'action': 'set_sound', 'source': 'vban:v1', 'group': 'siffler', 'label': 'Whistling', 'enabled': True})
    assert result(mqtt)['success']
    g = ctl.load_settings()['saved_vban_sources'][0]['sound_groups'][1]
    assert g['threshold'] == 0.3 and g['sound_whitelist'] == {'Whistling': True}
    command({'action': 'delete_group', 'source': 'AtomSalon', 'group': 'siffler'})
    assert result(mqtt)['success']
    assert 'homeassistant/number/claptrap/vban_atomsalon_siffler_seuil/config' in \
        [t for t, p, r in mqtt.published if p == '']   # entites du groupe retirees


def test_commands_sources(ctl, mqtt):
    command({'action': 'add_rtsp', 'name': 'Garage', 'url': 'rtsp://h/garage'})
    assert result(mqtt)['success']
    command({'action': 'update_source', 'source': 'Garage', 'gain': 20})
    assert result(mqtt)['success']
    assert next(s for s in ctl.load_settings()['rtsp_sources'] if s['name'] == 'Garage')['gain'] == 20
    command({'action': 'delete_source', 'source': 'Garage'})
    assert result(mqtt)['success']
    assert all(s['name'] != 'Garage' for s in ctl.load_settings()['rtsp_sources'])


def test_command_errors(ctl, mqtt):
    command({'action': 'update_group', 'source': 'inconnue', 'group': 'clap', 'threshold': 0.2})
    r = result(mqtt)
    assert not r['success'] and r['status'] == 404 and 'introuvable' in r['error']
    command({'action': 'nexiste_pas', 'source': 'mic', 'group': 'clap'})
    assert 'inconnue' in result(mqtt)['error']
    command({'action': 'api', 'method': 'GET', 'path': '/api/settings'})   # pas d'acces brut a l'API
    assert 'inconnue' in result(mqtt)['error']
    ha_control._handle(ha_control.COMMAND_TOPIC, b'pas du json')
    assert 'actions' in result(mqtt)


def test_get_settings_masks_secrets(ctl, mqtt):
    command({'action': 'get_settings'})
    text = json.dumps(result(mqtt)['data'])
    assert 'secretid' not in text and 'motdepasse' not in text
    assert result(mqtt)['data']['overview'][0]['source'] == 'mic'


def test_analysis_sensors(ctl, mqtt, monkeypatch):
    ha_control.on_analysis('vban_atomsalon', [('Silence', 0.9)], {'clap': 0.0})
    assert last(mqtt, 'claptrap/vban_atomsalon/analyse') is None   # le silence ne remplace rien
    ha_control.on_analysis('vban_atomsalon', [('Whistling', 0.71), ('Music', 0.2), ('Speech', 0.1)], {'clap': 0.12})
    p = json.loads(last(mqtt, 'claptrap/vban_atomsalon/analyse'))
    assert p['classe'] == 'Whistling' and p['groupes'] == {'clap': 0.12}
    assert [t['classe'] for t in p['top3']] == ['Whistling', 'Music', 'Speech']
    ha_control.on_analysis('vban_atomsalon', [('Clapping', 0.8)], {'clap': 0.8})
    assert json.loads(last(mqtt, 'claptrap/vban_atomsalon/analyse'))['classe'] == 'Whistling'   # limite a 1 / 2 s
    monkeypatch.setattr(ha_control, 'ANALYSIS_INTERVAL', 0)
    ha_control.on_analysis('vban_atomsalon', [('Clapping', 0.8)], {'clap': 0.8})
    assert json.loads(last(mqtt, 'claptrap/vban_atomsalon/analyse'))['classe'] == 'Clapping'


def _seen(topic, cfg):
    import types
    payload = json.dumps(cfg).encode() if cfg is not None else b''
    ha._on_message(None, None, types.SimpleNamespace(topic=topic, payload=payload, retain=True))


def _connect(monkeypatch):
    """_after_connect sans attente (configurations retenues deja recues)."""
    import time as _time
    import types
    monkeypatch.setattr(ha, 'time', types.SimpleNamespace(sleep=lambda s: None, time=_time.time,
                                                          monotonic=_time.monotonic))
    monkeypatch.setattr(ha, 'DISCOVERY_MAX_WAIT', 0)
    ha._after_connect()


def test_legacy_entities_migrated_then_orphans_cleaned(ctl, mqtt, monkeypatch):
    """Passage a la decouverte par appareil : chaque ancienne configuration
    d'une entite toujours publiee recoit migrate_discovery AVANT la
    configuration de l'appareil, puis est videe ; les orphelines sont
    effacees avec leurs etats, jamais un etat partage avec une entite gardee."""
    monkeypatch.setattr(ha, 'MIGRATE_CLEAR_DELAY', 0.05)
    p = 'homeassistant'
    seuil, conf = f'{p}/number/claptrap/mic_clap_seuil/config', f'{p}/sensor/claptrap/configuration/config'
    old = f'{p}/number/claptrap/rtsp_old_clap_seuil/config'
    stuck = f'{p}/switch/claptrap/mic_source_active/config'
    ha._discovery_ready.clear()
    mqtt.published.clear()
    ha.sync_sources()                                          # pendant la connexion : rien de publie
    assert not [t for t, *_ in mqtt.published if '/device/' in t]
    _seen(seuil, {'state_topic': 'claptrap/mic/clap/seuil'})
    _seen(conf, {'state_topic': 'claptrap/configuration'})
    _seen(stuck, {'migrate_discovery': True})                  # migration interrompue (redemarrage)
    _seen(old, {'state_topic': 'claptrap/rtsp_old/clap/seuil'})
    _seen(f'{p}/sensor/claptrap/rtsp_old_top3/config',
          {'state_topic': 'claptrap/mic/analyse', 'json_attributes_topic': 'claptrap/mic/analyse'})
    _seen(f'{p}/number/claptrap/autre_seuil/config', {'state_topic': 'zigbee2mqtt/lampe'})
    _connect(monkeypatch)
    wait_until(lambda: (seuil, '', True) in mqtt.published and (stuck, '', True) in mqtt.published)
    pub = mqtt.published
    idx = lambda topic, payload: next(i for i, (t, pl, r) in enumerate(pub) if t == topic and pl == payload)  # noqa: E731
    migrate = json.dumps({'migrate_discovery': True})
    mic_device = next(i for i, (t, pl, r) in enumerate(pub) if t == f'{p}/device/claptrap/mic/config' and pl)
    hub_device = next(i for i, (t, pl, r) in enumerate(pub) if t == f'{p}/device/claptrap/hub/config' and pl)
    assert idx(seuil, migrate) < mic_device < idx(seuil, '')
    wait_until(lambda: (conf, '', True) in mqtt.published)
    assert idx(conf, migrate) < hub_device < idx(conf, '')
    assert (old, migrate, True) not in pub and (old, '', True) in pub   # orpheline : effacee, pas migree
    cleared = {t for t, pl, r in pub if pl == '' and r}
    assert 'claptrap/rtsp_old/clap/seuil' in cleared
    assert not {'claptrap/mic/analyse', 'zigbee2mqtt/lampe', 'claptrap/mic/clap/seuil', 'claptrap/configuration'} & cleared
    assert ha._legacy == {}


def test_late_source_migrated_never_deleted(ctl, mqtt, monkeypatch):
    """Ancienne entite d'une source absente au demarrage (reglages illisibles) :
    pas effacee par le nettoyage, migree quand la source est publiee."""
    monkeypatch.setattr(ha, 'MIGRATE_CLEAR_DELAY', 0.05)
    legacy = 'homeassistant/number/claptrap/rtsp_abcdef12_clap_seuil/config'
    info = ha._source_info.pop('rtsp_abcdef12')
    _seen(legacy, {'state_topic': 'claptrap/rtsp_abcdef12/clap/seuil'})
    ha._source_info['rtsp_abcdef12'] = info
    assert 'homeassistant/number/claptrap/rtsp_abcdef12_clap_seuil/config' not in str(ha.cleanup_orphans())
    assert (legacy, '', True) not in mqtt.published
    ha.republish_all()
    wait_until(lambda: (legacy, '', True) in mqtt.published)
    migrate = [i for i, (t, pl, r) in enumerate(mqtt.published) if t == legacy and pl and 'migrate' in pl]
    device = [i for i, (t, pl, r) in enumerate(mqtt.published) if t.endswith('/device/claptrap/rtsp_abcdef12/config')]
    assert migrate and device and migrate[0] < device[-1]


def test_reconnect_during_wait_leaves_it_to_new_session(ctl, mqtt, monkeypatch):
    ha._discovery_ready.clear()
    import types
    import time as _time
    monkeypatch.setattr(ha, 'time', types.SimpleNamespace(
        sleep=lambda s: setattr(ha, '_session', ha._session + 1), time=_time.time, monotonic=_time.monotonic))
    monkeypatch.setattr(ha, 'DISCOVERY_MAX_WAIT', 0.01)
    mqtt.published.clear()
    ha._after_connect()
    assert not ha._discovery_ready.is_set() and not [t for t, *_ in mqtt.published if '/device/' in t]


def test_source_entity_ids_match_configs(ctl):
    for info in ha._source_info.values():
        assert ha_control.source_entity_ids(info) == {obj for _c, obj, _cfg in ha_control.source_entities(info)}


def test_device_payload_and_component_removal(ctl, mqtt):
    """Un message par appareil : dev, o et cmps (plateforme dans p, ni device
    ni origin par entite) ; un nombre de claps retire est reduit a {"p": …}."""
    dev = json.loads(last(mqtt, 'homeassistant/device/claptrap/mic/config'))
    assert dev['dev']['identifiers'] == ['claptrap_mic'] and dev['dev']['via_device'] == 'claptrap'
    assert dev['o']['name'] == 'ClapTrap'
    c = dev['cmps']['mic_clap_2claps']
    assert c['p'] == 'binary_sensor' and c['uniq_id'] == 'claptrap_mic_clap_2claps'   # cles abregees
    assert c['def_ent_id'] == 'binary_sensor.claptrap_mic_clap_2claps' and 'unique_id' not in c
    assert not {'device', 'origin'} & set(c)
    assert dev['cmps']['mic_siffler_seuil']['p'] == 'number'
    ha.republish_all()
    hub = json.loads(last(mqtt, 'homeassistant/device/claptrap/hub/config'))
    assert hub['dev']['identifiers'] == ['claptrap'] and {'detection', 'detection_active', 'configuration',
                                                          'derniere_commande'} == set(hub['cmps'])
    s = ctl.load_settings()
    s['microphone']['sound_groups'][0]['ha_entities'] = [1]
    ctl.save_settings(s)
    ha.sync_sources()
    dev = json.loads(last(mqtt, 'homeassistant/device/claptrap/mic/config'))
    assert dev['cmps']['mic_clap_2claps'] == {'p': 'binary_sensor'} and 'mic_clap_1clap' in dev['cmps']
    ha.republish_all()                                        # envoi suivant : retrait deja fait
    assert 'mic_clap_2claps' not in json.loads(last(mqtt, 'homeassistant/device/claptrap/mic/config'))['cmps']


def test_large_results_kept_out_of_attributes(ctl, mqtt, monkeypatch):
    sensor = next(cfg for _c, obj, cfg in ha_control.global_entities() if obj == 'derniere_commande')
    assert sensor['json_attributes_topic'] == ha_control.RESULT_ATTRIBUTES_TOPIC
    attributes = lambda: json.loads(last(mqtt, ha_control.RESULT_ATTRIBUTES_TOPIC))  # noqa: E731
    monkeypatch.setattr(ha_control, 'ATTRIBUTES_MAX', 1000)   # reglages de test plus courts que 4 Ko
    command({'id': 'r', 'action': 'get_settings'})
    assert result(mqtt)['data'] and 'data' not in attributes() and attributes()['id'] == 'r'
    assert 'attributs' not in result(mqtt)                    # resultat publie tel quel
    command({'action': 'add_group', 'source': 'Cam', 'name': 'Toc'})
    assert attributes() == result(mqtt)                       # petit resultat : detail garde


def test_command_topic_can_be_disabled(mqtt, monkeypatch, tmp_path):
    subscribed = []
    client = type('C', (), {'subscribe': lambda self, t: subscribed.append(t)})()
    monkeypatch.setattr(ha.threading, 'Thread', lambda *a, **k: type('T', (), {'start': lambda s: None})())
    opts = tmp_path / 'options.json'
    monkeypatch.setattr(ha, 'OPTIONS_FILE', str(opts))
    for value, expected in ((None, True), (True, True), (False, False)):
        opts.write_text(json.dumps({} if value is None else {'mqtt_commands': value}))
        subscribed.clear()
        ha._on_connect(client, None, None, 0)
        assert (ha_control.COMMAND_TOPIC in subscribed) is expected
        assert ha_control.DETECTION_SET_TOPIC in subscribed


def test_stale_entity_command_reports_error(ctl, mqtt):
    ha_control._handle('claptrap/rtsp_disparue/clap/seuil/set', b'40')
    assert result(mqtt)['success'] is False
    ha_control._handle('claptrap/mic/clap/seuil/set', b'abc')
    assert 'refusée' in result(mqtt)['error']


def test_retained_commands_ignored_and_cleared(ctl, mqtt):
    ha_control.handle_message(ha_control.COMMAND_TOPIC, b'{"action": "add_rtsp"}', retained=True)
    assert (ha_control.COMMAND_TOPIC, '', True) in mqtt.published
    assert last(mqtt, ha_control.RESULT_TOPIC) is None   # pas executee


def test_commands_run_in_order(ctl, mqtt):
    ha_control.handle_message(ha_control.COMMAND_TOPIC,
                              json.dumps({'action': 'add_rtsp', 'name': 'Garage', 'url': 'rtsp://h/g'}).encode())
    ha_control.handle_message(ha_control.COMMAND_TOPIC,
                              json.dumps({'id': 'g', 'action': 'add_group', 'source': 'Garage', 'name': 'Toc'}).encode())
    wait_until(lambda: last(mqtt, ha_control.RESULT_TOPIC) and result(mqtt).get('id') == 'g')
    assert result(mqtt)['success']


def test_ambiguous_source_refused(ctl, mqtt):
    command({'action': 'add_vban', 'name': 'Cam', 'ip': '2.2.2.2'})   # meme nom que la camera RTSP
    command({'action': 'delete_source', 'source': 'Cam'})
    r = result(mqtt)
    assert not r['success'] and 'ambiguë' in r['error']
    assert len(ctl.load_settings()['rtsp_sources']) == 1


def test_results_masked(ctl, mqtt):
    command({'action': 'add_rtsp', 'name': 'Garage', 'url': 'rtsp://admin:topsecret@h/g'})
    raw = last(mqtt, ha_control.RESULT_TOPIC)
    assert json.loads(raw)['success'] and 'topsecret' not in raw


def test_deleted_source_clears_retained_states(ctl, mqtt):
    ha_control.publish_states()
    command({'action': 'delete_source', 'source': 'vban:v1'})
    cleared = {t for t, p, r in mqtt.published if p == ''}
    assert {'claptrap/vban_atomsalon/source_active', 'claptrap/vban_atomsalon/clap/seuil',
            'claptrap/vban_atomsalon/analyse'} <= cleared


def test_background_noise_not_republished(ctl, mqtt, monkeypatch):
    monkeypatch.setattr(ha_control, 'ANALYSIS_INTERVAL', 0)
    ha_control.on_analysis('mic', [('Inside, small room', 0.41)], {'clap': 0.02, 'siffler': 0.0})
    n = len(mqtt.published)
    ha_control.on_analysis('mic', [('Inside, small room', 0.43)], {'clap': 0.03, 'siffler': 0.0})
    assert len(mqtt.published) == n   # fluctuation du bruit de fond : rien
    ha_control.on_analysis('mic', [('Inside, small room', 0.43)], {'clap': 0.3, 'siffler': 0.0})
    assert len(mqtt.published) == n + 1   # le score d'un groupe bouge nettement


def test_disabled_commands_ignored_on_reception(ctl, mqtt, monkeypatch):
    monkeypatch.setattr(ha, 'commands_enabled', lambda: False)
    ha_control.handle_message(ha_control.COMMAND_TOPIC, b'{"action": "add_rtsp", "url": "rtsp://h/z"}')
    assert last(mqtt, ha_control.RESULT_TOPIC) is None
    assert 'désactivées' in ha_control._configuration_payload(ctl.load_settings())['details']['commandes']


def test_detection_switch_on_while_running_is_not_an_error(ctl, mqtt, monkeypatch):
    monkeypatch.setattr(ha_control, '_api', lambda m, p, b=None: (409, {'error': 'La détection est déjà en cours'})
                        if p == '/api/detection/start' else (200, {}))
    ha_control._handle(ha_control.DETECTION_SET_TOPIC, b'ON')
    assert last(mqtt, ha_control.RESULT_TOPIC) is None            # pas de « Dernière commande : erreur »


def test_webhook_refused_over_mqtt(ctl, mqtt):
    command({'action': 'update_source', 'source': 'Cam', 'webhook_url': 'http://192.168.1.1/x'})
    r = result(mqtt)
    assert not r['success'] and 'interface' in r['error'] and r['status'] == 403
    assert not ctl.load_settings()['rtsp_sources'][0].get('webhook_url')


def test_unknown_sound_refused_and_case_fixed(ctl, mqtt):
    command({'action': 'set_sound', 'source': 'Cam', 'group': 'Clap', 'label': 'clapping', 'enabled': True})
    assert result(mqtt)['success']
    wl = ctl.load_settings()['rtsp_sources'][0]['sound_groups'][0]['sound_whitelist']
    assert wl.get('Clapping') is True and 'clapping' not in wl     # forme exacte de YAMNet
    command({'action': 'set_sound', 'source': 'Cam', 'group': 'Clap', 'label': 'Claping', 'enabled': True})
    r = result(mqtt)
    assert not r['success'] and 'Clapping' in r['error']           # suggestion
    command({'action': 'set_exclusion', 'label': 'speech', 'excluded': True})
    assert 'Speech' in ctl.load_settings()['global']['sound_exclusions']


def test_entity_commands_not_blocked_by_long_json_command(ctl, mqtt, monkeypatch):
    """Une prise de calibrage (longue) n'empeche plus un interrupteur de HA
    de repondre : deux files, deux fils."""
    import threading
    gate = threading.Event()
    real = ha_control._handle

    def handle(topic, payload):
        if topic == ha_control.COMMAND_TOPIC:
            gate.wait(5)
        real(topic, payload)
    monkeypatch.setattr(ha_control, '_handle', handle)
    ha_control.handle_message(ha_control.COMMAND_TOPIC, b'{"action": "help"}')
    ha_control.handle_message('claptrap/mic/clap/seuil/set', b'42')
    wait_until(lambda: ctl.load_settings()['microphone']['sound_groups'][0]['threshold'] == 0.42)
    gate.set()


def test_get_settings_keeps_full_sound_lists(ctl, mqtt):
    command({'action': 'get_settings'})
    mic = next(s for s in result(mqtt)['data']['overview'] if s['source'] == 'mic')
    assert mic['groups'][0]['sounds'] == ['Clapping']
