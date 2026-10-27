"""Calibrage du comptage : propositions et rejeu sur des prises synthetiques."""
import random

import pytest

import calibration
import count_calibration as cc


def blocks(claps=(), seconds=3.0, t0=100.0, noise=0.002, rebound=None, seed=1):
    """Prise synthetique : un bloc de 100 ms par pas, bruit faible, claps
    (instant relatif, pic brut) ; `rebound` : (decalage, pic) apres chaque clap."""
    rng = random.Random(seed)
    out = []
    n = int(seconds * 10)
    for i in range(n):
        t = t0 + i * 0.1
        raw, pre = noise * (1 + rng.random()), noise
        for c_t, c_raw in claps:
            if abs(t - (t0 + c_t)) < 0.05:
                raw, pre = c_raw, noise
            elif rebound and abs(t - (t0 + c_t + rebound[0])) < 0.05:
                raw, pre = rebound[1], noise * 2
        out.append((raw, 1.0, t, pre))
    return out


@pytest.fixture
def session(monkeypatch):
    s = calibration._Session('mic', 'mic', 'mic', 'Micro', True)
    s.purpose = 'count'
    monkeypatch.setattr(calibration, '_session', s)
    monkeypatch.setattr(cc, '_params', lambda s=None: {'delay': 1.5, 'peak_cooldown': 0.08, 'peak_ratio': 3.0})
    s.count_takes = [
        {'kind': 'fond', 'blocks': blocks(seconds=5)},
        {'kind': 'simple', 'blocks': blocks([(1.0, 0.3)], rebound=(0.1, 0.08), seed=2)},
        {'kind': 'simple', 'blocks': blocks([(1.0, 0.25)], rebound=(0.1, 0.07), seed=3)},
        {'kind': 'double', 'blocks': blocks([(0.5, 0.3), (1.1, 0.28)], seed=4)},
        {'kind': 'triple', 'blocks': blocks([(0.5, 0.3), (1.0, 0.3), (1.5, 0.29)], seed=5)},
        {'kind': 'separe', 'blocks': blocks([(0.5, 0.3), (1.0, 0.3), (3.5, 0.3)], seconds=5, seed=6)},
    ]
    return s


def test_simulate_counts_gestures_like_the_detector():
    bg = blocks(seconds=5)
    p = {'delay': 1.5, 'peak_cooldown': 0.08, 'peak_ratio': 3.0}
    assert cc._simulate(bg, blocks([(0.5, 0.3), (1.0, 0.3)]), p) == [2]
    assert cc._simulate(bg, blocks([(0.5, 0.3), (1.0, 0.3), (3.5, 0.3)], seconds=5), p) == [2, 1]
    assert cc._simulate(bg, blocks(seconds=5), p) == []   # bruit seul : aucun clap


def test_analyze_proposes_values_that_fix_the_takes(session):
    out = cc.analyze()
    prop = out['proposed']
    # Rebond a 0,1 s : compte comme un 2e clap avec l'ecart actuel (0,08 s)
    simple = [r for r in out['rows'] if r['kind'] == 'simple']
    assert all(r['current'] == [2] and not r['current_ok'] for r in simple)
    assert 0.1 < prop['peak_cooldown'] <= 0.3                          # au-dela du rebond, sous l'ecart entre claps
    assert prop['delay'] == 1.4                                        # geste le plus long (1,0 s) + 0,4 s
    assert out['measures']['noise_ratio'] * 1.3 < prop['peak_ratio'] < out['measures']['weakest_clap_ratio'] * 0.7
    assert out['score']['proposed'] == out['score']['takes'] > out['score']['current']
    assert all(r['proposed_ok'] for r in out['rows'])
    assert out['warnings'] == []


def test_analyze_warns_when_commands_would_merge(session):
    session.count_takes[-1] = {'kind': 'separe', 'blocks': blocks([(0.5, 0.3), (1.0, 0.3), (1.6, 0.3)], seconds=5, seed=7)}
    out = cc.analyze()
    assert any('entre deux commandes' in w for w in out['warnings'])


def test_analyze_warns_when_claps_are_barely_above_noise(session):
    session.count_takes[0] = {'kind': 'fond', 'blocks': blocks([(2.0, 0.02)], seconds=5)}   # bruit parasite fort
    session.count_takes[1:] = [
        {'kind': 'simple', 'blocks': blocks([(1.0, 0.032)])},
        {'kind': 'double', 'blocks': blocks([(0.5, 0.032), (1.0, 0.032)], seed=9)},
    ]
    out = cc.analyze()
    assert any('à peine plus forts que le bruit' in w for w in out['warnings'])


