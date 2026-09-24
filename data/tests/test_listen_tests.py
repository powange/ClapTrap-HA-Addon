"""Tests du son (« Tester le son ») : un seul a la fois, jetons, erreurs."""
import time
import types

import numpy as np
import pytest


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
    monkeypatch.setattr(t, 'rtsp_source', lambda url: readers.append(Fake(1000)) or readers[-1])
    monkeypatch.setattr(t, '_socketio', types.SimpleNamespace(emit=lambda e, d: emitted.append((e, d))))
    app = Flask(__name__)
    app.register_blueprint(t.testing_bp)
    yield t, app.test_client(), readers, emitted
    t._replace(None) if hasattr(t, '_replace') else None


def test_one_test_at_a_time_with_tokens(env):
    t, c, readers, emitted = env
    a = c.post('/api/rtsp/test/start', json={'id': 'A', 'url': 'rtsp://a'}).json['token']
    time.sleep(0.3)
    b = c.post('/api/rtsp/test/start', json={'id': 'B', 'url': 'rtsp://b'}).json['token']
    time.sleep(0.3)
    assert readers[0].closed and not readers[1].closed
    c.post('/api/rtsp/test/stop', json={'token': a})     # ancien jeton : ignore
    assert not readers[1].closed
    c.post('/api/rtsp/test/stop', json={'token': b})
    time.sleep(0.2)
    assert readers[1].closed
    ids = [d.get('id') for e, d in emitted if e == 'rtsp_level']
    assert ids and ids[-1] == 'B'


def test_rtsp_test_refuses_other_protocols(env):
    t, c, readers, emitted = env
    assert c.post('/api/rtsp/test/start', json={'id': 'A', 'url': 'file:///etc/passwd'}).status_code == 400


def test_error_reported_when_stream_ends(env, monkeypatch):
    t, c, readers, emitted = env
    monkeypatch.setattr(t, 'rtsp_source', lambda url: readers.append(Fake(2)) or readers[-1])
    c.post('/api/rtsp/test/start', json={'id': 'A', 'url': 'rtsp://a'})
    time.sleep(0.5)
    errors = [d.get('error') for e, d in emitted if e == 'rtsp_level' and d.get('error')]
    assert errors and 'Unauthorized' in errors[0]


def test_vban_test_requires_known_source(env):
    t, c, readers, emitted = env
    assert c.post('/api/vban/test/start', json={'id': 'nope'}).status_code == 404
