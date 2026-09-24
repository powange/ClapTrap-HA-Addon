"""Comptage des pics sur de vrais echantillons (position du pic, niveau qui
le precede, horloge audio). L'horloge murale est figee : tous les blocs
arrivent « en rafale », seul le temps audio compte."""
import numpy as np
import pytest

import audio_detector
from audio_detector import AudioDetector
from audio_utils import BLOCK_SAMPLES

SR = 16000


@pytest.fixture
def det(fake_classifier, monkeypatch):
    monkeypatch.setattr(audio_detector.time, 'monotonic', lambda: 1000.0)
    d = AudioDetector('m')
    d.initialize(max_results=5, score_threshold=0.3)
    d.set_groups([{'slug': 'clap', 'name': 'Clap', 'whitelist': {'Clapping': True}, 'threshold': 0.5}])
    d.configure('s')
    d.start()
    return d


def signal(seconds, claps, noise=0.002, amp=0.6, decay_ms=15, tail=None, seed=1):
    """Bruit + claps (salve de bruit a decroissance exponentielle).
    `tail` : (niveau, duree_s) d'une traine ajoutee apres chaque clap."""
    rng = np.random.default_rng(seed)
    x = rng.normal(0, noise, int(seconds * SR)).astype(np.float32)
    for t0, a in [(c if isinstance(c, tuple) else (c, amp)) for c in claps]:
        i = int(t0 * SR)
        n = int(0.3 * SR)
        env = a * np.exp(-np.arange(n) / (decay_ms / 1000 * SR))
        if tail:
            level, dur = tail
            env = np.maximum(env, level * np.exp(-np.arange(n) / (dur * SR)) * (np.arange(n) > 40))
        burst = env * rng.uniform(-1, 1, n)
        burst[0:3] = [a * 0.5, a, -a]   # attaque franche
        x[i:i + n] += burst[:len(x) - i]
    return np.clip(x, -1, 1)


def feed(det, x, gain=1.0):
    for i in range(0, len(x) - BLOCK_SAMPLES + 1, BLOCK_SAMPLES):
        det.process_audio(x[i:i + BLOCK_SAMPLES], gain=gain)
    return len(det.tracker.peak_times)


def test_single_clap(det):
    assert feed(det, signal(3, [1.55])) == 1


def test_two_claps_150ms_apart_in_adjacent_blocks(det):
    """1er clap au debut du bloc 15, 2e dans le bloc 16 : le pic du bloc 16
    n'est pas 1,5 fois celui du bloc 15 (l'ancienne regle le perdait), mais
    bien plus fort que le niveau juste avant lui."""
    assert feed(det, signal(3, [1.505, 1.655])) == 2


def test_clap_straddling_block_boundary_counts_once(det):
    assert feed(det, signal(3, [1.6 - 0.0001])) == 1


def test_reverberant_clap_counts_once(det):
    assert feed(det, signal(3, [1.5], tail=(0.4, 0.4))) == 1


def test_second_clap_in_loud_tail_counted(det):
    """2e clap pendant la traine forte du premier (micro sature, camera
    compressee) : compte grace au niveau mesure juste avant lui."""
    assert feed(det, signal(3, [(1.505, 0.9), (1.655, 1.0)], tail=(0.6, 0.08), decay_ms=8)) == 2


def test_burst_delivery_keeps_audio_spacing(det):
    """Blocs livres d'un coup (horloge murale figee) : deux claps a 200 ms
    d'ecart dans l'audio restent deux pics."""
    assert feed(det, signal(3, [1.5, 1.7])) == 2
    times = det.tracker.peak_times
    assert abs((times[1] - times[0]) - 0.2) < 0.01


def test_weak_source_with_gain(det):
    """Camera faible (gain x10) : clap brut 0,008 sur un bruit de 0,0005."""
    assert feed(det, signal(3, [1.5], noise=0.0005, amp=0.008), gain=10) == 1


def test_sustained_noise_no_peaks(det):
    rng = np.random.default_rng(3)
    x = signal(1, [])
    loud = rng.normal(0, 0.08, 3 * SR).astype(np.float32)   # aspirateur
    assert feed(det, np.concatenate([x, loud])) <= 1   # au plus l'arrivee du bruit
