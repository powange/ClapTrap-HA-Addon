"""Routes de l'API via le client de test Flask (sans Home Assistant)."""
import io
import json
import types

import pytest
import requests


@pytest.fixture
def client(settings_dir, monkeypatch):
    from flask import Flask
    import routes.sources as rs
    from routes.detection import detection_bp
    from routes.settings_routes import settings_bp
    monkeypatch.setattr(rs, '_find_pulse_name', lambda s: ('alsa.usb', None))
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024
    for bp in (rs.sources_bp, settings_bp, detection_bp):
        app.register_blueprint(bp)
    return app.test_client()


def test_patch_mic_is_atomic_and_rejects_unknown_fields(client, settings_dir):
    settings_dir.save_settings({'microphone': {'configured': True, 'auto_volume': True}})
    r = client.patch('/api/sources/mic/mic', json={'device': {'index': 5, 'name': 'B'}, 'volume': 80})
    assert r.status_code == 400
    assert settings_dir.load_settings()['microphone'].get('audio_source') != 'B'   # rien d'ecrit
    r = client.patch('/api/sources/mic/mic', json={'threshold': 0.4})
    assert r.status_code == 400 and 'threshold' in r.json['error']


def test_rtsp_add_duplicate_update_delete(client, settings_dir):
    r = client.post('/api/rtsp/stream', json={'name': 'Cam', 'url': 'cam/1'})
    sid = r.json['stream']['id']
    assert r.json['stream']['url'] == 'rtsp://cam/1'
    assert client.post('/api/rtsp/stream', json={'name': 'B', 'url': 'rtsp://cam/1'}).status_code == 409
    assert client.post('/api/rtsp/stream', json={'url': 'file:///etc/passwd'}).status_code == 400
    assert client.patch(f'/api/sources/rtsp/{sid}', json={'gain': 20, 'name': 'Salon'}).status_code == 200
    assert settings_dir.load_settings()['rtsp_sources'][0]['name'] == 'Salon'
    assert client.delete('/api/rtsp/stream/nope').status_code == 404
    assert client.delete(f'/api/rtsp/stream/{sid}').status_code == 200
    assert settings_dir.load_settings()['rtsp_sources'] == []


def test_vban_add_rename_and_delete_by_id(client, settings_dir):
    assert client.post('/api/vban/save', json={'name': 'A', 'ip': '300.1.1.1', 'port': 6980}).status_code == 400
    r = client.post('/api/vban/save', json={'name': 'Bureau', 'ip': '10.0.0.5', 'port': 6980, 'stream_name': 'Stream1'})
    vid = r.json['source']['id']
    assert client.post('/api/vban/save', json={'name': 'X', 'ip': '10.0.0.5', 'port': 6980,
                                               'stream_name': 'Stream1'}).status_code == 400
    assert client.patch(f'/api/sources/vban/{vid}', json={'name': 'PC'}).status_code == 200
    v = settings_dir.load_settings()['saved_vban_sources'][0]
    assert (v['name'], v['stream_name']) == ('PC', 'Stream1')
    assert client.delete(f'/api/vban/{vid}').status_code == 200
    assert client.delete(f'/api/vban/{vid}').status_code == 404


def test_groups_crud(client, settings_dir):
    settings_dir.save_settings({'microphone': {'configured': True, 'sound_groups': [
        {'slug': 'clap', 'name': 'Clap', 'sound_whitelist': {'Clapping': True, 'Knock': False}, 'ha_entities': [1]}]}})
    r = client.post('/api/source/sound_groups', json={'kind': 'mic', 'name': 'Portes', 'ha_entities': []})
    g = [x for x in settings_dir.load_settings()['microphone']['sound_groups'] if x['slug'] == 'portes'][0]
    # Nouveau groupe vide, sans ajout automatique des sons entendus
    assert r.status_code == 200 and g['ha_entities'] == [] and g['sound_whitelist'] == {} and g['auto_add_sounds'] is False
    r = client.put('/api/source/sound_groups', json={'kind': 'mic', 'group_slug': 'portes', 'auto_add_sounds': True})
    assert r.status_code == 200 and r.json['group']['auto_add_sounds'] is True
    assert client.put('/api/source/sound_groups', json={'kind': 'mic', 'group_slug': 'portes', 'auto_add_sounds': 'peut-être'}).status_code == 400
    r = client.put('/api/source/sound_whitelist', json={'kind': 'mic', 'label': 'Clapping', 'enabled': True,
                                                        'group_slug': 'portes'})
    assert r.status_code == 409 and r.json['conflict_group'] == 'clap'
    assert client.delete('/api/source/sound_groups', json={'kind': 'mic', 'group_slug': 'clap'}).status_code == 400  # groupe par defaut protege
    assert client.delete('/api/source/sound_groups', json={'kind': 'mic', 'group_slug': 'portes'}).status_code == 200


