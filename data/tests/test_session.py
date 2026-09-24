"""Session de detection avec de fausses sources (MediaPipe simule)."""
import time
import types

import numpy as np
import pytest

from audio_utils import BLOCK_SAMPLES


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
    assert ok and classify.is_running()
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
    assert not classify.is_running() and reader.closed
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
    det = classify.get_detector('rtsp_cam1')
    assert det.groups[0]['whitelist'].get('Knock') is True
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
    assert not classify.is_running()
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
