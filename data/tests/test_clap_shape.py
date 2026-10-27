"""Forme d'un pic : un clap est bref et aigu (clap_shape), et les groupes de
claps ne comptent que les pics qui en ont la forme."""
import numpy as np
import pytest

import clap_shape

SR = 16000
RNG = np.random.default_rng(0)
T = np.arange(3000) / SR


def lowpass(x, fc):
    f = np.fft.rfft(x)
    f[np.fft.rfftfreq(x.size, 1 / SR) > fc] = 0
    return np.fft.irfft(f, x.size)


def placed(sig, at=1000, n=4000):
    x = RNG.normal(0, 0.002, n).astype(np.float32)
    x[at:at + sig.size] += sig[:n - at].astype(np.float32)
    return x, at + int(np.argmax(np.abs(sig[:200])))


SOUNDS = {
    'clap': (RNG.normal(0, 1, 3000) * np.exp(-T / 0.003) * 0.6, True),
    'clap reverberant': ((RNG.normal(0, 1, 3000) * (np.exp(-T / 0.008) + 0.15 * np.exp(-T / 0.06))) * 0.5, True),
    'claquement de doigts': (RNG.normal(0, 1, 3000) * np.exp(-T / 0.0015) * 0.5, True),
    'clap camera 8 kHz': (lowpass(RNG.normal(0, 1, 3000) * np.exp(-T / 0.004), 3500) * 0.6, True),
    'porte': (lowpass(RNG.normal(0, 1, 3000), 250) * np.exp(-T / 0.04) * 3, False),
    'voix tenue': (sum(np.sin(2 * np.pi * f * T) / k for k, f in enumerate((180, 360, 540, 720, 900), 1))
                   * 0.3 * np.minimum(1, T / 0.005), False),
}


@pytest.mark.parametrize('name', list(SOUNDS))
def test_verdicts(name):
    sig, expected = SOUNDS[name]
    x, idx = placed(sig)
    ok, reason = clap_shape.verdict(clap_shape.measure(x, idx))
    assert ok is expected, (name, clap_shape.measure(x, idx))
    if not ok:
        assert reason in ('tenu', 'grave')


def _tracker(whitelist, counts=(1, 2)):
    from clap_logic import ClapTracker
    t = ClapTracker(window=1.5)
    t.set_groups([{'slug': 'g', 'name': 'G', 'whitelist': whitelist, 'threshold': 0.5, 'clap_counts': list(counts)}])
    for i in range(12):
        t.feed_peak(0.01, 99.0 + i * 0.01)
    return t


def test_clap_groups_ignore_peaks_without_clap_shape():
    t = _tracker({'Clapping': True, 'Hands': True})
    t.feed_peak(0.3, 100.0, pre_level=0.01)
    t.set_peak_shape(100.0, False, 'grave', {'decay': 0.3, 'high': 0.01})
    assert t.near_misses == []                                           # rien tant que le son n'est pas reconnu
    assert t.on_classification([('Clapping', 0.9)], 100.3) == [] and not t.pending()
    assert [m['kind'] for m in t.near_misses] == ['shape'] and t.near_misses[0]['reason'] == 'grave'
    t.on_classification([('Clapping', 0.9)], 100.8)
    assert len(t.near_misses) == 1                                       # une fois par pic
    # Bruit grave sans son de clap reconnu : rien dans le journal
    q = _tracker({'Clapping': True})
    q.feed_peak(0.3, 100.0, pre_level=0.01)
    q.set_peak_shape(100.0, False, 'grave', {})
    q.on_classification([('Door', 0.9)], 100.3)
    assert q.near_misses == []
    # Groupe d'un autre son (toc) : la forme ne compte pas
    k = _tracker({'Knock': True})
    k.feed_peak(0.3, 100.0, pre_level=0.01)
    k.set_peak_shape(100.0, False, 'grave', {})
    assert k.near_misses == []
    k.on_classification([('Knock', 0.9)], 100.3)
    assert k.pending()
    # Verification desactivee : compte
    off = _tracker({'Clapping': True})
    off.shape_check = False
    off.feed_peak(0.3, 100.0, pre_level=0.01)
    off.set_peak_shape(100.0, False, 'tenu', {})
    off.on_classification([('Clapping', 0.9)], 100.3)
    assert off.pending() and off.near_misses == []


def test_window_armed_on_a_rejected_peak_moves_to_the_next_clap():
    """Groupe arme sur un pic que la mesure ecarte ensuite (porte reconnue
    « Clapping ») : la fenetre repart du clap suivant, sinon il se desarme."""
    t = _tracker({'Clapping': True}, counts=(1, 2))
    t.feed_peak(0.3, 100.0, pre_level=0.01)
    t.on_classification([('Clapping', 0.6)], 100.05)                       # arme avant la mesure
    assert t._group_state['g']['armed_at'] == 100.0
    t.feed_peak(0.01, 100.5)
    t.feed_peak(0.3, 101.2, pre_level=0.01)
    t.set_peak_shape(100.0, False, 'grave', {})
    assert t._group_state['g']['armed_at'] == 101.2                        # repart du vrai clap
    u = _tracker({'Clapping': True})
    u.feed_peak(0.3, 100.0, pre_level=0.01)
    u.on_classification([('Clapping', 0.6)], 100.05)
    u.set_peak_shape(100.0, False, 'tenu', {})
    assert not u.pending() and [m['kind'] for m in u.near_misses] == ['shape']


def test_detector_measures_the_shape_of_real_peaks(fake_classifier, monkeypatch):
    """Son grave (porte) : pic ecarte pour un groupe de claps ; clap bref : compte."""
    import time
    from audio_detector import AudioDetector
    from audio_utils import BLOCK_SAMPLES
    clock = [1000.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    det = AudioDetector('model.tflite')
    det.initialize(max_results=-1, score_threshold=0.3)
    det.set_groups([{'slug': 'clap', 'name': 'Clap', 'whitelist': {'Clapping': True}, 'threshold': 0.5, 'clap_counts': [1, 2]}])
    misses, events = [], []
    det.configure('s', detection_callback=events.append, near_miss_callback=misses.append)
    det.start()
    seen = []
    real = det.tracker.set_peak_shape
    det.tracker.set_peak_shape = lambda t, ok, reason='', metrics=None: seen.append((t, ok)) or real(t, ok, reason, metrics)
    door = (np.sin(2 * np.pi * 120 * T + np.pi / 2) * np.exp(-T / 0.04) * 0.6).astype(np.float32)   # coup sourd, attaque nette
    clap = (RNG.normal(0, 1, 3000) * np.exp(-T / 0.003) * 0.6).astype(np.float32)
    sig = RNG.normal(0, 0.002, 16000 * 5).astype(np.float32)
    sig[32000:32000 + door.size] += door                                  # apres l'apprentissage du bruit
    sig[56000:56000 + clap.size] += clap
    for i in range(0, sig.size, BLOCK_SAMPLES):
        clock[0] += 0.1
        det.process_audio(sig[i:i + BLOCK_SAMPLES])
        det.wait_idle()
    for _ in range(20):
        clock[0] += 0.1
        det.process_audio(RNG.normal(0, 0.002, BLOCK_SAMPLES).astype(np.float32))
        det.wait_idle()
    det.stop()
    assert [ok for _t, ok in sorted(seen)] == [False, True], seen         # porte ecartee, clap garde
    assert [m['kind'] for m in misses].count('shape') == 1
