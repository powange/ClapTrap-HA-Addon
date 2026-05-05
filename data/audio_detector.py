import numpy as np
import threading
import math
from scipy.signal import resample_poly
from mediapipe.tasks import python
from mediapipe.tasks.python import audio
from mediapipe.tasks.python.components import containers
from mediapipe.tasks.python.audio import audio_classifier
import mediapipe as mp
import time
import logging

class AudioDetector:
    def __init__(self, model_path, sample_rate=16000, buffer_duration=1.0):
        self.model_path = model_path
        self.sample_rate = sample_rate
        self.buffer_size = int(buffer_duration * sample_rate)
        self.sources = {}  # Dict pour stocker les buffers et callbacks par source
        self.source_ids = {}  # Dict pour mapper les noms de source aux IDs numériques
        self.next_source_id = 1  # Commencer à 1 pour éviter les problèmes avec 0
        self.classifier = None
        self.running = False
        self.lock = threading.Lock()
        self.last_detection_time = {}  # Dict pour stocker le dernier temps de détection par source
        self.last_timestamp_ms = {}  # Dict pour stocker le dernier timestamp par source
        self._global_timestamp_ms = 0  # Timestamp global monotone pour classify_async
        self.start_time_ms = None
        self._active_source_id = None  # Source unique pour ce detector (1 detector = 1 source)
        self._source_label = None  # Nom lisible de la source (ex: "RTSP: Bureau 2")
        self.score_threshold = 0.3
        self._result_count = 0
        self._clap_windows = {}  # source_id -> {'first_clap_time': float, 'count': int}
        self._clap_window_duration = 1.5  # durée de la fenêtre multi-clap (configurable)
        self._energy_state = {}  # source_id -> state dict
        self._peak_cooldown = 0.08  # minimum entre deux pics (secondes)
        self._peak_ratio = 3.0  # un pic doit etre 3x le niveau moyen pour compter
        self._peak_reset = 0.3  # delai apres lequel 'above' est force a False meme si l'amplitude reste haute
        self._whitelist = {}  # {label_name: True} — sons que l'utilisateur a coches comme "clap"
        self._exclusions = set()  # labels exclus globalement (prioritaire sur whitelist)
        self._sound_seen_callback = None  # callable({label, score}) pour l'auto-decouverte

    def set_whitelist(self, whitelist):
        """Met a jour la whitelist de sons (appele en direct sans restart)."""
        self._whitelist = dict(whitelist or {})

    def set_exclusions(self, labels):
        """Met a jour les exclusions globales (appele en direct sans restart)."""
        self._exclusions = set(labels or [])

    def set_sound_seen_callback(self, cb):
        self._sound_seen_callback = cb

    def initialize(self, max_results=10, score_threshold=0.3, clap_window=1.5,
                   peak_cooldown=0.08, peak_ratio=3.0, peak_reset=0.3):
        """Initialise le classificateur audio"""
        self.score_threshold = score_threshold
        self._clap_window_duration = clap_window
        self._peak_cooldown = peak_cooldown
        self._peak_ratio = peak_ratio
        self._peak_reset = peak_reset
        try:
            base_options = python.BaseOptions(model_asset_path=self.model_path)
            
            # Pas de category_allowlist : l'utilisateur definit par source les
            # labels a considerer via `_whitelist`. Les labels hors whitelist
            # sont quand meme remontes (si au-dessus du seuil) pour permettre
            # l'auto-decouverte dans l'UI.
            options = audio.AudioClassifierOptions(
                base_options=base_options,
                running_mode=audio.RunningMode.AUDIO_STREAM,
                max_results=max_results,
                score_threshold=0,
                result_callback=self._handle_result
            )
            self.classifier = audio.AudioClassifier.create_from_options(options)
            self.running = True
            logging.info(f"Classificateur audio initialisé avec succès (sample_rate: {self.sample_rate}Hz)")
            logging.info(f"Options du classificateur: max_results={max_results}, score_threshold={score_threshold}")
        except Exception as e:
            logging.error(f"Erreur lors de l'initialisation du classificateur: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            raise
        
    def add_source(self, source_id, detection_callback=None, labels_callback=None, label=None):
        """Ajoute une nouvelle source audio avec ses callbacks"""
        self._active_source_id = source_id
        self._source_label = label or source_id
        with self.lock:
            # Attribuer un ID numérique à la source
            numeric_id = self.next_source_id
            self.next_source_id += 1
            self.source_ids[source_id] = numeric_id
            
            # Ring buffer pré-alloué : buffer_size + marge pour un bloc
            ring_size = self.buffer_size + 1600
            self.sources[source_id] = {
                'ring': np.zeros(ring_size, dtype=np.float32),
                'ring_len': 0,
                'detection_callback': detection_callback,
                'labels_callback': labels_callback,
                'numeric_id': numeric_id
            }
            self.last_detection_time[source_id] = 0
            self.last_timestamp_ms[source_id] = 0
            logging.info(f"Source audio ajoutée: {source_id} (ID interne: {numeric_id})")

    def remove_source(self, source_id):
        """Supprime une source audio"""
        with self.lock:
            if source_id in self.sources:
                numeric_id = self.sources[source_id]['numeric_id']
                del self.source_ids[source_id]
                del self.sources[source_id]
                del self.last_detection_time[source_id]
                del self.last_timestamp_ms[source_id]
                logging.info(f"Source audio supprimée: {source_id} (ID interne: {numeric_id})")

    def _handle_result(self, result, timestamp):
        """Gère les résultats de classification"""
        try:
            self._result_count += 1
            if self._result_count <= 5 or self._result_count % 100 == 0:
                has_results = bool(result and result.classifications)
                logging.info(f"Classifier result #{self._result_count}: has_results={has_results}, timestamp={timestamp}")

            if not result or not result.classifications:
                return

            # Utiliser la source unique de ce detector (1 detector = 1 source)
            source_id = self._active_source_id
            if not source_id or source_id not in self.sources:
                return
            with self.lock:
                labels_callback = self.sources[source_id]['labels_callback']
                detection_callback = self.sources[source_id]['detection_callback']

            classification = result.classifications[0]

            whitelist = self._whitelist or {}
            exclusions = self._exclusions or set()

            # Scoring : somme des scores des labels coches par l'utilisateur,
            # en retirant les exclusions globales.
            score_sum = sum(
                cat.score for cat in classification.categories
                if whitelist.get(cat.category_name, False)
                and cat.category_name not in exclusions
            )

            # Auto-decouverte : emettre chaque label au-dessus du seuil pour
            # que l'UI puisse l'afficher comme "son entendu mais pas coche".
            if self._sound_seen_callback:
                for cat in classification.categories:
                    if cat.score >= self.score_threshold:
                        try:
                            self._sound_seen_callback({
                                'label': cat.category_name,
                                'score': float(cat.score),
                            })
                        except Exception:
                            pass

            all_labels = [(c.category_name, round(c.score, 3)) for c in classification.categories]
            hot_labels = [(c.category_name, round(c.score, 3)) for c in classification.categories
                          if whitelist.get(c.category_name, False) and c.score >= self.score_threshold]
            if hot_labels or self._result_count <= 10 or self._result_count % 100 == 0:
                logging.info(f"[{self._source_label}] labels={all_labels}")

            # Log du score calculé
            if score_sum > self.score_threshold * 0.2:
                logging.debug(f"Score de clap calculé pour {self._source_label}: {score_sum}")

            # Préparer les labels pour le callback (sans les exclusions globales,
            # et uniquement les labels dont le score >= le seuil de la source).
            top3_labels = sorted(
                [c for c in classification.categories if c.category_name not in exclusions],
                key=lambda x: x.score,
                reverse=True
            )[:3]
            labels_data = [
                {"label": label.category_name, "score": float(label.score)}
                for label in top3_labels
                if label.score >= self.score_threshold
            ]

            # Log pour déboguer les labels
            logging.debug(f"Labels détectés pour {self._source_label}: {labels_data}")

            # Envoyer les labels si un callback est défini
            if labels_callback and labels_data:
                try:
                    labels_callback(labels_data)
                except Exception as e:
                    logging.error(f"Erreur dans le callback des labels pour {self._source_label}: {str(e)}")

            # Vérifier si on a détecté un clap
            current_time = time.time()
            with self.lock:
                last_det = self.last_detection_time.get(source_id, 0)
                es = self._energy_state.get(source_id, {})
                peak_times = es.get('peak_times', [])

                # Le classifier a détecté un clap
                if score_sum > self.score_threshold:
                    if 'clap_detected_at' not in es or es['clap_detected_at'] == 0:
                        # Utiliser le premier pic comme début de fenêtre (pas le moment du classifier)
                        first_peak = peak_times[0] if peak_times else current_time
                        es['clap_detected_at'] = first_peak
                        es['clap_score'] = score_sum
                        es['clap_labels'] = {}

                    # Accumuler les labels qui ont contribué au clap (max score par label)
                    contributing_labels = es.setdefault('clap_labels', {})
                    for cat in classification.categories:
                        if (whitelist.get(cat.category_name, False)
                                and cat.category_name not in exclusions
                                and cat.score > 0):
                            existing = contributing_labels.get(cat.category_name, 0)
                            if cat.score > existing:
                                contributing_labels[cat.category_name] = float(cat.score)

                # Émettre le résultat si la fenêtre multi-clap est expirée
                clap_detected_at = es.get('clap_detected_at', 0)
                if clap_detected_at > 0 and (current_time - clap_detected_at) >= self._clap_window_duration:
                    # Compter tous les pics depuis le début de la fenêtre
                    recent_peaks = [t for t in peak_times if t >= clap_detected_at]
                    clap_count = max(1, len(recent_peaks))
                    clap_score = es.get('clap_score', score_sum)
                    clap_labels = sorted(
                        ({'label': name, 'score': score}
                         for name, score in es.get('clap_labels', {}).items()),
                        key=lambda x: x['score'],
                        reverse=True
                    )

                    self.last_detection_time[source_id] = current_time
                    avg = es.get('avg_level', 0)
                    logging.info(f"[{self._source_label}] CLAP: {clap_count} pic(s), score={clap_score:.2f}, fenetre={self._clap_window_duration}s")

                    if detection_callback:
                        try:
                            detection_callback({
                                'timestamp': current_time,
                                'score': float(clap_score),
                                'source_id': source_id,
                                'clap_count': clap_count,
                                'labels': clap_labels
                            })
                        except Exception as e:
                            logging.error(f"Erreur callback détection {self._source_label}: {e}")

                    # Reset après émission
                    es['clap_detected_at'] = 0
                    es['clap_score'] = 0
                    es['peak_times'] = []
                    es['clap_labels'] = {}
                
        except Exception as e:
            logging.error(f"Erreur dans le traitement du résultat: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())

    def process_audio(self, audio_data, source_id):
        """Traite les données audio pour une source spécifique"""
        try:

            if source_id not in self.sources:
                logging.warning(f"Source inconnue: {source_id}")
                return

            # Vérifier si le classificateur est actif
            if not self.running:
                logging.warning("Le classificateur n'est pas actif, démarrage...")
                self.start()
                if not self.running:
                    logging.error("Impossible de démarrer le classificateur")
                    return

            # Rééchantillonnage anti-aliasé si nécessaire
            if len(audio_data) > self.buffer_size:
                audio_data = resample_poly(audio_data, 1, 3).astype(np.float32)

            # S'assurer que les données sont 1D float32
            if audio_data.ndim > 1:
                audio_data = audio_data.flatten()
            if audio_data.dtype != np.float32:
                audio_data = audio_data.astype(np.float32)

            # Supprimer le DC offset (certains micros ont un signal non centré sur zéro)
            audio_data = (audio_data - np.mean(audio_data)).astype(np.float32)

            # Normalisation adaptative : n'amplifie que les blocs avec un vrai signal
            # (peak significativement au-dessus du bruit de fond moyen)
            raw_peak = float(np.max(np.abs(audio_data)))
            if source_id not in self._energy_state:
                self._energy_state[source_id] = {'above': False, 'last_peak_time': 0, 'peak_times': [], 'avg_level': 0.001}
            es = self._energy_state[source_id]
            noise_floor = es.get('avg_level', 0.001)
            # Amplifier doucement les signaux faibles (eviter le clipping)
            if raw_peak > noise_floor * 2 and raw_peak < 0.05 and raw_peak > 0.003:
                auto_gain = min(0.15 / raw_peak, 5.0)  # max 5x, cible 0.15
                audio_data = (audio_data * auto_gain).astype(np.float32)

            # Log des statistiques audio (guard pour éviter le calcul inutile)
            if logging.getLogger().isEnabledFor(logging.DEBUG) and len(audio_data) > 0:
                logging.debug(f"Audio stats ({self._source_label}) - min: {np.min(audio_data):.4f}, max: {np.max(audio_data):.4f}, mean: {np.mean(audio_data):.4f}, std: {np.std(audio_data):.4f}")

            # Compter les pics d'énergie (pour le multi-clap)
            peak = float(np.max(np.abs(audio_data)))
            with self.lock:
                if source_id not in self._energy_state:
                    self._energy_state[source_id] = {
                        'above': False, 'last_peak_time': 0, 'peak_times': [],
                        'avg_level': 0.001  # niveau moyen du bruit de fond
                    }
                es = self._energy_state[source_id]
                current_time = time.time()

                # Mettre à jour le niveau moyen (moyenne glissante lente, exclure les pics)
                # Utiliser raw_peak (pré-amplification) pour ne pas biaiser avec auto_gain
                if not es.get('above', False):
                    es['avg_level'] = es['avg_level'] * 0.995 + raw_peak * 0.005

                # Seuil dynamique : pic doit être 3x le niveau moyen (minimum 0.05)
                dynamic_threshold = max(0.05, es['avg_level'] * self._peak_ratio)

                # Nettoyer les pics anciens (garder fenêtre + marge)
                max_age = max(2.0, self._clap_window_duration + 1.0)
                es['peak_times'] = [t for t in es['peak_times'] if (current_time - t) < max_age]

                if peak > dynamic_threshold and not es['above']:
                    # Front montant : nouveau pic détecté
                    if (current_time - es['last_peak_time']) > self._peak_cooldown:
                        es['last_peak_time'] = current_time
                        es['peak_times'].append(current_time)
                        logging.debug(f"[{self._source_label}] Pic #{len(es['peak_times'])}: peak={peak:.4f}, seuil={dynamic_threshold:.4f}, avg={es['avg_level']:.4f}")
                    es['above'] = True
                elif peak < dynamic_threshold * 0.6:
                    # Seuil de retour plus souple (60% du seuil au lieu de 30%)
                    es['above'] = False
                elif es['above'] and (current_time - es['last_peak_time']) > self._peak_reset:
                    # Reset temporel : si le pic dure trop longtemps, on le
                    # considere fini pour pouvoir compter les claps suivants
                    # meme si l'amplitude reste au-dessus du seuil.
                    es['above'] = False

            # Écrire dans le ring buffer pré-alloué (zéro allocation)
            src = self.sources[source_id]
            ring = src['ring']
            rlen = src['ring_len']
            n = len(audio_data)
            if rlen + n > len(ring):
                # Débordement : ne garder que les dernières données
                if n >= len(ring):
                    ring[:] = audio_data[-len(ring):]
                    src['ring_len'] = len(ring)
                    return  # Skip classifier for this oversized block
                keep = len(ring) - n
                ring[:keep] = ring[rlen - keep:rlen]
                rlen = keep
            ring[rlen:rlen + n] = audio_data
            rlen += n
            src['ring_len'] = rlen

            # Traiter avec le classificateur
            if self.running and self.classifier and self.start_time_ms is not None:
                block_size = 1600
                pos = 0
                while pos + block_size <= rlen:
                    block = ring[pos:pos + block_size]
                    pos += block_size

                    # Timestamp monotone croissant (requis par MediaPipe)
                    block_duration_ms = int((block_size / self.sample_rate) * 1000)
                    self._global_timestamp_ms += block_duration_ms
                    next_timestamp = self._global_timestamp_ms

                    # Classifier le bloc
                    try:
                        audio_data_container = containers.AudioData.create_from_array(block, self.sample_rate)
                        self.classifier.classify_async(audio_data_container, next_timestamp)
                    except Exception as e:
                        logging.error(f"Erreur lors de la classification: {str(e)}")

                # Compacter le ring buffer (déplacer les données restantes au début)
                remaining = rlen - pos
                if remaining > 0:
                    ring[:remaining] = ring[pos:rlen]
                src['ring_len'] = remaining
            
        except Exception as e:
            logging.error(f"Erreur dans le traitement audio: {e}")
            import traceback
            logging.error(traceback.format_exc())

    def start(self):
        """Démarre la détection"""
        if not self.classifier:
            self.initialize()
        
        # Réinitialiser les timestamps
        self.start_time_ms = int(time.time() * 1000)
        self._global_timestamp_ms = self.start_time_ms
        for source_id in self.sources:
            self.last_timestamp_ms[source_id] = self.start_time_ms
        
        # Démarrer le task runner de MediaPipe
        if self.classifier:
            try:
                # Créer un conteneur audio vide pour démarrer le stream
                empty_data = np.zeros(1600, dtype=np.float32)
                audio_data = containers.AudioData.create_from_array(
                    empty_data,
                    self.sample_rate
                )
                # Démarrer le stream avec le timestamp initial
                self.classifier.classify_async(audio_data, self.start_time_ms)
                self.running = True
                logging.info("Task runner MediaPipe démarré avec succès")
            except Exception as e:
                logging.error(f"Erreur lors du démarrage du task runner: {e}")
                return False
        
        self.running = True
        return True

    def stop(self):
        """Arrête le classificateur"""
        self.running = False
        if self.classifier:
            try:
                self.classifier.close()
                self.classifier = None
                logging.info("Classificateur audio arrêté")
            except Exception as e:
                logging.error(f"Erreur lors de l'arrêt du classificateur: {e}")
                
    def __del__(self):
        """Destructeur pour s'assurer que les classificateurs sont bien arrêtés"""
        self.stop()