def test_analyze_requires_background_and_claps(session):
    session.count_takes = [t for t in session.count_takes if t['kind'] != 'fond']
    with pytest.raises(calibration.CalibrationError, match='bruit de fond'):
        cc.analyze()
    session.count_takes = [{'kind': 'fond', 'blocks': blocks(seconds=5)}]
    with pytest.raises(calibration.CalibrationError, match='clap simple'):
        cc.analyze()


def test_apply_saves_proposed_or_adjusted_values(session, monkeypatch):
    calls, finished = [], []
    monkeypatch.setattr(calibration, '_api', lambda m, p, b=None: calls.append((m, p, b)))
    monkeypatch.setattr(calibration, '_finish', lambda s: finished.append(s))
    cc.analyze()
    out = cc.apply({'delay': 2.0})
    assert calls[0][0:2] == ('PUT', '/api/settings/advanced')
    assert calls[0][2]['delay'] == 2.0 and 'peak_ratio' in calls[0][2] and finished == [session]
    assert out['applied']['delay'] == 2.0
    with pytest.raises(calibration.CalibrationError):
        cc.apply({'inconnu': 1})


def test_apply_saves_peak_ratio_on_a_source_that_has_its_own(session, monkeypatch, settings_dir):
    """Source avec sa propre force minimale : analysee avec elle, et
    enregistree sur elle (le reste en global), en une seule ecriture ; la
    portee est celle de l'analyse, pas relue a l'application."""
    import routes.sources as rs
    applied = []
    monkeypatch.setattr(rs, '_apply_live', lambda: applied.append(1))
    monkeypatch.setattr(calibration, '_finish', lambda s: None)
    monkeypatch.setattr(calibration, '_api', lambda *a: pytest.fail('pas d\'appel API en portee source'))
    settings_dir.save_settings({'microphone': {'configured': True, 'peak_ratio': 7.0}, 'global': {'peak_ratio': 3.0}})
    monkeypatch.setattr(cc, '_params', lambda s=None: {'delay': 1.5, 'peak_cooldown': 0.08, 'peak_ratio': 7.0})
    d = cc.analyze()
    assert d['current']['peak_ratio'] == 7.0 and d['peak_ratio_scope'] == 'source'
    cc.apply({'peak_ratio': 5.5, 'delay': 2.0})
    st = settings_dir.load_settings()
    assert st['microphone']['peak_ratio'] == 5.5 and st['global']['peak_ratio'] == 3.0 and st['global']['delay'] == 2.0
    assert applied


def test_params_use_the_source_peak_ratio(settings_dir):
    real = cc._params
    settings_dir.save_settings({'microphone': {'configured': True, 'peak_ratio': 6.0}, 'global': {'peak_ratio': 3.0}})
    s = calibration._Session('mic', 'mic', 'mic', 'Micro', True)
    assert real(s)['peak_ratio'] == 6.0 and real()['peak_ratio'] == 3.0


def test_record_stores_take_and_replaces_background(monkeypatch, session):
    bg = blocks(seconds=2)
    monkeypatch.setattr(calibration, 'capture_take', lambda s, d, g: {'blocks': bg})
    out = cc.record('fond', 2)
    assert out['counts']['fond'] == 1 and sum(1 for t in session.count_takes if t['kind'] == 'fond') == 1
    for bad in ('inconnu', [], {}, None):   # liste ou objet : 400, pas 500 (TypeError)
        with pytest.raises(calibration.CalibrationError):
            cc.record(bad)


def test_failed_take_is_ignored_and_reported(session):
    """Prise « 2 claps » sans clap (fait trop tot) : ecartee, signalee, et
    elle ne fausse plus la fenetre ni la force minimale."""
    session.count_takes.insert(3, {'kind': 'double', 'blocks': blocks(seconds=4, seed=11)})   # bruit seul
    out = cc.analyze()
    assert any('prises ignorées' in w and '0 clap(s) trouvé(s) au lieu de 2' in w for w in out['warnings'])
    assert out['proposed']['delay'] == 1.4 and out['proposed']['peak_ratio'] > 3


