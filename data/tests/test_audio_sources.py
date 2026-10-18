"""Sources audio : process externes (chien de garde, arret) et relance."""
import inspect
import sys
import threading
import time

import numpy as np
import pytest

from audio_sources import ProcessSource, run_source, rtsp_source
from audio_utils import BLOCK_SAMPLES
from helpers import wait_until

PY = sys.executable


def emitter(blocks, then='exit', stderr=''):
    """Commande qui ecrit `blocks` blocs float32 puis s'arrete ou se bloque."""
    tail = {'exit': '', 'hang': 'import time; time.sleep(60)'}[then]
    code = ("import sys, struct\n"
            f"sys.stderr.write({stderr!r}); sys.stderr.flush()\n"
            f"for _ in range({blocks}):\n"
            f"    sys.stdout.buffer.write(struct.pack('<{BLOCK_SAMPLES}f', *([0.25] * {BLOCK_SAMPLES})))\n"
            "sys.stdout.flush()\n" + tail)
    return [PY, '-c', code]


def test_process_source_reads_blocks_and_error_line():
    src = ProcessSource(emitter(3, stderr='401 Unauthorized\n'), 'fake')
    blocks = list(src.iter_blocks(threading.Event()))
    assert len(blocks) == 3 and blocks[0].dtype == np.float32 and blocks[0][0] == 0.25
    wait_until(lambda: 'Unauthorized' in src.last_error)


def test_process_source_watchdog_kills_stalled_stream():
    src = ProcessSource(emitter(1, then='hang'), 'fake', stall_timeout=1)
    t0 = time.monotonic()
    blocks = list(src.iter_blocks(threading.Event()))
    assert len(blocks) == 1 and time.monotonic() - t0 < 5


def test_close_unblocks_read():
    src = ProcessSource(emitter(0, then='hang'), 'fake', stall_timeout=60)
    out = []
    th = threading.Thread(target=lambda: out.extend(src.iter_blocks(threading.Event())))
    th.start()
    # Le fil doit etre bloque dans proc.stdout.read() (ligne lue dans le code).
    lines, first = inspect.getsourcelines(ProcessSource.iter_blocks)
    read_line = first + next(i for i, line in enumerate(lines) if 'proc.stdout.read(' in line)

    def blocked_in_read():
        frame = sys._current_frames().get(th.ident)
        return frame is not None and frame.f_code.co_name == 'iter_blocks' and frame.f_lineno == read_line
    wait_until(blocked_in_read)
    src.close()
    th.join(3)
    assert not th.is_alive()


def test_rtsp_command_restricts_protocols_and_masks_credentials():
    src = rtsp_source('rtsp://admin:secret@cam/x')
    assert '-protocol_whitelist' in src.cmd and 'file' not in src.cmd[src.cmd.index('-protocol_whitelist') + 1]
    assert 'secret' not in src.sanitize('erreur sur rtsp://admin:secret@cam/x')


class FlakySource:
    """Livre 2 blocs puis s'arrete, a chaque tentative."""
    name = 'flaky'
    last_error = ''

    def __init__(self):
        self.runs = 0

    def iter_blocks(self, stop):
        self.runs += 1
        for _ in range(2):
            yield np.zeros(BLOCK_SAMPLES, np.float32)


def test_run_source_retries_with_statuses():
    src, stop, statuses, blocks = FlakySource(), threading.Event(), [], []
    th = threading.Thread(target=run_source, args=(src, blocks.append, stop),
                          kwargs={'on_status': lambda s, e=None: statuses.append(s), 'max_backoff': 1})
    th.start()
    wait_until(lambda: src.runs >= 2 and len(statuses) >= 3 and len(blocks) == 2 * src.runs)
    stop.set()
    th.join(3)
    assert src.runs >= 2 and len(blocks) == 2 * src.runs
    assert statuses[:3] == ['connecting', 'connected', 'reconnecting']


def test_run_source_reports_error_detail():
    class Failing(FlakySource):
        last_error = '401 Unauthorized'

        def iter_blocks(self, stop):
            self.runs += 1
            return iter(())

    src, stop, statuses = Failing(), threading.Event(), []
    th = threading.Thread(target=run_source, args=(src, lambda b: None, stop),
                          kwargs={'on_status': lambda s, e=None: statuses.append((s, e))})
    th.start()
    wait_until(lambda: ('error', '401 Unauthorized') in statuses)
    stop.set()
    th.join(3)


