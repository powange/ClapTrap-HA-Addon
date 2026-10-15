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
    report = ha_registry.reconcile()
    assert fake.updates == [] and ', 0 conflits, ' in report[0]
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
    """Faux registre de HA (WebSocket) : get, update, liste compacte,
    get_entries, elements lies. `config/entity_registry/list` (registre
    complet : erreur 1009 chez les grosses installations) est interdit."""

    def __init__(self, entries, refuse=(), related=None, drop_at=None):
        self.reg = {e['entity_id']: dict(e) for e in entries}
        self.refuse, self.related = set(refuse), related or {}
        self.updates, self.types, self.drop_at = [], [], drop_at

    def call(self, payload):
        t = payload['type']
        self.types.append(t)
        if self.drop_at is not None and len(self.types) == self.drop_at:
            self.drop_at = None
            raise ha_registry.RegistryError('connexion à Home Assistant perdue (Connection closed: 1009)')
        assert t != 'config/entity_registry/list', 'registre complet demandé'
        if t == 'config/entity_registry/get':
            e = self.reg.get(payload['entity_id'])
            return {'success': True, 'result': e} if e else {'success': False, 'error': {'code': 'not_found'}}
        if t == 'config/entity_registry/update':
            old, new = payload['entity_id'], payload['new_entity_id']
            self.updates.append((old, new))
            if new in self.refuse or new in self.reg:
                return {'success': False, 'error': {'message': 'Entity with this ID is already registered'}}
            self.reg[new] = dict(self.reg.pop(old), entity_id=new)
            return {'success': True}
        if t == 'config/entity_registry/list_for_display':
            return {'success': True, 'result': {'entities': [{'ei': k, 'pl': v.get('platform')} for k, v in self.reg.items()]}}
        if t == 'config/entity_registry/get_entries':
            return {'success': True, 'result': {i: self.reg.get(i) for i in payload['entity_ids']}}
        if t == 'search/related':
            return {'success': True, 'result': self.related.get(payload['item_id'], {})}
        raise AssertionError(t)

    def close(self):
        pass


OLD_IDS = [i.replace('_snap_', '_siffler_') for i in SNAP_IDS]
UIDS = {o: 'claptrap_vban_atomsalon_siffler_' + o.split('_siffler_')[1] for o in OLD_IDS}


def entries_at(ids):
    """Entites de ClapTrap du groupe, sous les ids donnes (meme ordre que OLD_IDS)."""
    return [{'platform': 'mqtt', 'unique_id': UIDS[o], 'entity_id': i} for o, i in zip(OLD_IDS, ids)]


@pytest.fixture
def no_wait(monkeypatch):
    monkeypatch.setattr(ha_registry.time, 'sleep', lambda s: None)


def test_registry_rename_normal_path(ren, settings_dir, monkeypatch, caplog):
    rename(ren, 'Snap')
    entries = entries_at(OLD_IDS)
    entries[2]['entity_id'] = 'number.seuil_sifflet_salon'      # renomme a la main dans HA
    fake = FakeSession(entries)
    monkeypatch.setattr(ha_registry, '_connect', lambda: fake)
    with caplog.at_level(logging.INFO):
        report = ha_registry.reconcile()
    assert set(fake.updates) == {(o, n) for o, n in zip(OLD_IDS, SNAP_IDS) if not o.startswith('number.')}
    assert 'binary_sensor.claptrap_vban_atomsalon_siffler_3claps -> binary_sensor.claptrap_vban_atomsalon_snap_3claps' in caplog.text
    assert report[0] == ('bilan : 6 renommées, 0 déjà à jour, 1 conservées (id personnalisé), '
                         '0 conflits, 0 introuvables, 0 erreurs')
    assert 'number.seuil_sifflet_salon' in fake.reg                 # id choisi dans HA : conserve
    assert '_renamed_from' not in group(settings_dir)


def test_normal_path_never_loads_the_registry(ren, monkeypatch):
    rename(ren, 'Snap')
    fake = FakeSession(entries_at(OLD_IDS))
    monkeypatch.setattr(ha_registry, '_connect', lambda: fake)
    ha_registry.reconcile()
    assert 'config/entity_registry/list' not in fake.types
    assert 'config/entity_registry/list_for_display' not in fake.types   # pas de recours necessaire


