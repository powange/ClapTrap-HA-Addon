"""Detecteur avec un faux classifieur : gain, auto-gain, arret."""
import threading

import numpy as np

from audio_detector import AudioDetector
from audio_utils import BLOCK_SAMPLES


def make(fake_classifier, groups=None):
    det = AudioDetector('model.tflite')
    det.initialize(max_results=5, score_threshold=0.3)
    det.set_groups(groups or [{'slug': 'clap', 'name': 'Clap', 'whitelist': {'Clapping': True}, 'threshold': 0.5}])
    events = []
    det.configure('s', detection_callback=events.append)
    det.start()
    return det, fake_classifier.instances[-1], events


def heard(det):
    """Dernier bloc donne au classifieur (fin de la fenetre glissante)."""
    return det._blocks[-1]


def block(level, noise=0.0):
    b = np.random.default_rng(0).normal(0, noise, BLOCK_SAMPLES).astype(np.float32) if noise else \
        np.zeros(BLOCK_SAMPLES, np.float32)
    b[100] = level
    return b


def test_gain_applied_to_classifier_only(fake_classifier):
    """Pics comptes sur le signal brut, gain seulement a l'entree du classifieur
    (le gain RTSP x10 empechait de compter le moindre pic)."""
    det, clf, _ = make(fake_classifier)
    for _ in range(20):
        det.process_audio(block(0.003, 0.001), gain=10)
    raw_floor = det.tracker.avg_level
    assert raw_floor < 0.01          # bruit mesure avant gain
    assert np.abs(heard(det)).max() > 0.02   # le classifieur recoit le signal amplifie


def test_agc_does_not_clip_clap_onset(fake_classifier):
    """Piece calme : l'auto-gain monte, mais baisse immediatement sur un clap."""
    det, clf, _ = make(fake_classifier)
    for _ in range(30):
        det.process_audio(block(0.002, 0.0005))   # piece calme
    for _ in range(10):
        det.process_audio(block(0.02, 0.0005))    # son faible, net au-dessus du bruit
    assert det._agc > 2              # auto-gain monte sur les sons faibles
    det.process_audio(block(0.5, 0.002))
    assert np.abs(heard(det)).max() < 1.0    # pas d'ecretage du clap


def test_counts_claps_end_to_end(fake_classifier):
    det, clf, events = make(fake_classifier, [{'slug': 'clap', 'name': 'Clap', 'whitelist': {'Clapping': True},
                                               'threshold': 0.5, 'clap_counts': [1, 2]}])
    import time
    t0 = time.monotonic()
    for i in range(60):
        det.process_audio(block(0.6 if i in (20, 24) else 0.003, 0.001))
        # temps reel approximatif : blocs toutes les 20 ms au lieu de 100
        time.sleep(0.02)
    time.sleep(0.1)
    for _ in range(40):
        det.process_audio(block(0.003, 0.001))
        time.sleep(0.02)
    assert [e['clap_count'] for e in events if not e['ignored']] == [2], time.monotonic() - t0


def test_stop_during_processing_is_safe(fake_classifier):
    det, clf, _ = make(fake_classifier)
    stop = threading.Event()

    def feed():
        while not stop.is_set():
            det.process_audio(block(0.3, 0.001))

    t = threading.Thread(target=feed)
    t.start()
    det.stop()
    stop.set()
    t.join(2)
    assert clf.closed and det.classifier is None
    det.process_audio(block(0.3))   # detecteur arrete : ignore sans erreur


def test_agc_stays_up_on_isolated_weak_clap(fake_classifier):
    """Clap faible isole dans une piece calme : amplifie pour le classifieur
    (l'ancien auto-gain retombait a 1 sur le bruit et n'atteignait que x1,4)."""
    det, clf, _ = make(fake_classifier)
    for _ in range(30):
        det.process_audio(block(0.002, 0.0005))
    det.process_audio(block(0.03, 0.0005))
    assert np.abs(heard(det)).max() > 0.1   # clap 0,03 amplifie
    det.process_audio(block(0.5, 0.0005))
    assert np.abs(heard(det)).max() < 1.0   # son fort : pas d'ecretage