def test_import_export_roundtrip_and_shareable_export(client, settings_dir):
    settings_dir.save_settings({'microphone': {'configured': True, 'webhook_url': 'http://ha:8123/api/webhook/SECRET'},
                                'rtsp_sources': [{'id': 'a', 'url': 'rtsp://admin:pw@cam/1'}]})
    full = client.get('/api/settings/export').data
    assert b'SECRET' in full and b'pw@' in full
    share = client.get('/api/settings/export?secrets=0').data
    assert b'SECRET' not in share and b'pw@' not in share
    settings_dir.save_settings({'rtsp_sources': []})
    r = client.post('/api/settings/import', data={'file': (io.BytesIO(full), 'c.json')},
                    content_type='multipart/form-data')
    assert r.status_code == 200 and settings_dir.load_settings()['rtsp_sources'][0]['url'] == 'rtsp://admin:pw@cam/1'
    r = client.post('/api/settings/import', json={'global': {'threshold': 'abc'}})
    assert r.status_code == 400 and r.json['success'] is False
    big = client.post('/api/settings/import', data=b'x' * (3 * 1024 * 1024), content_type='application/json')
    assert big.status_code == 413


def test_advanced_and_debug_validation(client):
    assert client.put('/api/settings/advanced', json={'delay': 2}).status_code == 200
    assert client.put('/api/settings/advanced', json={'delay': 99}).status_code == 400
    assert client.put('/api/settings/advanced', json={'peak_reset': 1}).status_code == 400
    assert client.put('/api/settings/debug', json={}).status_code == 400


def test_webhook_test_payload_and_no_redirect(client, monkeypatch):
    import routes.settings_routes as sr
    sent = {}

    def fake_send(url, payload):
        sent.update(url=url, payload=payload)
        raise requests.exceptions.HTTPError('redirection HTTP 302')   # redirection : echec explicite
    monkeypatch.setattr(sr, 'send_webhook', fake_send)
    r = client.post('/api/webhook/test', json={'url': 'http://ha:8123/api/webhook/x', 'source': 'mic'})
    assert r.status_code == 502 and 'journal' in r.json['error']   # message unique (pas de sondage du reseau)
    assert sent['payload']['test'] is True
    assert {'event', 'source_id', 'entity_key', 'source_name', 'clap_count', 'labels'} <= set(sent['payload'])

    def failing(url, payload):
        raise requests.exceptions.ConnectionError('x')
    monkeypatch.setattr(sr, 'send_webhook', failing)
    assert client.post('/api/webhook/test', json={'url': 'http://ha/x'}).status_code == 502
    assert client.post('/api/webhook/test', json={'url': 'ftp://x'}).status_code == 400


def test_detection_routes_errors(client):
    r = client.post('/api/detection/start')
    assert r.status_code == 400 and r.json == {'success': False, 'error': 'Aucune source audio activée'}
    assert client.post('/api/detection/stop').json == {'success': True}
    st = client.get('/status').json
    assert st['running'] is False and st['source'] is None and set(st['perf']) == {'processeur', 'memoire_mo'}


def test_legacy_routes_are_gone(client):
    for method, url in (('get', '/api/rtsp/streams'), ('put', '/api/microphone/threshold'),
                        ('post', '/api/settings/save'), ('get', '/api/ha/entities'), ('get', '/api/vban/saved-sources')):
        assert getattr(client, method)(url, json={}).status_code in (404, 405)


def test_collision_refused_on_group_update_and_import(client, settings_dir):
    settings_dir.save_settings({'saved_vban_sources': [
        {'id': 'a', 'entity_key': 'vban_salon', 'ip': '10.0.0.1', 'name': 'Salon', 'stream_name': 'S1',
         'sound_groups': [{'slug': 'tele_clap', 'name': 'T', 'ha_entities': [1]}]},
        {'id': 'b', 'entity_key': 'vban_salon_tele', 'ip': '10.0.0.2', 'name': 'Salon Tele', 'stream_name': 'S2',
         'sound_groups': [{'slug': 'clap', 'name': 'C', 'ha_entities': [2]}]}]})
    r = client.put('/api/source/sound_groups', json={'kind': 'vban', 'source_key': 'a', 'group_slug': 'tele_clap',
                                                     'ha_entities': [1, 2]})
    assert r.status_code == 409
    exported = json.loads(client.get('/api/settings/export').data)
    exported['saved_vban_sources'][0]['sound_groups'][0]['ha_entities'] = [1, 2]
    assert client.post('/api/settings/import', json=exported).status_code == 409