def test_already_renamed_by_hand_is_a_no_op(ren, settings_dir, monkeypatch):
    """Cas reel : entites deja renommees a la main dans HA (meme unique_id)."""
    rename(ren, 'Snap')
    fake = FakeSession(entries_at(SNAP_IDS))
    monkeypatch.setattr(ha_registry, '_connect', lambda: fake)
    report = ha_registry.reconcile()
    assert fake.updates == []
    assert report[0].startswith('bilan : 0 renommées, 7 déjà à jour, 0 conservées')
    assert sorted(fake.reg) == sorted(SNAP_IDS)                      # aucun doublon « _2 »
    assert '_renamed_from' not in group(settings_dir)


def test_conflict_with_foreign_entity(ren, monkeypatch, caplog):
    rename(ren, 'Snap')
    entries = entries_at(OLD_IDS)
    entries[0]['entity_id'] = 'binary_sensor.autre_chose'          # 3claps : ni ancien ni nouvel id
    # nouvel id du 3claps et du 4claps deja pris par une autre entite
    entries += [{'platform': 'template', 'unique_id': 'x1', 'entity_id': SNAP_IDS[0]},
                {'platform': 'template', 'unique_id': 'x2', 'entity_id': SNAP_IDS[1]}]
    fake = FakeSession(entries)
    monkeypatch.setattr(ha_registry, '_connect', lambda: fake)
    with caplog.at_level(logging.WARNING):
        report = ha_registry.reconcile()
    assert 'conflits' in report[0] and ', 2 conflits,' in report[0]
    assert fake.reg[SNAP_IDS[0]]['unique_id'] == 'x1' and fake.reg[SNAP_IDS[1]]['unique_id'] == 'x2'   # rien d'ecrase
    assert 'déjà pris par une autre entité' in caplog.text


def test_entities_not_found(ren, settings_dir, monkeypatch):
    rename(ren, 'Snap')
    fake = FakeSession([])
    monkeypatch.setattr(ha_registry, '_connect', lambda: fake)
    report = ha_registry.reconcile()
    assert ', 7 introuvables,' in report[0]
    assert fake.types.count('config/entity_registry/list_for_display') == 1   # recours construit une fois
    assert '_renamed_from' not in group(settings_dir)


def test_connection_drop_resumes(ren, settings_dir, monkeypatch, no_wait, caplog):
    rename(ren, 'Snap')
    fake = FakeSession(entries_at(OLD_IDS), drop_at=4)              # coupure en cours de route
    connects = []
    monkeypatch.setattr(ha_registry, '_connect', lambda: connects.append(1) or fake)
    with caplog.at_level(logging.INFO):
        report = ha_registry.reconcile()
    assert len(connects) == 2 and 'reprise 1/3' in caplog.text
    assert report[0].startswith('bilan : 7 renommées')
    assert sorted(fake.reg) == sorted(SNAP_IDS)


def test_registry_unreachable_keeps_pending(ren, settings_dir, monkeypatch):
    rename(ren, 'Snap')

    def fail():
        raise ha_registry.RegistryError('Home Assistant injoignable')
    monkeypatch.setattr(ha_registry, '_connect', fail)
    assert ha_registry.reconcile() is None
    assert group(settings_dir)['_renamed_from'] == 'siffler'   # retente plus tard


def test_partial_rename_then_second_rename(ren, settings_dir, monkeypatch):
    """Renommage interrompu, puis nouveau renommage : les entites deja passees
    par l'id intermediaire suivent aussi."""
    rename(ren, 'Snap')
    rename(ren, 'Pop')
    assert group(settings_dir)['_renamed_from'] == 'siffler snap'
    entries = entries_at(OLD_IDS)
    entries[0]['entity_id'] = SNAP_IDS[0]                          # deja renomme une fois
    fake = FakeSession(entries)
    monkeypatch.setattr(ha_registry, '_connect', lambda: fake)
    ha_registry.reconcile()
    assert (SNAP_IDS[0], SNAP_IDS[0].replace('_snap_', '_pop_')) in fake.updates
    assert (OLD_IDS[1], OLD_IDS[1].replace('_siffler_', '_pop_')) in fake.updates


