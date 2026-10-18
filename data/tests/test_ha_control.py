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
    monkeypatch.setattr(rs, '_resolve_pulse_name', lambda s: 'alsa.usb')
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
    assert 'actions' in conf['details']
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


def test_orphan_settings_entities_cleaned(ctl, mqtt):
    stale = 'homeassistant/number/claptrap/rtsp_old_clap_seuil/config'
    ha._discovered_ctrl.update({stale, 'homeassistant/number/claptrap/mic_clap_seuil/config',
                                'homeassistant/sensor/claptrap/configuration/config'})
    assert ha.cleanup_orphans() == ['rtsp_old_clap_seuil']
    assert (stale, '', True) in mqtt.published


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
