"""Renommage d'un groupe : entity_id renommes dans HA (unique_id stables),
reglages conserves, validation, entite text, sensor des sons."""
import json
import logging

import pytest

import ha_control
import ha_entities as ha
import ha_registry


@pytest.fixture
def ren(settings_dir, mqtt, monkeypatch):
    from flask import Flask
    import routes.sources as rs
    from routes.settings_routes import settings_bp
    monkeypatch.setattr(rs, '_resolve_pulse_name', lambda s: 'alsa.usb')
    monkeypatch.setattr(ha_registry, 'schedule_reconcile', lambda *a, **k: None)
    app = Flask(__name__)
    for bp in (rs.sources_bp, settings_bp):
        app.register_blueprint(bp)
    client = app.test_client()

    def call(method, path, body=None):
        r = client.open(path, method=method, json=body)
        return r.status_code, r.get_json(silent=True) or {}
    monkeypatch.setattr(ha_control, '_api', call)
    settings_dir.save_settings({'saved_vban_sources': [{
        'id': 'v1', 'entity_key': 'vban_atomsalon', 'name': 'AtomSalon', 'ip': '1.1.1.1', 'port': 6980,
        'stream_name': 'S', 'enabled': True, 'sound_groups': [
            {'slug': 'clap', 'name': 'Clap', 'threshold': 0.5, 'ha_entities': [1, 2],
             'sound_whitelist': {'Clapping': True}},
            {'slug': 'siffler', 'name': 'Siffler', 'threshold': 0.4, 'ha_entities': [3, 4], 'count_mode': 'tenus',
             'enabled': True, 'sound_whitelist': {'Whistling': True, 'Speech': False}}]}]})
    ha.sync_sources()
    return client


def rename(client, name, slug='siffler'):
    return client.put('/api/source/sound_groups', json={'kind': 'vban', 'source_key': 'v1', 'group_slug': slug,
                                                       'name': name})


def group(settings_dir, slug='siffler'):
    return next(g for g in settings_dir.load_settings()['saved_vban_sources'][0]['sound_groups'] if g['slug'] == slug)


def configs(client):
    return {t: json.loads(p) for t, p, r in client.published if t.endswith('/config') and p}


SNAP_IDS = [
    'binary_sensor.claptrap_vban_atomsalon_snap_3claps',
    'binary_sensor.claptrap_vban_atomsalon_snap_4claps',
    'number.claptrap_vban_atomsalon_snap_seuil',
    'sensor.claptrap_vban_atomsalon_snap_score',
    'sensor.claptrap_vban_atomsalon_snap_sons',
    'switch.claptrap_vban_atomsalon_snap_groupe_actif',
    'text.claptrap_vban_atomsalon_snap_nom',
]


def test_rename_keeps_settings_and_unique_ids(ren, settings_dir, mqtt):
    before = group(settings_dir)
    mqtt.published.clear()
    r = rename(ren, 'Snap')
    assert r.status_code == 200
    g = group(settings_dir)
    assert g['name'] == 'Snap' and g['slug'] == 'siffler'          # slug : unique_id, topics, webhook
    assert g['entity_slug'] == 'snap' and g['_renamed_from'] == 'siffler'
    for key in ('threshold', 'ha_entities', 'count_mode', 'enabled', 'sound_whitelist'):
        assert g[key] == before[key]
    cfg = configs(mqtt)
    mine = {c['default_entity_id']: c['unique_id'] for c in cfg.values() if '_siffler_' in c['unique_id']}
    assert sorted(mine) == SNAP_IDS                                   # entity_id proposes : nouveau nom
    assert mine['binary_sensor.claptrap_vban_atomsalon_snap_3claps'] == 'claptrap_vban_atomsalon_siffler_3claps'
    assert not [t for t, p, r in mqtt.published if p == '' and '/config' in t]   # aucune entite supprimee
    ids = ha.entity_ids_for_settings(settings_dir.load_settings())['vban:v1']['siffler']
    assert ids == SNAP_IDS[:2]


@pytest.mark.parametrize('name, status', [('', 400), ('   ', 400), ('x' * 81, 400), ('Clap', 409), ('CLAP ', 409)])
def test_rename_validation(ren, settings_dir, name, status):
    r = rename(ren, name)
    assert r.status_code == status
    assert group(settings_dir)['name'] == 'Siffler' and 'entity_slug' not in group(settings_dir)


def test_duplicate_name_refused_at_creation(ren):
    r = ren.post('/api/source/sound_groups', json={'kind': 'vban', 'source_key': 'v1', 'name': 'siffler'})
    assert r.status_code == 409


def test_rename_back_before_ha_is_a_no_op(ren, settings_dir, monkeypatch):
    rename(ren, 'Snap')
    rename(ren, 'Siffler')
    g = group(settings_dir)
    assert g['entity_slug'] == 'siffler'
    fake = FakeSession([{'platform': 'mqtt', 'unique_id': 'claptrap_vban_atomsalon_siffler_3claps',
                         'entity_id': 'binary_sensor.claptrap_vban_atomsalon_siffler_3claps'}])
    monkeypatch.setattr(ha_registry, '_connect', lambda: fake)
    assert ha_registry.reconcile() == [] and fake.updates == []
    assert '_renamed_from' not in group(settings_dir)


