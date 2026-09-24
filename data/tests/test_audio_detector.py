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
    assert np.abs(clf.blocks[-1]).max() > 0.02   # le classifieur recoit le signal amplifie


def test_agc_does_not_clip_clap_onset(fake_classifier):
    """Piece calme : l'auto-gain monte, mais baisse immediatement sur un clap."""
    det, clf, _ = make(fake_classifier)
    for _ in range(30):
        det.process_audio(block(0.002, 0.0005))   # piece calme
    for _ in range(10):
        det.process_audio(block(0.02, 0.0005))    # son faible, net au-dessus du bruit
    assert det._agc > 2              # auto-gain monte sur les sons faibles
    det.process_audio(block(0.5, 0.002))
    assert np.abs(clf.blocks[-1]).max() < 1.0    # pas d'ecretage du clap


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
