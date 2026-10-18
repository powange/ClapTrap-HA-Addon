"""audio_utils : peripheriques audio (Supervisor, repli pactl), processus."""
import subprocess
import sys
import types

import pytest

import audio_utils
from helpers import wait_until


@pytest.fixture(autouse=True)
def no_cache(monkeypatch):
    monkeypatch.setattr(audio_utils, '_audio_devices_cache', None)
    monkeypatch.setattr(audio_utils, '_audio_devices_cache_time', 0)


def _resp(ok=True, status=200, data=None):
    return types.SimpleNamespace(ok=ok, status_code=status, json=lambda: data)


def test_devices_from_supervisor_then_cached(monkeypatch):
    monkeypatch.setenv('SUPERVISOR_TOKEN', 'tok')
    calls = []

    def get(url, headers, timeout):
        calls.append((url, headers))
        return _resp(data={'result': 'ok', 'data': {'audio': {'input': [
            {'name': 'alsa_input.usb', 'description': 'Micro USB', 'index': 3},
            {'name': 'alsa_input.hdmi'}]}}})
    monkeypatch.setattr(audio_utils.requests, 'get', get)
    devices = audio_utils.get_audio_input_devices()
    assert devices == [{'index': 3, 'name': 'Micro USB', 'pulse_name': 'alsa_input.usb'},
                       {'index': 1, 'name': 'alsa_input.hdmi', 'pulse_name': 'alsa_input.hdmi'}]
    assert calls[0][1]['Authorization'] == 'Bearer tok'
    assert audio_utils.get_audio_input_devices() == devices and len(calls) == 1   # cache de 60 s


@pytest.mark.parametrize('resp', [
    _resp(ok=False, status=502),
    _resp(data={'data': {'audio': {'input': []}}}),
    RuntimeError('supervisor injoignable'),
])
def test_supervisor_failure_falls_back_to_pactl(monkeypatch, resp):
    monkeypatch.setenv('SUPERVISOR_TOKEN', 'tok')

    def get(*a, **k):
        if isinstance(resp, Exception):
            raise resp
        return resp
    monkeypatch.setattr(audio_utils.requests, 'get', get)
    out = ('1\talsa_input.usb\tmodule\ts16le\n2\talsa_output.monitor\tmodule\ts16le\n'
           'x\tmalformed\n5\talsa_input.hdmi\tmodule\ts16le\n')
    monkeypatch.setattr(audio_utils.subprocess, 'run',
                        lambda *a, **k: types.SimpleNamespace(stdout=out))
    assert audio_utils.get_audio_input_devices() == [
        {'index': 1, 'name': 'alsa_input.usb', 'pulse_name': 'alsa_input.usb'},
        {'index': 5, 'name': 'alsa_input.hdmi', 'pulse_name': 'alsa_input.hdmi'}]   # moniteurs et lignes sans index ecartes


def test_no_device_listing_possible(monkeypatch):
    monkeypatch.delenv('SUPERVISOR_TOKEN', raising=False)

    def run(*a, **k):
        raise FileNotFoundError('pactl')
    monkeypatch.setattr(audio_utils.subprocess, 'run', run)
    assert audio_utils.get_audio_input_devices() == []


def test_set_pulse_volume_command(monkeypatch):
    cmds = []
    monkeypatch.setattr(audio_utils.subprocess, 'run', lambda cmd, **k: cmds.append(cmd))
    audio_utils.set_pulse_volume('-alsa.x', 80)
    assert cmds == [['pactl', 'set-source-volume', '--', '-alsa.x', '80%']]   # nom commencant par « - »


def test_drain_stderr_sanitizes_and_reports_lines():
    proc = subprocess.Popen([sys.executable, '-c', "import sys; sys.stderr.write('rtsp://u:secret@h\\n\\nfin\\n')"],
                            stderr=subprocess.PIPE)
    lines = []
    audio_utils.drain_stderr(proc, 'test', sanitize=lambda line: line.replace('secret', '***'), on_line=lines.append)
    proc.wait(5)
    wait_until(lambda: lines == ['rtsp://u:***@h', 'fin'])   # lignes vides ignorees


def test_terminate_process_kills_when_terminate_hangs():
    events = []

    class Stubborn:
        def terminate(self):
            events.append('term')

        def wait(self, timeout):
            if events[-1] == 'term':
                raise subprocess.TimeoutExpired('x', timeout)
            events.append('reaped')

        def kill(self):
            events.append('kill')
    audio_utils.terminate_process(Stubborn(), timeout=0.1)
    assert events == ['term', 'kill', 'reaped']


def test_level_db():
    assert audio_utils.level_db(1.0) == pytest.approx(0.0)
    assert audio_utils.level_db(0) == -60.0 and audio_utils.level_db(0.1) == -20.0   # borne a -60 dB