def test_shareable_export_refused_at_import(client, settings_dir):
    settings_dir.save_settings({'rtsp_sources': [{'id': 'a', 'url': 'rtsp://admin:pw@cam/1'}]})
    share = json.loads(client.get('/api/settings/export?secrets=0').data)
    r = client.post('/api/settings/import', json=share)
    assert r.status_code == 400 and 'sans secrets' in r.json['error']
    del share['_shareable']
    r = client.post('/api/settings/import', json=share)
    assert r.status_code == 400 and 'masqués' in r.json['error']


def test_import_validates_entity_key_and_vban_duplicates(client):
    r = client.post('/api/settings/import', json={'saved_vban_sources': [{'ip': '1.1.1.1', 'entity_key': ['x']}]})
    assert r.status_code == 400
    r = client.post('/api/settings/import', json={'saved_vban_sources': [
        {'ip': '1.1.1.1', 'stream_name': 'S'}, {'ip': '1.1.1.1', 'stream_name': 'S'}]})
    assert r.status_code == 400 and 'double' in r.json['error']


def test_disabled_source_add_publishes_and_delete_does_not_restart(client, settings_dir, monkeypatch):
    import routes.sources as rs
    calls = []
    monkeypatch.setattr(rs, '_sync_and_apply', lambda: calls.append('restart'))
    monkeypatch.setattr(rs, '_sync_ha_entities', lambda: calls.append('sync'))
    sid = client.post('/api/rtsp/stream', json={'name': 'Cam', 'url': 'rtsp://c/1', 'enabled': False}).json['stream']['id']
    assert calls == ['sync']
    calls.clear()
    client.delete(f'/api/rtsp/stream/{sid}')
    assert calls == ['sync']
    calls.clear()
    client.post('/api/microphone')
    assert calls == ['sync']


def test_cleanup_refused_when_mqtt_down(client):
    import ha_entities
    ha_entities._mqtt_connected.clear()
    assert client.post('/api/ha/cleanup').status_code == 503


def test_route_validation_consistency(client, settings_dir):
    assert client.post('/api/rtsp/stream', json={'name': 'x' * 81, 'url': 'rtsp://c/1'}).status_code == 400
    assert client.post('/api/vban/save', json={'name': 'x' * 81, 'ip': '1.1.1.1', 'port': 6980}).status_code == 400
    settings_dir.save_settings({'microphone': {'configured': True}})
    assert client.patch('/api/sources/mic/mic', json={'volume': 999}).status_code == 400
    assert client.put('/api/source/sound_whitelist', json={'kind': 'mic', 'label': ['x'], 'enabled': True}).status_code == 400


def test_restart_result_reported_in_response(client, settings_dir, monkeypatch):
    import routes.sources as rs
    settings_dir.save_settings({'microphone': {'configured': True, 'enabled': False}})
    monkeypatch.setattr(rs, '_sync_and_apply', lambda: rs._note_restart('ok'))
    r = client.patch('/api/sources/mic/mic', json={'enabled': True})
    assert r.json['restart'] == 'ok'
    monkeypatch.setattr(rs, '_sync_and_apply', lambda: None)
    r = client.patch('/api/sources/mic/mic', json={'enabled': False})
    assert 'restart' not in r.json


def test_http_errors_are_json(client):
    r = client.post('/api/settings/import', data=b'x' * (3 * 1024 * 1024), content_type='application/json')
    assert r.status_code == 413 and r.json['success'] is False
    r = client.get('/api/nexiste/pas')
    assert r.status_code == 404


def test_wizard_mic_single_request(client, settings_dir, monkeypatch):
    import routes.sources as rs
    calls = []
    monkeypatch.setattr(rs, '_sync_and_apply', lambda: calls.append('restart'))
    monkeypatch.setattr(rs, '_sync_ha_entities', lambda: calls.append('sync'))
    r = client.post('/api/microphone', json={'device': {'name': 'USB', 'index': 2, 'pulse_name': 'alsa.usb'}, 'enabled': True})
    assert r.status_code == 200
    mic = settings_dir.load_settings()['microphone']
    assert mic['configured'] and mic['enabled'] and mic['audio_source'] == 'USB'
    assert calls == ['restart']   # une seule application (et synchronisation HA)


def test_whitelist_unknown_group_refused(client, settings_dir):
    settings_dir.save_settings({'microphone': {'configured': True}})
    before = settings_dir.load_settings()['microphone']['sound_groups']
    r = client.put('/api/source/sound_whitelist', json={'kind': 'mic', 'label': 'Clapping', 'enabled': True,
                                                        'group_slug': 'inconnu'})
    assert r.status_code == 404
    assert settings_dir.load_settings()['microphone']['sound_groups'] == before   # aucun groupe cree