def test_renamed_again_during_reconcile_kept_pending(ren, settings_dir, monkeypatch):
    rename(ren, 'Snap')

    class Racing(FakeSession):
        def call(self, payload):
            if not self.types:
                rename(ren, 'Pop')               # renomme pendant l'operation
            return super().call(payload)
    monkeypatch.setattr(ha_registry, '_connect', lambda: Racing([]))
    ha_registry.reconcile()
    assert group(settings_dir)['_renamed_from'] == 'siffler snap'


def test_outside_home_assistant_clears_pending(ren, settings_dir, monkeypatch):
    rename(ren, 'Snap')
    monkeypatch.delenv('SUPERVISOR_TOKEN', raising=False)
    assert ha_registry.reconcile() == []
    assert '_renamed_from' not in group(settings_dir)


def test_connection_persistently_lost_is_retried_later(ren, settings_dir, monkeypatch, no_wait):
    rename(ren, 'Snap')

    class Broken:
        def receive(self, timeout=None):
            raise ConnectionError('closed')

        def send(self, data):
            pass

        def close(self):
            pass

    def broken_session():
        session = ha_registry._Session.__new__(ha_registry._Session)
        session.ws, session._id = Broken(), 0
        return session
    monkeypatch.setattr(ha_registry, '_connect', broken_session)
    assert ha_registry.reconcile() is None
    assert group(settings_dir)['_renamed_from'] == 'siffler'


def test_client_accepts_large_messages():
    assert ha_registry.MAX_MESSAGE is None        # aucune limite cote client


def test_replayed_update_after_drop_counts_as_renamed(ren, monkeypatch):
    """Coupure juste apres une mise a jour appliquee par HA : la requete
    renvoyee echoue (ancien id disparu), mais l'entite est bien renommee."""
    rename(ren, 'Snap')

    class Replayed(FakeSession):
        def call(self, payload):
            if payload['type'] == 'config/entity_registry/update' and payload['entity_id'] in self.reg \
                    and payload['entity_id'] == OLD_IDS[0]:
                super().call(payload)            # appliquee par HA...
                return {'success': False, 'error': {'code': 'not_found'}}   # ...reponse de la reprise
            return super().call(payload)
    fake = Replayed(entries_at(OLD_IDS))
    monkeypatch.setattr(ha_registry, '_connect', lambda: fake)
    report = ha_registry.reconcile()
    assert report[0].startswith('bilan : 7 renommées') and ', 0 erreurs' in report[0]


def test_transient_error_keeps_group_pending(ren, settings_dir, monkeypatch):
    rename(ren, 'Snap')

    class Flaky(FakeSession):
        def call(self, payload):
            if payload['type'] == 'config/entity_registry/update' and payload['entity_id'] == OLD_IDS[1]:
                return {'success': False, 'error': {'message': 'Internal error'}}
            return super().call(payload)
    fake = Flaky(entries_at(OLD_IDS))
    monkeypatch.setattr(ha_registry, '_connect', lambda: fake)
    report = ha_registry.reconcile()
    assert ', 1 erreurs' in report[0] and ha_registry._retry is True
    assert group(settings_dir)['_renamed_from'] == 'siffler'      # retente plus tard


def test_multi_pass_rename(ren, settings_dir, monkeypatch):
    """Renommage refuse a la premiere passe (cible momentanement prise par une
    entite renommee dans la meme operation) : retente a la passe suivante."""
    rename(ren, 'Snap')
    entries = entries_at(OLD_IDS)
    order = []

    class Ordered(FakeSession):
        def call(self, payload):
            if payload['type'] == 'config/entity_registry/update':
                order.append(payload['entity_id'])
                # premiere tentative du 3claps refusee (cible momentanement prise)
                if payload['entity_id'] == OLD_IDS[0] and order.count(OLD_IDS[0]) == 1:
                    self.updates.append((payload['entity_id'], payload['new_entity_id']))
                    return {'success': False, 'error': {'message': 'already registered'}}
            return super().call(payload)
    fake = Ordered(entries)
    monkeypatch.setattr(ha_registry, '_connect', lambda: fake)
    report = ha_registry.reconcile()
    assert order.count(OLD_IDS[0]) == 2 and report[0].startswith('bilan : 7 renommées')


