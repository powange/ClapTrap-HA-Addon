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
