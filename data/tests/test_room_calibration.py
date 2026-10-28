"""Calibrage de l'arbitrage entre pieces : forces, poids, session."""
import threading
import time

import pytest

import calibration
import ha_control
import room_calibration as rc


def test_solve_lowers_the_source_that_hides_its_noise():
    # Chambre : camera au fond efface, forces enormes partout.
    takes = [{'home': 'salon', 'forces': {'salon': 25.0, 'chambre': 80.0}},
             {'home': 'chambre', 'forces': {'salon': 4.0, 'chambre': 900.0}}]
    weights, margin = rc.solve(takes, ['salon', 'chambre'])
    assert weights['salon'] == 1.0 and 0.05 <= weights['chambre'] < 25 / 80
    assert margin > 1.5   # les deux prises gagnent avec de la marge
    for t in takes:
        assert rc._winner(t, weights) == t['home']


def test_solve_reports_takes_that_cannot_be_separated():
    takes = [{'home': 'a', 'forces': {'a': 5.0, 'b': 10.0}},
             {'home': 'b', 'forces': {'a': 10.0, 'b': 5.0}}]
    weights, margin = rc.solve(takes, ['a', 'b'])
    assert margin <= 1.0 and max(weights.values()) == 1.0


def test_force_of_takes_the_strongest_attack_over_the_floor():
    blocks = [(0.01, 1.0, i * 0.1, 0.01, None) for i in range(50)]
    for i, raw in ((10, 0.3), (20, 0.2), (30, 0.25)):
        blocks[i] = (raw, 1.0, i * 0.1, 0.01, [(raw, i * 0.1, 0.01)])
    assert rc.force_of(blocks) == (25.0, 3)   # mediane des 3 claps
    smooth = [(0.01, 1.0, i * 0.1, 0.01, None) for i in range(50)]
    smooth[5] = (0.05, 1.0, 0.5, 0.04, None)   # montee lente : pas d'attaque nette
    assert rc.force_of(smooth) == (5.0, 0)
    assert rc.force_of([]) == (None, 0)


def test_margin_reflects_clamped_weights():
    # La camera voudrait un poids sous 0,05 (borne) : la marge annoncee est
    # celle des poids appliques, pas celle visee.
    takes = [{'home': 'mic', 'forces': {'mic': 4.0, 'cam': 30.0}},
             {'home': 'cam', 'forces': {'mic': 0.5, 'cam': 2000.0}}]
    weights, margin = rc.solve(takes, ['mic', 'cam'])
    assert weights['cam'] == 0.05 and margin == pytest.approx(4 / 1.5)


class FakeDetector:
    """Detecteur : emet des blocs pendant la prise (fond `floor`, claps
    `claps` aux blocs 10, 20, 30)."""
    def __init__(self, floor, clap):
        self.floor, self.clap = floor, clap
        self._clock_end = 100.0
        self._taps = []

    def add_tap(self, fn, events=None):
        self._taps.append(fn)
        threading.Thread(target=self._play, daemon=True).start()

    def remove_tap(self, fn):
        self._taps.remove(fn)

    def _play(self):
        for i in range(40):
            self._clock_end += 0.1
            raw = self.clap if i in (10, 20, 30) else self.floor
            ons = [(raw, self._clock_end, self.floor)] if raw > self.floor else None
            for fn in list(self._taps):
                fn('block', raw, 1.0, self._clock_end, self.floor, ons)
            time.sleep(0.002)


