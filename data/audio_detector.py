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

    def initialize(self, max_results=10, score_threshold=0.3, clap_window=1.5,
                   peak_cooldown=0.08, peak_ratio=3.0):
        """Initialise le classificateur audio"""
        self.score_threshold = score_threshold
        self._clap_window_duration = clap_window
        self._peak_cooldown = peak_cooldown
        self._peak_ratio = peak_ratio
        try:
            base_options = python.BaseOptions(model_asset_path=self.model_path)
            
            # Créer un seul classificateur en mode stream
            # score_threshold bas pour MediaPipe (laisser passer tous les labels)
            # Le seuil utilisateur est appliqué dans _handle_result sur notre scoring custom
            options = audio.AudioClassifierOptions(
                base_options=base_options,
                running_mode=audio.RunningMode.AUDIO_STREAM,
                max_results=max_results,
                score_threshold=0.05,
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

            # Scoring pondéré pour la détection de clap
            CLAP_WEIGHTS = {
                "Hands": 0.8,
                "Clapping": 1.0,
                "Slap, smack": 0.6,
                "Whack, thwack": 0.5,
                "Knock": 0.3,
                "Cap gun": 0.3,
                "Snap": 0.3,
                "Crack": 0.2,
            }
            NOISE_WEIGHTS = {"Finger snapping": 0.5, "Writing": 0.3, "Typing": 0.2}

            # Log top results + labels clap
            top = [(c.category_name, round(c.score, 3)) for c in classification.categories[:5]]
            clap_labels = [(c.category_name, round(c.score, 3)) for c in classification.categories
                           if c.category_name in CLAP_WEIGHTS]
            if clap_labels or self._result_count <= 10 or self._result_count % 100 == 0:
                logging.info(f"[{self._source_label}] top={top} clap={clap_labels}")
            score_sum = sum(
                category.score * CLAP_WEIGHTS[category.category_name]
                for category in classification.categories
                if category.category_name in CLAP_WEIGHTS
            )
            score_sum -= sum(
                category.score * NOISE_WEIGHTS[category.category_name]
                for category in classification.categories
                if category.category_name in NOISE_WEIGHTS
            )
            score_sum = max(0.0, score_sum)

            # Log du score calculé
            if score_sum > 0.1:
                logging.debug(f"Score de clap calculé pour {self._source_label}: {score_sum}")

            # Préparer les labels pour le callback
            top3_labels = sorted(
                classification.categories,
                key=lambda x: x.score,
                reverse=True
            )[:3]
            labels_data = [
                {"label": label.category_name, "score": float(label.score)}
                for label in top3_labels
                if label.score > 0.1
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
            # Deux modes : score au-dessus du seuil, OU pic d'énergie récent + score minimal
            current_time = time.time()
            last_det = self.last_detection_time.get(source_id, 0)
            es = self._energy_state.get(source_id, {})
            recent_peaks = [t for t in es.get('peak_times', []) if (current_time - t) < 2.0]
            has_recent_peak = len(recent_peaks) > 0

            # Detection hybride :
            # 1. Score classifier au-dessus du seuil (detection classique)
            # 2. Pic d'énergie récent + classifier ne voit pas "Silence" pur (detection par pic)
            top_label = classification.categories[0].category_name if classification.categories else "Silence"
            top_score = classification.categories[0].score if classification.categories else 1.0
            is_silence = (top_label == "Silence" and top_score > 0.5)

            clap_detected = False
            if score_sum > self.score_threshold and (current_time - last_det) > 0.3:
                clap_detected = True
            elif has_recent_peak and not is_silence and (current_time - last_det) > 0.3:
                # Un pic d'énergie a été détecté ET le classifier voit autre chose que du silence
                clap_detected = True
                logging.info(f"[{self._source_label}] Detection par pic d'énergie (top: {top_label}={top_score:.2f}, score_clap={score_sum:.3f})")

            if clap_detected:
                self.last_detection_time[source_id] = current_time

                clap_count = max(1, len(recent_peaks))

                avg = es.get('avg_level', 0)
                logging.info(f"[{self._source_label}] CLAP: {clap_count} pic(s), score={score_sum:.2f}, avg_level={avg:.4f}")

                if detection_callback:
                    try:
                        detection_callback({
                            'timestamp': current_time,
                            'score': float(score_sum),
                            'source_id': source_id,
                            'clap_count': clap_count
                        })
                    except Exception as e:
                        logging.error(f"Erreur callback détection {self._source_label}: {e}")

                # Reset les pics après émission
                if source_id in self._energy_state:
                    self._energy_state[source_id]['peak_times'] = []
                
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

            # Normalisation adaptative : n'amplifie que les blocs avec un vrai signal
            # (peak significativement au-dessus du bruit de fond moyen)
            peak = float(np.max(np.abs(audio_data)))
            if source_id not in self._energy_state:
                self._energy_state[source_id] = {'above': False, 'last_peak_time': 0, 'peak_times': [], 'avg_level': 0.001}
            es = self._energy_state[source_id]
            noise_floor = es.get('avg_level', 0.001)
            # Amplifier doucement les signaux faibles (eviter le clipping)
            if peak > noise_floor * 2 and peak < 0.05 and peak > 0.003:
                auto_gain = min(0.15 / peak, 5.0)  # max 5x, cible 0.15
                audio_data = (audio_data * auto_gain).astype(np.float32)

            # Log des statistiques audio (guard pour éviter le calcul inutile)
            if logging.getLogger().isEnabledFor(logging.DEBUG) and len(audio_data) > 0:
                logging.debug(f"Audio stats ({self._source_label}) - min: {np.min(audio_data):.4f}, max: {np.max(audio_data):.4f}, mean: {np.mean(audio_data):.4f}, std: {np.std(audio_data):.4f}")

            # Compter les pics d'énergie (pour le multi-clap)
            peak = float(np.max(np.abs(audio_data)))
            if source_id not in self._energy_state:
                self._energy_state[source_id] = {
                    'above': False, 'last_peak_time': 0, 'peak_times': [],
                    'avg_level': 0.001  # niveau moyen du bruit de fond
                }
            es = self._energy_state[source_id]
            current_time = time.time()

            # Mettre à jour le niveau moyen (moyenne glissante lente, exclure les pics)
            if not es.get('above', False):
                es['avg_level'] = es['avg_level'] * 0.995 + peak * 0.005

            # Seuil dynamique : pic doit être 3x le niveau moyen (minimum 0.008)
            dynamic_threshold = max(0.008, es['avg_level'] * self._peak_ratio)

            # Nettoyer les pics de plus de 2 secondes
            es['peak_times'] = [t for t in es['peak_times'] if (current_time - t) < 2.0]

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

            # Écrire dans le ring buffer pré-alloué (zéro allocation)
            src = self.sources[source_id]
            ring = src['ring']
            rlen = src['ring_len']
            n = len(audio_data)
            if rlen + n > len(ring):
                # Débordement : ne garder que les dernières données
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
