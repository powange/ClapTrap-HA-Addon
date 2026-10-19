"""Autotest du vrai YAMNet (MediaPipe + yamnet.tflite), lance au build de
l'image : une mise a jour de mediapipe, numpy ou du modele qui casse la
classification, ou qui change le decoupage en fenetres dont depend la
datation des resultats (audio_detector.YAMNET_WINDOW_MS), echoue ici et non
chez l'utilisateur. Les tests unitaires utilisent un faux MediaPipe.

Usage : python selftest_yamnet.py (code de sortie 0 si tout va bien).
"""
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from audio_detector import YAMNET_WINDOW_MS  # noqa: E402
from audio_utils import BLOCK_SAMPLES  # noqa: E402

SAMPLE_RATE = 16000
BLOCK_MS = int(BLOCK_SAMPLES / SAMPLE_RATE * 1000)


def main():
    import mp_light   # comme l'add-on : sans OpenCV ni matplotlib
    mp_light.install_stubs()
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import audio
    from mediapipe.tasks.python.components import containers

    results = []
    options = audio.AudioClassifierOptions(
        base_options=mp_python.BaseOptions(model_asset_path=os.path.join(HERE, 'yamnet.tflite')),
        running_mode=audio.RunningMode.AUDIO_STREAM, max_results=5, score_threshold=0.0,
        result_callback=lambda result, ts: results.append((ts, result)))
    classifier = audio.AudioClassifier.create_from_options(options)
    rng = np.random.default_rng(0)
    start = ts = 1_000_000
    for i in range(50):   # 5 s de bruit, un pic au milieu
        ts += BLOCK_MS
        block = (rng.standard_normal(BLOCK_SAMPLES) * 0.01).astype(np.float32)
        if i == 25:
            block[800:900] += 0.8
        classifier.classify_async(containers.AudioData.create_from_array(block, SAMPLE_RATE), ts)
    deadline = time.monotonic() + 10
    while len(results) < 4 and time.monotonic() < deadline:
        time.sleep(0.05)
    classifier.close()

    stamps = [t - start for t, _ in results if t - start < 10_000_000]
    names = [r.classifications[0].categories[0].category_name
             for _, r in results if r.classifications and r.classifications[0].categories]
    print(f"YAMNet : {len(stamps)} résultats, horodatages {stamps[:6]}, classes {names[:4]}")
    if len(stamps) < 4 or not names:
        print("ÉCHEC : trop peu de résultats ou aucune classe reconnue")
        return 1
    # Les deux cadences que gere audio_detector._window_end_clock : un resultat
    # par fenetre date a son DEBUT (mesure sur MediaPipe 1.0), ou un resultat
    # par bloc date sur ce bloc. Toute autre cadence fausserait la datation.
    cadences = {'fenêtre': YAMNET_WINDOW_MS, 'bloc': BLOCK_MS}
    match = [name for name, step in cadences.items()
             if stamps[:4] == [BLOCK_MS + k * step for k in range(4)]]
    if not match:
        print(f"ÉCHEC : découpage inattendu {stamps[:4]} (attendu un pas de "
              f"{YAMNET_WINDOW_MS} ou {BLOCK_MS} ms depuis {BLOCK_MS}) : "
              "revoir audio_detector._window_end_clock")
        return 1
    print(f"YAMNet OK (un résultat par {match[0]})")
    return 0


if __name__ == '__main__':
    sys.exit(main())
