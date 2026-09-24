import ipaddress
import math
import socket
import struct
import threading
import time
import logging
from collections import namedtuple

import numpy as np
from scipy.signal import resample_poly

from audio_utils import BLOCK_SAMPLES
from settings_manager import load_settings as _load_settings_from_manager

# Index de sample rate VBAN (octet 4, bits 0-4) -> Hz
VBAN_SAMPLE_RATES = {
    0: 6000, 1: 12000, 2: 24000, 3: 48000, 4: 96000,
    5: 192000, 6: 384000, 7: 8000, 8: 16000, 9: 32000,
    10: 64000, 11: 128000, 12: 256000, 13: 512000,
    14: 11025, 15: 22050, 16: 44100, 17: 88200,
    18: 176400, 19: 352800,
}
VBAN_PROTOCOL_AUDIO = 0x00   # octet 4, bits 5-7
VBAN_CODEC_PCM = 0x00        # octet 7, bits 4-7
# octet 7, bits 0-2 : type d'echantillon -> (taille en octets, nom)
VBAN_DATATYPES = {1: (2, 'INT16'), 2: (3, 'INT24'), 3: (4, 'INT32'),
                  4: (4, 'FLOAT32'), 5: (8, 'FLOAT64')}

SOURCE_TIMEOUT = 5   # s sans paquet avant de retirer une source de la decouverte

VBANHeader = namedtuple('VBANHeader', 'name sample_rate channels datatype')


def decode_vban_samples(payload, datatype):
    """Decode la charge utile PCM d'un paquet VBAN en float32 dans [-1, 1]."""
    size, _ = VBAN_DATATYPES[datatype]
    n = len(payload) // size
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    raw = payload[:n * size]
    if datatype == 1:
        return np.frombuffer(raw, dtype='<i2').astype(np.float32) / 32768.0
    if datatype == 2:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v & 0x800000, v - 0x1000000, v)
        return v.astype(np.float32) / 8388608.0
    if datatype == 3:
        return (np.frombuffer(raw, dtype='<i4') / 2147483648.0).astype(np.float32)
    if datatype == 4:
        return np.frombuffer(raw, dtype='<f4').astype(np.float32)
    return np.frombuffer(raw, dtype='<f8').astype(np.float32)


class StreamResampler:
    """Reechantillonne un flux continu vers 16 kHz par blocs de ~100 ms.

    resample_poly applique un zero-padding aux bords de chaque appel : appele
    sur chaque paquet (~256 echantillons, ~190 fois/s) il creait un transitoire
    a chaque frontiere de paquet. Ici chaque bloc est reechantillonne avec
    `ctx` echantillons de contexte de chaque cote, et seule la partie centrale
    est gardee : pas de discontinuite, un appel par 100 ms.
    """

    def __init__(self, sample_rate, target_rate=16000):
        self.sample_rate = sample_rate
        g = math.gcd(target_rate, sample_rate)
        self.up = target_rate // g
        self.down = sample_rate // g
        self.passthrough = (self.up == 1 and self.down == 1)
        # Bloc natif multiple de `down` pour que la sortie tombe juste.
        self.block = self.down * max(1, round(sample_rate * 0.1 / self.down))
        # Demi-longueur du filtre de resample_poly (10 * max(up, down) a la
        # frequence sur-echantillonnee), ramenee en echantillons d'entree.
        half = math.ceil(10 * max(self.up, self.down) / self.up) + 2
        self.ctx = self.down * math.ceil(half / self.down)
        self._buf = np.zeros(0, dtype=np.float32)

    def process(self, samples):
        if self.passthrough:
            return samples
        self._buf = np.concatenate((self._buf, samples))
        need = self.block + 2 * self.ctx
        out = []
        start = self.ctx * self.up // self.down
        length = self.block * self.up // self.down
        while len(self._buf) >= need:
            y = resample_poly(self._buf[:need], self.up, self.down)
            out.append(y[start:start + length].astype(np.float32))
            self._buf = self._buf[self.block:]
        if not out:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(out)


