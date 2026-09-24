"""Session de detection avec de fausses sources (MediaPipe simule)."""
import time
import types

import numpy as np
import pytest

from audio_utils import BLOCK_SAMPLES


def det_of(source_id):
    import classify
    s = classify._current()
    return s.detectors.get(source_id) if s else None


class FakeReader:
    name = 'fake'
    last_error = ''

    def __init__(self, clap_blocks=(), n=80):
        self.clap_blocks, self.n, self.runs, self.closed = set(clap_blocks), n, 0, False

    def iter_blocks(self, stop):
        self.runs += 1
        for i in range(self.n):
            if stop.is_set() or self.closed:
                return
            b = np.random.default_rng(i).normal(0, 0.002, BLOCK_SAMPLES).astype(np.float32)
            if self.runs == 1 and i in self.clap_blocks:
                b[10] = 0.5
            time.sleep(0.01)
            yield b

    def close(self):
        self.closed = True


@pytest.fixture
def session_env(settings_dir, fake_classifier, monkeypatch):
    import classify
    import ha_entities
    calls = {'claps': [], 'listening': []}
    monkeypatch.setattr(ha_entities, 'on_clap_detected', lambda *a, **k: calls['claps'].append((a, k)))
    monkeypatch.setattr(ha_entities, 'update_detection_state', lambda *a, **k: None)
    monkeypatch.setattr(ha_entities, 'set_source_listening', lambda key, on: calls['listening'].append((key, on)))
    monkeypatch.setattr(classify, '_http', types.SimpleNamespace(post=lambda *a, **k: None))
    monkeypatch.setattr(classify, 'SEEN_FLUSH_DELAY', 0.2)
    emitted = []
    sock = types.SimpleNamespace(emit=lambda ev, data=None: emitted.append((ev, data)))
    settings = {'global': {'threshold': 0.5, 'delay': 1.5}, 'microphone': {'enabled': False},
                'rtsp_sources': [{'id': 'cam1', 'name': 'Cam', 'url': 'rtsp://u:p@h/x', 'enabled': True, 'gain': 1,
                                  'sound_groups': [{'slug': 'clap', 'name': 'Clap', 'threshold': 0.5,
                                                    'ha_entities': [1, 2], 'sound_whitelist': {'Clapping': True}}]}],
                'saved_vban_sources': []}
    settings_dir.save_settings(settings)
    yield classify, sock, emitted, calls, settings_dir
    classify.stop_detection(timeout=5)


def test_session_counts_emits_and_persists(session_env, monkeypatch):
    classify, sock, emitted, calls, sm = session_env
    reader = FakeReader(clap_blocks=(30, 45))
    monkeypatch.setattr(classify, 'rtsp_source', lambda url: reader)
    ok, _ = classify.start_from_settings(sock)
    assert ok and classify.get_status()['running']
    time.sleep(2.5)
    claps = [d for e, d in emitted if e == 'clap']
    assert [(c['source_id'], c['clap_count'], c['entity_key'], c['source_name']) for c in claps] == \
        [('rtsp_cam1', 2, 'rtsp_cam1', 'Cam')]
    assert calls['claps'] and calls['claps'][0][0][0] == 'rtsp_cam1'
    statuses = [d['status'] for e, d in emitted if e == 'source_status']
    assert statuses[:2] == ['connecting', 'connected']
    assert ('rtsp_cam1', True) in calls['listening']
    assert not any('u:p' in str(d) for e, d in emitted)   # jamais l'URL
    classify.stop_detection()
    assert not classify.get_status()['running'] and reader.closed
    assert ('rtsp_cam1', False) in calls['listening']
    time.sleep(0.3)
    wl = sm.load_settings()['rtsp_sources'][0]['sound_groups'][0]['sound_whitelist']
    assert wl == {'Clapping': True, 'Speech': False}   # son entendu enregistre (par lot)


def test_stop_during_slow_initialisation_closes_detector(session_env, monkeypatch, fake_classifier):
    classify, sock, emitted, calls, sm = session_env
    fake_classifier.init_delay = 1.0
    monkeypatch.setattr(classify, 'rtsp_source', lambda url: FakeReader())
    classify.start_from_settings(sock)
    time.sleep(0.2)
    classify.stop_detection(timeout=0.5)   # l'arret n'attend pas la fin de l'init
    time.sleep(1.5)
    assert fake_classifier.instances and all(c.closed for c in fake_classifier.instances)


