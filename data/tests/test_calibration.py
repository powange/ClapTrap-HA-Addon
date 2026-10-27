"""Calibrage d'une source : prises, analyse, application."""
import threading
import time

import pytest

import calibration
import ha_control
from helpers import wait_until


class FakeDetector:
    """Detecteur minimal : horloge audio et ecoutes, alimentees par un fil."""
    def __init__(self, script):
        self._clock_end = 100.0
        self._taps = []
        self.script = script   # fn(det) appelee pendant la prise

    def add_tap(self, fn, events=None):
        self._taps.append(fn)
        threading.Thread(target=self.script, args=(self,), daemon=True).start()

    def remove_tap(self, fn):
        self._taps.remove(fn)

    def emit(self, *event):
        for fn in list(self._taps):
            fn(*event)


@pytest.fixture
def cal(settings_dir, mqtt, monkeypatch):
    from flask import Flask
    import classify
    import routes.sources as rs
    from routes.calibration import calibration_bp
    from routes.settings_routes import settings_bp
    monkeypatch.setattr(rs, '_find_pulse_name', lambda s: ('alsa.usb', None))
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
    monkeypatch.setattr(calibration, '_session', None)
    monkeypatch.setattr(calibration, 'RESULT_GRACE', 0.05)
    monkeypatch.setattr(calibration, 'MIN_DURATION', 0.1)
    monkeypatch.setattr(classify, 'get_status', lambda: {'running': True})
    settings_dir.save_settings({'saved_vban_sources': [{
        'id': 'v1', 'entity_key': 'vban_atomsalon', 'name': 'AtomSalon', 'ip': '1.1.1.1', 'port': 6980,
        'stream_name': 'S', 'enabled': True, 'gain': 1, 'sound_groups': [
            {'slug': 'clap', 'name': 'Clap', 'threshold': 0.5, 'ha_entities': [1, 2],
             'sound_whitelist': {'Clapping': True, 'Whistling': True}},
            {'slug': 'siffler', 'name': 'Siffler', 'threshold': 0.5, 'ha_entities': [3, 4],
             'sound_whitelist': {}}]}]})
    import ha_entities
    ha_entities.sync_sources()
    yield client
    if calibration._session is not None:
        classify.mute_source(calibration._session.source_id, False)


def take(target, classes, attacks=0, sustains=0, max_raw=0.05, kind='group', group=None, expected=None):
    return {'target': target, 'kind': kind, 'group': group, 'expected': expected, 'duration': 4,
            'blocks': 40, 'results': 4, 'max_raw': max_raw, 'gain': 1.0, 'clipped': False,
            'attacks': attacks, 'sustains': sustains, 'classes': classes, 'top': []}


def session_with_takes(monkeypatch):
    s = calibration._Session('vban', 'v1', 'vban_v1', 'VBAN: AtomSalon', True)
    s.takes = (
        [take('Clap', {'Clapping': 0.85, 'Hands': 0.5, 'Whistling': 0.05}, attacks=2, sustains=2,
              max_raw=0.05, group='clap', expected=2) for _ in range(4)]
        # sifflements : pris pour des claps (Clapping 0,6), aucune attaque nette
        + [take('Siffler', {'Whistling': 0.75, 'Clapping': 0.6}, attacks=0, sustains=3,
                max_raw=0.04, group='siffler', expected=3) for _ in range(4)]
        + [take('Toc', {'Knock': 0.7, 'Tap': 0.3}, attacks=1, sustains=1, max_raw=0.1,
                kind='new', expected=1) for _ in range(3)]
        + [take('fond', {'Speech': 0.4, 'Clapping': 0.1}, kind='fond', max_raw=0.005)])
    monkeypatch.setattr(calibration, '_session', s)
    return s


def test_analysis_separates_groups_and_counting(cal, monkeypatch):
    session_with_takes(monkeypatch)
    p = calibration.analyze()
    by = {g['target']: g for g in p['groups']}
    clap, siffler, toc = by['Clap'], by['Siffler'], by['Toc']
    assert 'Clapping' in clap['sounds'] and 'Whistling' in clap['remove']
    assert clap['threshold']['proposed'] > 0.6           # au-dessus du « Clapping » des sifflements
    assert clap['after']['false_positive'] == 0 and clap['before']['false_positive'] > 0
    assert siffler['sounds'] == ['Whistling'] and siffler['add'] == ['Whistling']
    assert siffler['count_mode']['proposed'] == 'tenus'   # 3 pics comptes en sons tenus
    assert clap['count_mode']['proposed'] == 'attaques'
    assert toc['new'] and toc['sounds'][0] == 'Knock' and toc['ha_entities'] == [1]
    assert p['gain']['field'] == 'gain' and p['gain']['change'] and p['gain']['proposed'] == 5


