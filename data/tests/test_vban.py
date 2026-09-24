"""Ecoute VBAN : decodage, en-tete, reechantillonnage, routage, flux muet."""
import struct
import threading
import time

import numpy as np
import pytest
from scipy.signal import resample_poly

from audio_sources import VbanSource
from audio_utils import BLOCK_SAMPLES
from vban_listener import StreamResampler, VBANDetector, decode_vban_samples


def packet(name, samples, sr_idx=3, channels=1, datatype=1, raw_name=None):
    name_bytes = raw_name if raw_name is not None else name.encode().ljust(16, b'\0')
    header = b'VBAN' + bytes([sr_idx & 0x1F, 255, channels - 1, datatype]) + name_bytes[:16] + struct.pack('<I', 7)
    return header + samples


INT16_SILENCE = (np.ones(256) * 1000).astype('<i2').tobytes()


@pytest.fixture
def listener():
    d = VBANDetector()   # pas de socket : paquets injectes directement
    yield d


# --- Decodage ------------------------------------------------------------------

def test_decode_int16_int24_int32_float():
    assert list(decode_vban_samples(np.array([0, 16384, -32768], '<i2').tobytes(), 1)) == [0, 0.5, -1]
    int24 = b''.join(int(x).to_bytes(3, 'little', signed=True) for x in (0, 4194304, -8388608, -1))
    out = decode_vban_samples(int24, 2)
    assert list(out[:3]) == [0, 0.5, -1] and out[3] < 0     # negatifs INT24
    assert list(decode_vban_samples(np.array([0, 1 << 30, -(1 << 31)], '<i4').tobytes(), 3)) == [0, 0.5, -1]
    assert list(decode_vban_samples(np.array([0.25, -0.5], '<f4').tobytes(), 4)) == [0.25, -0.5]
    assert list(decode_vban_samples(np.array([0.25, -0.5], '<f8').tobytes(), 5)) == [0.25, -0.5]


def test_decode_truncated_payload():
    assert len(decode_vban_samples(b'\x01', 1)) == 0
    assert len(decode_vban_samples(b'\x01\x02\x03\x04\x05', 2)) == 1


# --- En-tete -------------------------------------------------------------------

def test_header_name_of_16_chars_is_stable(listener):
    """Nom de 16 caracteres sans NUL : le compteur de trame (octets 24-27) ne
    doit pas s'y coller (paquets jetes, sources fantomes)."""
    names = set()
    for counter in range(0, 2000, 37):
        data = bytearray(packet('ABCDEFGHIJKLMNOP', INT16_SILENCE))
        data[24:28] = struct.pack('<I', counter * 0x01010101 & 0xFFFFFFFF)
        hdr = listener._parse_header(bytes(data), '10.0.0.5')
        names.add(hdr.name)
    assert names == {'ABCDEFGHIJKLMNOP'}


def test_non_audio_and_unknown_format_ignored(listener):
    text = b'VBAN' + bytes([0x40, 0, 0, 0]) + b'A'.ljust(16, b'\0') + b'\0' * 4 + b'hello'
    assert listener._parse_header(text, '10.0.0.5') is None
    assert listener._parse_header(packet('A', INT16_SILENCE, datatype=0x10 | 1), '10.0.0.5') is None
    assert listener._parse_header(b'VBAN', '10.0.0.5') is None


def test_names_cleaned_like_headers(listener):
    assert listener.clean_vban_name('Mic (L)') == listener.clean_vban_name(b'Mic (L)\0\0')


# --- Reechantillonnage ------------------------------------------------------------

