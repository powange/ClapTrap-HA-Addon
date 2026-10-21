"""Ecoute d'une source pendant quelques secondes (calibrage, extraits).

capture() : ce que le detecteur d'une source produit pendant une prise
(pics, resultats YAMNet, et au besoin le son donne a YAMNet).

record() : l'extrait enregistre depuis les reglages d'une source, pendant la
detection : quelques secondes du son donne a YAMNet (gain, auto-gain et
limiteur appliques) en WAV, avec les classes reconnues sur la meme periode.
On entend ainsi ce qui gene la reconnaissance (echo, saturation, reduction
de bruit de la camera...) au lieu de le deviner.
"""
import base64
import bisect
import io
import logging
import threading
import time
import wave

import numpy as np

from audio_detector import AGC_CEILING, YAMNET_WINDOW_MS

SAMPLE_RATE = 16000
DEFAULT_SECONDS = 5.0
MIN_SECONDS = 1.0
MAX_SECONDS = 10.0
RESULT_GRACE = 1.6       # derniers resultats YAMNet de la prise (fenetre de 0,975 s, toutes les 0,5 s, + retard du flux)
YAMNET_WINDOW = YAMNET_WINDOW_MS / 1000

_busy = set()            # sources en cours d'enregistrement (requete bloquante)
_busy_lock = threading.Lock()


class RecordingError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def saturation(max_raw, gain):
    """(sature, limite) d'une prise : sature des l'entree (micro, camera :
    le gain n'y peut rien), ou son fort reduit par le limiteur (gain eleve)."""
    return max_raw >= 0.99, max_raw * gain > AGC_CEILING


def capture(det, seconds, grace=RESULT_GRACE, audio=False, scores=False):
    """Ecoute le detecteur `seconds` s, puis `grace` s pour les derniers
    resultats YAMNet. Renvoie {'blocks': [(pic_brut, gain, instant)],
    'peaks': [(type, instant)], 'results': [(categories, instant)],
    'audio': [(bloc, horloge_apres_bloc)], 'scores': [(521 classes,
    instant)], 't0', 't1'} ; instants sur l'horloge audio du detecteur.
    Blocs, pics et son : pendant la prise seulement."""
    events = {'block', 'peak', 'result'} | ({'audio', 'audio_raw'} if audio else set()) | ({'scores'} if scores else set())
    out = {'blocks': [], 'peaks': [], 'results': [], 'audio': [], 'audio_raw': [], 'scores': []}
    lock = threading.Lock()
    capturing = [True]

    def tap(event, *data):
        with lock:
            if event == 'result':
                out['results'].append(data)
            elif event == 'scores':
                out['scores'].append(data)
            elif capturing[0]:
                if event in ('audio', 'audio_raw'):
                    out[event].append((data[0], det._clock_end) + tuple(data[1:]))
                else:
                    out['blocks' if event == 'block' else 'peaks'].append(data)
    out['t0'] = det._clock_end
    det.add_tap(tap, events=events)
    try:
        time.sleep(seconds)
        with lock:
            capturing[0] = False
        out['t1'] = det._clock_end
        time.sleep(grace)
    finally:
        det.remove_tap(tap)
    return out


def to_wav(samples):
    """WAV mono 16 bits a 16 kHz d'un signal float32 (-1..1)."""
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype('<i2')
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _timeline(audio, results):
    """Resultats places dans l'extrait : chaque bloc garde l'horloge audio a
    sa fin, un resultat (date a la fin de sa fenetre) est place au bloc qui
    termine sa fenetre. Une coupure du flux ou un recalage de l'horloge ne
    decale donc pas les lignes par rapport au son."""
    clocks, ends, total = [], [], 0
    for block, clock in audio:
        total += block.size
        clocks.append(clock)
        ends.append(total)
    half = YAMNET_WINDOW / 2
    rows = []
    for categories, when in results:
        # Fenetre a moitie au moins dans l'extrait.
        if not clocks or not (clocks[0] - half < when <= clocks[-1] + half):
            continue
        i = min(bisect.bisect_left(clocks, when), len(ends) - 1)
        start = max(0.0, ends[i] / SAMPLE_RATE - YAMNET_WINDOW)
        rows.append({'t': round(start, 2),
                     'top': [{'classe': label, 'score': round(float(score), 3)} for label, score in categories[:3]]})
    return rows


