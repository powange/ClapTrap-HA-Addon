import ipaddress
import socket
import struct
import time
from collections import defaultdict
import numpy as np
import sounddevice as sd
import math
from scipy.signal import resample_poly
import collections
import threading
import logging
from settings_manager import load_settings as _load_settings_from_manager

class VBANDetector:
    def __init__(self, port=6980):
        self.port = port
        self.sources = defaultdict(lambda: {'last_seen': 0, 'name': '', 'sample_rate': 0, 'channels': 0})
        self.running = False
        self._socket = None
        self.audio_callback = None  # legacy single callback (compat)
        self.source_callback = None
        self.target_sample_rate = 16000  # Taux d'échantillonnage cible

        # Ring buffer partage (legacy, utilise quand audio_callback est set)
        self._buf = np.zeros(self.target_sample_rate + 4800, dtype=np.float32)
        self._buf_len = 0

        # Per-IP ring buffers + callbacks pour multi-source
        self._per_ip = {}  # ip -> {'buf': np.array, 'buf_len': int, 'callback': callable}

        self.last_timestamp = 0
        self.stream = None
        self._lock = threading.Lock()  # Verrou pour la thread-safety
        self._settings_lock = threading.Lock()  # Verrou pour les paramètres
        self._settings_cache = None
        self._last_settings_load = 0
        self._settings_cache_duration = 5  # Durée du cache en secondes
        self._joined_multicast_groups = set()  # IPs multicast deja rejointes
        self._last_mcast_sync = 0
        # Tap pour le VU-metre de test (une seule IP surveillee a la fois)
        self._test_tap_ip = None
        self._test_tap_callback = None

    def add_source_callback(self, ip, callback):
        """Enregistre un callback audio pour une IP source specifique.

        Chaque IP a son propre ring buffer (pas de melange inter-sources).
        """
        with self._lock:
            emit_samples = 1600
            self._per_ip[ip] = {
                'buf': np.zeros(emit_samples + 4800, dtype=np.float32),
                'buf_len': 0,
                'callback': callback,
            }
        logging.info(f"VBAN: callback audio enregistre pour {ip}")

    def remove_source_callback(self, ip, callback=None):
        """Retire le callback audio d'une IP source.

        Si `callback` est fourni, ne retire QUE si c'est bien celui actuellement
        enregistre pour cette IP. Evite qu'un ancien thread (lors d'un
        redemarrage) supprime le callback qu'un nouveau thread vient de
        reenregistrer pour la meme IP.
        """
        with self._lock:
            entry = self._per_ip.get(ip)
            if entry is not None and (callback is None or entry.get('callback') is callback):
                self._per_ip.pop(ip, None)
            else:
                return
        logging.info(f"VBAN: callback audio retire pour {ip}")

    def set_test_tap(self, ip, callback):
        """Configure un tap pour le VU-metre : appelle callback(peak) pour
        chaque paquet dont l'IP source correspond. ip=None / callback=None
        pour desactiver."""
        self._test_tap_ip = ip
        self._test_tap_callback = callback

    @staticmethod
    def _is_multicast(ip_str):
        """True si l'IP est dans la plage multicast IPv4 (224.0.0.0/4)."""
        try:
            return ipaddress.IPv4Address(ip_str).is_multicast
        except Exception:
            return False

    def _sync_multicast_groups(self):
        """Synchronise les memberships multicast avec les sources configurees.

        Joint automatiquement les groupes pour toute source sauvegardee dont
        l'IP est multicast, et quitte ceux qui n'ont plus de source associee.
        """
        if not self._socket:
            return
        try:
            settings = self._load_settings() or {}
        except Exception:
            return
        desired = set()
        for src in (settings.get('saved_vban_sources') or []):
            ip = src.get('ip', '')
            if ip and self._is_multicast(ip):
                desired.add(ip)

        # Join nouveaux groupes
        for ip in desired - self._joined_multicast_groups:
            try:
                mreq = struct.pack("4sL", socket.inet_aton(ip), socket.INADDR_ANY)
                self._socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                self._joined_multicast_groups.add(ip)
                logging.info(f"VBAN: groupe multicast rejoint ({ip})")
            except Exception as exc:
                logging.warning(f"VBAN: echec du join multicast {ip}: {exc}")

        # Leave groupes obsoletes
        for ip in list(self._joined_multicast_groups - desired):
            try:
                mreq = struct.pack("4sL", socket.inet_aton(ip), socket.INADDR_ANY)
                self._socket.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, mreq)
                logging.info(f"VBAN: groupe multicast quitte ({ip})")
            except Exception:
                pass
            self._joined_multicast_groups.discard(ip)

    def start_listening(self):
        """Démarre l'écoute des flux VBAN"""
        if self._socket:
            try:
                self._socket.close()
            except:
                pass

        self.running = True
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Permet a un autre process de binder le meme port (utile si plusieurs
        # consommateurs multicast sur la meme machine).
        if hasattr(socket, 'SO_REUSEPORT'):
            try:
                self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except Exception:
                pass
        self._socket.settimeout(0.5)
        # Augmenter le TTL multicast (defaut 1 = meme subnet). 2 suffit pour
        # traverser un routeur mais reste cantonne au LAN.
        try:
            self._socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        except Exception:
            pass
        logging.info(f"Démarrage de l'écoute VBAN sur le port {self.port}")
        self._socket.bind(('0.0.0.0', self.port))

        # Auto-join des groupes multicast declares dans les sources sauvegardees.
        self._sync_multicast_groups()
        self._last_mcast_sync = time.time()

        # Démarrer l'écoute dans un thread séparé
        self._listen_thread = threading.Thread(target=self._listen_loop)
        self._listen_thread.daemon = True
        self._listen_thread.start()

    def _listen_loop(self):
        """Boucle d'écoute des flux VBAN"""
        logging.info("Thread d'écoute VBAN démarré")
        logged_sources = set()
        
        while self.running:
            try:
                data, addr = self._socket.recvfrom(2048)
                
                # Vérifier que le paquet est assez grand pour contenir l'en-tête VBAN (28 bytes)
                if len(data) < 28:
                    logging.warning(f"Paquet trop petit ({len(data)} bytes), ignoré")
                    continue
                    
                source = self._parse_vban_packet(data, addr, logged_sources)
                if source:
                    # Vérifier si la source est activée dans settings.json
                    settings = self._load_settings()
                    if settings and 'saved_vban_sources' in settings:
                        source_enabled = False
                        for saved_source in settings['saved_vban_sources']:
                            if (saved_source['ip'] == source.ip and 
                                saved_source['stream_name'] == source.name and 
                                saved_source.get('enabled', False)):
                                source_enabled = True
                                break
                        
                        if not source_enabled:
                            continue  # Ignorer les sources désactivées
                            
                    try:
                        # Calculer le nombre d'échantillons complets disponibles
                        audio_bytes = data[28:]
                        num_samples = len(audio_bytes) // 2  # 2 bytes par échantillon int16
                        
                        if num_samples == 0:
                            logging.warning("Pas de données audio dans le paquet")
                            continue
                            
                        # N'utiliser que les bytes correspondant à des échantillons complets
                        audio_data = np.frombuffer(audio_bytes[:num_samples*2], dtype=np.int16)
                        
                        # Convertir en float32 et normaliser entre -1 et 1
                        audio_data = audio_data.astype(np.float32) / 32768.0

                        # VU-meter test tap (avant mixing / resampling pour avoir
                        # le signal brut multi-canal si besoin).
                        if self._test_tap_callback and self._test_tap_ip == addr[0]:
                            try:
                                peak = float(np.max(np.abs(audio_data))) if len(audio_data) else 0.0
                                self._test_tap_callback(peak)
                            except Exception:
                                pass
                        
                        # Convertir en mono si nécessaire
                        if source.channels > 1:
                            # S'assurer que la taille des données est divisible par le nombre de canaux
                            samples_per_channel = len(audio_data) // source.channels
                            audio_data = audio_data[:samples_per_channel * source.channels]
                            audio_data = audio_data.reshape(-1, source.channels)
                            audio_data = np.mean(audio_data, axis=1)
                        
                        # Rééchantillonnage anti-aliasé si nécessaire
                        if source.sample_rate != self.target_sample_rate:
                            gcd = math.gcd(self.target_sample_rate, source.sample_rate)
                            up = self.target_sample_rate // gcd
                            down = source.sample_rate // gcd
                            audio_data = resample_poly(audio_data, up, down).astype(np.float32)
                        
                        # Log de debug uniquement (sinon spam continu des que
                        # quelqu'un diffuse du VBAN, meme hors detection).
                        if logging.getLogger().isEnabledFor(logging.DEBUG) and \
                                (audio_data.max() > 0.3 or audio_data.min() < -0.3):
                            logging.debug(f"Son fort détecté sur {addr[0]}, amplitude: min={audio_data.min():.3f}, max={audio_data.max():.3f}")
                        
                        # Ajouter au ring buffer de cette IP et emettre par
                        # paquets de 100 ms (1600 echantillons a 16 kHz).
                        with self._lock:
                            src_ip = addr[0]
                            pip = self._per_ip.get(src_ip)

                            if pip and pip['callback']:
                                # Buffer per-IP (multi-source propre)
                                n = len(audio_data)
                                buf = pip['buf']
                                bl = pip['buf_len']
                                if bl + n > len(buf):
                                    keep = len(buf) - n
                                    buf[:keep] = buf[bl - keep:bl]
                                    bl = keep
                                buf[bl:bl + n] = audio_data
                                bl += n
                                emit_samples = 1600
                                while bl >= emit_samples:
                                    chunk = buf[:emit_samples].copy()
                                    remaining = bl - emit_samples
                                    if remaining > 0:
                                        buf[:remaining] = buf[emit_samples:bl]
                                    bl = remaining
                                    try:
                                        pip['callback'](chunk, time.time())
                                    except Exception as exc:
                                        logging.error(f"VBAN callback {src_ip}: {exc}")
                                pip['buf_len'] = bl
                            elif self.audio_callback:
                                # Fallback legacy : buffer partage unique
                                n = len(audio_data)
                                if self._buf_len + n > len(self._buf):
                                    keep = len(self._buf) - n
                                    self._buf[:keep] = self._buf[self._buf_len - keep:self._buf_len]
                                    self._buf_len = keep
                                self._buf[self._buf_len:self._buf_len + n] = audio_data
                                self._buf_len += n
                                emit_samples = 1600
                                while self._buf_len >= emit_samples:
                                    chunk = self._buf[:emit_samples].copy()
                                    remaining = self._buf_len - emit_samples
                                    if remaining > 0:
                                        self._buf[:remaining] = self._buf[emit_samples:self._buf_len]
                                    self._buf_len = remaining
                                    self.audio_callback(chunk, time.time())
                        
                        # Mettre à jour les informations de la source
                        self.sources[addr[0]].update({
                            'last_seen': time.time(),
                            'name': source.name,
                            'sample_rate': source.sample_rate,
                            'channels': source.channels
                        })
                        
                        # Appeler le callback source si défini
                        if self.source_callback:
                            self.source_callback(self.get_active_sources())
                            
                    except Exception as e:
                        logging.error(f"Erreur lors du traitement des données audio: {str(e)}")
                        continue
            except socket.timeout:
                # Nettoyer les sources inactives (plus de 5 secondes)
                current_time = time.time()
                inactive = [ip for ip, info in self.sources.items()
                          if current_time - info['last_seen'] > 5]
                for ip in inactive:
                    del self.sources[ip]
                    if self.source_callback:
                        self.source_callback(self.get_active_sources())

                # Re-sync multicast groups periodically (user may have added
                # or removed a multicast source via the UI).
                if current_time - self._last_mcast_sync > 10:
                    self._sync_multicast_groups()
                    self._last_mcast_sync = current_time
                    
    def _parse_vban_packet(self, data, addr, logged_sources=None):
        """Parse un paquet VBAN et retourne les informations de la source"""
        try:
            if len(data) >= 28 and data[0:4] == b'VBAN':
                # Extraire les informations du header VBAN
                sr_index = data[4] & 0x1F
                channels = ((data[4] & 0xE0) >> 5) + 1
                name = self.clean_vban_name(data[8:28])
                ip = addr[0]
                port = addr[1]
                
                # Convertir l'index de sample rate en Hz
                sample_rates = {
                    0: 6000, 1: 12000, 2: 24000, 3: 48000, 4: 96000,
                    5: 192000, 6: 384000, 7: 8000, 8: 16000, 9: 32000,
                    10: 64000, 11: 128000, 12: 256000, 13: 512000,
                    14: 11025, 15: 22050, 16: 44100, 17: 88200,
                    18: 176400, 19: 352800
                }
                sample_rate = sample_rates.get(sr_index, 44100)
                
                # Mettre à jour le dictionnaire des sources
                with self._lock:
                    self.sources[ip] = {
                        'last_seen': time.time(),
                        'name': name,
                        'sample_rate': sample_rate,
                        'channels': channels
                    }
                
                # Créer un objet source
                source = type('VBANSource', (), {
                    'name': name,
                    'ip': ip,
                    'port': port,
                    'channels': channels,
                    'sample_rate': sample_rate
                })
                
                # Log si demandé
                if logged_sources is not None and ip not in logged_sources:
                    logging.info(f"Source VBAN détectée: {name} ({ip}), {channels} canaux @ {sample_rate}Hz")
                    logged_sources.add(ip)
                
                # Notifier le callback des sources si défini
                if self.source_callback:
                    try:
                        self.source_callback(self.get_active_sources())
                    except Exception as e:
                        logging.error(f"Erreur dans le callback des sources: {e}")
                
                return source
                
        except Exception as e:
            logging.error(f"Erreur lors du parsing du paquet VBAN: {e}")
            return None

    def stop_listening(self):
        """Arrête l'écoute des flux VBAN"""
        self.running = False
        if self._socket:
            self._socket.close()
            
    def get_active_sources(self):
        """Retourne un dictionnaire des sources actives"""
        return dict(self.sources)
        
    def set_audio_callback(self, callback):
        """Définit le callback pour les données audio"""
        self.audio_callback = callback
        
    def set_source_callback(self, callback):
        """Définit le callback pour les changements de sources"""
        self.source_callback = callback
        
    def clean_vban_name(self, raw_name):
        """Nettoie le nom VBAN en retirant les caractères non désirés"""
        if isinstance(raw_name, bytes):
            try:
                end_idx = None
                for i, byte in enumerate(raw_name):
                    if byte == 0 or not (32 <= byte <= 126):
                        end_idx = i
                        break
                if end_idx is not None:
                    raw_name = raw_name[:end_idx]
                name = raw_name.decode('ascii', errors='ignore')
            except:
                return ""
        else:
            name = str(raw_name)
            
        name = name.strip()
        while name and not (name[-1].isalnum() or name[-1].isspace()):
            name = name[:-1]
            
        return name

    def cleanup(self):
        """Arrête l'écoute et nettoie les ressources"""
        self.running = False
        if self._socket:
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
                self._socket.close()
            except:
                pass
            self._socket = None
        
        # Attendre que le thread d'écoute se termine
        if hasattr(self, '_listen_thread') and self._listen_thread.is_alive():
            self._listen_thread.join(timeout=1.0)

    def get_sources(self, timeout=1.0):
        """Obtient la liste des sources VBAN actives de manière thread-safe
        
        Args:
            timeout (float): Temps maximum d'attente en secondes
            
        Returns:
            list: Liste des sources VBAN actives
        """
        if not self.running or not self._socket:
            return []
            
        active_sources = []
        start_time = time.time()
        
        with self._lock:
            # Nettoyer les sources inactives
            current_time = time.time()
            inactive = [ip for ip, info in self.sources.items() 
                      if current_time - info['last_seen'] > 5]
            for ip in inactive:
                del self.sources[ip]
                
            # Retourner les sources actives
            for ip, info in self.sources.items():
                if current_time - info['last_seen'] <= timeout:
                    active_sources.append({
                        'ip': ip,
                        'name': info['name'],
                        'sample_rate': info['sample_rate'],
                        'channels': info['channels'],
                        'last_seen': info['last_seen'],
                        'port': self.port  # Add the port number
                    })
                    
        return active_sources

    def _load_settings(self):
        """Charge les paramètres via le module centralisé (avec cache TTL)."""
        return _load_settings_from_manager()