def test_clap_decay_is_not_a_rebound(session):
    """Decroissance normale du clap (blocs suivants plus faibles, sans
    attaque) : pas un rebond, l'ecart minimal ne monte pas a sa limite."""
    def decaying(seed):
        out = blocks([(1.0, 0.3)], seed=seed)
        # (pic, niveau juste avant : la fin du bloc precedent, encore haut)
        tail = {1.1: (0.05, 0.3), 1.2: (0.03, 0.05), 1.3: (0.02, 0.03)}
        return [tail[round(t - 100.0, 1)][:1] + (g, t) + tail[round(t - 100.0, 1)][1:]
                if round(t - 100.0, 1) in tail else (raw, g, t, pre) for raw, g, t, pre in out]
    session.count_takes[1] = {'kind': 'simple', 'blocks': decaying(2)}
    session.count_takes[2] = {'kind': 'simple', 'blocks': decaying(3)}
    out = cc.analyze()
    assert out['measures']['rebound'] is None and out['proposed']['peak_cooldown'] == 0.08


def test_window_rounding_has_no_float_error(session):
    session.count_takes[3] = {'kind': 'double', 'blocks': blocks([(0.5, 0.3), (1.3, 0.28)], seed=4)}   # 0,8 s
    session.count_takes[4] = {'kind': 'triple', 'blocks': blocks([(0.5, 0.3), (0.9, 0.3), (1.2, 0.29)], seed=5)}
    assert cc.analyze()['proposed']['delay'] == 1.2   # 0,8 + 0,4, et non 1,3


def test_other_calibration_session_is_not_used(session):
    session.purpose = 'source'   # un calibrage de source a remplace la session
    with pytest.raises(calibration.CalibrationError, match='un autre calibrage'):
        cc.analyze()


def test_routes(monkeypatch, settings_dir):
    from flask import Flask
    from routes.calibration import calibration_bp
    app = Flask(__name__)
    app.register_blueprint(calibration_bp)
    c = app.test_client()
    monkeypatch.setattr(cc, 'analyze', lambda: {'proposed': {'delay': 1.4}})
    assert c.post('/api/calibration/count/analyze').json == {'success': True, 'proposed': {'delay': 1.4}}
    assert c.post('/api/calibration/count/apply', json={'values': [1]}).status_code == 400
    r = c.post('/api/calibration/count/record', json={'kind': 'fond'})
    assert r.status_code == 409   # aucune session de calibrage


def test_record_with_real_detector_finds_the_claps(monkeypatch, fake_classifier):
    """Prise capturee sur le vrai detecteur (pics et niveau avant pic), puis
    rejouee : on retrouve les 2 claps joues."""
    import threading
    import time
    import numpy as np
    import classify
    from audio_detector import AudioDetector
    from audio_utils import BLOCK_SAMPLES
    det = AudioDetector('model.tflite')
    det.initialize(max_results=5, score_threshold=0.3)
    det.set_groups([{'slug': 'clap', 'name': 'Clap', 'whitelist': {'Clapping': True}, 'threshold': 0.5}])
    det.configure('x', detection_callback=lambda e: None)
    det.start()
    s = calibration._Session('mic', 'mic', 'x', 'Micro', True)
    s.purpose = 'count'
    monkeypatch.setattr(calibration, '_session', s)
    monkeypatch.setattr(classify, 'detector_for', lambda sid: det)
    monkeypatch.setattr(cc, '_params', lambda s=None: {'delay': 1.5, 'peak_cooldown': 0.08, 'peak_ratio': 3.0})
    rng = np.random.default_rng(0)
    phase = {'claps_at': set(), 'i': 0}
    stop = threading.Event()

    def feed():
        while not stop.is_set():
            b = rng.normal(0, 0.002, BLOCK_SAMPLES).astype(np.float32)
            if phase['i'] in phase['claps_at']:
                b[400] = 0.5
            det.process_audio(b)
            phase['i'] += 1
            time.sleep(0.02)
    th = threading.Thread(target=feed, daemon=True)
    th.start()
    try:
        time.sleep(0.5)
        cc.record('fond', 1.0)
        start = phase['i']
        phase['claps_at'] = {start + 8, start + 14}       # deux claps a 0,6 s
        out = cc.record('double', 1.5)
    finally:
        stop.set()
        th.join(2)
        det.stop()
    assert out['gestures'] == [2]
    blocks = next(t for t in s.count_takes if t['kind'] == 'double')['blocks']
    assert all(len(b) == 5 for b in blocks) and any(b[3] is not None for b in blocks)   # niveau avant pic, attaques