def test_same_name_changes_nothing(ren, settings_dir, mqtt):
    settings_dir.modify_settings(lambda s: s['saved_vban_sources'][0]['sound_groups'][1].update(name='Sifflet'))
    mqtt.published.clear()
    assert rename(ren, 'Sifflet').status_code == 200   # meme nom (entite text, assistant)
    g = group(settings_dir)
    assert 'entity_slug' not in g and '_renamed_from' not in g


def test_rename_collision_with_another_source(ren, settings_dir):
    settings_dir.modify_settings(lambda s: s['saved_vban_sources'].append({
        'id': 'v2', 'entity_key': 'vban_atomsalon_tele', 'name': 'AtomSalon Tele', 'ip': '2.2.2.2', 'port': 6980,
        'stream_name': 'T', 'enabled': True,
        'sound_groups': [{'slug': 'clap', 'name': 'Clap', 'ha_entities': [3]}]}))
    # « Tele clap » sur AtomSalon viserait binary_sensor.claptrap_vban_atomsalon_tele_clap_3claps
    r = rename(ren, 'Tele clap')
    assert r.status_code == 409 and group(settings_dir)['name'] == 'Siffler'


def test_text_entity_renames_and_restores_on_error(ren, settings_dir, mqtt, caplog):
    ha_control.publish_states()
    assert [p for t, p, r in mqtt.published if t == 'claptrap/vban_atomsalon/siffler/nom'][-1] == 'Siffler'
    ha_control._handle('claptrap/vban_atomsalon/siffler/nom/set', b'Snap')
    assert group(settings_dir)['name'] == 'Snap'
    ha_control.publish_states()
    assert [p for t, p, r in mqtt.published if t == 'claptrap/vban_atomsalon/siffler/nom'][-1] == 'Snap'
    with caplog.at_level(logging.WARNING):
        ha_control._handle('claptrap/vban_atomsalon/siffler/nom/set', b'clap')
    res = json.loads([p for t, p, r in mqtt.published if t == ha_control.RESULT_TOPIC][-1])
    assert res['success'] is False and 'existe déjà' in res['error']
    assert 'refusée' in caplog.text
    ha_control.publish_states()
    assert [p for t, p, r in mqtt.published if t == 'claptrap/vban_atomsalon/siffler/nom'][-1] == 'Snap'


def test_sounds_sensor(ren, mqtt):
    ha_control.publish_states()
    sons = json.loads([p for t, p, r in mqtt.published if t == 'claptrap/vban_atomsalon/siffler/sons'][-1])
    assert sons == {'nombre': 1, 'sons': ['Whistling'], 'non_coches': 1, 'comptage': 'tenus'}
    cfg = configs(mqtt)['homeassistant/sensor/claptrap/vban_atomsalon_siffler_sons/config']
    assert cfg['entity_category'] == 'diagnostic' and cfg['value_template'] == '{{ value_json.nombre }}'


class FakeSession:
    def __init__(self, entries, refuse=()):
        self.entries, self.refuse, self.updates = entries, set(refuse), []

    def call(self, payload):
        if payload['type'] == 'config/entity_registry/list':
            return {'success': True, 'result': self.entries}
        old, new = payload['entity_id'], payload['new_entity_id']
        self.updates.append((old, new))
        if new in self.refuse:
            return {'success': False, 'error': {'message': 'Entity with this ID is already registered'}}
        return {'success': True}

    def close(self):
        pass


def registry_entries():
    olds = [i.replace('_snap_', '_siffler_') for i in SNAP_IDS]
    uids = {i: 'claptrap_vban_atomsalon_siffler_' + i.split('_siffler_')[1] for i in olds}
    entries = [{'platform': 'mqtt', 'unique_id': uids[o], 'entity_id': o} for o in olds]
    # le seuil a ete renomme a la main dans HA : on n'y touche pas
    entries[2]['entity_id'] = 'number.seuil_sifflet_salon'
    return entries


def test_registry_rename_preserves_entities(ren, settings_dir, monkeypatch, caplog):
    rename(ren, 'Snap')
    fake = FakeSession(registry_entries(), refuse={'sensor.claptrap_vban_atomsalon_snap_sons'})
    monkeypatch.setattr(ha_registry, '_connect', lambda: fake)
    with caplog.at_level(logging.INFO):
        report = ha_registry.reconcile()
    olds = [i.replace('_snap_', '_siffler_') for i in SNAP_IDS]
    expected = [(o, n) for o, n in zip(olds, SNAP_IDS) if not o.startswith('number.')]
    assert set(fake.updates) == set(expected)        # le refus est retente a la passe suivante
    assert 'binary_sensor.claptrap_vban_atomsalon_siffler_3claps → binary_sensor.claptrap_vban_atomsalon_snap_3claps' in caplog.text
    assert any('number.seuil_sifflet_salon' in r and 'conservé' in r for r in report)
    assert any('impossible' in r for r in report)
    assert '_renamed_from' not in group(settings_dir)


