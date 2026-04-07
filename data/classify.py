import time
import requests
import ffmpeg
import logging
import numpy as np
import sounddevice as sd
from mediapipe.tasks import python
from mediapipe.tasks.python.components import containers
from mediapipe.tasks.python import audio
from flask_socketio import SocketIO
import json
import warnings
import wave
import os
import collections
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from vban_manager import get_vban_detector
from audio_detector import AudioDetector
from settings_manager import load_settings

# Pool de threads pour les webhooks (non-bloquant)
_webhook_executor = ThreadPoolExecutor(max_workers=2)

# Configuration du logging en DEBUG
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

warnings.filterwarnings("ignore", category=UserWarning, module="google.protobuf.symbol_database")

# Variables globales
detection_running = False
_detection_lock = threading.Lock()
classifier = None
record = None
model = "yamnet.tflite"
output_file = "recorded_audio.wav"
current_audio_source = None
_socketio = None  # Renamed to _socketio to avoid conflict with parameter

def reload_settings():
    """Recharge les paramètres depuis settings_manager (avec cache)."""
    return load_settings()

def save_audio_to_wav(audio_data, sample_rate, filename):
    if not audio_data.size:
        logging.warning("No audio data to save.")
        return
    try:
        with wave.open(filename, 'wb') as wf:
            wf.setnchannels(1)  # Mono
            wf.setsampwidth(2)  # 2 bytes per sample
            wf.setframerate(sample_rate)
            wf.writeframes(audio_data.tobytes())
        logging.info(f"Audio saved to {filename}")
    except Exception as e:
        logging.error(f"Failed to save audio to {filename}: {e}")

def read_audio_from_rtsp(rtsp_url, buffer_size):
    """Lit un flux RTSP audio en continu sans buffer fichier"""
    try:
        # Configuration du processus ffmpeg pour lire le flux RTSP
        process = (
            ffmpeg
            .input(rtsp_url)
            .output('pipe:', 
                   format='f32le',  # Format PCM 32-bit float
                   acodec='pcm_f32le', 
                   ac=1,  # Mono
                   ar='16000',
                   buffer_size='64k'  # Réduire la taille du buffer
            )
            .run_async(pipe_stdout=True, pipe_stderr=True)
        )

        while True:
            # Lecture des données audio par blocs
            in_bytes = process.stdout.read(buffer_size * 4)  # 4 bytes par sample float32
            if not in_bytes:
                break
                
            # Conversion en numpy array
            audio_chunk = np.frombuffer(in_bytes, np.float32)
            
            if len(audio_chunk) > 0:
                yield audio_chunk.reshape(-1, 1)
            
    except Exception as e:
        logging.error(f"Erreur lors de la lecture RTSP: {e}")
        yield None
    finally:
        if process:
            process.kill()

def start_detection(
    model,
    max_results,
    score_threshold: float,
    overlapping_factor,
    socketio: SocketIO,
    webhook_url: str,
    delay: float,
    audio_source: str,
    rtsp_url: str = None,
):
    global detection_running, classifier, record, current_audio_source, _socketio

    try:
        # Validation des paramètres AVANT de toucher à l'état global
        if (overlapping_factor <= 0) or (overlapping_factor >= 1.0):
            raise ValueError("Overlapping factor must be between 0 and 1.")

        if (score_threshold < 0) or (score_threshold > 1.0):
            raise ValueError("Score threshold must be between (inclusive) 0 et 1.")

        # Check-and-set atomique avec lock
        with _detection_lock:
            if detection_running:
                return False
            detection_running = True

        # Utiliser l'audio_source déjà résolu par start_detection_route
        # (ne pas recharger depuis le disque pour éviter les incohérences)
        logging.info(f"Démarrage détection avec audio_source={audio_source}")
        current_audio_source = audio_source
        _socketio = socketio  # Store the socketio instance globally

        # Démarrer la détection dans un thread séparé
        detection_thread = threading.Thread(target=run_detection, args=(
            model,
            max_results,
            score_threshold,
            overlapping_factor,
            socketio,
            webhook_url,
            delay,
            audio_source,
            rtsp_url
        ))
        detection_thread.daemon = True
        detection_thread.start()
        
        return True
        
    except Exception as e:
        logging.error(f"Erreur pendant le démarrage de la détection: {e}")
        with _detection_lock:
            detection_running = False
        return False

