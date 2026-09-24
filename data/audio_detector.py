"""Detecteur d'une source audio : pretraitement + YAMNet (MediaPipe) + logique
de claps (clap_logic.ClapTracker). Un detecteur = une source."""

import collections
import logging
import threading
import time

import numpy as np
from mediapipe.tasks import python
from mediapipe.tasks.python import audio
from mediapipe.tasks.python.components import containers

from audio_utils import BLOCK_SAMPLES
from clap_logic import ClapTracker

PRE_WINDOW = 320   # 20 ms : niveau mesure juste avant un pic (attaque)
PRE_GAP = 80       # 5 ms laisses entre cette mesure et le pic
AGC_MEMORY = 30    # blocs (3 s) de pics pris en compte par l'auto-gain
AGC_TARGET = 0.3   # niveau vise pour le son le plus fort recent
AGC_MAX = 5.0


class AudioDetector:
    def __init__(self, model_path, sample_rate=16000):
        self.model_path = model_path
        self.sample_rate = sample_rate
        self.source_id = None
        self.label = None
        self.classifier = None
        self.running = False
        self.score_threshold = 0.3
        self.tracker = ClapTracker()
        # Protege l'etat du tracker (thread audio vs thread de resultats MediaPipe)
        self.lock = threading.Lock()
        # Serialise classify_async et close() : close() pendant un classify_async
        # n'est pas sur cote natif.
        self._clf_lock = threading.Lock()
        self._timestamp_ms = 0
        self._pending = np.zeros(0, dtype=np.float32)
        self._dc = 0.0            # composante continue, suivie en douceur
        self._agc = 1.0           # auto-gain lisse (pour le classifieur seulement)
        self._last_total = 1.0    # gain total applique au bloc precedent
        self._recent_peaks = collections.deque(maxlen=AGC_MEMORY)
        self._prev_tail = np.zeros(0, dtype=np.float32)  # fin du bloc precedent (|x|)
        self._clock_end = 0.0     # horloge audio : fin du dernier bloc recu
        self._cadence = []        # instants des premiers resultats (mesure de cadence)
        self._errors_logged = 0
        self._detection_callback = None
        self._labels_callback = None
        self._sound_seen_callback = None
        self._scores_callback = None

    # --- Configuration ------------------------------------------------------

    def configure(self, source_id, label=None, detection_callback=None,
                  labels_callback=None, sound_seen_callback=None,
                  scores_callback=None):
        self.source_id = source_id
        self.label = label or source_id
        self._detection_callback = detection_callback
        self._labels_callback = labels_callback
        self._sound_seen_callback = sound_seen_callback
        # Retour en direct pour l'interface : meilleur score de chaque groupe.
        self._scores_callback = scores_callback

    def set_groups(self, groups):
        """Groupes de sons (modifiables en direct). Le seuil d'auto-decouverte
        et d'emission des labels est le minimum des seuils de groupe."""
        with self.lock:
            self.tracker.set_groups(groups, default_threshold=self.score_threshold)
            if self.tracker.min_threshold is not None:
                self.score_threshold = self.tracker.min_threshold

    @property
    def groups(self):
        with self.lock:
            return [dict(g, whitelist=dict(g['whitelist'])) for g in self.tracker.groups]

    def set_exclusions(self, labels):
        with self.lock:
            self.tracker.exclusions = set(labels or [])

    def set_params(self, **params):
        with self.lock:
            self.tracker.set_params(**params)

    def initialize(self, max_results=10, score_threshold=0.3, clap_window=1.5,
                   peak_cooldown=0.08, peak_ratio=3.0):
        """Cree le classifieur YAMNet en mode flux."""
        self.score_threshold = score_threshold
        self.tracker.set_params(window=clap_window, peak_cooldown=peak_cooldown,
                                peak_ratio=peak_ratio)
        # Pas de category_allowlist : les labels hors groupes sont remontes
        # pour l'auto-decouverte dans l'UI.
        options = audio.AudioClassifierOptions(
            base_options=python.BaseOptions(model_asset_path=self.model_path),
            running_mode=audio.RunningMode.AUDIO_STREAM,
            max_results=max_results,
            score_threshold=0,
            result_callback=self._handle_result,
        )
        self.classifier = audio.AudioClassifier.create_from_options(options)
        logging.info(f"Classificateur audio initialisé (max_results={max_results})")

    def start(self):
        # Timestamps monotones exiges par MediaPipe
        self._timestamp_ms = int(time.time() * 1000)
        self.running = True

    def stop(self):
        self.running = False
        with self._clf_lock:
            classifier, self.classifier = self.classifier, None
        if classifier:
            try:
                classifier.close()
                logging.info(f"Classificateur arrêté ({self.label})")
            except Exception as e:
                logging.error(f"Erreur lors de l'arrêt du classificateur: {e}")

    # --- Audio ----------------------------------------------------------------

    def process_audio(self, audio_data, gain=1.0):
        """Bloc audio mono 16 kHz, AVANT gain. Ne relance jamais un detecteur
        arrete.

        Les pics sont comptes sur le signal brut ; le gain de la source et
        l'auto-gain ne s'appliquent qu'a l'entree du classifieur. Amplifie et
        ecrete avant le comptage (gain RTSP par defaut x10), le bruit de fond
        d'une camera depassait 1/ratio et plus aucun pic n'etait detecte.
        """
        if not self.running:
            return
        try:
            audio_data = np.asarray(audio_data, dtype=np.float32).reshape(-1)
            if not np.isfinite(audio_data).all():
                # Un bloc corrompu empoisonnait sinon le niveau moyen du bruit.
                audio_data = np.nan_to_num(audio_data, nan=0.0, posinf=0.0, neginf=0.0)
            if audio_data.size:
                # Composante continue suivie en douceur (une soustraction par
                # bloc creait des marches a chaque frontiere de 100 ms).
                self._dc = 0.95 * self._dc + 0.05 * float(audio_data.mean())
                audio_data = audio_data - np.float32(self._dc)
            raw_peak, peak_time, pre_level = self._locate_peak(audio_data)

            with self.lock:
                self.tracker.feed_peak(raw_peak, peak_time, pre_level=pre_level, gain=gain)
            # Auto-gain (classifieur seulement) sur le son le plus fort des 3
            # dernieres secondes : il reste eleve sur un clap faible isole (le
            # bruit qui suit ne le fait plus retomber) et baisse aussitot sur
            # un son fort, qui n'est donc pas ecrete.
            self._recent_peaks.append(raw_peak * gain)
            loudest = max(self._recent_peaks)
            target = min(AGC_TARGET / loudest, AGC_MAX) if 0 < loudest < AGC_TARGET / 2 else 1.0
            self._agc = target if target < self._agc else 0.95 * self._agc + 0.05 * target
            total = gain * self._agc
            if total > self._last_total + 1e-3 and audio_data.size:
                # Remontee en rampe sur le bloc : pas de marche toutes les 100 ms.
                ramp = np.linspace(self._last_total, total, audio_data.size, dtype=np.float32)
                audio_data = np.clip(audio_data * ramp, -1.0, 1.0)
            elif abs(total - 1.0) > 1e-3:
                audio_data = np.clip(audio_data * np.float32(total), -1.0, 1.0)
            self._last_total = total

            if self._pending.size:
                audio_data = np.concatenate((self._pending, audio_data))
            n_blocks = audio_data.size // BLOCK_SAMPLES
            for i in range(n_blocks):
                self._classify(audio_data[i * BLOCK_SAMPLES:(i + 1) * BLOCK_SAMPLES])
            self._pending = audio_data[n_blocks * BLOCK_SAMPLES:].copy()
        except Exception:
            logging.exception(f"Erreur dans le traitement audio ({self.label})")

    def _locate_peak(self, x):
        """(pic, instant du pic, niveau juste avant lui) d'un bloc.

        L'instant suit une horloge audio (duree des blocs recus) et non l'heure
        de traitement : des blocs livres en rafale par ffmpeg gardent leur
        espacement reel. Elle n'est recalee (vers l'avant seulement) que si le
        flux s'est interrompu (plus de 0,5 s de retard). Les resultats de YAMNet
        sont dates sur la meme horloge (fin du dernier bloc recu).
        """
        now = time.monotonic()
        n = x.size
        dur = n / self.sample_rate
        start = max(self._clock_end, now - 0.5 - dur)
        self._clock_end = start + dur
        if not n:
            return 0.0, now, None
        mag = np.abs(x)
        idx = int(mag.argmax())
        peak = float(mag[idx])
        buf = np.concatenate((self._prev_tail, mag)) if self._prev_tail.size else mag
        j = idx + self._prev_tail.size
        lo, hi = max(0, j - PRE_GAP - PRE_WINDOW), max(0, j - PRE_GAP)
        pre = float(buf[lo:hi].max()) if hi > lo else None
        self._prev_tail = mag[-(PRE_GAP + PRE_WINDOW):].copy()
        return peak, start + idx / self.sample_rate, pre

    def _classify(self, block):
        self._timestamp_ms += int(BLOCK_SAMPLES / self.sample_rate * 1000)
        container = containers.AudioData.create_from_array(block, self.sample_rate)
        with self._clf_lock:
            if not self.running or not self.classifier:
                return
            try:
                self.classifier.classify_async(container, self._timestamp_ms)
            except Exception as e:
                # Limite : une erreur persistante produisait 10 lignes/s.
                self._errors_logged += 1
                if self._errors_logged <= 5 or self._errors_logged % 600 == 0:
                    logging.error(f"Erreur lors de la classification ({self._errors_logged}x): {e}")

    # --- Resultats (thread MediaPipe) --------------------------------------

    def _log_cadence(self):
        """Mesure une fois l'intervalle reel entre resultats YAMNet (100 ms ou
        ~1 s selon MediaPipe) : il conditionne la protection anti-redeclenchement."""
        if self._cadence is None:
            return
        self._cadence.append(time.monotonic())
        if len(self._cadence) == 31:
            gaps = [b - a for a, b in zip(self._cadence, self._cadence[1:])]
            cadence = sorted(gaps)[len(gaps) // 2]
            logging.info(f"[{self.label}] cadence YAMNet mesurée : un résultat toutes les "
                         f"{1000 * cadence:.0f} ms")
            if cadence > 0.5:
                # Un resultat par ~seconde : le pic d'un clap peut preceder de
                # plus d'1 s le resultat qui le reconnait.
                with self.lock:
                    self.tracker.peak_lookback = 1.5
            self._cadence = None

    def _handle_result(self, result, timestamp):
        try:
            self._log_cadence()
            if not result or not result.classifications:
                return
            categories = [(c.category_name, float(c.score)) for c in result.classifications[0].categories]
            with self.lock:
                exclusions = set(self.tracker.exclusions)
                threshold = self.score_threshold
                if logging.getLogger().isEnabledFor(logging.DEBUG):
                    logging.debug(f"[{self.label}] labels={[(n, round(s, 3)) for n, s in categories]}")
                events = self.tracker.on_classification(categories, self._clock_end or time.monotonic())
                group_scores = {
                    g['slug']: max((s for n, s in categories
                                    if g['whitelist'].get(n) and n not in exclusions), default=0.0)
                    for g in self.tracker.groups
                }
            if self._scores_callback:
                try:
                    self._scores_callback(group_scores)
                except Exception:
                    pass

            # Callbacks HORS verrou (E/S : socketio, MQTT, HTTP).
            if self._sound_seen_callback:
                for name, score in categories:
                    if score >= threshold and name not in exclusions:
                        try:
                            self._sound_seen_callback({'label': name, 'score': score})
                        except Exception as e:
                            logging.debug(f"sound_seen: {e}")

            if self._labels_callback:
                top = sorted((c for c in categories if c[0] not in exclusions),
                             key=lambda c: c[1], reverse=True)[:3]
                labels = [{'label': n, 'score': s} for n, s in top if s >= threshold]
                if labels:
                    try:
                        self._labels_callback(labels)
                    except Exception as e:
                        logging.error(f"Erreur callback labels ({self.label}): {e}")

            if events:
                winner = next(e for e in events if not e['ignored'])
                logging.info(f"[{self.label}] CLAP groupe={winner['group']['name']}: "
                             f"{winner['clap_count']} pic(s), score={winner['score']:.2f}")
            for ev in events:
                if not self._detection_callback:
                    break
                group = ev['group']
                try:
                    self._detection_callback({
                        'timestamp': time.time(),
                        'score': ev['score'],
                        'source_id': self.source_id,
                        'clap_count': ev['clap_count'],
                        'labels': ev['labels'],
                        'group_slug': group['slug'],
                        'group_name': group['name'],
                        'group_clap_counts': list(group.get('clap_counts', [1, 2])),
                        'ignored': ev['ignored'],
                    })
                except Exception as e:
                    logging.error(f"Erreur callback détection ({self.label}): {e}")
        except Exception:
            logging.exception("Erreur dans le traitement du résultat")