def test_apply_through_api(cal, settings_dir, monkeypatch):
    session_with_takes(monkeypatch)
    calibration.analyze()
    r = calibration.apply()
    assert not r['errors'], r['errors']
    src = settings_dir.load_settings()['saved_vban_sources'][0]
    groups = {g['slug']: g for g in src['sound_groups']}
    assert src['gain'] == 5
    assert groups['clap']['sound_whitelist']['Whistling'] is False
    assert groups['siffler']['sound_whitelist']['Whistling'] is True and groups['siffler']['count_mode'] == 'tenus'
    assert groups['toc']['sound_whitelist']['Knock'] is True and groups['toc']['ha_entities'] == [1]
    assert calibration._session is None


def _blocks(peaks, floor=0.01, start=10.0):
    """Blocs bruts d'une prise (pic, gain, instant, niveau avant, attaques) :
    niveau `floor`, et une attaque nette aux instants (relatifs) de `peaks`."""
    out = []
    for i in range(30):
        t = start + i * 0.1
        raw = next((r for at, r in peaks if abs(at - i * 0.1) < 1e-6), floor)
        out.append((raw, 1.0, t, floor, [(raw, t, floor)] if raw > floor else None))
    return out


def with_blocks(s, fond_noise=0.02, clap=0.3):
    """Blocs des prises : bruit de fond (un pic parasite) et claps du groupe
    « Clap » (2 attendus, comptes en attaques)."""
    for t in s.takes:
        if t['kind'] == 'fond':
            s.take_blocks[id(t)] = _blocks([(1.0, fond_noise)])
        elif t['target'] == 'Clap':
            s.take_blocks[id(t)] = _blocks([(0.5, clap), (0.9, clap * 0.8)])


def test_peak_ratio_proposed_and_applied_on_source(cal, settings_dir, monkeypatch):
    s = session_with_takes(monkeypatch)
    with_blocks(s)
    p = calibration.analyze()
    r = p['peak_ratio']
    # Parasite x2, clap le plus faible x24 : moyenne geometrique de 2,6 et 16,8.
    assert r['field'] == 'peak_ratio' and r['current'] == 3.0 and not r['own']
    assert r['change'] and r['proposed'] == 6.6 and r['sounds'] == 8
    res = calibration.apply()
    assert not res['errors'], res['errors']
    src = settings_dir.load_settings()['saved_vban_sources'][0]
    assert src['peak_ratio'] == 6.6
    assert settings_dir.load_settings()['global'].get('peak_ratio', 3.0) == 3.0   # global inchange


def test_peak_ratio_kept_when_close_or_unchecked(cal, settings_dir, monkeypatch):
    settings_dir.modify_settings(lambda st: st['saved_vban_sources'][0].update(peak_ratio=6.5))
    s = session_with_takes(monkeypatch)
    with_blocks(s)
    r = calibration.analyze()['peak_ratio']
    assert r['own'] and not r['change'] and r['proposed'] == 6.5
    s.proposal['peak_ratio'].update(change=True, proposed=9.0)
    calibration.apply(peak_ratio=False)
    assert settings_dir.load_settings()['saved_vban_sources'][0]['peak_ratio'] == 6.5


def test_peak_ratio_covers_held_sounds_and_chosen_groups(cal, settings_dir, monkeypatch):
    s = session_with_takes(monkeypatch)
    with_blocks(s)
    # Sifflements (sons tenus) : sommet x6, sous les claps : la force reste
    # assez basse pour eux (le meme seuil filtre les sons tenus).
    for t in s.takes:
        if t['target'] == 'Siffler':
            s.take_blocks[id(t)] = _blocks([(0.5, 0.06)])
    p = calibration.analyze()
    assert p['peak_ratio']['proposed'] == 3.0 and not p['peak_ratio']['change']
    # Siffler non applique : ses prises ne comptent plus, force des claps seuls.
    res = calibration.apply(groups=['Clap'])
    assert not res['errors'], res['errors']
    assert settings_dir.load_settings()['saved_vban_sources'][0]['peak_ratio'] == 6.6
    assert any('force minimale × 3.0 → × 6.6' in a for a in res['applied'])


def test_peak_ratio_reports_ignored_takes(cal, monkeypatch):
    s = session_with_takes(monkeypatch)
    with_blocks(s)
    clap = next(t for t in s.takes if t['target'] == 'Clap')
    s.take_blocks[id(clap)] = _blocks([(0.5, 0.3)])   # un clap oublie sur 2
    p = calibration.analyze()
    assert p['peak_ratio']['sounds'] == 6
    assert any('prises ignorées' in w and 'Clap (1)' in w for w in p['warnings'])


def test_peak_ratio_needs_background_take(cal, monkeypatch):
    s = session_with_takes(monkeypatch)
    with_blocks(s)
    s.take_blocks = {k: v for k, v in s.take_blocks.items()
                     if k != id(next(t for t in s.takes if t['kind'] == 'fond'))}
    r = calibration.analyze()['peak_ratio']
    assert r['field'] is None and 'bruit de fond' in r['note']


def test_too_few_takes(cal, monkeypatch):
    s = session_with_takes(monkeypatch)
    s.takes = s.takes[:1]
    with pytest.raises(calibration.CalibrationError, match='au moins 2'):
        calibration.analyze()