def test_settings_changed_during_initialisation_are_applied(session_env, monkeypatch, fake_classifier):
    classify, sock, emitted, calls, sm = session_env
    fake_classifier.init_delay = 0.6
    monkeypatch.setattr(classify, 'rtsp_source', lambda url: FakeReader(n=200))
    classify.start_from_settings(sock)
    time.sleep(0.1)

    def mut(s):
        s['rtsp_sources'][0]['sound_groups'][0]['sound_whitelist']['Knock'] = True
        s['global']['delay'] = 0.4
    sm.modify_settings(mut)
    time.sleep(1.0)
    det = det_of('rtsp_cam1')
    assert det.tracker.groups[0]['whitelist'].get('Knock') is True
    assert det.tracker.window == 0.4


def test_live_gain_by_source_id(session_env, monkeypatch):
    classify, sock, emitted, calls, sm = session_env
    monkeypatch.setattr(classify, 'rtsp_source', lambda url: FakeReader(n=200))
    classify.start_from_settings(sock)
    time.sleep(0.3)
    classify.update_source_gain('rtsp_cam1', 7)
    assert classify.live_gain('rtsp_cam1', 1) == 7
    assert classify.live_gain('rtsp_other', 3) == 3


def test_all_sources_dead_stops_session(session_env, monkeypatch):
    classify, sock, emitted, calls, sm = session_env

    class Boom(FakeReader):
        def iter_blocks(self, stop):
            raise RuntimeError('boom')
    monkeypatch.setattr(classify, 'rtsp_source', lambda url: Boom())
    monkeypatch.setattr(classify, 'run_source', lambda reader, on_block, stop, **k: None)
    classify.start_from_settings(sock)
    time.sleep(1.5)
    assert not classify.get_status()['running']
    assert ('detection_status', {'status': 'stopped'}) in emitted


def test_live_gains_cleared_after_stop(session_env, monkeypatch):
    classify, sock, emitted, calls, sm = session_env
    monkeypatch.setattr(classify, 'rtsp_source', lambda url: FakeReader(n=200))
    classify.start_from_settings(sock)
    time.sleep(0.3)
    classify.update_source_gain('rtsp_cam1', 7)
    classify.stop_detection()
    assert classify.live_gain('rtsp_cam1', 3) == 3


def test_pending_sounds_flushed_before_cleanup(session_env, monkeypatch):
    """Un son en attente d'ecriture groupee ne reapparait pas apres « Vider
    la liste des sons non coches »."""
    classify, sock, emitted, calls, sm = session_env
    from flask import Flask
    import routes.sources as rs
    monkeypatch.setattr(classify, 'SEEN_FLUSH_DELAY', 30)
    classify._queue_sound_seen('rtsp', 'cam1', 'Dog')
    app = Flask(__name__)
    app.register_blueprint(rs.sources_bp)
    r = app.test_client().post('/api/source/sound_whitelist/cleanup',
                               json={'kind': 'rtsp', 'source_key': 'cam1', 'group_slug': 'clap'})
    assert r.status_code == 200
    classify._flush_sound_seen()
    wl = sm.load_settings()['rtsp_sources'][0]['sound_groups'][0]['sound_whitelist']
    assert 'Dog' not in wl


def test_listening_off_while_reconnecting(session_env, monkeypatch):
    classify, sock, emitted, calls, sm = session_env

    class Short(FakeReader):
        def iter_blocks(self, stop):
            self.runs += 1
            for i in range(20 if self.runs == 1 else 0):
                if stop.is_set():
                    return
                time.sleep(0.01)
                yield np.zeros(BLOCK_SAMPLES, np.float32)
    monkeypatch.setattr(classify, 'rtsp_source', lambda url: Short())
    classify.start_from_settings(sock)
    time.sleep(1.5)
    assert ('rtsp_cam1', True) in calls['listening']
    assert calls['listening'][-1] == ('rtsp_cam1', False)   # flux perdu, reconnexion en cours


