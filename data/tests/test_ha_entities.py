"""Entites Home Assistant (MQTT discovery) avec un faux client paho."""
import json
import re
import threading
import time
import types

import ha_entities as ha
from helpers import wait_until

G = lambda slug, name, counts: {'slug': slug, 'name': name, 'ha_entities': counts}  # noqa: E731


def settings(**over):
    s = {'microphone': {'configured': True, 'enabled': True, 'audio_source': 'USB',
                        'sound_groups': [G('clap', 'Clap', [1, 2])]},
         'rtsp_sources': [{'id': 'abcdef123456', 'name': 'Cam', 'enabled': False,
                           'sound_groups': [G('bebe', 'Bébé', [1])]}],
         'saved_vban_sources': [{'id': 'v1', 'entity_key': 'vban_salon', 'ip': '1.1.1.1', 'name': 'Salon',
                                 'enabled': True, 'sound_groups': [G('clap', 'Clap', [])]}]}
    s.update(over)
    return s


def entity_ids(client):
    return sorted(json.loads(p)['default_entity_id'] for t, p, r in client.published
                  if t.endswith('/config') and p and '/binary_sensor/' in t)


def test_publication_ascii_ids_and_empty_list(mqtt):
    s = settings()
    ha.sync_sources(s)
    assert entity_ids(mqtt) == ['binary_sensor.claptrap_mic_clap_1clap', 'binary_sensor.claptrap_mic_clap_2claps',
                                'binary_sensor.claptrap_rtsp_abcdef12_bebe_1clap']
    assert all(re.match(r'^homeassistant/binary_sensor/claptrap/[a-z0-9_]+/config$', t) for t in mqtt.configs())
    # identiques a ceux affiches par l'interface
    shown = sorted(e for groups in ha.entity_ids_for_settings(s).values() for ids in groups.values() for e in ids)
    assert shown == entity_ids(mqtt)


def test_sync_is_idempotent_and_rename_keeps_entities(mqtt):
    s = settings()
    ha.sync_sources(s)
    mqtt.published.clear()
    ha.sync_sources(s)
    assert mqtt.published == []
    s['microphone']['sound_groups'][0]['name'] = 'Applaudissements'
    ha.sync_sources(s)
    assert mqtt.deleted() == [] and len(mqtt.configs()) == 2


def test_removed_count_and_source_deleted(mqtt):
    s = settings()
    ha.sync_sources(s)
    s['microphone']['sound_groups'][0]['ha_entities'] = [1]
    s['rtsp_sources'] = []
    mqtt.published.clear()
    ha.sync_sources(s)
    assert sorted(t.split('/')[3] for t in mqtt.deleted()) == ['mic_clap_2claps', 'rtsp_abcdef12_bebe_1clap']


def test_disabled_mic_published_unavailable_then_removed_when_unconfigured(mqtt):
    s = settings()
    s['microphone']['enabled'] = False
    ha.sync_sources(s)
    assert ('claptrap/mic/availability', 'offline', True) in mqtt.published
    assert 'binary_sensor.claptrap_mic_clap_1clap' in entity_ids(mqtt)
    s['microphone']['configured'] = False
    mqtt.published.clear()
    ha.sync_sources(s)
    assert sorted(t.split('/')[3] for t in mqtt.deleted()) == ['mic_clap_1clap', 'mic_clap_2claps']


def test_availability_follows_listening(mqtt):
    ha.sync_sources(settings())
    assert ('claptrap/mic/availability', 'offline', True) in mqtt.published   # pas encore a l'ecoute
    mqtt.published.clear()
    ha.set_source_listening('mic', True)
    assert mqtt.published == [('claptrap/mic/availability', 'online', True)]
    mqtt.published.clear()
    ha.set_source_listening('mic', True)   # pas de changement : rien
    assert mqtt.published == []


def test_entity_collision_detected_and_skipped(mqtt):
    s = settings(saved_vban_sources=[
        {'id': 'a', 'entity_key': 'vban_salon', 'ip': '1', 'name': 'Salon', 'enabled': True,
         'sound_groups': [G('tele_clap', 'Télé clap', [1])]},
        {'id': 'b', 'entity_key': 'vban_salon_tele', 'ip': '2', 'name': 'Salon Télé', 'enabled': True,
         'sound_groups': [G('clap', 'Clap', [1])]}])
    assert ha.find_entity_collision(s) == 'VBAN: Salon Télé'
    ha.sync_sources(s)
    assert entity_ids(mqtt).count('binary_sensor.claptrap_vban_salon_tele_clap_1clap') == 1


