"""Detecteur d'une source audio : pretraitement + YAMNet (MediaPipe) + logique
de claps (clap_logic.ClapTracker). Un detecteur = une source."""

import logging
import threading
import time

import numpy as np
from mediapipe.tasks import python
from mediapipe.tasks.python import audio
from mediapipe.tasks.python.components import containers

from clap_logic import ClapTracker

BLOCK_SAMPLES = 1600  # 100 ms a 16 kHz


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
        self._result_count = 0
        self._pending = np.zeros(0, dtype=np.float32)
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
                   peak_cooldown=0.08, peak_ratio=3.0, peak_reset=0.3):
        """Cree le classifieur YAMNet en mode flux."""
        self.score_threshold = score_threshold
        self.tracker.set_params(window=clap_window, peak_cooldown=peak_cooldown,
                                peak_ratio=peak_ratio, peak_reset=peak_reset)
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
        if not self.classifier:
            self.initialize()
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

    def process_audio(self, audio_data, source_id=None):
        """Bloc audio mono 16 kHz. Ne relance jamais un detecteur arrete."""
        if not self.running:
            return
        try:
            audio_data = np.asarray(audio_data, dtype=np.float32).reshape(-1)
            if not np.isfinite(audio_data).all():
                # Un bloc corrompu empoisonnait sinon le niveau moyen du bruit.
                audio_data = np.nan_to_num(audio_data, nan=0.0, posinf=0.0, neginf=0.0)
            audio_data = audio_data - np.float32(audio_data.mean())  # DC offset
            raw_peak = float(np.abs(audio_data).max()) if audio_data.size else 0.0

            with self.lock:
                noise_floor = self.tracker.avg_level
                self.tracker.feed_peak(raw_peak, time.time())
            # Amplifier doucement les signaux faibles pour le classifieur
            # uniquement (les pics sont comptes sur le signal brut).
            if 0.003 < raw_peak < 0.05 and raw_peak > noise_floor * 2:
                audio_data = audio_data * np.float32(min(0.15 / raw_peak, 5.0))

            if self._pending.size:
                audio_data = np.concatenate((self._pending, audio_data))
            n_blocks = audio_data.size // BLOCK_SAMPLES
            for i in range(n_blocks):
                self._classify(audio_data[i * BLOCK_SAMPLES:(i + 1) * BLOCK_SAMPLES])
            self._pending = audio_data[n_blocks * BLOCK_SAMPLES:].copy()
        except Exception:
            logging.exception(f"Erreur dans le traitement audio ({self.label})")

    def _classify(self, block):
        self._timestamp_ms += int(BLOCK_SAMPLES / self.sample_rate * 1000)
        container = containers.AudioData.create_from_array(block, self.sample_rate)
        with self._clf_lock:
            if not self.running or not self.classifier:
                return
            try:
                self.classifier.classify_async(container, self._timestamp_ms)
            except Exception as e:
                logging.error(f"Erreur lors de la classification: {e}")

    # --- Resultats (thread MediaPipe) --------------------------------------

    def _handle_result(self, result, timestamp):
        try:
            self._result_count += 1
            if not result or not result.classifications:
                return
            categories = [(c.category_name, float(c.score)) for c in result.classifications[0].categories]
            with self.lock:
                exclusions = set(self.tracker.exclusions)
                threshold = self.score_threshold
                if logging.getLogger().isEnabledFor(logging.DEBUG):
                    logging.debug(f"[{self.label}] labels={[(n, round(s, 3)) for n, s in categories]}")
                events = self.tracker.on_classification(categories, time.time())
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
                    if score >= threshold:
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