def run_detection(model, max_results, score_threshold, overlapping_factor, socketio, webhook_url, delay, audio_source, rtsp_url):
    """Fonction qui exécute la détection dans un thread séparé"""
    try:
        # Initialiser le détecteur audio
        detector = AudioDetector(model, sample_rate=16000, buffer_duration=1.0)
        detector.initialize()
        
        def create_detection_callback(source_name, webhook_url=None):
            def handle_detection(detection_data):
                try:
                    logging.info(f"CLAP détecté sur {source_name} avec score {detection_data['score']}")
                    if socketio:
                        socketio.emit('clap', {
                            'source_id': source_name,
                            'timestamp': detection_data['timestamp'],
                            'score': detection_data['score']
                        })
                    
                    # Webhook non-bloquant via thread pool
                    if webhook_url:
                        logging.info(f"Envoi webhook pour {source_name} vers {webhook_url}")
                        _webhook_executor.submit(requests.post, webhook_url, timeout=5)
                except Exception as e:
                    logging.error(f"Erreur lors de l'envoi de l'événement clap pour {source_name}: {str(e)}")
            return handle_detection
        
        def create_labels_callback(source_name):
            def handle_labels(labels):
                logging.debug(f"Labels détectés sur {source_name}: {labels}")
                if socketio:
                    socketio.emit("labels", {"source": source_name, "detected": labels})
            return handle_labels
        
        # Vérifier si une source audio est configurée
        if not audio_source:
            logging.error("Aucune source audio n'est configurée ou active")
            return
            
        # Initialiser la source audio en fonction du paramètre audio_source
        if audio_source.startswith("rtsp"):
            if not rtsp_url:
                raise ValueError("RTSP URL must be provided for RTSP audio source.")
                
            source_id = f"rtsp_{rtsp_url}"
            
            # Récupérer le webhook_url depuis les paramètres RTSP
            settings = reload_settings()
            rtsp_webhook_url = None
            if settings and 'rtsp_sources' in settings:
                for source in settings['rtsp_sources']:
                    if source.get('url') == rtsp_url and source.get('enabled', True):
                        rtsp_webhook_url = source.get('webhook_url')
                        break
            
            # Utiliser le webhook spécifique à la source RTSP s'il existe, sinon utiliser celui par défaut
            webhook_url_to_use = rtsp_webhook_url or webhook_url
            
            detector.add_source(
                source_id=source_id,
                detection_callback=create_detection_callback(source_id, webhook_url_to_use),
                labels_callback=create_labels_callback(source_id)
            )
            
            # Démarrer la détection
            detector.start()
            logging.info(f"Détection démarrée pour la source RTSP {source_id}")
            
            reconnect_delay = 1
            while detection_running:
                try:
                    rtsp_reader = read_audio_from_rtsp(rtsp_url, int(16000 * 0.1))
                    for audio_data in rtsp_reader:
                        if not detection_running:
                            break
                        if audio_data is not None:
                            detector.process_audio(audio_data, source_id)
                    # Le flux s'est terminé normalement, tenter une reconnexion
                    if detection_running:
                        logging.warning(f"Flux RTSP {rtsp_url} interrompu, reconnexion dans {reconnect_delay}s...")
                        time.sleep(reconnect_delay)
                        reconnect_delay = min(reconnect_delay * 2, 30)  # Backoff exponentiel, max 30s
                except Exception as e:
                    if detection_running:
                        logging.error(f"Erreur RTSP {rtsp_url}: {e}, reconnexion dans {reconnect_delay}s...")
                        time.sleep(reconnect_delay)
                        reconnect_delay = min(reconnect_delay * 2, 30)
                
        elif audio_source.startswith("vban://"):
            vban_ip = audio_source.replace("vban://", "")
            source_id = f"vban_{vban_ip}"
            
            # Récupérer le webhook_url depuis les paramètres VBAN
            settings = reload_settings()
            vban_webhook_url = None
            if settings and 'saved_vban_sources' in settings:
                for source in settings['saved_vban_sources']:
                    if source.get('ip') == vban_ip and source.get('enabled', True):
                        vban_webhook_url = source.get('webhook_url')
                        break
            
            # Utiliser le webhook spécifique à la source VBAN s'il existe, sinon utiliser celui par défaut
            webhook_url_to_use = vban_webhook_url or webhook_url
            
            detector.add_source(
                source_id=source_id,
                detection_callback=create_detection_callback(source_id, webhook_url_to_use),
                labels_callback=create_labels_callback(source_id)
            )
            
            # Démarrer la détection
            detector.start()
            logging.info(f"Détection démarrée pour la source VBAN {source_id} avec webhook {webhook_url_to_use}")
            
            vban_detector = get_vban_detector()
            
            def audio_callback(audio_data, timestamp):
                if not detection_running:
                    return
                    
                active_sources = vban_detector.get_active_sources()
                if vban_ip not in active_sources:
                    return
                    
                detector.process_audio(audio_data, source_id)
            
            vban_detector.set_audio_callback(audio_callback)
            
            # Maintenir le thread en vie tant que la détection est active
            while detection_running:
                time.sleep(0.1)  # Éviter de surcharger le CPU
                
                # Vérifier périodiquement si la source est toujours active
                active_sources = vban_detector.get_active_sources()
                if vban_ip not in active_sources:
                    logging.warning(f"Source VBAN {vban_ip} non trouvée")
                    time.sleep(1)  # Attendre un peu plus longtemps avant la prochaine vérification
                    
        else:  # Microphone
            # Récupérer les paramètres du périphérique
            settings = reload_settings()
            device_name = settings.get('microphone', {}).get('audio_source', 'default')
            pulse_name = settings.get('microphone', {}).get('pulse_name', '')
            saved_index = int(settings.get('microphone', {}).get('device_index', 0))

            # Résoudre le pulse_name depuis l'API Supervisor si absent
            if not pulse_name and device_name and device_name != 'default':
                try:
                    from app import get_audio_input_devices
                    devices = get_audio_input_devices()
                    for dev in devices:
                        if dev.get('name') == device_name and dev.get('pulse_name'):
                            pulse_name = dev['pulse_name']
                            logging.info(f"pulse_name résolu depuis l'API Supervisor: {pulse_name}")
                            break
                except Exception as e:
                    logging.warning(f"Impossible de résoudre pulse_name: {e}")

            # Régler le volume PulseAudio
            if pulse_name:
                try:
                    import subprocess
                    mic_volume = settings.get('microphone', {}).get('volume', 100)
                    subprocess.run(['pactl', 'set-source-volume', pulse_name, f'{mic_volume}%'],
                                   capture_output=True, text=True, timeout=5)
                    logging.info(f"Volume PulseAudio mis à {mic_volume}% pour {pulse_name}")
                except Exception as e:
                    logging.warning(f"Impossible de régler le volume PulseAudio: {e}")
            else:
                logging.warning(f"Pas de pulse_name pour '{device_name}'")

            source_id = f"mic_{saved_index}"

            # Récupérer le webhook_url depuis les paramètres du microphone
            microphone_webhook_url = settings.get('microphone', {}).get('webhook_url')
            webhook_url_to_use = microphone_webhook_url or webhook_url

            detector.add_source(
                source_id=source_id,
                detection_callback=create_detection_callback(source_id, webhook_url_to_use),
                labels_callback=create_labels_callback(source_id)
            )

            # Démarrer la détection
            detector.start()
            logging.info(f"Détection démarrée pour la source microphone {source_id}")

            # Utiliser parecord pour capturer l'audio (bypass sounddevice/PortAudio)
            import subprocess
            cmd = ['parecord', '--format=float32le', '--rate=16000', '--channels=1', '--raw']
            if pulse_name:
                cmd.append(f'--device={pulse_name}')
            logging.info(f"Lancement capture audio: {' '.join(cmd)}")
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            block_size = 1600  # 100ms à 16kHz
            bytes_per_block = block_size * 4  # float32 = 4 bytes
            read_count = 0

            try:
                while detection_running:
                    data = proc.stdout.read(bytes_per_block)
                    if not data:
                        stderr = proc.stderr.read().decode(errors='replace')
                        logging.error(f"parecord a cessé de produire des données: {stderr}")
                        break
                    samples = np.frombuffer(data, dtype=np.float32)
                    read_count += 1
                    if read_count <= 5 or read_count % 100 == 0:
                        peak = float(np.max(np.abs(samples)))
                        logging.info(f"Détection micro #{read_count}: {len(samples)} samples, peak={peak:.6f}, running={detector.running}, start_time_ms={detector.start_time_ms}")
                    detector.process_audio(samples, source_id)
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except Exception:
                    proc.kill()
                    
        detector.stop()
        return True
        
    except Exception as e:
        logging.error(f"Erreur dans run_detection: {str(e)}")
        return False

def stop_detection():
    """Arrête la détection"""
    global detection_running, classifier, record, current_audio_source, _socketio

    try:
        with _detection_lock:
            detection_running = False
        
        # Notify clients that detection has stopped
        if _socketio:
            _socketio.emit("detection_status", {"status": "stopped"})
        
        if record:
            record.stop()
            record.close()
            record = None
            
        if classifier:
            classifier.close()
            classifier = None

        current_audio_source = None  # Réinitialisation de la source audio
        
        return True  # Retourner True si tout s'est bien passé
        
    except Exception as e:
        logging.error(f"Erreur lors de l'arrêt de la détection: {e}")
        return False  # Retourner False en cas d'erreur

def is_running():
    with _detection_lock:
        return detection_running

# Ajout d'une commande simple pour démarrer et arrêter la détection pour les tests
if __name__ == "__main__":
    try:
        socketio = SocketIO()
        start_detection(
            model=model,
            max_results=5,
            score_threshold=0.5,
            overlapping_factor=0.8,
            socketio=socketio,
            webhook_url="http://example.com/webhook",
            delay=2.0,
            audio_source=audio_source,
            rtsp_url=rtsp_url,
        )
    except KeyboardInterrupt:
        logging.info("Detection stopped by user.")
        stop_detection()