def test_record_with_detector_and_routes(cal, monkeypatch):
    import classify

    def script(det):
        for i in range(5):
            det._clock_end += 0.1
            det.emit('block', 0.2 if i == 2 else 0.01, 1.0, det._clock_end)
            if i == 2:
                det.emit('peak', 'attack', det._clock_end)
                det.emit('peak', 'sustain', det._clock_end)
            time.sleep(0.02)
        det.emit('result', [('Clapping', 0.82), ('Hands', 0.4)], det._clock_end)
        det.emit('result', [('Speech', 0.9)], det._clock_end + 5)   # hors de la prise
    det = FakeDetector(script)
    monkeypatch.setattr(classify, 'detector_for', lambda sid: det if sid == 'vban_v1' else None)
    r = cal.post('/api/calibration/start', json={'source': 'AtomSalon'})
    assert r.status_code == 200 and 'vban_v1' in classify._muted_sources
    r = cal.post('/api/calibration/record', json={'target': 'Clap', 'expected': 2, 'duration': 0.3})
    t = r.json
    assert r.status_code == 200 and t['attacks'] == 1 and t['max_raw'] == 0.2
    assert t['classes'] == {'Clapping': 0.82, 'Hands': 0.4} and t['take_number'] == 1
    assert cal.get('/api/calibration').json['takes'] == {'Clap': 1}
    assert cal.post('/api/calibration/analyze').status_code == 400   # une seule prise
    assert cal.post('/api/calibration/cancel').status_code == 200
    assert 'vban_v1' not in classify._muted_sources
    assert cal.post('/api/calibration/record', json={'target': 'Clap'}).status_code == 409


def test_disabled_source_refused(cal, settings_dir):
    settings_dir.modify_settings(lambda s: s['saved_vban_sources'][0].update(enabled=False))
    r = cal.post('/api/calibration/start', json={'source': 'AtomSalon'})
    assert r.status_code == 400 and 'désactivée' in r.json['error']


def test_mqtt_calibration_commands(cal, mqtt):
    import json
    ha_control._handle(ha_control.COMMAND_TOPIC, json.dumps({'action': 'calibration_status'}).encode())
    res = json.loads([p for t, p, r in mqtt.published if t == ha_control.RESULT_TOPIC][-1])
    assert res['success'] and res['data']['active'] is False


def test_group_deleted_after_takes(cal, settings_dir, monkeypatch):
    session_with_takes(monkeypatch)
    settings_dir.modify_settings(lambda st: st['saved_vban_sources'][0]['sound_groups'].pop(1))
    p = calibration.analyze()
    siffler = next(g for g in p['groups'] if g['target'] == 'Siffler')
    assert 'supprimé' in siffler['error']


def test_apply_keeps_gain_when_refused_and_reports_exclusive_sound(cal, settings_dir, monkeypatch):
    session_with_takes(monkeypatch)
    calibration.analyze()
    # « Siffler » appliqué seul : « Whistling » reste coché dans « Clap » (non retenu)
    r = cal.post('/api/calibration/apply', json={'gain': 'false', 'groups': ['Siffler']})
    assert r.status_code == 200
    src = settings_dir.load_settings()['saved_vban_sources'][0]
    assert src['gain'] == 1
    assert any('reste dans « Clap »' in e for e in r.json['errors'])


def test_muted_source_does_not_feed_sound_discovery():
    import classify
    s = classify.DetectionSession([], None)
    s.seen['vban_v1'] = set()
    classify.mute_source('vban_v1', True)
    try:
        s._on_sound_seen({'source_id': 'vban_v1', 'kind': 'vban', 'source_key': 'v1'}, {'label': 'Speech'})
        assert s.seen['vban_v1'] == set()
    finally:
        classify.mute_source('vban_v1', False)


def test_concurrent_take_refused_and_cancel_answers_during_take(cal, monkeypatch):
    """Une seule prise a la fois (double clic : 409 au lieu d'un fil bloque),
    et « Annuler » repond pendant la prise ; la prise annulee est ecartee."""
    import classify

    def script(det):
        for _ in range(8):
            det._clock_end += 0.1
            det.emit('block', 0.01, 1.0, det._clock_end)
            time.sleep(0.05)
    monkeypatch.setattr(classify, 'detector_for', lambda sid: FakeDetector(script))
    assert cal.post('/api/calibration/start', json={'source': 'AtomSalon'}).status_code == 200
    first = {}
    t = threading.Thread(target=lambda: first.update(
        r=cal.post('/api/calibration/record', json={'target': 'Clap', 'duration': 0.4})))
    t.start()
    wait_until(lambda: calibration._session.taking)
    assert cal.post('/api/calibration/record', json={'target': 'Clap', 'duration': 0.4}).status_code == 409
    start = time.monotonic()
    assert cal.post('/api/calibration/cancel').status_code == 200
    assert time.monotonic() - start < 0.3                     # sans attendre la fin de la prise
    t.join(5)
    assert first['r'].status_code == 409
