"""Tests du son (« Tester le son ») : un seul a la fois, jetons, erreurs."""
import time
import types

import numpy as np
import pytest
from helpers import wait_until


class Fake:
    def __init__(self, n):
        self.n, self.closed, self.last_error = n, False, 'Unauthorized'
        self.name = 'fake'

    def iter_blocks(self, stop):
        for _ in range(self.n):
            if stop.is_set() or self.closed:
                return
            time.sleep(0.02)
            yield np.full(1600, 0.1, np.float32)

    def close(self):
        self.closed = True


@pytest.fixture
def env(settings_dir, monkeypatch):
    import routes.testing as t
    from flask import Flask
    readers, emitted = [], []
    monkeypatch.setattr(t, 'rtsp_source', lambda url, **kw: readers.append(Fake(1000)) or readers[-1])
    monkeypatch.setattr(t, '_socketio', types.SimpleNamespace(emit=lambda e, d: emitted.append((e, d))))
    app = Flask(__name__)
    app.register_blueprint(t.testing_bp)
    yield t, app.test_client(), readers, emitted
    t._replace(None) if hasattr(t, '_replace') else None


def test_one_test_at_a_time_with_tokens(env):
    t, c, readers, emitted = env
    levels = lambda tid: [d for e, d in emitted if e == 'rtsp_level' and d.get('id') == tid]  # noqa: E731
    a = c.post('/api/rtsp/test/start', json={'id': 'A', 'url': 'rtsp://a'}).json['token']
    wait_until(lambda: levels('A'))                      # A tourne et emet
    b = c.post('/api/rtsp/test/start', json={'id': 'B', 'url': 'rtsp://b'}).json['token']
    wait_until(lambda: len(readers) == 2 and readers[0].closed and levels('B'))
    assert not readers[1].closed
    c.post('/api/rtsp/test/stop', json={'token': a})     # ancien jeton : ignore
    assert not readers[1].closed
    c.post('/api/rtsp/test/stop', json={'token': b})
    wait_until(lambda: readers[1].closed)
    ids = [d.get('id') for e, d in emitted if e == 'rtsp_level']
    assert ids and ids[-1] == 'B'


def test_rtsp_test_refuses_other_protocols(env):
    t, c, readers, emitted = env
    assert c.post('/api/rtsp/test/start', json={'id': 'A', 'url': 'file:///etc/passwd'}).status_code == 400


def test_error_reported_when_stream_ends(env, monkeypatch):
    t, c, readers, emitted = env
    monkeypatch.setattr(t, 'rtsp_source', lambda url, **kw: readers.append(Fake(2)) or readers[-1])
    c.post('/api/rtsp/test/start', json={'id': 'A', 'url': 'rtsp://a'})
    errors = wait_until(lambda: [d.get('error') for e, d in emitted if e == 'rtsp_level' and d.get('error')])
    assert 'Unauthorized' in errors[0]


def test_vban_test_requires_known_source(env):
    t, c, readers, emitted = env
    assert c.post('/api/vban/test/start', json={'id': 'nope'}).status_code == 404


def test_rtsp_test_reports_camera_audio_format(env, settings_dir):
    """Detection arretee : le format de la camera s'affiche pendant le test,
    seulement pour son adresse enregistree (pas une adresse en cours de saisie)."""
    t, c, readers, emitted = env
    settings_dir.save_settings({'rtsp_sources': [{'id': 'A', 'name': 'Cam', 'url': 'rtsp://a', 'enabled': True}]})
    fmt = {'codec': 'pcm_alaw', 'sample_rate': 8000, 'channels': 'mono'}
    c.post('/api/rtsp/test/start', json={'id': 'A', 'url': 'rtsp://autre'})
    wait_until(lambda: readers)
    readers[-1].on_format(fmt)
    assert not [e for e, d in emitted if e == 'source_format']
    c.post('/api/rtsp/test/start', json={'id': 'A', 'url': 'rtsp://a'})
    wait_until(lambda: len(readers) == 2)
    readers[-1].on_format(fmt)
    assert ('source_format', {'source_id': 'rtsp_A', 'format': fmt}) in emitted


def test_rtsp_test_refused_while_detection_listens(env, monkeypatch, settings_dir):
    """Seconde connexion RTSP refusee : certaines cameras ne servent l'audio
    qu'a un client, la detection pouvait le perdre."""
    import classify
    t, c, readers, emitted = env
    settings_dir.save_settings({'rtsp_sources': [{'id': 'A', 'name': 'Cam', 'url': 'rtsp://a', 'enabled': True}]})
    monkeypatch.setattr(classify, 'detector_for', lambda sid: object() if sid == 'rtsp_A' else None)
    r = c.post('/api/rtsp/test/start', json={'id': 'A', 'url': 'rtsp://a'})
    assert r.status_code == 409 and readers == []
    # Adresse en cours de modification (autre flux) : test autorise
    assert c.post('/api/rtsp/test/start', json={'id': 'A', 'url': 'rtsp://a2'}).status_code == 200


def test_vban_test_taps_with_saved_gain_and_clears_on_stop(env, settings_dir, monkeypatch):
    """Test du son VBAN : tap sur l'ecoute partagee (gain enregistre, 0
    respecte), retire a l'arret et a l'expiration."""
    t, c, readers, emitted = env
    settings_dir.save_settings({'saved_vban_sources': [{'id': 'v1', 'name': 'Salon', 'ip': '10.0.0.5', 'port': 6980,
                                                        'stream_name': 'S', 'gain': 0}]})
    import classify
    monkeypatch.setattr(classify, '_live_gains', {})      # gain enregistre, pas celui d'un test precedent
    taps = []
    det = types.SimpleNamespace(set_test_tap=lambda ip, fn, stream_name='': taps.append((ip, fn, stream_name)))
    import vban_manager
    monkeypatch.setattr(vban_manager, 'get_vban_detector', lambda: det)
    r = c.post('/api/vban/test/start', json={'id': 'v1'})
    assert r.status_code == 200 and taps[-1][0] == '10.0.0.5' and taps[-1][2] == 'S'
    taps[-1][1](0.8)                                     # paquet : niveau au gain enregistre (0)
    levels = [d for e, d in emitted if e == 'vban_level']
    assert levels and 'error' not in levels[-1] and levels[-1]['peak'] == 0
    assert c.post('/api/vban/test/stop', json={'token': r.json['token']}).status_code == 200
    wait_until(lambda: taps[-1] == (None, None, ''))     # tap retire
    monkeypatch.setattr(t, 'TEST_MAX_SECONDS', 0.1)
    c.post('/api/vban/test/start', json={'id': 'v1'})
    wait_until(lambda: taps[-1] == (None, None, ''))     # expiration : retire aussi