def test_rtsp_tcp_option_saved_and_restarts(client, settings_dir, monkeypatch):
    import routes.sources as rs
    calls = []
    monkeypatch.setattr(rs, '_sync_and_apply', lambda: calls.append('restart'))
    sid = client.post('/api/rtsp/stream', json={'name': 'Cam', 'url': 'rtsp://c/1', 'enabled': True}).json['stream']['id']
    calls.clear()
    r = client.patch(f'/api/sources/rtsp/{sid}', json={'rtsp_tcp': True})
    assert r.status_code == 200 and settings_dir.load_settings()['rtsp_sources'][0]['rtsp_tcp'] is True
    assert calls == ['restart']
    assert client.patch(f'/api/sources/rtsp/{sid}', json={'rtsp_tcp': 'peut-être'}).status_code == 400
    from classify import build_sources_from_settings
    assert build_sources_from_settings(settings_dir.load_settings())[0]['rtsp_tcp'] is True


def test_sound_test_routes_reject_non_object_body(client):
    from flask import Flask
    from routes.testing import testing_bp
    app = Flask(__name__)
    app.register_blueprint(testing_bp)
    c = app.test_client()
    for path in ('/api/mic/test/stop', '/api/rtsp/test/stop', '/api/vban/test/stop'):
        assert c.post(path, json=[1, 2]).status_code < 500, path


def test_rtsp_tcp_at_creation_and_sound_test(client, settings_dir, monkeypatch):
    sid = client.post('/api/rtsp/stream', json={'name': 'Cam', 'url': 'rtsp://c/1', 'rtsp_tcp': True}).json['stream']['id']
    assert settings_dir.load_settings()['rtsp_sources'][0]['rtsp_tcp'] is True
    from flask import Flask
    import routes.testing as rt
    seen = []
    monkeypatch.setattr(rt, 'rtsp_source', lambda url, tcp=False: seen.append(tcp) or types.SimpleNamespace())
    monkeypatch.setattr(rt.threading, 'Thread',
                        lambda **kw: types.SimpleNamespace(start=lambda: None, join=lambda timeout=None: None,
                                                           is_alive=lambda: False))
    app = Flask(__name__)
    app.register_blueprint(rt.testing_bp)
    c = app.test_client()
    c.post('/api/rtsp/test/start', json={'id': sid, 'url': 'rtsp://c/1'})               # camera enregistree
    c.post('/api/rtsp/test/start', json={'id': '', 'url': 'rtsp://c/2', 'rtsp_tcp': True})  # assistant
    c.post('/api/rtsp/test/start', json={'id': sid, 'url': 'rtsp://c/1', 'rtsp_tcp': False})
    assert seen == [True, True, False]


def test_deeply_nested_json_is_400(client):
    deep = '[' * 100000 + ']' * 100000
    r = client.post('/api/settings/import', data=deep, content_type='application/json')
    assert r.status_code == 400
    r = client.put('/api/sound_exclusions', data=deep, content_type='application/json')
    assert r.status_code == 400


def test_rtsp_duplicate_refused_on_update_and_case(client, settings_dir):
    client.post('/api/rtsp/stream', json={'name': 'A', 'url': 'rtsp://h/1'})
    assert client.post('/api/rtsp/stream', json={'name': 'B', 'url': 'RTSP://h/1'}).status_code == 409
    b = client.post('/api/rtsp/stream', json={'name': 'B', 'url': 'rtsp://h/2'}).json['stream']['id']
    assert client.patch(f'/api/sources/rtsp/{b}', json={'url': 'rtsp://h/1'}).status_code == 409
    assert client.patch(f'/api/sources/rtsp/{b}', json={'url': 'rtsp://h/2'}).status_code == 200   # elle-meme


def test_non_text_names_refused(client, settings_dir):
    assert client.post('/api/rtsp/stream', json={'name': ['B'], 'url': 'rtsp://h/3'}).status_code == 400
    settings_dir.save_settings({'microphone': {'configured': True}})
    r = client.patch('/api/sources/mic/mic', json={'device': {'name': ['a'], 'pulse_name': 'x'}})
    assert r.status_code == 400


def test_import_drops_unknown_keys_and_bounds(client, settings_dir):
    r = client.post('/api/settings/import', json={
        'junk': 'x' * 1000, 'global': {'threshold': 0.5, 'bidule': 1},
        'microphone': {'configured': True, 'inconnu': 1,
                       'sound_groups': [{'slug': 'clap', 'name': 'Clap', 'truc': 2}]}})
    assert r.status_code == 200
    s = settings_dir.load_settings()
    assert 'junk' not in s and 'bidule' not in s['global'] and 'inconnu' not in s['microphone']
    assert 'truc' not in s['microphone']['sound_groups'][0]
    r = client.post('/api/settings/import', json={'global': {'sound_exclusions': ['x' * 300]}})
    assert r.status_code == 400
    r = client.put('/api/sound_exclusions', json={'label': 'x' * 1_500_000, 'excluded': True})
    assert r.status_code == 400


