"""Reglages : validation, migrations, ecriture atomique."""
import json
import threading

import pytest


def write_raw(sm, data):
    json.dump(data, open(sm.SETTINGS_FILE, 'w'))
    sm._cache = None


def test_legacy_fields_migrated_and_stripped(settings_dir):
    sm = settings_dir
    write_raw(sm, {'microphone': {'enabled': True, 'threshold': 0.7, 'ha_entities': [3],
                                  'sound_whitelist': {'Knock': True}},
                   'vban': {'ip': '1'}, 'global': {'peak_reset': 0.3}})
    s = sm.load_settings()
    g = s['microphone']['sound_groups'][0]
    assert (g['threshold'], g['ha_entities'], g['sound_whitelist']) == (0.7, [3], {'Knock': True})
    disk = json.load(open(sm.SETTINGS_FILE))
    assert not {'threshold', 'ha_entities', 'sound_whitelist'} & set(disk['microphone'])
    assert 'vban' not in disk and 'peak_reset' not in disk['global']
    assert disk['microphone']['configured'] is True   # micro deja utilise : garde


def test_vban_ids_entity_keys_and_stream_names(settings_dir):
    sm = settings_dir
    write_raw(sm, {'saved_vban_sources': [{'ip': '1.1.1.1', 'name': 'Stream1'}, {'ip': '2.2.2.2', 'name': 'Stream1'}]})
    vb = sm.load_settings()['saved_vban_sources']
    assert all(v['id'] for v in vb) and vb[0]['id'] != vb[1]['id']
    assert vb[0]['entity_key'] == 'vban_stream1' and vb[1]['entity_key'].startswith('vban_stream1_')
    assert [v['stream_name'] for v in vb] == ['Stream1', 'Stream1']
    assert sm.load_settings()['saved_vban_sources'][0]['id'] == vb[0]['id']   # stable


def test_group_slugs_made_ascii_and_unique(settings_dir):
    sm = settings_dir
    write_raw(sm, {'microphone': {'configured': True, 'sound_groups': [{'slug': 'bébé', 'name': 'Bébé'},
                                                                      {'slug': 'bebe', 'name': 'B'}]}})
    assert [g['slug'] for g in sm.load_settings()['microphone']['sound_groups']] == ['bebe', 'bebe2']


@pytest.mark.parametrize('data, message', [
    ({'global': {'threshold': 'abc'}}, 'global.threshold'),
    ({'global': {'delay': 99}}, 'global.delay'),
    ({'microphone': {'sound_groups': [{'slug': 'a', 'clap_counts': 3}]}}, 'clap_counts'),
    ({'microphone': {'sound_groups': [{'slug': 'a', 'ha_entities': [5]}]}}, 'ha_entities'),
    ({'rtsp_sources': [{'url': 'http://x'}]}, 'rtsp://'),
    ({'saved_vban_sources': [{'ip': 'nope'}]}, 'adresse IP'),
    ({'microphone': {'webhook_url': 'ftp://x'}}, 'webhook_url'),
    ({'saved_vban_sources': [{'ip': '1.1.1.1', 'port': 70000}]}, 'port'),
])
def test_normalize_rejects_invalid(settings_dir, data, message):
    with pytest.raises(ValueError, match=message):
        settings_dir.normalize_settings(data)


def test_normalize_converts_and_dedupes(settings_dir):
    data = {'global': {'threshold': '0.6', 'debug': 'true'},
            'rtsp_sources': [{'id': 'x', 'url': 'cam/1'}, {'id': 'x', 'url': 'RTSP://h/2'}],
            'microphone': {'sound_groups': [{'slug': 'a', 'clap_counts': ['1', 2]}]}}
    settings_dir.normalize_settings(data)
    assert data['global'] == {'threshold': 0.6, 'debug': True}
    assert [s['url'] for s in data['rtsp_sources']] == ['rtsp://cam/1', 'RTSP://h/2']
    assert data['rtsp_sources'][0]['id'] != data['rtsp_sources'][1]['id']
    assert data['microphone']['sound_groups'][0]['ha_entities'] == [1, 2]


def test_modify_settings_really_deletes_keys(settings_dir):
    sm = settings_dir
    sm.save_settings({'microphone': {'configured': True, 'audio_source': 'USB'}})
    sm.modify_settings(lambda s: s['microphone'].pop('audio_source'))
    sm._cache = None
    assert 'audio_source' not in json.load(open(sm.SETTINGS_FILE))['microphone']


def test_import_replaces_sections_present(settings_dir):
    sm = settings_dir
    sm.save_settings({'microphone': {'configured': True, 'device_index': 4}, 'rtsp_sources': [{'id': 'a', 'url': 'rtsp://a'}]})
    sm.save_settings({'microphone': {'configured': True}})
    s = sm.load_settings()
    assert s['microphone']['device_index'] == 0          # section remplacee
    assert [r['id'] for r in s['rtsp_sources']] == ['a']  # section absente : conservee


def test_concurrent_modifications_are_not_lost(settings_dir):
    sm = settings_dir
    sm.save_settings({'global': {'sound_exclusions': []}})

    def add(i):
        sm.modify_settings(lambda s: s['global']['sound_exclusions'].append(f'L{i}'))
    threads = [threading.Thread(target=add, args=(i,)) for i in range(30)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    sm._cache = None
    assert len(sm.load_settings()['global']['sound_exclusions']) == 30


def test_restore_from_backup_when_main_file_corrupted(settings_dir):
    sm = settings_dir
    sm.save_settings({'global': {'threshold': 0.42}})
    sm.save_settings({'global': {'threshold': 0.43}})   # la sauvegarde contient 0.42
    open(sm.SETTINGS_FILE, 'w').write('{corrompu')
    sm._cache = None
    assert sm.load_settings()['global']['threshold'] == 0.42


def test_new_install_hides_mic(settings_dir):
    assert settings_dir.load_settings()['microphone']['configured'] is False


def test_degraded_mode_refuses_background_writes(settings_dir):
    sm = settings_dir
    open(sm.SETTINGS_FILE, 'w').write('{corrompu')
    open(sm.SETTINGS_BACKUP, 'w').write('{aussi')
    sm._cache = None
    sm.load_settings()
    ok, _ = sm.atomic_update(lambda s: s['global'].update(threshold=0.9))
    assert not ok and open(sm.SETTINGS_FILE).read() == '{corrompu'   # rien d'ecrase
    sm.modify_settings(lambda s: s['global'].update(threshold=0.7))  # action de l'utilisateur
    import glob
    assert glob.glob(sm.SETTINGS_FILE + '.corrompu-*')               # fichier illisible conserve
    sm._cache = None
    assert sm.load_settings()['global']['threshold'] == 0.7