def two_cameras(sm):
    s = sm.load_settings()
    cam2 = dict(s['rtsp_sources'][0], id='cam2', name='Cam2', url='rtsp://h/2')
    s['rtsp_sources'].append(cam2)
    sm.save_settings(s)


def test_apply_settings_restarts_only_changed_source(session_env, monkeypatch):
    classify, sock, emitted, calls, sm = session_env
    urls = []
    monkeypatch.setattr(classify, 'rtsp_source', lambda url: urls.append(url) or FakeReader(n=400))
    two_cameras(sm)
    classify.start_from_settings(sock)
    time.sleep(0.5)
    det1, det2 = det_of('rtsp_cam1'), det_of('rtsp_cam2')
    assert det1 and det2

    # Changement de groupe seulement : rien n'est relance
    def add_sound(s):
        s['rtsp_sources'][1]['sound_groups'][0]['sound_whitelist']['Knock'] = True
    sm.modify_settings(add_sound)
    assert classify.apply_settings_if_running() is False
    assert det_of('rtsp_cam2') is det2
    assert det2.tracker.groups[0]['whitelist'].get('Knock') is True

    # Adresse de la camera 2 : seule elle est relancee
    sm.modify_settings(lambda s: s['rtsp_sources'][1].update(url='rtsp://h/2b'))
    assert classify.apply_settings_if_running() is True
    time.sleep(0.5)
    assert det_of('rtsp_cam1') is det1 and not det1.tracker is None
    new2 = det_of('rtsp_cam2')
    assert new2 is not None and new2 is not det2 and det2.classifier is None
    assert urls[-1] == 'rtsp://h/2b'

    # Camera 1 desactivee : arretee, la 2 continue
    sm.modify_settings(lambda s: s['rtsp_sources'][0].update(enabled=False))
    classify.apply_settings_if_running()
    assert det_of('rtsp_cam1') is None
    time.sleep(0.5)   # classifieur ferme en arriere-plan (la requete n'attend plus)
    assert det1.classifier is None
    assert classify.get_status()['sources'] == ['rtsp_cam2']
    assert ('rtsp_cam1', False) in calls['listening']

    # Plus aucune source : la session s'arrete d'elle-meme
    sm.modify_settings(lambda s: s['rtsp_sources'][1].update(enabled=False))
    classify.apply_settings_if_running()
    time.sleep(1.2)
    assert not classify.get_status()['running']


def test_no_live_emits_without_clients(session_env, monkeypatch):
    classify, sock, emitted, calls, sm = session_env
    monkeypatch.setattr(classify, 'rtsp_source', lambda url: FakeReader(n=100))
    monkeypatch.setattr(classify, '_clients', 0)
    classify.start_from_settings(sock)
    time.sleep(0.8)
    assert not [e for e, d in emitted if e in ('source_level', 'group_scores', 'labels')]
    classify.client_connected(+1)
    time.sleep(0.5)
    assert [e for e, d in emitted if e == 'source_level']
    classify.client_connected(-1)


def test_restarting_only_source_keeps_session_alive(session_env, monkeypatch):
    """Relance de l'unique source avec un arret lent (auto-volume, init YAMNet) :
    la session survit et l'etat HA reste « en cours » (elle s'arretait pendant
    la fenetre sans source, en 6.51-6.52)."""
    classify, sock, emitted, calls, sm = session_env
    import ha_entities
    states = []
    monkeypatch.setattr(ha_entities, 'update_detection_state', lambda running, *a: states.append(running))
    monkeypatch.setattr(classify, 'rtsp_source', lambda url: FakeReader(n=400))
    classify.start_from_settings(sock)
    time.sleep(0.4)
    slow = classify.DetectionSession._set_listening
    monkeypatch.setattr(classify.DetectionSession, '_set_listening',
                        staticmethod(lambda src, on: (time.sleep(1.0), slow(src, on))))
    sm.modify_settings(lambda s: s['rtsp_sources'][0].update(url='rtsp://h/nouvelle'))
    assert classify.apply_settings_if_running() is True
    time.sleep(1.0)
    assert classify.get_status()['running'] and det_of('rtsp_cam1') is not None
    assert states == [True]   # jamais « arretee » puis « en cours »