def test_group_and_source_counts_bounded(client, settings_dir, monkeypatch):
    import routes.sources as rs
    monkeypatch.setattr(rs, 'MAX_GROUPS', 2)
    settings_dir.save_settings({'microphone': {'configured': True}})
    assert client.post('/api/source/sound_groups', json={'kind': 'mic', 'name': 'G2'}).status_code == 200
    assert client.post('/api/source/sound_groups', json={'kind': 'mic', 'name': 'G3'}).status_code == 400


def test_mic_device_and_auto_volume_in_one_request(client, settings_dir, monkeypatch):
    import routes.sources as rs
    monkeypatch.setattr(rs, '_find_pulse_name', lambda s: ('', None))        # ancien micro : aucun
    monkeypatch.setattr(rs, 'get_audio_input_devices', lambda: [{'name': 'USB', 'pulse_name': 'alsa.usb'}])
    settings_dir.save_settings({'microphone': {'configured': True, 'audio_source': 'default'}})
    r = client.patch('/api/sources/mic/mic', json={'device': {'name': 'USB', 'pulse_name': 'alsa.usb'},
                                                   'auto_volume': True})
    assert r.status_code == 200 and settings_dir.load_settings()['microphone']['auto_volume'] is True
    r = client.patch('/api/sources/mic/mic', json={'device': {'name': 'Fantome', 'pulse_name': 'bogus'},
                                                   'auto_volume': True})
    assert r.status_code == 400                                         # micro absent : refuse


def test_collision_message_is_neutral(client, settings_dir):
    settings_dir.save_settings({'saved_vban_sources': [
        {'id': 'a', 'entity_key': 'vban_salon', 'ip': '1.1.1.1', 'name': 'Salon', 'enabled': True,
         'sound_groups': [{'slug': 'clap', 'name': 'Clap', 'ha_entities': [1]}]},
        {'id': 'b', 'entity_key': 'vban_salon_tele', 'ip': '1.1.1.2', 'name': 'Salon Tele', 'enabled': True,
         'sound_groups': [{'slug': 'clap', 'name': 'Clap', 'ha_entities': [1]}]}]})
    r = client.post('/api/source/sound_groups', json={'kind': 'vban', 'source_key': 'a', 'name': 'Tele clap',
                                                      'ha_entities': [1]})
    assert r.status_code == 409 and 'Deux sources produiraient' in r.json['error']


def test_mic_test_rejects_unknown_pulse_name(monkeypatch):
    from flask import Flask
    import routes.testing as rt
    monkeypatch.setattr('audio_utils.get_audio_input_devices', lambda: [{'name': 'USB', 'pulse_name': 'alsa.usb'}])
    app = Flask(__name__)
    app.register_blueprint(rt.testing_bp)
    r = app.test_client().post('/api/mic/test/start', json={'pulse_name': '--help'})
    assert r.status_code == 400


def test_webhook_redirects_not_followed_by_default(monkeypatch):
    import webhook
    seen = {}

    def fake_post(url, json=None, timeout=None, allow_redirects=None):
        seen['allow'] = allow_redirects
        return types.SimpleNamespace(status_code=seen.get('code', 200), headers={'Location': 'https://ha/api/webhook/x'},
                                     raise_for_status=lambda: None)
    monkeypatch.setattr(webhook._session, 'post', fake_post)
    webhook.send_webhook('http://ha:8123/api/webhook/x', {'a': 1})
    assert seen['allow'] is False
    seen['code'] = 301
    with pytest.raises(requests.exceptions.HTTPError):        # redirection : echec explicite, pas silencieux
        webhook.send_webhook('http://ha:8123/api/webhook/x', {'a': 1})


def test_pactl_volume_uses_double_dash(monkeypatch):
    import audio_utils
    calls = []
    monkeypatch.setattr(audio_utils.subprocess, 'run', lambda cmd, **k: calls.append(cmd))
    audio_utils.set_pulse_volume('-x', 50)
    assert calls[0][:3] == ['pactl', 'set-source-volume', '--']


def test_overlong_exclusion_can_be_removed(client, settings_dir):
    settings_dir.modify_settings(lambda st: st['global'].update(sound_exclusions=['y' * 300]))
    r = client.put('/api/sound_exclusions', json={'label': 'y' * 300, 'excluded': False})
    assert r.status_code == 200 and settings_dir.load_settings()['global']['sound_exclusions'] == []