def test_shorter_delay_advances_scheduled_retry(monkeypatch):
    started = []

    class FakeTimer:
        def __init__(self, delay, fn):
            self.delay = delay
            self.cancelled = False

        def start(self):
            started.append(self)

        def cancel(self):
            self.cancelled = True
    monkeypatch.setattr(ha_registry.threading, 'Timer', FakeTimer)
    monkeypatch.setattr(ha_registry, '_timer', None)
    ha_registry.schedule_reconcile(900)
    ha_registry.schedule_reconcile(2)                     # renommage : ne pas attendre 15 min
    ha_registry.schedule_reconcile(60)                    # plus lointain : ignore
    assert [t.delay for t in started] == [900, 2] and started[0].cancelled
    monkeypatch.setattr(ha_registry, '_timer', None)


def test_usage_unknown_group_is_not_reported_empty(ren):
    r = ren.get('/api/source/sound_groups/usage?kind=vban&source_key=v1&group_slug=inconnu')
    assert r.json['available'] is False


AUTOMATION = {'platform': 'automation', 'unique_id': '1700', 'entity_id': 'automation.lumiere_salon',
              'name': None, 'original_name': 'Lumière salon'}
RELATED = {'binary_sensor.claptrap_vban_atomsalon_siffler_3claps': {'automation': ['automation.lumiere_salon'],
                                                                     'device': ['abc']},
           'number.claptrap_vban_atomsalon_siffler_seuil': {'script': ['script.nuit']}}


def test_usage_listed_before_rename(ren, monkeypatch):
    entries = [{'platform': 'mqtt', 'unique_id': 'claptrap_vban_atomsalon_siffler_3claps',
                'entity_id': 'binary_sensor.claptrap_vban_atomsalon_siffler_3claps'},
               {'platform': 'mqtt', 'unique_id': 'claptrap_vban_atomsalon_siffler_seuil',
                'entity_id': 'number.claptrap_vban_atomsalon_siffler_seuil'}, AUTOMATION]
    monkeypatch.setattr(ha_registry, '_connect', lambda: FakeSession(entries, related=RELATED))
    r = ren.get('/api/source/sound_groups/usage?kind=vban&source_key=v1&group_slug=siffler')
    assert r.status_code == 200 and r.json['available'] is True
    assert r.json['items'] == [
        {'id': 'automation.lumiere_salon', 'nom': 'Lumière salon', 'type': 'automatisation',
         'entites': ['binary_sensor.claptrap_vban_atomsalon_siffler_3claps']},
        {'id': 'script.nuit', 'nom': 'script.nuit', 'type': 'script',
         'entites': ['number.claptrap_vban_atomsalon_siffler_seuil']}]


def test_usage_unavailable_outside_ha(ren, monkeypatch):
    monkeypatch.delenv('SUPERVISOR_TOKEN', raising=False)
    r = ren.get('/api/source/sound_groups/usage?kind=vban&source_key=v1&group_slug=siffler')
    assert r.json['available'] is False and r.json['items'] == []


def test_rename_report_lists_items_to_adapt(ren, settings_dir, monkeypatch, caplog):
    rename(ren, 'Snap')
    entries = [{'platform': 'mqtt', 'unique_id': 'claptrap_vban_atomsalon_siffler_3claps',
                'entity_id': 'binary_sensor.claptrap_vban_atomsalon_siffler_3claps'}, AUTOMATION]
    monkeypatch.setattr(ha_registry, '_connect', lambda: FakeSession(entries, related=RELATED))
    with caplog.at_level(logging.WARNING):
        report = ha_registry.reconcile()
    line = next(r for r in report if r.startswith('à adapter'))
    assert '« Lumière salon »' in line and '→ binary_sensor.claptrap_vban_atomsalon_snap_3claps' in line
    assert 'Lumière salon' in caplog.text


def test_hand_renamed_with_other_spelling_kept(ren, monkeypatch):
    """Capteurs de claps renommes a la main en « …_snap_3_claps » : retrouves
    par unique_id et laisses tels quels, le reste deja a jour."""
    rename(ren, 'Snap')
    ids = [i.replace('_3claps', '_3_claps').replace('_4claps', '_4_claps') for i in SNAP_IDS]
    fake = FakeSession(entries_at(ids))
    monkeypatch.setattr(ha_registry, '_connect', lambda: fake)
    report = ha_registry.reconcile()
    assert fake.updates == []
    assert report[0].startswith('bilan : 0 renommées, 5 déjà à jour, 2 conservées (id personnalisé), 0 conflits')