def _classify_clip(det, samples, results, blocks):
    """Sons reconnus sur un extrait que la detection n'a pas analyse (le son
    avant la reduction du bruit) : memes fenetres que celles de la detection
    (meme fin, placees comme dans _timeline), meme classifieur et memes
    classes (sons appris compris) ; les lignes s'alignent sur celles du son
    reduit. Quelques analyses de plus : le verrou du classifieur est partage
    avec la detection, une fenetre a la fois."""
    from audio_detector import WINDOW_SAMPLES
    clocks, ends, total = [], [], 0
    for block, clock in blocks:
        total += block.size
        clocks.append(clock)
        ends.append(total)
    half = YAMNET_WINDOW / 2
    rows = []
    for _categories, when in results:
        if not clocks or not (clocks[0] - half < when <= clocks[-1] + half):
            continue
        end = ends[min(bisect.bisect_left(clocks, when), len(ends) - 1)]
        win = samples[max(0, end - WINDOW_SAMPLES):end]
        if win.size < WINDOW_SAMPLES:
            win = np.pad(win, (WINDOW_SAMPLES - win.size, 0))
        cats = det.classify_window(win)
        if cats is None:
            break   # classifieur arrete (detection arretee pendant l'enregistrement)
        rows.append({'t': round(max(0.0, end / SAMPLE_RATE - YAMNET_WINDOW), 2),
                     'top': [{'classe': n, 'score': round(float(sc), 3)} for n, sc in cats[:3]]})
    return rows


def record(source_id, seconds=DEFAULT_SECONDS):
    """Enregistre `seconds` s de la source (detection en cours). Bloque
    seconds + ~1,6 s."""
    import classify
    det = classify.detector_for(source_id)
    if det is None:
        raise RecordingError("La source n'est pas écoutée : démarrez la détection (et vérifiez que la source "
                             "est active et connectée) pour enregistrer un extrait.", 409)
    with _busy_lock:
        if source_id in _busy:
            raise RecordingError('Un enregistrement de cette source est déjà en cours.', 409)
        _busy.add(source_id)
    try:
        cap = capture(det, seconds, audio=True)
    finally:
        with _busy_lock:
            _busy.discard(source_id)
    if not cap['audio']:
        raise RecordingError("Aucun son reçu pendant l'enregistrement : la source est-elle connectée ?", 409)
    samples = np.concatenate([block for block, _clock in cap['audio']])
    max_raw = max((b[0] for b in cap['blocks']), default=0.0)
    clipped, limited = saturation(max_raw, max((b[1] for b in cap['blocks']), default=1.0))
    out = {}
    level = max((item[2] for item in cap['audio_raw'] if len(item) > 2), default=0)
    if level:
        # Reduction du bruit active : « avant » (meme gain, sans reduction) et
        # « apres » (exactement ce que YAMNet a recu), bloc pour bloc, pour
        # comparer. Sons reconnus : ceux de la detection (son reduit), et
        # ceux du son d'avant, analyse ici avec le meme classifieur.
        out = {'wav_denoised': base64.b64encode(to_wav(samples)).decode('ascii'), 'noise_reduction': level}
        samples = np.concatenate([item[0] for item in cap['audio_raw']])
        try:
            out['timeline_raw'] = _classify_clip(det, samples, cap['results'],
                                                 [(item[0], item[1]) for item in cap['audio_raw']])
        except Exception as e:
            logging.warning(f"Sons de l'extrait avant réduction non analysés : {e}")
            out['timeline_raw'] = []
    return {
        **out,
        'seconds': round(samples.size / SAMPLE_RATE, 2),
        'wav': base64.b64encode(to_wav(samples)).decode('ascii'),
        'timeline': _timeline(cap['audio'], cap['results']),
        'clipped': clipped,
        'limited': limited,
    }