def test_old_backup_exclusions_coerced(client, settings_dir):
    r = client.post('/api/settings/import', json={'global': {'sound_exclusions': ['Speech', 3, '', None]}})
    assert r.status_code == 200 and settings_dir.load_settings()['global']['sound_exclusions'] == ['Speech', '3']


def test_find_pulse_name_never_writes(settings_dir, monkeypatch):
    """Validation et test du son : le micro est cherche sans rien enregistrer ;
    seule la resolution enregistre la correction."""
    import routes.sources as rs
    monkeypatch.setattr(rs, 'get_audio_input_devices',
                        lambda: [{'name': 'Micro USB', 'pulse_name': 'alsa.usb'}])
    written = []
    monkeypatch.setattr(rs, '_persist_pulse_name', written.append)
    settings = {'microphone': {'audio_source': 'Micro USB', 'pulse_name': 'alsa.old'}}
    assert rs._find_pulse_name(settings) == ('alsa.usb', 'alsa.usb') and written == []
    assert rs._resolve_pulse_name(settings) == 'alsa.usb' and written == ['alsa.usb']
    assert rs._find_pulse_name({'microphone': {'audio_source': 'default', 'pulse_name': 'x'}}) == ('', '')
    assert rs._find_pulse_name({'microphone': {'audio_source': 'Micro USB', 'pulse_name': 'alsa.usb'}}) == ('alsa.usb', None)


def test_restart_added_only_to_successful_json(client):
    from flask import Flask, g, jsonify
    import routes.sources as rs
    app = Flask(__name__)
    with app.test_request_context():
        g.restart = 'ok'
        failed = jsonify({'success': False})
        failed.status_code = 400
        assert 'restart' not in rs.add_restart_to_response(failed).get_json() and 'restart' not in g   # consomme
        g.restart = 'ok'
        assert rs.add_restart_to_response(jsonify({'success': True})).get_json() == {'success': True, 'restart': 'ok'}
        assert rs.add_restart_to_response(jsonify([1, 2])).get_json() == [1, 2]   # listes inchangees


def test_auto_volume_saves_corrected_pulse_name_with_change(client, settings_dir, monkeypatch):
    """Nom PulseAudio perime : la correction est enregistree avec l'activation
    du volume automatique (plus pendant la seule validation)."""
    import routes.sources as rs
    monkeypatch.setattr(rs, '_find_pulse_name', lambda s: ('alsa.new', 'alsa.new'))
    settings_dir.save_settings({'microphone': {'configured': True, 'audio_source': 'Micro', 'pulse_name': 'alsa.old'}})
    r = client.patch('/api/sources/mic/mic', json={'auto_volume': True})
    assert r.status_code == 200, r.json
    mic = settings_dir.load_settings()['microphone']
    assert mic['auto_volume'] is True and mic['pulse_name'] == 'alsa.new'


def test_new_source_gets_clap_sounds_immediately(client, settings_dir):
    """Groupe « Clap » coche (applaudissements) des l'ajout, sans attendre le
    prochain chargement du fichier (la carte s'affichait sans groupe)."""
    assert client.post('/api/rtsp/stream', json={'name': 'Cam', 'url': 'rtsp://h/new'}).status_code == 200
    cam = next(s for s in settings_dir.load_settings()['rtsp_sources'] if s['url'] == 'rtsp://h/new')
    assert cam['sound_groups'][0]['slug'] == 'clap'
    assert cam['sound_groups'][0]['sound_whitelist'] == {'Clapping': True, 'Hands': True, 'Applause': True}


def test_readded_microphone_gets_clap_sounds_and_known_sounds_route(client, settings_dir):
    """Micro remis par l'assistant : groupe « Clap » vide complete ; liste
    complete des sons de YAMNet (521, embarquee dans le modele)."""
    settings_dir.save_settings({'microphone': {'configured': False, 'enabled': False}})
    r = client.post('/api/microphone', json={'enabled': True})
    assert r.status_code == 200
    mic = settings_dir.load_settings()['microphone']
    assert mic['sound_groups'][0]['sound_whitelist'] == {'Clapping': True, 'Hands': True, 'Applause': True}
    labels = client.get('/api/sounds/known').json['labels']
    assert len(labels) == 521 and 'Whistling' in labels and 'Clapping' in labels