@pytest.fixture
def rooms(settings_dir, monkeypatch):
    from flask import Flask
    import classify
    import routes.sources as rs
    from routes.calibration import calibration_bp
    from routes.settings_routes import settings_bp
    app = Flask(__name__)
    for bp in (rs.sources_bp, settings_bp, calibration_bp):
        app.register_blueprint(bp)
    client = app.test_client()

    def call(method, path, body=None):
        if path.startswith('/api/detection/'):
            return 200, {'success': True}
        r = client.open(path, method=method, json=body)
        return r.status_code, r.get_json(silent=True) or {}
    monkeypatch.setattr(ha_control, '_api', call)
    monkeypatch.setattr(rc, '_session', None)
    monkeypatch.setattr(rc, 'DURATION', 0.15)
    monkeypatch.setattr(rc, 'GRACE', 0.0)
    monkeypatch.setattr(calibration, 'DETECTOR_WAIT', 0.3)
    monkeypatch.setattr(classify, 'get_status', lambda: {'running': True})
    settings_dir.save_settings({'global': {'room_arbitration': True}, 'saved_vban_sources': [
        {'id': 'salon', 'entity_key': 'vban_salon', 'name': 'Salon', 'ip': '1.1.1.1', 'port': 6980,
         'stream_name': 'S', 'enabled': True},
        {'id': 'chambre', 'entity_key': 'vban_chambre', 'name': 'Chambre', 'ip': '1.1.1.2', 'port': 6980,
         'stream_name': 'C', 'enabled': True}]})
    dets = {}
    monkeypatch.setattr(classify, 'detector_for', lambda sid: dets.get(sid))
    yield client, dets
    rc.stop_quietly()            # meme apres un echec en cours de test : sources rendues
    calibration.stop_quietly()


def test_session_through_the_api(rooms, settings_dir):
    import classify
    client, dets = rooms
    groups = client.get('/api/calibration/rooms').json['groups']
    assert [s['name'] for s in groups[0]['sources']] == ['Salon', 'Chambre'] and groups[0]['zone'] is None
    r = client.post('/api/calibration/rooms/start', json={})
    assert r.status_code == 503   # aucune source n'ecoute
    # Salon : fond normal ; chambre : camera qui efface son bruit.
    dets['vban_salon'] = FakeDetector(0.01, 0.3)
    dets['vban_chambre'] = FakeDetector(0.0005, 0.04)
    r = client.post('/api/calibration/rooms/start', json={})
    assert r.status_code == 200 and {'vban_salon', 'vban_chambre'} <= classify._muted_sources
    t = client.post('/api/calibration/rooms/record', json={'source': 'vban_salon'}).json
    assert t['forces'] == {'Salon': 30.0, 'Chambre': 80.0} and t['loudest'] == 'Chambre'
    assert client.post('/api/calibration/rooms/analyze').status_code == 400   # chambre sans prise
    dets['vban_chambre'].clap, dets['vban_salon'].clap = 0.4, 0.05
    assert client.post('/api/calibration/rooms/record', json={'source': 'vban_chambre'}).status_code == 200
    p = client.post('/api/calibration/rooms/analyze').json
    by = {x['name']: x for x in p['sources']}
    assert by['Salon']['proposed'] == 1.0 and by['Chambre']['proposed'] < 0.375
    assert p['score'] == {'current': 1, 'proposed': 2, 'takes': 2} and p['margin'] > 1.5
    r = client.post('/api/calibration/rooms/apply', json={})
    assert r.status_code == 200 and rc._session is None
    weights = settings_dir.load_settings()['global']['room_weights']
    assert set(weights) == {'vban_chambre'} and weights['vban_chambre'] == by['Chambre']['proposed']
    assert not ({'vban_salon', 'vban_chambre'} & classify._muted_sources)


def test_record_refuses_a_take_without_claps(rooms):
    client, dets = rooms
    dets['vban_salon'] = FakeDetector(0.01, 0.015)   # claps a peine au-dessus du fond
    dets['vban_chambre'] = FakeDetector(0.01, 0.2)
    assert client.post('/api/calibration/rooms/start', json={}).status_code == 200
    r = client.post('/api/calibration/rooms/record', json={'source': 'vban_salon'})
    assert r.status_code == 400 and 'clap(s) net(s) sur « Salon »' in r.json['error']
    assert client.post('/api/calibration/rooms/record', json={'source': 'autre'}).status_code == 400