@pytest.mark.parametrize('sr', [48000, 44100, 11025, 8000])
def test_stream_resampler_matches_one_shot(sr):
    t = np.arange(sr * 2) / sr
    x = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    r = StreamResampler(sr)
    out = np.concatenate([r.process(x[i:i + 256]) for i in range(0, len(x), 256)])
    g = np.gcd(16000, sr)
    ref = resample_poly(x, 16000 // g, sr // g)
    off = r.ctx * r.up // r.down
    n = min(len(out), len(ref) - off) - 200
    assert 32000 - 2 * BLOCK_SAMPLES <= len(out) <= 32000   # au plus ~2 blocs de latence
    assert np.max(np.abs(out[:n] - ref[off:off + n])) < 1e-3   # pas de transitoire aux frontieres


# --- Routage ----------------------------------------------------------------------

def test_two_streams_from_same_ip_do_not_mix(listener):
    got = {'A': 0, 'B': 0}
    listener.add_source_callback('10.0.0.5', lambda c, t: got.__setitem__('A', got['A'] + len(c)), stream_name='A')
    listener.add_source_callback('10.0.0.5', lambda c, t: got.__setitem__('B', got['B'] + len(c)), stream_name='B')
    for _ in range(300):
        listener._handle_packet(packet('A', INT16_SILENCE), '10.0.0.5')
    for _ in range(30):
        listener._handle_packet(packet('B', INT16_SILENCE), '10.0.0.5')
    assert got['A'] > 10 * got['B'] > 0
    assert got['A'] % BLOCK_SAMPLES == 0


def test_multicast_routed_by_name_and_first_sender_locked(listener, caplog):
    got = []
    listener.add_source_callback('239.1.1.1', lambda c, t: got.append(len(c)), stream_name='Mc')
    for _ in range(100):
        listener._handle_packet(packet('Mc', INT16_SILENCE), '10.0.0.9')
    first = sum(got)
    for _ in range(100):
        listener._handle_packet(packet('Mc', INT16_SILENCE), '10.0.0.10')   # autre emetteur
    assert first > 0 and sum(got) == first
    assert 'ignoré' in caplog.text


def test_same_ip_other_name_warned_once(listener, caplog):
    listener.add_source_callback('10.0.0.5', lambda c, t: None, stream_name='Stream1')
    for _ in range(5):
        listener._handle_packet(packet('Stream2', INT16_SILENCE), '10.0.0.5')
    assert caplog.text.count('aucune source ne porte ce nom') == 1


def test_manual_name_with_trailing_punctuation_matches(listener):
    got = []
    listener.add_source_callback('10.0.0.5', lambda c, t: got.append(len(c)), stream_name='Mic (L)')
    for _ in range(100):
        listener._handle_packet(packet('Mic (L)', INT16_SILENCE), '10.0.0.5')
    assert sum(got) > 0


def test_test_tap_filters_ip_and_name(listener):
    peaks = []
    listener.set_test_tap('10.0.0.5', peaks.append, stream_name='A')
    listener._handle_packet(packet('B', INT16_SILENCE), '10.0.0.5')
    listener._handle_packet(packet('A', INT16_SILENCE), '10.0.0.6')
    assert peaks == []
    listener._handle_packet(packet('A', INT16_SILENCE), '10.0.0.5')
    assert len(peaks) == 1


# --- Flux muet ----------------------------------------------------------------------

class FakeListener:
    def __init__(self):
        self.cb = None

    def add_source_callback(self, ip, cb, stream_name=''):
        self.cb = cb

    def remove_source_callback(self, ip, cb=None, stream_name=''):
        self.cb = None


def test_vban_source_reports_idle_and_recovery(monkeypatch):
    fake = FakeListener()
    calls = []
    src = VbanSource(fake, '1.2.3.4', 'S', on_idle=lambda idle, msg: calls.append(idle))
    monkeypatch.setattr(VbanSource, 'IDLE_TIMEOUT', 0.6)
    stop = threading.Event()
    blocks = []
    th = threading.Thread(target=lambda: [blocks.append(b) for b in src.iter_blocks(stop)])
    th.start()
    time.sleep(1.3)
    assert calls == [True]
    fake.cb(np.zeros(BLOCK_SAMPLES, np.float32), 0)
    time.sleep(0.3)
    stop.set()
    th.join(2)
    assert calls == [True, False] and len(blocks) == 1
    assert fake.cb is None   # desabonne a l'arret


def test_real_listener_unsubscribes_bound_method(listener):
    """VbanSource s'abonne avec une methode liee : le desabonnement doit la
    reconnaitre (chaque acces cree un nouvel objet)."""
    src = VbanSource(listener, '10.0.0.5', 'Stream1')
    stop = threading.Event()
    th = threading.Thread(target=lambda: list(src.iter_blocks(stop)))
    th.start()
    time.sleep(0.2)
    assert ('10.0.0.5', 'Stream1') in listener._streams
    stop.set()
    th.join(2)
    assert ('10.0.0.5', 'Stream1') not in listener._streams
