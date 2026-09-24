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
    monkeypatch.setattr(rs, '_resolve_pulse_name', lambda s: 'alsa.usb')
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
    assert r.status_code == 200 and g['ha_entities'] == [] and g['sound_whitelist'] == {'Clapping': False, 'Knock': False}
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

    def fake_send(url, payload, follow_redirects=True):
        sent.update(url=url, payload=payload, follow=follow_redirects)
        return types.SimpleNamespace(status_code=302)
    monkeypatch.setattr(sr, 'send_webhook', fake_send)
    r = client.post('/api/webhook/test', json={'url': 'http://ha:8123/api/webhook/x', 'source': 'mic'})
    assert r.status_code == 502 and 'journal' in r.json['error']   # message unique (pas de sondage du reseau)
    assert sent['follow'] is False and sent['payload']['test'] is True
    assert {'event', 'source_id', 'entity_key', 'source_name', 'clap_count', 'labels'} <= set(sent['payload'])

    def failing(url, payload, follow_redirects=True):
        raise requests.exceptions.ConnectionError('x')
    monkeypatch.setattr(sr, 'send_webhook', failing)
    assert client.post('/api/webhook/test', json={'url': 'http://ha/x'}).status_code == 502
    assert client.post('/api/webhook/test', json={'url': 'ftp://x'}).status_code == 400


def test_detection_routes_errors(client):
    r = client.post('/api/detection/start')
    assert r.status_code == 400 and r.json == {'success': False, 'error': 'Aucune source audio activée'}
    assert client.post('/api/detection/stop').json == {'success': True}
    assert client.get('/status').json == {'running': False, 'source': None}


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