def test_rtsp_tcp_transport_option():
    from audio_sources import rtsp_source
    assert '-rtsp_transport' not in rtsp_source('rtsp://cam/x').cmd
    # Piste audio seule (ffmpeg sans decodeur video sondait la video ~5 s).
    cmd = rtsp_source('rtsp://cam/x').cmd
    assert cmd[cmd.index('-allowed_media_types') + 1] == 'audio' and cmd.index('-allowed_media_types') < cmd.index('-i')
    cmd = rtsp_source('rtsp://cam/x', tcp=True).cmd
    assert cmd[cmd.index('-rtsp_transport') + 1] == 'tcp' and cmd.index('-rtsp_transport') < cmd.index('-i')


# --- Format audio de la camera (ffmpeg -loglevel level+info) ------------------------
FFMPEG_INFO = (
    "[info] Input #0, rtsp, from 'rtsp://cam/x':\n"
    "[info]   Stream #0:0: Audio: pcm_mulaw, 8000 Hz, mono, s16, 64 kb/s\n"
    "[info] Stream mapping:\n"
    "[info]   Stream #0:0: Audio: pcm_f32le, 16000 Hz, mono, flt, 512 kb/s\n"
    "[udp @ 0x1] [warning] Circular buffer overrun\n")


@pytest.mark.parametrize('line, fmt', [
    ('  Stream #0:0: Audio: pcm_mulaw, 8000 Hz, mono, s16, 64 kb/s',
     {'codec': 'pcm_mulaw', 'sample_rate': 8000, 'channels': 'mono'}),
    ('  Stream #0:1: Audio: aac (LC), 48000 Hz, stereo, fltp, start 0.161104',
     {'codec': 'aac', 'sample_rate': 48000, 'channels': 'stereo'}),
    ('  Stream #0:1[0x101](und): Audio: adpcm_g726, 8000 Hz, 1 channels',
     {'codec': 'adpcm_g726', 'sample_rate': 8000, 'channels': '1 channels'}),
    ('  Stream #0:0: Video: h264, yuv420p, 1920x1080', None),
])
def test_parse_audio_format(line, fmt):
    from audio_sources import parse_audio_format
    assert parse_audio_format(line) == fmt


def test_ffmpeg_levels_format_and_errors():
    """Lignes info : format de la camera (le premier flux, pas la sortie) ;
    avertissement : pas une erreur ; ligne error : retenue comme cause."""
    seen = []
    src = ProcessSource(emitter(2, stderr=FFMPEG_INFO + "[rtsp @ 0x2] [error] method DESCRIBE failed: 401 Unauthorized\n"),
                        'ffmpeg', levels=True)
    src.on_format = seen.append
    assert len(list(src.iter_blocks(threading.Event()))) == 2
    wait_until(lambda: src.last_error)
    assert seen == [{'codec': 'pcm_mulaw', 'sample_rate': 8000, 'channels': 'mono'}]
    assert src.audio_format == seen[0]
    # niveau retire (contexte garde, comme avant), avertissement ignore
    assert src.last_error == '[rtsp @ 0x2] method DESCRIBE failed: 401 Unauthorized'


def test_rtsp_command_reports_levels():
    cmd = rtsp_source('rtsp://cam/x').cmd
    assert cmd[cmd.index('-loglevel') + 1] == 'level+info' and rtsp_source('rtsp://cam/x').levels


def test_output_stream_never_taken_for_camera_format():
    """Entree sans frequence lisible : pas de format, plutot que la sortie
    f32le 16 kHz (qui rassurerait a tort)."""
    seen = []
    info = ("[info] Input #0, rtsp, from 'rtsp://cam/x':\n"
            "[info]   Stream #0:0: Audio: aac, 0 channels\n"
            "[info] Stream mapping:\n"
            "[info]   Stream #0:0: Audio: pcm_f32le, 16000 Hz, mono, flt, 512 kb/s\n"
            "[error] Error while decoding stream\n")
    src = ProcessSource(emitter(1, stderr=info), 'ffmpeg', levels=True)
    src.on_format = seen.append
    list(src.iter_blocks(threading.Event()))
    wait_until(lambda: src.last_error)
    assert seen == [] and src.audio_format is None
    assert src.last_error == 'Error while decoding stream'   # erreur sans contexte, apres le format