def test_needs_arbitration_and_two_sources(rooms, settings_dir):
    client, dets = rooms
    settings_dir.modify_settings(lambda s: s['global'].update(room_arbitration=False))
    r = client.post('/api/calibration/rooms/start', json={})
    assert r.status_code == 400 and 'Seule la source la plus proche' in r.json['error']
    settings_dir.modify_settings(lambda s: s['global'].update(
        room_arbitration=True, room_zones=[{'name': 'Haut', 'sources': ['vban_chambre']}]))
    r = client.post('/api/calibration/rooms/start', json={'zone': 'Haut'})
    assert r.status_code == 400 and 'deux sources' in r.json['error']


def test_rooms_route_merges_and_cleans_weights(rooms, settings_dir):
    client, _ = rooms
    r = client.put('/api/settings/rooms', json={'weights': {'vban_chambre': 0.3}})
    assert r.status_code == 200 and r.json['room_weights'] == {'vban_chambre': 0.3}
    client.put('/api/settings/rooms', json={'weights': {'vban_salon': 2}})
    assert settings_dir.load_settings()['global']['room_weights'] == {'vban_chambre': 0.3, 'vban_salon': 2.0}
    r = client.put('/api/settings/rooms', json={'weights': {'rtsp_faute': 0.5}})   # source inconnue : ignoree
    assert r.json['room_weights'] == {'vban_chambre': 0.3, 'vban_salon': 2.0}
    client.put('/api/settings/rooms', json={'weights': {'vban_salon': 1}})   # 1 : sans correction
    assert settings_dir.load_settings()['global']['room_weights'] == {'vban_chambre': 0.3}
    assert client.put('/api/settings/rooms', json={'weights': {'vban_salon': 0}}).status_code == 400
    # Source supprimee : sa correction disparait
    settings_dir.modify_settings(lambda s: s.update(saved_vban_sources=s['saved_vban_sources'][:1]))
    assert settings_dir.load_settings()['global']['room_weights'] == {}


def test_idle_sessions_end_and_release_their_sources(rooms, monkeypatch):
    """Socle commun : une session inactive depuis IDLE_TIMEOUT se termine
    a la requete suivante, sources rendues (arbitrage comme source)."""
    import classify
    client, dets = rooms
    dets['vban_salon'] = FakeDetector(0.01, 0.3)
    dets['vban_chambre'] = FakeDetector(0.01, 0.3)
    assert client.post('/api/calibration/rooms/start', json={}).status_code == 200
    rc._session.last_activity -= calibration.IDLE_TIMEOUT + 1
    r = client.post('/api/calibration/rooms/analyze')
    assert r.status_code == 409 and 'aucun calibrage' in r.json['error']
    assert rc._session is None and not ({'vban_salon', 'vban_chambre'} & classify._muted_sources)
    # Calibrage d'une source : meme socle
    assert client.post('/api/calibration/start', json={'source': 'Salon'}).status_code == 200
    assert 'vban_salon' in classify._muted_sources
    calibration._session.last_activity -= calibration.IDLE_TIMEOUT + 1
    assert client.post('/api/calibration/analyze').status_code == 409
    assert calibration._session is None and 'vban_salon' not in classify._muted_sources


def test_one_calibration_at_a_time(rooms):
    """Commencer l'un arrete l'autre : sourdines rendues, pas de melange."""
    import classify
    client, dets = rooms
    dets['vban_salon'] = FakeDetector(0.01, 0.3)
    dets['vban_chambre'] = FakeDetector(0.01, 0.3)
    assert client.post('/api/calibration/start', json={'source': 'Chambre'}).status_code == 200
    assert client.post('/api/calibration/rooms/start', json={}).status_code == 200
    assert calibration._session is None and rc._session is not None
    assert client.post('/api/calibration/start', json={'source': 'Salon'}).status_code == 200
    assert rc._session is None and classify._muted_sources & {'vban_salon', 'vban_chambre'} == {'vban_salon'}
    calibration.cancel()
