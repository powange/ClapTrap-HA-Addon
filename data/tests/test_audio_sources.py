"""Sources audio : process externes (chien de garde, arret) et relance."""
import inspect
import sys
import threading
import time

import numpy as np

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
