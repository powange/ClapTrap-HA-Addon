"""Forme d'un pic : ressemble-t-il a un clap ?

Un clap (ou un claquement de doigts) est un bruit tres bref : attaque nette,
energie etalee jusque dans les aigus, et decroissance en quelques dizaines de
millisecondes. Une porte ou un coup sourd sont graves ; une voix, une musique
ou un objet qui resonne durent. Les deux mesures sont faites sur le son brut
(avant gain et reduction du bruit), autour du pic, bruit de fond retire
(mesure juste avant le pic) :

- decroissance : niveau 30 a 80 ms apres le pic, rapporte au niveau du pic ;
- aigus : part de l'energie au-dessus de 1,5 kHz dans les 20 ms centrees
  sur le pic.

Seuils volontairement larges : on n'ecarte que ce qui ne peut clairement pas
etre un clap (un clap rate coute plus qu'un faux pic, que YAMNet filtre deja).
"""
import numpy as np

AFTER = 0.085            # s de son necessaires apres le pic
MAX_DECAY = 0.5          # niveau 30-80 ms apres / niveau du pic : au-dessus, le son dure
MIN_HIGH = 0.12          # part de l'energie au-dessus de HIGH_HZ : en dessous, le son est grave
HIGH_HZ = 1500.0


def _energy(x):
    return float(np.mean(np.square(x, dtype=np.float64))) if x.size else 0.0


def _spectrum(seg):
    return np.abs(np.fft.rfft(seg * np.hanning(seg.size))) ** 2


def measure(samples, idx, sample_rate=16000):
    """{'decay', 'high'} du pic a l'indice `idx` de `samples` (son brut)."""
    sr = sample_rate
    x = np.asarray(samples, dtype=np.float32)
    ms = sr // 1000
    head = x[max(0, idx - ms):idx + 10 * ms]                           # 1 ms avant, 10 ms apres
    tail = x[idx + 30 * ms:idx + 80 * ms]                              # 30 a 80 ms apres
    noise = x[max(0, idx - 80 * ms):max(0, idx - 20 * ms)]             # bruit de fond juste avant
    floor = _energy(noise) if noise.size >= 10 * ms else 0.0
    head_e = max(_energy(head) - floor, 1e-12)
    decay = float(np.sqrt(max(_energy(tail) - floor, 0.0) / head_e))
    high = 0.0
    seg = x[max(0, idx - 10 * ms):idx + 10 * ms]                       # 20 ms centrees sur le pic
    if seg.size >= 32:
        spec = _spectrum(seg)
        before = x[max(0, idx - 20 * ms - seg.size):max(0, idx - 20 * ms)]
        if before.size == seg.size:
            spec = np.maximum(spec - _spectrum(before), 0.0)           # ronflement, ventilation retires
        freqs = np.fft.rfftfreq(seg.size, 1.0 / sr)
        total = float(spec[1:].sum())
        high = float(spec[freqs >= HIGH_HZ].sum()) / total if total > 0 else 0.0
    return {'decay': round(decay, 3), 'high': round(high, 3)}


def verdict(metrics):
    """(est un clap, raison) : raison 'tenu' (le son dure) ou 'grave'."""
    if metrics['decay'] > MAX_DECAY:
        return False, 'tenu'
    if metrics['high'] < MIN_HIGH:
        return False, 'grave'
    return True, ''