class VBANDetector:
    def __init__(self, port=6980):
        self.port = port
        # (ip, stream_name) -> {'ip', 'name', 'last_seen', 'sample_rate', 'channels'}
        self.sources = {}
        self.running = False
        self._socket = None
        self.target_sample_rate = 16000
        # (ip, stream_name) -> etat du flux abonne (callback, resampler, buffer).
        # Indexe par flux et non par IP : deux flux d'une meme machine
        # (Voicemeeter) ne se melangent plus dans le meme buffer.
        self._streams = {}
        # stream_name -> cle d'un flux abonne dont l'IP est un groupe multicast :
        # les paquets arrivent avec l'IP unicast de l'emetteur, on route par nom.
        self._mcast_by_name = {}
        self._lock = threading.Lock()
        self._joined_multicast_groups = set()
        self._last_mcast_sync = 0
        self._last_housekeeping = 0
        self._warned = set()  # avertissements deja emis (un par flux et par cause)
        # Tap pour le VU-metre de test (une seule IP surveillee a la fois)
        self._test_tap_ip = None
        self._test_tap_name = ''
        self._test_tap_callback = None

    # --- Abonnements -------------------------------------------------------

    def add_source_callback(self, ip, callback, stream_name=''):
        """Enregistre un callback audio (blocs de 1600 echantillons a 16 kHz)
        pour le flux (ip, stream_name)."""
        # Meme nettoyage que les en-tetes recus : un nom saisi a la main avec
        # une ponctuation finale (« Mic (L) ») ne correspondait jamais.
        stream_name = self.clean_vban_name(stream_name or '')
        key = (ip, stream_name)
        with self._lock:
            self._streams[key] = {
                'callback': callback,
                'resampler': None,
                'out': np.zeros(0, dtype=np.float32),
            }
            if self._is_multicast(ip) and stream_name:
                self._mcast_by_name[stream_name] = key
        logging.info(f"VBAN: callback audio enregistre pour {ip}/{stream_name}")

    def remove_source_callback(self, ip, callback=None, stream_name=''):
        """Retire le callback du flux (ip, stream_name).

        Si `callback` est fourni, ne retire QUE si c'est bien celui actuellement
        enregistre. Evite qu'un ancien thread (lors d'un redemarrage) supprime
        le callback qu'un nouveau thread vient de reenregistrer.
        """
        stream_name = self.clean_vban_name(stream_name or '')
        key = (ip, stream_name)
        with self._lock:
            entry = self._streams.get(key)
            if entry is None or (callback is not None and entry.get('callback') is not callback):
                return
            self._streams.pop(key, None)
            if self._mcast_by_name.get(stream_name) == key:
                self._mcast_by_name.pop(stream_name, None)
        logging.info(f"VBAN: callback audio retire pour {ip}/{stream_name}")

    def set_test_tap(self, ip, callback, stream_name=''):
        """Configure un tap pour le VU-metre : appelle callback(peak) pour
        chaque paquet du flux. Pour un groupe multicast, les paquets arrivent
        avec l'IP de l'emetteur : on reconnait alors le flux par son nom.
        ip=None / callback=None pour desactiver."""
        self._test_tap_callback = None
        self._test_tap_ip = ip
        self._test_tap_name = self.clean_vban_name(stream_name or '') if ip else ''
        self._test_tap_callback = callback

    # --- Multicast -----------------------------------------------------------

    @staticmethod
    def _is_multicast(ip_str):
        """True si l'IP est dans la plage multicast IPv4 (224.0.0.0/4)."""
        try:
            return ipaddress.IPv4Address(ip_str).is_multicast
        except Exception:
            return False

    def _sync_multicast_groups(self):
        """Synchronise les memberships multicast avec les sources configurees."""
        if not self._socket:
            return
        try:
            settings = _load_settings_from_manager() or {}
        except Exception:
            return
        desired = set()
        for src in (settings.get('saved_vban_sources') or []):
            ip = src.get('ip', '')
            if ip and self._is_multicast(ip):
                desired.add(ip)

        for ip in desired - self._joined_multicast_groups:
            try:
                mreq = struct.pack("4sL", socket.inet_aton(ip), socket.INADDR_ANY)
                self._socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                self._joined_multicast_groups.add(ip)
                logging.info(f"VBAN: groupe multicast rejoint ({ip})")
            except Exception as exc:
                logging.warning(f"VBAN: echec du join multicast {ip}: {exc}")

        for ip in list(self._joined_multicast_groups - desired):
            try:
                mreq = struct.pack("4sL", socket.inet_aton(ip), socket.INADDR_ANY)
                self._socket.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, mreq)
                logging.info(f"VBAN: groupe multicast quitte ({ip})")
            except Exception:
                pass
            self._joined_multicast_groups.discard(ip)

    # --- Ecoute ----------------------------------------------------------------

    def start_listening(self):
        """Démarre l'écoute des flux VBAN"""
        if self._socket:
            try:
                self._socket.close()
            except Exception:
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
        try:
            self._socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        except Exception:
            pass
        logging.info(f"Démarrage de l'écoute VBAN sur le port {self.port}")
        try:
            self._socket.bind(('0.0.0.0', self.port))
        except OSError:
            # Port occupe : ne pas laisser le socket ouvert jusqu'au GC.
            self._socket.close()
            self._socket = None
            self.running = False
            raise

        self._sync_multicast_groups()
        self._last_mcast_sync = time.time()

        self._listen_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._listen_thread.start()

    def _housekeeping(self, now):
        """Purge des sources muettes et resynchro multicast.

        Execute sur une horloge et non plus sur timeout du socket : avec un flux
        continu il n'y avait jamais 0,5 s de silence, donc jamais de purge ni de
        join des groupes multicast ajoutes depuis l'UI.
        """
        self._last_housekeeping = now
        with self._lock:
            for key in [k for k, info in self.sources.items()
                        if now - info['last_seen'] > SOURCE_TIMEOUT]:
                del self.sources[key]
        if now - self._last_mcast_sync > 10:
            self._sync_multicast_groups()
            self._last_mcast_sync = now

    def _listen_loop(self):
        """Boucle d'écoute des flux VBAN"""
        logging.info("Thread d'écoute VBAN démarré")
        while self.running:
            now = time.time()
            if now - self._last_housekeeping >= 1.0:
                try:
                    self._housekeeping(now)
                except Exception as e:
                    logging.error(f"VBAN: erreur de maintenance: {e}")
            try:
                data, addr = self._socket.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError as e:
                # Socket ferme (arret/redemarrage) : sortir proprement si on
                # ne tourne plus, sinon logger et continuer.
                if not self.running:
                    break
                logging.error(f"VBAN: erreur socket, poursuite: {e}")
                time.sleep(0.1)
                continue
            try:
                self._handle_packet(data, addr[0])
            except Exception as e:
                # Filet de securite : AUCUNE exception ne doit tuer le thread
                # d'ecoute (sinon la detection VBAN meurt en silence).
                logging.error(f"VBAN: erreur inattendue dans la boucle d'écoute: {e}")

    def _warn_once(self, key, message):
        if key not in self._warned:
            self._warned.add(key)
            logging.warning(message)

    def _parse_header(self, data, ip):
        """Parse l'en-tete VBAN. Retourne None si le paquet n'est pas de l'audio
        PCM exploitable."""
        if len(data) < 28 or data[0:4] != b'VBAN':
            logging.debug(f"VBAN: paquet non VBAN ou trop court ({len(data)} octets) de {ip}")
            return None
        # Nom du flux : 16 octets (8-23). Les octets 24-27 sont le compteur de
        # trame : les inclure collait un caractere variable aux noms de 16
        # caracteres (paquets jetes, sources fantomes dans la decouverte).
        name = self.clean_vban_name(data[8:24])
        if (data[4] & 0xE0) != VBAN_PROTOCOL_AUDIO:
            return None  # VBAN-TEXT, serial, service : pas de l'audio
        sample_rate = VBAN_SAMPLE_RATES.get(data[4] & 0x1F)
        if sample_rate is None:
            self._warn_once((ip, name, 'sr'),
                            f"VBAN: sample rate inconnu (index {data[4] & 0x1F}) pour {name} ({ip}), flux ignore")
            return None
        datatype = data[7] & 0x07
        codec = data[7] & 0xF0
        if codec != VBAN_CODEC_PCM or datatype not in VBAN_DATATYPES:
            self._warn_once((ip, name, 'fmt'),
                            f"VBAN: format non supporte pour {name} ({ip}) "
                            f"(codec=0x{codec:02x}, type={datatype}), flux ignore")
            return None
        # NBC = octet 6 (nb de canaux - 1).
        channels = (data[6] & 0xFF) + 1
        return VBANHeader(name, sample_rate, channels, datatype)

    def _handle_packet(self, data, ip):
        hdr = self._parse_header(data, ip)
        if hdr is None:
            return
        key = (ip, hdr.name)
        now = time.time()
        with self._lock:
            if key not in self.sources:
                logging.info(f"Source VBAN détectée: {hdr.name} ({ip}), {hdr.channels} canaux @ "
                             f"{hdr.sample_rate}Hz, {VBAN_DATATYPES[hdr.datatype][1]}")
            self.sources[key] = {
                'ip': ip, 'name': hdr.name, 'last_seen': now,
                'sample_rate': hdr.sample_rate, 'channels': hdr.channels,
            }
            stream = self._streams.get(key)
            if stream is None:
                mkey = self._mcast_by_name.get(hdr.name)
                stream = self._streams.get(mkey) if mkey else None
                if stream is not None:
                    # Flux multicast : on s'attache au premier emetteur vu. Un
                    # autre emetteur du meme nom (unicast par exemple) melangeait
                    # son audio et recreait le reechantillonneur a chaque paquet.
                    sender = stream.setdefault('sender', ip)
                    if sender != ip:
                        if (mkey, ip) not in self._warned:
                            self._warned.add((mkey, ip))
                            logging.warning(f"VBAN: flux « {hdr.name} » reçu aussi de {ip}, ignoré "
                                            f"(émetteur retenu : {sender})")
                        stream = None
            if stream is None and any(k[0] == ip for k in self._streams):
                self._warn_once((ip, hdr.name, 'name'),
                                f"VBAN: flux « {hdr.name} » reçu de {ip} mais aucune source ne porte ce nom "
                                f"(sources de cette IP : {', '.join(k[1] for k in self._streams if k[0] == ip)})")

        tap_ip, tap_name = self._test_tap_ip, self._test_tap_name
        if tap_ip and self._is_multicast(tap_ip):
            match = bool(tap_name) and hdr.name == tap_name
        else:
            # Unicast : IP et nom, deux flux d'un meme PC ne se melangent plus.
            match = tap_ip == ip and (not tap_name or hdr.name == tap_name)
        tap = self._test_tap_callback if match else None
        # Rien d'abonne a ce flux : ne pas decoder ni reechantillonner.
        if stream is None and tap is None:
            return

        audio = decode_vban_samples(data[28:], hdr.datatype)
        if len(audio) == 0:
            return

        if tap is not None:
            try:
                tap(float(np.max(np.abs(audio))))
            except Exception:
                pass
        if stream is None:
            return

        if hdr.channels > 1:
            frames = len(audio) // hdr.channels
            audio = audio[:frames * hdr.channels].reshape(-1, hdr.channels).mean(axis=1)

        resampler = stream['resampler']
        if resampler is None or resampler.sample_rate != hdr.sample_rate:
            resampler = stream['resampler'] = StreamResampler(hdr.sample_rate, self.target_sample_rate)
        audio = resampler.process(audio.astype(np.float32, copy=False))
        if len(audio) == 0:
            return

        out = np.concatenate((stream['out'], audio))
        chunks = []
        while len(out) >= BLOCK_SAMPLES:
            chunks.append(out[:BLOCK_SAMPLES])
            out = out[BLOCK_SAMPLES:]
        stream['out'] = out

        callback = stream['callback']
        for chunk in chunks:
            try:
                callback(np.ascontiguousarray(chunk, dtype=np.float32), now)
            except Exception as exc:
                logging.error(f"VBAN callback {ip}/{hdr.name}: {exc}")

    # --- Arret / consultation ------------------------------------------------

    def stop_listening(self):
        """Arrête l'écoute des flux VBAN"""
        self.running = False
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None
        # Joindre le thread d'ecoute : evite qu'un ancien thread survive (et
        # reouvre un socket) pendant qu'un nouveau detecteur demarre.
        t = getattr(self, '_listen_thread', None)
        if t and t.is_alive():
            t.join(timeout=1.0)

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
            except Exception:
                return ""
        else:
            name = str(raw_name)

        name = name.strip()
        while name and not (name[-1].isalnum() or name[-1].isspace()):
            name = name[:-1]

        return name

    def get_sources(self, timeout=1.0):
        """Liste des flux VBAN entendus depuis moins de `timeout` s (un element
        par flux : deux flux d'une meme IP apparaissent separement)."""
        if not self.running or not self._socket:
            return []
        now = time.time()
        with self._lock:
            return [
                {
                    'ip': info['ip'],
                    'name': info['name'],
                    'sample_rate': info['sample_rate'],
                    'channels': info['channels'],
                    'last_seen': info['last_seen'],
                    'port': self.port,
                }
                for info in self.sources.values()
                if now - info['last_seen'] <= timeout
            ]