def test_registry_unreachable_keeps_pending(ren, settings_dir, monkeypatch):
    rename(ren, 'Snap')

    def fail():
        raise ha_registry.RegistryError('Home Assistant injoignable')
    monkeypatch.setattr(ha_registry, '_connect', fail)
    assert ha_registry.reconcile() is None
    assert group(settings_dir)['_renamed_from'] == 'siffler'   # retente plus tard


def test_partial_rename_then_second_rename(ren, settings_dir, monkeypatch):
    """Renommage interrompu (HA coupe), puis nouveau renommage : les entites
    deja passees par l'id intermediaire suivent aussi."""
    rename(ren, 'Snap')
    rename(ren, 'Pop')
    assert group(settings_dir)['_renamed_from'] == 'siffler snap'
    entries = [{'platform': 'mqtt', 'unique_id': 'claptrap_vban_atomsalon_siffler_3claps',
                'entity_id': 'binary_sensor.claptrap_vban_atomsalon_snap_3claps'},       # deja renommee une fois
               {'platform': 'mqtt', 'unique_id': 'claptrap_vban_atomsalon_siffler_4claps',
                'entity_id': 'binary_sensor.claptrap_vban_atomsalon_siffler_4claps'}]
    fake = FakeSession(entries)
    monkeypatch.setattr(ha_registry, '_connect', lambda: fake)
    ha_registry.reconcile()
    assert sorted(fake.updates) == [
        ('binary_sensor.claptrap_vban_atomsalon_siffler_4claps', 'binary_sensor.claptrap_vban_atomsalon_pop_4claps'),
        ('binary_sensor.claptrap_vban_atomsalon_snap_3claps', 'binary_sensor.claptrap_vban_atomsalon_pop_3claps')]


def test_rename_avoids_ids_still_held_by_another_group(ren, settings_dir):
    rename(ren, 'Snap')                       # « Siffler » garde ses ids siffler dans HA (en attente)
    r = ren.put('/api/source/sound_groups', json={'kind': 'vban', 'source_key': 'v1', 'group_slug': 'clap',
                                                   'name': 'Siffler'})
    assert r.status_code == 200 and group(settings_dir, 'clap')['entity_slug'] == 'siffler2'


def test_sequential_renames_resolved_in_several_passes(ren, settings_dir, monkeypatch):
    """Un id libere par un renommage en debloque un autre dans la meme passe."""
    rename(ren, 'Snap')
    entries = [{'platform': 'mqtt', 'unique_id': 'claptrap_vban_atomsalon_siffler_3claps',
                'entity_id': 'binary_sensor.claptrap_vban_atomsalon_siffler_3claps'},
               {'platform': 'mqtt', 'unique_id': 'claptrap_other', 'entity_id': 'binary_sensor.claptrap_vban_atomsalon_snap_3claps'}]
    fake = FakeSession(entries, refuse={'binary_sensor.claptrap_vban_atomsalon_snap_3claps'})
    monkeypatch.setattr(ha_registry, '_connect', lambda: fake)
    report = ha_registry.reconcile()
    assert any('impossible' in r for r in report)            # id pris par une autre entite de HA
    assert '_renamed_from' not in group(settings_dir)


def test_renamed_again_during_reconcile_kept_pending(ren, settings_dir, monkeypatch):
    rename(ren, 'Snap')

    class Racing(FakeSession):
        def call(self, payload):
            if payload['type'] == 'config/entity_registry/list':
                rename(ren, 'Pop')               # renomme pendant la reconciliation
            return super().call(payload)
    monkeypatch.setattr(ha_registry, '_connect', lambda: Racing([]))
    ha_registry.reconcile()
    assert group(settings_dir)['_renamed_from'] == 'siffler snap'


def test_outside_home_assistant_clears_pending(ren, settings_dir, monkeypatch):
    rename(ren, 'Snap')
    monkeypatch.delenv('SUPERVISOR_TOKEN', raising=False)
    assert ha_registry.reconcile() == []
    assert '_renamed_from' not in group(settings_dir)


def test_connection_lost_mid_session_is_retryable(ren, settings_dir, monkeypatch):
    rename(ren, 'Snap')

    class Broken:
        def receive(self, timeout=None):
            raise ConnectionError('closed')

        def send(self, data):
            pass

        def close(self):
            pass
    import types
    session = ha_registry._Session.__new__(ha_registry._Session)
    session.ws, session._id = Broken(), 0
    monkeypatch.setattr(ha_registry, '_connect', lambda: session)
    assert ha_registry.reconcile() is None
    assert group(settings_dir)['_renamed_from'] == 'siffler'