def test_api_bounds_match_import(client, settings_dir):
    """Ce que l'API accepte, l'import l'accepte aussi (et inversement)."""
    deep = '[' * 100000 + ']' * 100000
    assert client.post('/api/microphone', data=deep, content_type='application/json').status_code == 400
    settings_dir.save_settings({'microphone': {'configured': True}})
    r = client.patch('/api/sources/mic/mic', json={'device': {'name': 'x' * 201, 'pulse_name': 'p'}})
    assert r.status_code == 400
    from settings_manager import MAX_LABELS
    settings_dir.save_settings({'global': {'sound_exclusions': [f'L{i}' for i in range(MAX_LABELS)]}})
    assert client.put('/api/sound_exclusions', json={'label': 'Trop', 'excluded': True}).status_code == 400
    assert client.put('/api/sound_exclusions', json={'label': 'L1', 'excluded': False}).status_code == 200

    def imp(rtsp):
        return client.post('/api/settings/import', json={'rtsp_sources': rtsp})
    assert imp([{'name': 'x' * 81, 'url': 'rtsp://h/1'}]).status_code == 400
    r = imp([{'id': 'a', 'url': 'rtsp://h/1'}, {'id': 'b', 'url': 'RTSP://h/1'}])
    assert r.status_code == 400 and 'double' in r.json['error']
    assert imp([{'id': 'a/b', 'url': 'rtsp://h/1'}, {'id': 'vban_1.2.3.4_6980', 'url': 'rtsp://h/2'}]).status_code == 200
    ids = [s['id'] for s in settings_dir.load_settings()['rtsp_sources']]
    assert '/' not in ids[0] and ids[1] == 'vban_1.2.3.4_6980'


def test_sound_test_gain_validated(client, monkeypatch):
    from flask import Flask
    import routes.testing as rt
    monkeypatch.setattr(rt, 'rtsp_source', lambda url, tcp=False: types.SimpleNamespace())
    monkeypatch.setattr(rt.threading, 'Thread',
                        lambda **kw: types.SimpleNamespace(start=lambda: None, join=lambda timeout=None: None,
                                                           is_alive=lambda: False))
    app = Flask(__name__)
    app.register_blueprint(rt.testing_bp)
    c = app.test_client()
    for gain in ('nan', 'inf', 1e300, -1):
        assert c.post('/api/rtsp/test/start', json={'url': 'rtsp://c/1', 'gain': gain}).status_code == 400
    assert c.post('/api/rtsp/test/start', json={'url': 'rtsp://c/1', 'gain': 0}).status_code == 200


def test_detection_start_errors_and_degraded_status(client, settings_dir, monkeypatch):
    """« Déjà en cours » (409), paramètres invalides (400), démarrage réussi
    annoncé à l'interface, et statut dégradé si les réglages sont illisibles."""
    import classify
    import routes.detection as rd
    emitted = []
    monkeypatch.setattr(rd, '_socketio', types.SimpleNamespace(emit=lambda *a, **k: emitted.append(a)))
    mic = [{'label': 'Micro'}, {'label': 'Cam'}]
    monkeypatch.setattr(classify, 'start_from_settings', lambda sio: (False, mic))
    r = client.post('/api/detection/start')
    assert r.status_code == 409 and 'déjà en cours' in r.json['error']

    def bad(sio):
        raise ValueError('seuil illisible')
    monkeypatch.setattr(classify, 'start_from_settings', bad)
    r = client.post('/api/detection/start')
    assert r.status_code == 400 and 'seuil illisible' in r.json['error']
    monkeypatch.setattr(classify, 'start_from_settings', lambda sio: (True, mic))
    r = client.post('/api/detection/start')
    assert r.json == {'success': True, 'source': 'Micro + Cam'}
    assert emitted[-1] == ('detection_status', {'status': 'running', 'source': 'Micro + Cam'})
    open(settings_dir.SETTINGS_FILE, 'w').write('{corrompu')
    open(settings_dir.SETTINGS_BACKUP, 'w').write('{aussi')
    settings_dir._cache = None
    settings_dir.load_settings()
    assert client.get('/status').json['settings_degraded'] is True


def test_new_group_never_takes_an_id_pending_rename(client, settings_dir):
    settings_dir.save_settings({'microphone': {'configured': True, 'sound_groups': [
        {'slug': 'clap', 'name': 'Clap'},
        {'slug': 'siffler', 'name': 'Tac', 'entity_slug': 'tac', '_renamed_from': 'siffler toc'}]}})
    r = client.post('/api/source/sound_groups', json={'kind': 'mic', 'name': 'Toc'})
    g = r.json['group']
    assert r.status_code == 200 and g['slug'] == 'toc' and g['entity_slug'] == 'toc2'   # unique_id intact


def test_cleanup_route_refused_when_degraded_and_removes_only_orphans(client, settings_dir, mqtt):
    import ha_entities as ha
    settings_dir.save_settings({'microphone': {'configured': True, 'sound_groups': [{'slug': 'clap', 'name': 'Clap'}]}})
    ha.sync_sources(settings_dir.load_settings())
    ha._seen_devices.update({'mic': [], 'rtsp_old': ['claptrap/rtsp_old/analyse'], ha.HUB: []})
    import settings_manager as sm
    sm._degraded = True
    mqtt.published.clear()
    r = client.post('/api/ha/cleanup')
    assert r.status_code == 409 and mqtt.published == []
    sm._degraded = False
    r = client.post('/api/ha/cleanup')
    assert r.status_code == 200 and r.json['entities'] == ['appareil rtsp_old']
    cleared = {t for t, p, _r in mqtt.published if p == ''}
    assert 'homeassistant/device/claptrap/rtsp_old/config' in cleared
    assert 'homeassistant/device/claptrap/mic/config' not in cleared


