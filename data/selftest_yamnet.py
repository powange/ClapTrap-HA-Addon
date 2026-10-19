"""Autotest du vrai YAMNet (MediaPipe + yamnet.tflite), lance au build de
l'image : une mise a jour de mediapipe, numpy ou du modele qui casse la
classification, ou qui ne rend plus un resultat par fenetre analysee (la
datation des resultats en depend, voir audio_detector), echoue ici et non
chez l'utilisateur. Les tests unitaires utilisent un faux MediaPipe.

Usage : python selftest_yamnet.py (code de sortie 0 si tout va bien).
"""
import os
import sys
import types

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import audio_detector  # noqa: E402
from audio_detector import AudioDetector, HOP_BLOCKS, WINDOW_SAMPLES  # noqa: E402
from audio_utils import BLOCK_SAMPLES  # noqa: E402

SAMPLE_RATE = 16000


def main():
    det = AudioDetector(os.path.join(HERE, 'yamnet.tflite'), SAMPLE_RATE)
    det.initialize(max_results=5)   # importe mediapipe comme l'add-on (sans OpenCV ni matplotlib)
    # Une fenetre exacte donne un seul resultat (le detecteur ne garde que le
    # premier : un decoupage en plusieurs morceaux passerait sinon inapercu).
    containers = audio_detector._mediapipe[2]
    n = len(det.classifier.classify(containers.AudioData.create_from_array(
        np.zeros(WINDOW_SAMPLES, np.float32), SAMPLE_RATE)))
    if n != 1:
        print(f"ÉCHEC : {n} résultats pour une fenêtre de {WINDOW_SAMPLES} échantillons (1 attendu)")
        return 1
    results = []
    det.tracker.on_classification = lambda categories, now: results.append((now, categories)) or []
    # Horloge simulee : un bloc toutes les 100 ms, quelle que soit la vitesse
    # de la machine (un recalage de l'horloge audio fausserait les pas).
    clock = [1000.0]
    audio_detector.time = types.SimpleNamespace(monotonic=lambda: clock[0], time=lambda: clock[0])
    det.start()
    rng = np.random.default_rng(0)
    n_blocks = 50
    for i in range(n_blocks):   # 5 s de bruit, un pic au milieu
        clock[0] += BLOCK_SAMPLES / SAMPLE_RATE
        block = (rng.standard_normal(BLOCK_SAMPLES) * 0.01).astype(np.float32)
        if i == 25:
            block[800:900] += 0.8
        det.process_audio(block)
        det.wait_idle()   # aucune fenetre sautee (machine lente)
    det.stop()

    first = -(-WINDOW_SAMPLES // BLOCK_SAMPLES)          # premiere seconde complete
    expected = (n_blocks - first) // HOP_BLOCKS + 1
    steps = {round(b[0] - a[0], 3) for a, b in zip(results, results[1:])}
    names = [c[0][0] for _, c in results if c]
    print(f"YAMNet : {len(results)} résultats, pas {sorted(steps)} s, classes {names[:4]}")
    if len(results) != expected or not names:
        print(f"ÉCHEC : {len(results)} résultats au lieu de {expected} (un par fenêtre analysée), "
              "ou aucune classe reconnue : revoir audio_detector._push")
        return 1
    if steps != {round(HOP_BLOCKS * BLOCK_SAMPLES / SAMPLE_RATE, 3)}:
        print(f"ÉCHEC : résultats mal datés (pas {sorted(steps)})")
        return 1
    print("YAMNet OK (une fenêtre glissante analysée toutes les "
          f"{HOP_BLOCKS * BLOCK_SAMPLES / SAMPLE_RATE:g} s)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