def test_concurrent_syncs_end_with_current_settings(mqtt, settings_dir):
    """Deux modifications rapides : la derniere publication reflete les
    reglages courants (une copie perimee retirait une entite)."""
    s = settings()
    settings_dir.save_settings(s)
    ha.sync_sources()

    def add(count):
        def mut(st):
            counts = st['microphone']['sound_groups'][0]['ha_entities']
            if count not in counts:
                counts.append(count)
        settings_dir.modify_settings(mut)
        ha.sync_sources()
    threads = [threading.Thread(target=add, args=(n,)) for n in (3, 4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(ha._source_info['mic']['groups']['clap']['clap_counts']) == [1, 2, 3, 4]


def test_orphans_and_pending_removal(mqtt, monkeypatch):
    s = settings()
    ha.sync_sources(s)
    ha._seen_devices.update({'rtsp_old': ['claptrap/rtsp_old/analyse'], 'mic': [], ha.HUB: []})
    ha._legacy['homeassistant/binary_sensor/claptrap/mic_7_clap_1clap/config'] = ['claptrap/mic_7_clap_1clap/state']
    assert ha.cleanup_orphans() == ['appareil rtsp_old', 'binary_sensor.claptrap_mic_7_clap_1clap']
    cleared = {t for t, p, r in mqtt.published if p == ''}
    assert {'homeassistant/device/claptrap/rtsp_old/config', 'claptrap/rtsp_old/analyse',
            'homeassistant/binary_sensor/claptrap/mic_7_clap_1clap/config', 'claptrap/mic_7_clap_1clap/state'} <= cleared
    assert 'homeassistant/device/claptrap/mic/config' not in cleared
    ha._mqtt_connected.clear()
    ha.unregister_source('mic')
    assert ha._pending_devices == {'mic'} and 'claptrap/mic_clap_1clap/state' in ha._pending_topics
    ha._mqtt_connected.set()
    monkeypatch.setattr(ha, 'time', types.SimpleNamespace(sleep=lambda s: None, time=time.time,
                                                          monotonic=time.monotonic))
    monkeypatch.setattr(ha, 'DISCOVERY_MAX_WAIT', 0)
    mqtt.published.clear()
    ha._after_connect()
    assert {'mic_clap_1clap', 'mic_clap_2claps'} <= {t.split('/')[3] for t in mqtt.deleted()}


def test_pulse_off_then_on_and_empty_group(mqtt, monkeypatch):
    monkeypatch.setattr(ha, 'CLAP_PULSE_SECONDS', 0.3)
    ha.sync_sources(settings())
    mqtt.published.clear()
    ha.on_clap_detected('mic', 0.9, 1)
    time.sleep(0.1)
    ha.on_clap_detected('mic', 0.9, 1)
    wait_until(lambda: [p for t, p, r in mqtt.published if t == 'claptrap/mic_clap_1clap/state'] == ['ON', 'OFF', 'ON', 'OFF'])
    mqtt.published.clear()
    ha.on_clap_detected('vban_salon', 0.9, 1, group_slug='clap')   # groupe sans entite
    ha.on_clap_detected('mic', 0.9, 0)                              # aucun pic
    assert mqtt.published == []


def test_detection_entity_metadata(mqtt):
    ha._publish_detection()
    cfg = [json.loads(p) for t, p, r in mqtt.published if t.endswith('detection/config')][0]
    assert cfg['device_class'] == 'running' and cfg['entity_category'] == 'diagnostic'


def test_more_than_four_claps_ignored(mqtt):
    s = settings()
    s['microphone']['sound_groups'][0]['ha_entities'] = [1, 2, 3, 4]
    ha.sync_sources(s)
    mqtt.published.clear()
    ha.on_clap_detected('mic', 0.9, 5)
    assert mqtt.published == []


def test_stale_source_never_unpublishes_ids_claimed_by_kept_source(mqtt):
    """Collision creee hors des routes (fichier modifie a la main) : la source
    conservee garde ses entites, meme celles de meme identifiant."""
    s = settings(saved_vban_sources=[
        {'id': 'a', 'entity_key': 'vban_salon', 'ip': '1', 'name': 'Salon', 'enabled': True,
         'sound_groups': [G('tele_clap', 'T', [1])]},
        {'id': 'b', 'entity_key': 'vban_salon_tele', 'ip': '2', 'name': 'Salon Tele', 'enabled': True,
         'sound_groups': [G('clap', 'C', [2])]}])
    ha.sync_sources(s)
    s['saved_vban_sources'][0]['sound_groups'][0]['ha_entities'] = [1, 2]
    mqtt.published.clear()
    ha.sync_sources(s)
    last = [p for t, p, r in mqtt.published if t == 'homeassistant/binary_sensor/claptrap/vban_salon_tele_clap_2claps/config'][-1]
    assert last                                                   # entite de meme identifiant conservee
    assert ('homeassistant/device/claptrap/vban_salon_tele/config', '', True) not in mqtt.published
    kept = json.loads([p for t, p, r in mqtt.published if t == 'homeassistant/device/claptrap/vban_salon_tele/config'][-1])
    shared = {c for c, v in kept['cmps'].items() if len(v) > 1}   # reduit aux entites partagees
    assert 'vban_salon_tele_clap_2claps' in shared and shared <= ha._component_ids(ha._source_info['vban_salon'])
    assert 'vban_salon_tele_source_active' not in shared


def test_degraded_mode_keeps_entities(mqtt, monkeypatch):
    """Reglages illisibles (valeurs par defaut, aucune source) : aucune entite
    supprimee, nettoyage des orphelines suspendu."""
    import settings_manager as sm
    ha.sync_sources(settings())
    monkeypatch.setattr(sm, '_degraded', True)
    mqtt.published.clear()
    ha.sync_sources({'microphone': {}, 'rtsp_sources': [], 'saved_vban_sources': []})
    assert mqtt.deleted() == [] and ha._expected_keys is None
    ha._seen_devices['rtsp_old'] = []
    assert ha.cleanup_orphans() == []


def test_readded_source_purged_from_pending_removal(mqtt):
    """Source supprimee broker injoignable puis recreee : ses entites ne sont
    pas retirees a la reconnexion."""
    s = settings()
    ha.sync_sources(s)
    ha._mqtt_connected.clear()
    ha.unregister_source('mic')
    assert ha._pending_devices
    ha._mqtt_connected.set()
    ha.sync_sources(s)
    assert ha._pending_devices == set() and not ha._pending_topics


def colliding():
    return settings(saved_vban_sources=[
        {'id': 'a', 'entity_key': 'vban_salon', 'ip': '1', 'name': 'Salon', 'enabled': True,
         'sound_groups': [G('tele_clap', 'T', [1])]},
        {'id': 'b', 'entity_key': 'vban_salon_tele', 'ip': '2', 'name': 'Salon Tele', 'enabled': True,
         'sound_groups': [G('clap', 'C', [1])]}])


def test_legacy_collision_not_pulsed(mqtt):
    s = colliding()
    ha.sync_sources(s)
    mqtt.published.clear()
    ha.on_clap_detected('vban_salon_tele', 0.9, 1, 'clap')
    assert mqtt.published == []   # source non publiee : pas les entites de l'autre


def test_only_new_collisions_refused(mqtt):
    s = colliding()
    changed = json.loads(json.dumps(s))
    changed['microphone']['sound_groups'][0]['name'] = 'Autre'
    assert ha.find_entity_collision(changed) == 'VBAN: Salon Tele'
    assert ha.find_entity_collision(changed, before=s) is None   # collision heritee
    clean = settings()
    assert ha.find_entity_collision(s, before=clean) == 'VBAN: Salon Tele'


def test_external_broker_from_addon_options(tmp_path, monkeypatch):
    opts = tmp_path / 'options.json'
    opts.write_text(json.dumps({'mqtt_host': 'nas.local', 'mqtt_username': 'u', 'mqtt_password': 'p',
                                'mqtt_tls': True, 'mqtt_tls_verify': False}))
    monkeypatch.setattr(ha, 'OPTIONS_FILE', str(opts))
    monkeypatch.setattr(ha.requests, 'get', lambda *a, **k: (_ for _ in ()).throw(AssertionError('service lu')))
    info = ha._fetch_broker_info()
    assert info['host'] == 'nas.local' and info['port'] == 8883 and info['ssl'] and info['tls_insecure']
    opts.write_text('{}')                                    # vide : service du Supervisor
    monkeypatch.setattr(ha.requests, 'get', lambda *a, **k: types.SimpleNamespace(
        ok=True, json=lambda: {'data': {'host': 'core-mosquitto', 'port': 1883, 'ssl': True}}))
    assert ha._fetch_broker_info()['host'] == 'core-mosquitto'


def test_mqtt_client_uses_tls_fixed_id_and_qos1_will(monkeypatch, tmp_path):
    created = []

    class FakeClient:
        def __init__(self, *a, client_id=None, **k):
            self.client_id, self.calls = client_id, []
            created.append(self)

        def __getattr__(self, name):
            return lambda *a, **k: self.calls.append((name, a, k))
    import paho.mqtt.client as mqtt_module
    monkeypatch.setattr(mqtt_module, 'Client', FakeClient)
    monkeypatch.setattr(ha, 'OPTIONS_FILE', str(tmp_path / 'options.json'))
    monkeypatch.setattr(ha, '_fetch_broker_info', lambda: {'host': 'h', 'port': 8883, 'ssl': True,
                                                           'tls_insecure': True})
    monkeypatch.setattr(ha, '_mqtt_client', None)
    ha._mqtt_worker()
    c = created[-1]
    names = [n for n, a, k in c.calls]
    assert re.match(r'^claptrap-[0-9a-f]{12}$', c.client_id)
    import ssl
    assert ('tls_set', (), {'cert_reqs': ssl.CERT_NONE}) in c.calls and 'tls_insecure_set' in names
    monkeypatch.setattr(ha, '_mqtt_client', None)
    ha._mqtt_worker()
    assert created[-1].client_id == c.client_id               # meme identifiant pour cette installation
    will = next((a, k) for n, a, k in c.calls if n == 'will_set')
    assert will[1].get('qos') == 1 and will[1].get('retain') is True
    monkeypatch.setattr(ha, '_mqtt_client', None)


def test_retained_publishes_use_qos1(mqtt):
    ha.sync_sources(settings())
    assert all(q == 1 for t, qs in mqtt.qos.items() if t.endswith('/config') for q in qs)
    mqtt.qos.clear()
    ha.on_clap_detected('mic', 0.9, 1)
    assert mqtt.qos['claptrap/mic_clap_1clap/state'] == [0]     # impulsion


def test_stale_offline_is_corrected(mqtt, monkeypatch):
    monkeypatch.setattr(ha, '_shutting_down', False)
    mqtt.published.clear()
    ha._on_message(None, None, types.SimpleNamespace(topic=ha.AVAILABILITY_TOPIC, payload=b'offline', retain=False))
    assert (ha.AVAILABILITY_TOPIC, 'online', True) in mqtt.published   # LWT tardif, en direct
    mqtt.published.clear()
    ha._on_message(None, None, types.SimpleNamespace(topic=ha.AVAILABILITY_TOPIC, payload=b'offline', retain=True))
    assert mqtt.published == []                                   # retenu (arret precedent) : deja remplace
    monkeypatch.setattr(ha, '_shutting_down', True)              # arret propre : voulu
    ha._on_message(None, None, types.SimpleNamespace(topic=ha.AVAILABILITY_TOPIC, payload=b'offline', retain=False))
    assert mqtt.published == []


def test_short_entity_names(mqtt):
    ha.sync_sources(settings())
    cfgs = [json.loads(p) for t, p, r in mqtt.published if t.endswith('/config') and p and '/binary_sensor/' in t]
    shown = {f"{c['device']['name']} {c['name']}" for c in cfgs}       # comme HA : appareil + entite
    assert 'ClapTrap Micro Clap · 2 claps' in shown and 'ClapTrap Cam Bébé · 1 clap' in shown
    mqtt.published.clear()
    ha.sync_sources(settings(rtsp_sources=[{'id': 'abcdef123456', 'name': 'Salon', 'enabled': True,
                                            'sound_groups': [G('clap', 'Clap', [1])]}]))
    devices = {json.loads(p)['device']['name'] for t, p, r in mqtt.published if t.endswith('/config') and p and '/device/' not in t}
    # deux sources « Salon » (RTSP et VBAN) : libelle complet pour les distinguer
    assert {'ClapTrap RTSP: Salon', 'ClapTrap VBAN: Salon'} <= devices
    assert ha.short_name('VBAN: Salon') == 'Salon' and ha.short_name('Micro: alsa_input.usb-x') == 'Micro'


def test_event_entity_and_source_devices(mqtt, monkeypatch):
    monkeypatch.setattr(ha, '_ingress_url', 'homeassistant://hassio/ingress/abc_claptrap')
    ha.sync_sources(settings())
    cfgs = {t: json.loads(p) for t, p, r in mqtt.published if t.endswith('/config') and p and '/device/' not in t}
    ev = cfgs['homeassistant/event/claptrap/mic_clap_evenement/config']
    assert ev['event_types'] == ['1clap', '2claps'] and ev['default_entity_id'] == 'event.claptrap_mic_clap_evenement'
    binary = cfgs['homeassistant/binary_sensor/claptrap/mic_clap_1clap/config']
    assert binary['device']['identifiers'] == ['claptrap_mic'] and binary['device']['via_device'] == 'claptrap'
    assert binary['device']['name'] == 'ClapTrap Micro' and binary['device']['model'] == 'Micro'
    assert binary['device']['configuration_url'] == 'homeassistant://hassio/ingress/abc_claptrap'
    # groupe sans nombre de claps (evenement et webhook seulement) : pas d'entite event
    assert 'homeassistant/event/claptrap/vban_salon_clap_evenement/config' not in cfgs
    ha.republish_all()
    det = next(json.loads(p) for t, p, r in mqtt.published if t.endswith('/detection/config') and p)
    assert det['device']['identifiers'] == ['claptrap']           # entites globales : appareil principal
    mqtt.published.clear()
    ha.on_clap_detected('mic', 0.87, 2, labels=[{'label': 'Clapping', 'score': 0.87}])
    evt = [(t, json.loads(p), r) for t, p, r in mqtt.published if t == 'claptrap/mic/clap/evenement']
    assert evt == [('claptrap/mic/clap/evenement',
                    {'event_type': '2claps', 'score': 0.87, 'sons': [{'classe': 'Clapping', 'score': 0.87}]}, False)]


def test_ingress_url_from_supervisor(monkeypatch):
    monkeypatch.setattr(ha, '_ingress_url', None)
    monkeypatch.setenv('SUPERVISOR_TOKEN', 'x')
    monkeypatch.setattr(ha.requests, 'get', lambda *a, **k: types.SimpleNamespace(
        ok=True, json=lambda: {'data': {'slug': 'a0d7b954_claptrap'}}))
    assert ha._fetch_ingress_url() == 'homeassistant://hassio/ingress/a0d7b954_claptrap'


def test_event_config_removed_when_group_loses_clap_counts(mqtt):
    s = settings()
    ha.sync_sources(s)
    s['microphone']['sound_groups'][0]['ha_entities'] = []
    mqtt.published.clear()
    ha.sync_sources(s)
    assert ('homeassistant/event/claptrap/mic_clap_evenement/config', '', True) in mqtt.published


def test_broker_refusals_logged_rarely_with_explicit_auth_message(monkeypatch, caplog):
    import logging
    monkeypatch.setattr(ha, '_fail_streak_warned', -1e9)
    monkeypatch.setattr(ha, '_auth_failures', 0)
    monkeypatch.setattr(ha.threading, 'Thread', lambda *a, **k: types.SimpleNamespace(start=lambda: None))
    with caplog.at_level(logging.WARNING):
        for _ in range(30):
            ha._connection_failed(None, 'Not authorized')
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and 'identifiants refusés' in warnings[0]


def _retained(topic, payload):
    ha._on_message(None, None, types.SimpleNamespace(
        topic=topic, payload=json.dumps(payload).encode() if payload is not None else b'', retain=True))


def test_retained_device_message_read_through_mqtt(mqtt):
    """Message d'appareil retenu (session precedente) recu par MQTT : base des
    retraits (cles abregees comprises, entites reduites a {p} ignorees), etats
    a nettoyer ; nos echos apres la fin de l'attente ne la modifient plus."""
    topic = 'homeassistant/device/claptrap/rtsp_old/config'
    ha._discovery_ready.clear()
    _retained(topic, {'dev': {}, 'o': {}, 'cmps': {
        'rtsp_old_clap_1clap': {'p': 'binary_sensor', 'uniq_id': 'claptrap_rtsp_old_clap_1clap',
                                'stat_t': 'claptrap/rtsp_old_clap_1clap/state'},
        'rtsp_old_top3': {'p': 'sensor'}}})                  # retrait deja publie : ignore
    assert ha._device_cmps['rtsp_old'] == {'rtsp_old_clap_1clap': 'binary_sensor'}
    assert ha._seen_devices['rtsp_old'] == ['claptrap/rtsp_old_clap_1clap/state']
    ha._discovery_ready.set()
    _retained(topic, {'dev': {}, 'o': {}, 'cmps': {'autre': {'p': 'sensor', 'uniq_id': 'x'}}})
    assert ha._device_cmps['rtsp_old'] == {'rtsp_old_clap_1clap': 'binary_sensor'}   # echo : sans effet
    ha._on_message(None, None, types.SimpleNamespace(topic=topic, payload=b'{pas du json', retain=True))
    _retained(topic, None)
    assert 'rtsp_old' not in ha._seen_devices and 'rtsp_old' not in ha._device_cmps


def test_ha_birth_republishes_and_reconciles(mqtt, monkeypatch):
    calls = []
    monkeypatch.setattr(ha, 'republish_all', lambda: calls.append('republish'))
    import ha_registry
    monkeypatch.setattr(ha_registry, 'schedule_reconcile', lambda delay: calls.append(('reconcile', delay)))
    ha._on_message(None, None, types.SimpleNamespace(topic=ha.HA_STATUS_TOPIC, payload=b'online', retain=False))
    wait_until(lambda: 'republish' in calls)
    assert ('reconcile', 10.0) in calls
    ha._on_message(None, None, types.SimpleNamespace(topic='claptrap/mic/availability', payload=b'online', retain=True))
    assert 'mic' in ha._availability_seen


def test_alert_group_publishes_one_lasting_sensor(mqtt):
    s = settings()
    s['microphone']['sound_groups'].append({'slug': 'fumee', 'name': 'Détecteur de fumée', 'count_mode': 'alerte',
                                            'device_class': 'smoke', 'ha_entities': [1, 2],
                                            'sound_whitelist': {'Smoke detector, smoke alarm': True}})
    ha.sync_sources(s)
    cfgs = {t.split('/')[3]: json.loads(p) for t, p, r in mqtt.published
            if t.endswith('/config') and p and '/device/' not in t}
    alert = cfgs['mic_fumee_alerte']
    assert alert['device_class'] == 'smoke' and alert['default_entity_id'] == 'binary_sensor.claptrap_mic_fumee_alerte'
    assert not any(k.startswith('mic_fumee_') and k.endswith(('clap', 'claps', 'evenement')) for k in cfgs)
    ha.set_alert('mic', 'fumee', True)
    assert [p for t, p, r in mqtt.published if t == 'claptrap/mic_fumee_alerte/state'][-1] == 'ON'
    ha.republish_all()                                        # HA redemarre pendant l'alarme
    assert [p for t, p, r in mqtt.published if t == 'claptrap/mic_fumee_alerte/state'][-1] == 'ON'
    ha.set_alert('mic', 'fumee', False)
    assert [p for t, p, r in mqtt.published if t == 'claptrap/mic_fumee_alerte/state'][-1] == 'OFF'


def test_alert_forgotten_when_group_leaves_alert_mode(mqtt):
    s = settings()
    s['microphone']['sound_groups'].append({'slug': 'fumee', 'name': 'Fumée', 'count_mode': 'alerte'})
    ha.sync_sources(s)
    ha.set_alert('mic', 'fumee', True)
    s['microphone']['sound_groups'][-1]['count_mode'] = 'attaques'
    ha.sync_sources(s)
    ha.set_alert('mic', 'fumee', False)                       # extinction apres le changement de mode
    s['microphone']['sound_groups'][-1]['count_mode'] = 'alerte'
    mqtt.published.clear()
    ha.sync_sources(s)
    assert [p for t, p, r in mqtt.published if t == 'claptrap/mic_fumee_alerte/state'] == ['OFF']