def test_hot_reload_outcomes(client, settings_dir, monkeypatch):
    """Modification pendant la detection : plus aucune source (arret),
    sources relancees, ou echec signale."""
    import classify
    import routes.sources as rs
    notes = []
    monkeypatch.setattr(rs, '_note_restart', lambda status: notes.append(status))
    monkeypatch.setattr(rs, '_sync_ha_entities', lambda: None)
    monkeypatch.setattr(rs, '_socketio', types.SimpleNamespace(emit=lambda *a, **k: None))
    monkeypatch.setattr(classify, 'apply_settings_if_running', lambda s: None)
    assert rs._sync_and_apply() is None and notes == []                   # detection arretee
    monkeypatch.setattr(classify, 'apply_settings_if_running', lambda s: True)
    monkeypatch.setattr(classify, 'build_sources_from_settings', lambda s: [])
    rs._sync_and_apply()
    monkeypatch.setattr(classify, 'build_sources_from_settings', lambda s: [{'label': 'Micro'}])
    assert rs._sync_and_apply() is True
    def boom(s):
        raise RuntimeError('pactl')
    monkeypatch.setattr(classify, 'apply_settings_if_running', boom)
    assert rs._sync_and_apply() is None
    assert notes == ['stopped', 'ok', 'failed']


def test_group_presets_and_alert_settings(client, settings_dir):
    settings_dir.save_settings({'microphone': {'configured': True, 'sound_groups': [
        {'slug': 'clap', 'name': 'Clap', 'sound_whitelist': {'Clapping': True}, 'ha_entities': [1, 2]}]}})
    presets = client.get('/api/sound_groups/presets').json['presets']
    assert {'claps', 'fumee', 'verre', 'bebe'} <= {p['id'] for p in presets}
    r = client.post('/api/source/sound_groups', json={'kind': 'mic', 'preset': 'claps', 'name': 'Bravo'})
    g = r.json['group']
    assert g['name'] == 'Bravo' and g['sound_whitelist'] == {'Clapping': False, 'Hands': True, 'Applause': True}
    assert r.json['skipped_sounds'] == ['Clapping']                # deja coche dans « Clap »
    r = client.post('/api/source/sound_groups', json={'kind': 'mic', 'preset': 'fumee'})
    g = r.json['group']
    assert g['name'] == 'Détecteur de fumée' and g['count_mode'] == 'alerte' and g['ha_entities'] == []
    assert g['alert_confirm'] == 3 and g['device_class'] == 'smoke'
    assert client.post('/api/source/sound_groups', json={'kind': 'mic', 'preset': 'inconnu'}).status_code == 400
    r = client.put('/api/source/sound_groups', json={'kind': 'mic', 'source_key': None, 'group_slug': g['slug'],
                                                     'alert_confirm': 2, 'device_class': 'safety'})
    assert r.status_code == 200
    assert client.put('/api/source/sound_groups', json={'kind': 'mic', 'source_key': None, 'group_slug': g['slug'],
                                                        'device_class': 'lampe'}).status_code == 400
    ids = client.get('/api/ha/entity-ids').json['mic']
    assert ids[g['slug']] == ['binary_sensor.claptrap_mic_detecteur_de_fumee_alerte']


def test_same_preset_twice_gets_numbered_name(client, settings_dir):
    settings_dir.save_settings({'microphone': {'configured': True, 'sound_groups': [{'slug': 'clap', 'name': 'Clap'}]}})
    first = client.post('/api/source/sound_groups', json={'kind': 'mic', 'preset': 'aboiement'}).json['group']
    second = client.post('/api/source/sound_groups', json={'kind': 'mic', 'preset': 'aboiement'})
    assert second.status_code == 200 and (first['name'], second.json['group']['name']) == ('Aboiement', 'Aboiement 2')


def test_room_settings_route(client, settings_dir):
    r = client.put('/api/settings/rooms', json={'enabled': True, 'window': 1.5})
    assert r.status_code == 200
    g = settings_dir.load_settings()['global']
    assert g['room_arbitration'] is True and g['room_window'] == 1.5
    assert client.put('/api/settings/rooms', json={'window': 9}).status_code == 400
    assert client.put('/api/settings/rooms', json={'autre': 1}).status_code == 400