def test_removing_last_source_stops_session_once(session_env, monkeypatch):
    classify, sock, emitted, calls, sm = session_env
    import ha_entities
    states = []
    monkeypatch.setattr(ha_entities, 'update_detection_state', lambda running, *a: states.append(running))
    monkeypatch.setattr(classify, 'rtsp_source', lambda url: FakeReader(n=400))
    classify.start_from_settings(sock)
    time.sleep(0.4)
    sm.modify_settings(lambda s: s['rtsp_sources'][0].update(enabled=False))
    assert classify.apply_settings_if_running() is None
    time.sleep(1.2)
    assert not classify.get_status()['running'] and states == [True, False]


def test_sound_seen_not_duplicated_after_live_change(session_env, monkeypatch):
    """Son en attente d'ecriture : une mise a jour en direct ne le rend pas
    « nouveau » une seconde fois."""
    classify, sock, emitted, calls, sm = session_env
    monkeypatch.setattr(classify, 'rtsp_source', lambda url: FakeReader(n=400))
    monkeypatch.setattr(classify, 'SEEN_FLUSH_DELAY', 30)
    classify.start_from_settings(sock)
    time.sleep(0.6)
    before = [d['label'] for e, d in emitted if e == 'sound_seen']
    assert before.count('Speech') == 1
    classify.apply_settings_if_running()
    time.sleep(0.5)
    assert [d['label'] for e, d in emitted if e == 'sound_seen'].count('Speech') == 1


def test_entity_key_change_keeps_listening(session_env, monkeypatch):
    """Cle d'entite changee par un import (capture identique, pas de relance) :
    l'ancienne cle passe hors ligne, la nouvelle en ligne."""
    classify, sock, emitted, calls, sm = session_env
    monkeypatch.setattr(classify, 'get_vban_detector', lambda: object())
    monkeypatch.setattr(classify, 'VbanSource', lambda listener, ip, name, on_idle=None: FakeReader(n=400))
    sm.modify_settings(lambda s: s.update(rtsp_sources=[], saved_vban_sources=[
        {'id': 'v1', 'entity_key': 'vban_salon', 'ip': '1.1.1.1', 'name': 'Salon', 'stream_name': 'S',
         'enabled': True, 'sound_groups': [{'slug': 'clap', 'name': 'Clap', 'ha_entities': [1]}]}]))
    classify.start_from_settings(sock)
    time.sleep(0.5)
    calls['listening'].clear()
    sm.modify_settings(lambda s: s['saved_vban_sources'][0].update(entity_key='vban_bureau'))
    assert classify.apply_settings_if_running() is False   # aucune relance
    assert calls['listening'] == [('vban_salon', False), ('vban_bureau', True)]


def test_apply_settings_during_shutdown_is_ignored(session_env, monkeypatch):
    classify, sock, emitted, calls, sm = session_env
    monkeypatch.setattr(classify, 'rtsp_source', lambda url: FakeReader(n=400))
    classify.start_from_settings(sock)
    time.sleep(0.3)
    session = classify._current()
    session.stop_event.set()
    assert session.apply_settings(sm.load_settings()) is None
    assert session._applying == 0


def test_mic_runner_stopped_during_prepare(session_env, monkeypatch):
    """Micro arrete pendant la resolution PulseAudio : aucun auto-volume ni
    volume applique apres coup."""
    classify, sock, emitted, calls, sm = session_env
    import threading
    stop = threading.Event()
    stop.set()
    session = classify.DetectionSession([], sock)
    started = []
    import auto_volume
    monkeypatch.setattr(auto_volume.auto_volume_mgr, 'start', lambda *a, **k: started.append(a))
    import audio_utils
    monkeypatch.setattr(audio_utils, 'set_pulse_volume', lambda *a: None)
    monkeypatch.setattr(classify, 'mic_source', lambda name: FakeReader())
    sm.modify_settings(lambda s: s['microphone'].update(enabled=True, auto_volume=True, pulse_name='alsa.x'))
    session._prepare_mic(stop)
    assert started == []
