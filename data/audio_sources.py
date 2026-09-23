"""Sources audio : toutes produisent des blocs de 100 ms, mono, 16 kHz, float32.

- ProcessSource : un sous-processus qui ecrit du PCM float32 sur stdout
  (parecord pour le micro, ffmpeg pour le RTSP), avec chien de garde.
- VbanSource : blocs recus du listener UDP VBAN via une file ; l'inference se
  fait donc sur le thread de la source et plus sur le thread UDP partage.

run_source() lit une source en boucle et la relance avec backoff si elle
s'arrete, jusqu'a ce que `stop_event` soit leve.
"""

import logging
import queue
import subprocess
import threading
import time

import numpy as np

from audio_utils import drain_stderr, terminate_process
from url_validator import mask_url_credentials

BLOCK_SAMPLES = 1600


class ProcessSource:
    def __init__(self, cmd, name, stall_timeout=15, sanitize=None):
        self.cmd = cmd
        self.name = name
        self.stall_timeout = stall_timeout
        self.sanitize = sanitize or (lambda s: s)
        self._proc = None
        self._lock = threading.Lock()

    def describe(self):
        return self.sanitize(' '.join(self.cmd))

    def iter_blocks(self, stop_event):
        """Lance le process et produit ses blocs jusqu'a EOF (process mort,
        tue par le chien de garde ou par close())."""
        proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with self._lock:
            self._proc = proc
        drain_stderr(proc, self.name, sanitize=self.sanitize)
        last_data = [time.monotonic()]
        done = threading.Event()

        def _watchdog():
            # Un flux qui n'envoie plus rien sans fermer la connexion bloquait
            # read() indefiniment : on tue le process pour reconnecter.
            while not done.wait(1.0):
                if time.monotonic() - last_data[0] > self.stall_timeout:
                    logging.warning(f"{self.name}: aucune donnée depuis {self.stall_timeout}s, redémarrage")
                    self._kill(proc)
                    return
        threading.Thread(target=_watchdog, daemon=True).start()
        try:
            nbytes = BLOCK_SAMPLES * 4
            while not stop_event.is_set():
                data = proc.stdout.read(nbytes)
                if not data:
                    break
                last_data[0] = time.monotonic()
                usable = len(data) - (len(data) % 4)
                if usable:
                    yield np.frombuffer(data[:usable], dtype=np.float32)
        finally:
            done.set()
            terminate_process(proc)
            with self._lock:
                self._proc = None

    @staticmethod
    def _kill(proc):
        try:
            proc.kill()
        except Exception:
            pass

    def close(self):
        """Debloque un read() en cours (arret de la detection)."""
        with self._lock:
            proc = self._proc
        if proc:
            self._kill(proc)


def mic_source(pulse_name):
    cmd = ['parecord', '--format=float32le', '--rate=16000', '--channels=1',
           '--raw', '--latency-msec=50']
    if pulse_name:
        cmd.append(f'--device={pulse_name}')
    return ProcessSource(cmd, 'parecord', stall_timeout=10)


def rtsp_source(url):
    cmd = [
        'ffmpeg', '-nostdin', '-nostats', '-loglevel', 'error',
        # protocol_whitelist : empeche ffmpeg d'ouvrir file://, http://, etc.
        '-protocol_whitelist', 'rtsp,rtp,udp,tcp,tls',
        '-i', url,
        '-vn', '-f', 'f32le', '-acodec', 'pcm_f32le', '-ac', '1', '-ar', '16000',
        'pipe:1',
    ]
    return ProcessSource(cmd, 'ffmpeg', stall_timeout=15, sanitize=mask_url_credentials)


class VbanSource:
    """Flux VBAN (ip, nom) : abonne une file au listener UDP partage."""

    def __init__(self, listener, ip, stream_name):
        self.listener = listener
        self.ip = ip
        self.stream_name = stream_name
        self.name = f"VBAN {ip}/{stream_name}"
        # ~5 s de tampon : au-dela, les blocs les plus anciens sont jetes plutot
        # que de bloquer le thread UDP.
        self._queue = queue.Queue(maxsize=50)

    def describe(self):
        return self.name

    def _on_chunk(self, chunk, timestamp):
        try:
            self._queue.put_nowait(chunk)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(chunk)
            except (queue.Empty, queue.Full):
                pass

    def iter_blocks(self, stop_event):
        self.listener.add_source_callback(self.ip, self._on_chunk, stream_name=self.stream_name)
        try:
            while not stop_event.is_set():
                try:
                    yield self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
        finally:
            self.listener.remove_source_callback(self.ip, self._on_chunk, stream_name=self.stream_name)

    def close(self):
        pass


def run_source(source, on_block, stop_event, on_status=None, max_backoff=30):
    """Lit `source` jusqu'a stop_event ; la relance avec backoff si elle
    s'arrete (process mort, flux coupe). on_status(status, error=None) recoit
    connecting / connected / reconnecting / error."""
    delay = 1
    while not stop_event.is_set():
        if on_status:
            on_status('connecting')
        got_data = False
        try:
            for block in source.iter_blocks(stop_event):
                if not got_data:
                    got_data = True
                    delay = 1
                    if on_status:
                        on_status('connected')
                on_block(block)
        except Exception as e:
            if stop_event.is_set():
                break
            msg = mask_url_credentials(str(e))
            logging.error(f"{source.name}: {msg}")
            if on_status:
                on_status('error', msg)
        if stop_event.is_set():
            break
        logging.warning(f"{source.name}: flux interrompu, nouvelle tentative dans {delay}s")
        if on_status:
            on_status('reconnecting')
        stop_event.wait(delay)
        delay = min(delay * 2, max_backoff)
