"""Reduction du bruit de fond avant YAMNet (« spectral gating »), par source.

Traitement en continu du son brut de la source (avant gain et auto-gain),
bloc par bloc : tranches de 32 ms (transformee de Fourier, recouvrement de
75 %), reconstruites ensuite (retard de 24 ms).

Profil du bruit, par frequence : le niveau moyen de chaque bloc de 100 ms,
et le bruit est le plus faible de ces niveaux sur les NOISE_HISTORY derniers
blocs (30 s) : un bloc avec un clap ou une voix est plus fort, il ne compte
donc pas (« statistiques minimales »). Mis a jour a chaque
bloc, meme quand YAMNet ne tourne pas, et sur le son brut : l'auto-gain ne le
fausse pas. Un son continu ne devient du « bruit » qu'apres 30 s sans
interruption (une alarme qui bipe, des pleurs, une sonnette ne le sont
jamais).

Les frequences proches du bruit sont attenuees (souffle, ventilation,
ronflement d'un frigo), celles qui le depassent nettement sont gardees. Seule
l'entree du classifieur est traitee : les pics (comptage des claps) sont
mesures sur le son brut, comme avant. Desactive par defaut.
"""
import numpy as np

N_FFT = 512              # 32 ms a 16 kHz
HOP = N_FFT // 4         # recouvrement de 75 %
LATENCY = N_FFT - HOP    # echantillons de retard du son traite
NOISE_HISTORY = 300      # blocs de 100 ms (30 s)
EPS = 1e-10

# Niveaux : (sur-soustraction, attenuation maximale en dB).
LEVELS = {
    1: (1.5, 10.0),      # legere
    2: (2.0, 18.0),      # moyenne
    3: (3.0, 26.0),      # forte
}
MAX_LEVEL = max(LEVELS)   # repris par settings_manager.MAX_NR_LEVEL (verifie par les tests)

_WINDOW = np.hanning(N_FFT + 1)[:-1].astype(np.float32)   # periodique
# Somme des carres de 4 fenetres decalees de HOP : constante (reconstruction exacte).
_NORM = float(np.sum(_WINDOW.reshape(4, HOP) ** 2, axis=0).mean())


def clamp_level(value):
    """Niveau utilisable (0 = desactive) d'une valeur enregistree."""
    try:
        return min(MAX_LEVEL, max(0, int(value or 0)))
    except (TypeError, ValueError):
        return 0


class SpectralGate:
    """Reduction de bruit d'une source, en continu. process() est appele par
    un seul fil (lecture du son) ; le niveau peut changer depuis un autre."""

    def __init__(self, level=2):
        self.level = level
        self._history = np.zeros((NOISE_HISTORY, N_FFT // 2 + 1), dtype=np.float32)
        self._filled = 0
        self._next = 0
        self.noise = None    # amplitude du bruit par frequence (son brut)
        self.reset()

    @property
    def level(self):
        return self._level

    @level.setter
    def level(self, value):
        level = clamp_level(value) or 1
        self._params = LEVELS[level]   # un seul attribut : lu d'un bloc par process()
        self._level = level

    def reset(self):
        """Flux interrompu : la reconstruction repart de zero (le profil du
        bruit est garde)."""
        self._in = np.zeros(LATENCY, dtype=np.float32)
        self._out = np.zeros(LATENCY, dtype=np.float32)

    @property
    def delay(self):
        """Retard (echantillons) du son rendu sur le son recu."""
        return self._in.size

    def _update_noise(self, mag):
        self._history[self._next] = mag.mean(axis=0)
        self._next = (self._next + 1) % NOISE_HISTORY
        self._filled = min(self._filled + 1, NOISE_HISTORY)
        self.noise = self._history[:self._filled].min(axis=0)

    def process(self, x):
        """Son traite, en retard de LATENCY echantillons : sa longueur suit
        celle des tranches completes (multiple de HOP), pas celle de `x`."""
        buf = np.concatenate((self._in, np.asarray(x, dtype=np.float32)))
        n = (buf.size - N_FFT) // HOP + 1 if buf.size >= N_FFT else 0
        if n <= 0:
            self._in = buf
            return np.zeros(0, dtype=np.float32)
        idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n)[:, None]
        spec = np.fft.rfft(buf[idx] * _WINDOW, axis=1)
        self._in = buf[n * HOP:]
        mag = np.abs(spec)
        self._update_noise(mag)
        alpha, floor_db = self._params
        gain = np.clip(1.0 - alpha * self.noise[None, :] / (mag + EPS), 10 ** (-floor_db / 20), 1.0)
        # Lissage (3 tranches x 3 frequences) : moins de « bruit musical ».
        g = np.pad(gain, 1, mode='edge')
        gain = sum(g[i:i + n, j:j + gain.shape[1]] for i in range(3) for j in range(3)) / 9.0
        frames = np.fft.irfft(spec * gain, n=N_FFT, axis=1).astype(np.float32) * _WINDOW
        # Recouvrement-addition, en 4 quarts (sans boucle sur les tranches).
        acc = np.zeros((n + 3) * HOP, dtype=np.float32)
        acc[:LATENCY] += self._out
        quarters = frames.reshape(n, 4, HOP)
        for q in range(4):
            acc[q * HOP:(q + n) * HOP] += quarters[:, q, :].reshape(-1)
        self._out = acc[n * HOP:]
        return acc[:n * HOP] / _NORM