def test_sliding_window_every_half_second_dated_at_its_end(fake_classifier):
    """Une fenetre de 0,975 s analysee toutes les 0,5 s, des la premiere
    seconde recue, datee a sa fin sur l'horloge audio, y compris quand
    plusieurs blocs arrivent d'un coup (rafale ffmpeg)."""
    det, clf, _ = make(fake_classifier)
    seen = []
    orig = det.tracker.on_classification
    det.tracker.on_classification = lambda cats, now: seen.append(now) or orig(cats, now)

    def feed(b):
        det.process_audio(b)
        det.wait_idle()
    for _ in range(9):
        feed(block(0.002, 0.001))
    assert seen == []                                    # premiere seconde incomplete
    feed(block(0.002, 0.001))
    assert seen == [det._clock_end] and len(clf.blocks[-1]) == 15600
    for _ in range(10):
        feed(block(0.002, 0.001))
    assert np.allclose(np.diff(seen), 0.5) and seen[-1] == det._clock_end
    feed(np.concatenate([block(0.002, 0.001)] * 7))     # rafale de 0,7 s
    assert abs(seen[-1] - (det._clock_end - 0.2)) < 1e-9          # fin de la fenetre, pas de la rafale


def test_window_restarts_after_a_gap(fake_classifier):
    """Audio perdu (VBAN) ou flux interrompu : la seconde d'avant n'est pas
    analysee avec le son d'apres (un clap d'avant la coupure, date maintenant,
    pouvait declencher une detection)."""
    det, clf, _ = make(fake_classifier)
    for _ in range(12):
        det.process_audio(block(0.002, 0.001))
    det.wait_idle()
    n = len(clf.blocks)
    det.skip_audio(30 * BLOCK_SAMPLES)
    for _ in range(9):
        det.process_audio(block(0.002, 0.001))
    det.wait_idle()
    assert len(clf.blocks) == n                          # nouvelle seconde pas encore complete
    det.process_audio(block(0.002, 0.001))
    det.wait_idle()
    assert len(clf.blocks) == n + 1


def test_slow_analysis_never_blocks_audio(fake_classifier):
    """Analyse lente (Pi surcharge) : la lecture du son continue, les
    fenetres en retard sautent (la plus ancienne)."""
    import threading
    det, clf, _ = make(fake_classifier)
    gate = threading.Event()
    orig = clf.classify
    clf.classify = lambda w: gate.wait(5) and orig(w)
    import time
    t = time.monotonic()
    for _ in range(60):
        det.process_audio(block(0.002, 0.001))
    assert time.monotonic() - t < 1.0                    # rien n'a attendu l'analyse
    gate.set()
    det.wait_idle()
    assert det._dropped >= 1 and len(clf.blocks) <= 3


def test_no_result_after_stop(fake_classifier):
    """Detecteur arrete pendant une analyse : son resultat est ignore, et le
    fil d'analyse s'arrete."""
    det, clf, _ = make(fake_classifier)
    calls = []
    det.tracker.on_classification = lambda cats, now: calls.append(now) or []
    det.running = False
    import types
    cat = types.SimpleNamespace(category_name='Silence', score=0.8)
    det._handle_result(types.SimpleNamespace(classifications=[types.SimpleNamespace(categories=[cat])]), 10 ** 15)
    assert calls == []
    det.stop()
    det._worker.join(2)
    assert not det._worker.is_alive()


def test_skip_audio_advances_clock(fake_classifier):
    det, clf, _ = make(fake_classifier)
    det.process_audio(block(0.002))
    t = det._clock_end
    det.skip_audio(3 * BLOCK_SAMPLES)
    assert abs(det._clock_end - t - 0.3) < 1e-9


def test_loud_block_reduced_not_clipped_and_next_sounds_not_attenuated(fake_classifier):
    """Camera a gain x10 : un pic brut de 0,5 (x10 = 5) est ramene sous 1
    avant le classifieur au lieu d'etre ecrete ; un clap normal juste apres
    garde son niveau (limiteur par bloc, pas de memoire de 3 s)."""
    det, clf, _ = make(fake_classifier)
    for _ in range(12):
        det.process_audio(block(0.002, 0.0005), gain=10.0)
    det.process_audio(block(0.5, 0.0005), gain=10.0)
    loud = np.abs(heard(det)).max()
    assert 0.5 < loud <= 0.91
    det.process_audio(block(0.002, 0.0005), gain=10.0)
    det.process_audio(block(0.05, 0.0005), gain=10.0)    # clap normal : 0,05 x10 = 0,5
    assert np.abs(heard(det)).max() > 0.4


def test_audio_tap_gets_exactly_what_the_classifier_gets(fake_classifier):
    """Enregistrement d'extraits : le son ecoute est celui donne a YAMNet
    (gain et limiteur appliques), bloc pour bloc."""
    det, clf, _ = make(fake_classifier)
    heard = []
    det.add_tap(lambda event, *data: heard.append(data[0]) if event == 'audio' else None)
    for i in range(10):
        det.process_audio(block(0.6 if i == 2 else 0.003, 0.001), gain=10)
    det.wait_idle()
    assert len(heard) == 10 and len(clf.blocks) == 1
    assert np.array_equal(np.concatenate(heard)[-15600:], clf.blocks[0])
