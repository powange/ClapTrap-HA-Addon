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
        self.start_time_ms = None
        self._timestamp_to_source = {}  # Mappe timestamp → source_id (thread-safe via self.lock)

    def initialize(self, max_results=5, score_threshold=0.3):
        """Initialise le classificateur audio"""
        try:
            base_options = python.BaseOptions(model_asset_path=self.model_path)
            
            # Créer un seul classificateur en mode stream
            options = audio.AudioClassifierOptions(
                base_options=base_options,
                running_mode=audio.RunningMode.AUDIO_STREAM,
                max_results=max_results,
                score_threshold=score_threshold,
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
        
    def add_source(self, source_id, detection_callback=None, labels_callback=None):
        """Ajoute une nouvelle source audio avec ses callbacks"""
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

    _result_count = 0

    def _handle_result(self, result, timestamp):
        """Gère les résultats de classification"""
        try:
            AudioDetector._result_count += 1
            if AudioDetector._result_count <= 5 or AudioDetector._result_count % 100 == 0:
                has_results = bool(result and result.classifications)
                logging.info(f"Classifier result #{AudioDetector._result_count}: has_results={has_results}, timestamp={timestamp}")

            if not result or not result.classifications:
                return

            # Retrouver la source et ses callbacks de manière thread-safe
            with self.lock:
                source_id = self._timestamp_to_source.pop(timestamp, None)
                if not source_id or source_id not in self.sources:
                    if AudioDetector._result_count <= 10:
                        logging.warning(f"Result #{AudioDetector._result_count}: source_id introuvable pour timestamp {timestamp}")
                    return
                labels_callback = self.sources[source_id]['labels_callback']
                detection_callback = self.sources[source_id]['detection_callback']

            classification = result.classifications[0]

            # Log top results periodiquement
            if AudioDetector._result_count <= 10 or AudioDetector._result_count % 100 == 0:
                top = [(c.category_name, round(c.score, 3)) for c in classification.categories[:5]]
                logging.info(f"Classifier top labels: {top}")

            # Calculer le score pour la détection de clap
            score_sum = sum(
                category.score
                for category in classification.categories
                if category.category_name in ["Hands", "Clapping", "Cap gun"]
            )
            score_sum -= sum(
                category.score
                for category in classification.categories
                if category.category_name == "Finger snapping"
            )

            # Log du score calculé
            if score_sum > 0.1:
                logging.debug(f"Score de clap calculé pour source {source_id}: {score_sum}")

            # Préparer les labels pour le callback
            top3_labels = sorted(
                classification.categories,
                key=lambda x: x.score,
                reverse=True
            )[:3]
            labels_data = [
                {"label": label.category_name, "score": float(label.score)}
                for label in top3_labels
                if label.score > 0.5
            ]

            # Log pour déboguer les labels
            logging.debug(f"Labels détectés pour source {source_id}: {labels_data}")

            # Envoyer les labels si un callback est défini
            if labels_callback and labels_data:
                try:
                    labels_callback(labels_data)
                except Exception as e:
                    logging.error(f"Erreur dans le callback des labels pour source {source_id}: {str(e)}")

            # Vérifier si on a détecté un clap
            current_time = time.time()
            if score_sum > 0.3 and (current_time - self.last_detection_time.get(source_id, 0)) > 1.0:
                if detection_callback:
                    try:
                        detection_callback({
                            'timestamp': current_time,
                            'score': float(score_sum),
                            'source_id': source_id
                        })
                    except Exception as e:
                        logging.error(f"Erreur dans le callback de détection pour source {source_id}: {str(e)}")
                self.last_detection_time[source_id] = current_time
                
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

            # S'assurer que les données sont en float32
            if audio_data.dtype != np.float32:
                audio_data = audio_data.astype(np.float32)

            # Log des statistiques audio (guard pour éviter le calcul inutile)
            if logging.getLogger().isEnabledFor(logging.DEBUG) and len(audio_data) > 0:
                logging.debug(f"Audio stats (source {source_id}) - min: {np.min(audio_data):.4f}, max: {np.max(audio_data):.4f}, mean: {np.mean(audio_data):.4f}, std: {np.std(audio_data):.4f}")

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

                    # Calculer le prochain timestamp
                    block_duration_ms = int((block_size / self.sample_rate) * 1000)
                    next_timestamp = max(
                        self.last_timestamp_ms.get(source_id, 0) + block_duration_ms,
                        int(time.time() * 1000)
                    )
                    self.last_timestamp_ms[source_id] = next_timestamp

                    # Enregistrer le mapping timestamp → source pour le callback
                    with self.lock:
                        self._timestamp_to_source[next_timestamp] = source_id
                        # Éviction des entrées périmées (> 5 secondes)
                        stale = [ts for ts in self._timestamp_to_source if ts < next_timestamp - 5000]
                        for ts in stale:
                            del self._timestamp_to_source[ts]

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
