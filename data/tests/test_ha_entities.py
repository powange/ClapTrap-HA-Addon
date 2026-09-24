"""Entites Home Assistant (MQTT discovery) avec un faux client paho."""
import json
import re
import threading
import time
import types

import ha_entities as ha

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
    return sorted(json.loads(p)['default_entity_id'] for t, p, r in client.published if t.endswith('/config') and p)


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
    ha._discovered.update({'mic_7_clap_1clap', 'rtsp_old_clap_1clap', 'mic_clap_1clap', 'detection'})
    assert ha.cleanup_orphans() == ['mic_7_clap_1clap', 'rtsp_old_clap_1clap']
    ha._mqtt_connected.clear()
    ha.unregister_source('vban_salon')
    ha._mqtt_connected.set()
    assert ha._pending_removal == set()   # groupe sans entite : rien a retirer
    ha._mqtt_connected.clear()
    ha.unregister_source('mic')
    assert ha._pending_removal == {'mic_clap_1clap', 'mic_clap_2claps'}
    ha._mqtt_connected.set()
    monkeypatch.setattr(ha, 'time', types.SimpleNamespace(sleep=lambda s: None, time=time.time))
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
    time.sleep(0.5)
    assert [p for t, p, r in mqtt.published if t == 'claptrap/mic_clap_1clap/state'] == ['ON', 'OFF', 'ON', 'OFF']
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
