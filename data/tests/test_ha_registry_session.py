"""Client WebSocket du registre de HA (ha_registry._Session) avec un faux
serveur : authentification, appariement des reponses, delais, fermeture."""
import json

import pytest

import ha_registry


class FakeWS:
    def __init__(self, replies):
        self.replies = list(replies)   # dicts renvoyes dans l'ordre (None : delai)
        self.sent, self.closed = [], False

    def send(self, data):
        self.sent.append(json.loads(data))

    def receive(self, timeout=None):
        if not self.replies:
            return None
        r = self.replies.pop(0)
        return None if r is None else json.dumps(r)

    def close(self):
        self.closed = True


def connect_with(monkeypatch, ws=None, error=None):
    import simple_websocket

    def connect(url, **kw):
        if error:
            raise error
        return ws
    monkeypatch.setattr(simple_websocket.Client, 'connect', staticmethod(connect))


def test_auth_and_call_matches_its_own_answer(monkeypatch):
    ws = FakeWS([{'type': 'auth_required'}, {'type': 'auth_ok'},
                 {'id': 99, 'type': 'event'},                         # message intercale
                 {'id': 1, 'type': 'result', 'success': True, 'result': ['ok']}])
    connect_with(monkeypatch, ws)
    s = ha_registry._Session('jeton')
    assert ws.sent[0] == {'type': 'auth', 'access_token': 'jeton'}
    r = s.call({'type': 'config/entity_registry/list'})
    assert r['result'] == ['ok'] and ws.sent[-1]['id'] == 1
    s.close()
    assert ws.closed


def test_refused_auth_closes_socket(monkeypatch):
    ws = FakeWS([{'type': 'auth_required'}, {'type': 'auth_invalid'}])
    connect_with(monkeypatch, ws)
    with pytest.raises(ha_registry.RegistryError, match='authentification'):
        ha_registry._Session('mauvais')
    assert ws.closed


def test_no_answer_is_a_timeout_and_unreachable_is_an_error(monkeypatch):
    ws = FakeWS([{'type': 'auth_required'}, {'type': 'auth_ok'}, None])
    connect_with(monkeypatch, ws)
    s = ha_registry._Session('jeton')
    with pytest.raises(ha_registry._Timeout):
        s.call({'type': 'ping'})
    connect_with(monkeypatch, error=OSError('refusé'))
    with pytest.raises(ha_registry.RegistryError, match='injoignable'):
        ha_registry._Session('jeton')
    ws2 = FakeWS([{'type': 'auth_required'}, {'type': 'auth_ok'}])
    ws2.send = lambda data: (_ for _ in ()).throw(OSError('coupé'))
    connect_with(monkeypatch, ws2)
    with pytest.raises(ha_registry.RegistryError):
        ha_registry._Session('jeton')                    # envoi de l'auth impossible
    assert ws2.closed


def test_call_times_out_when_other_messages_keep_arriving(monkeypatch):
    """Reponse noyee dans d'autres messages : delai respecte (pas 2 x TIMEOUT)."""
    import time
    monkeypatch.setattr(ha_registry, 'TIMEOUT', 0.3)

    class Chatty(FakeWS):
        def receive(self, timeout=None):
            if self.replies:
                return super().receive(timeout)
            time.sleep(min(0.05, timeout or 0.05))
            return json.dumps({'id': 999, 'type': 'event'})
    ws = Chatty([{'type': 'auth_required'}, {'type': 'auth_ok'}])
    connect_with(monkeypatch, ws)
    s = ha_registry._Session('jeton')
    t = time.monotonic()
    with pytest.raises(ha_registry._Timeout):
        s.call({'type': 'ping'})
    assert time.monotonic() - t < 0.5
