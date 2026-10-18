"""Serveur : filtre ingress, configuration Gunicorn, script de demarrage."""
import importlib.util
import logging
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import types

import pytest

DATA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(DATA_DIR)


# --- Filtre ingress ------------------------------------------------------------------
def _call(mw, remote):
    seen = {}

    def start_response(status, headers):
        seen['status'] = status
    body = b''.join(mw({'REMOTE_ADDR': remote}, start_response))
    return seen.get('status', '200 OK'), body


def _middleware(monkeypatch, token):
    import app as appmod
    if token:
        monkeypatch.setenv('SUPERVISOR_TOKEN', 'x')
    else:
        monkeypatch.delenv('SUPERVISOR_TOKEN', raising=False)

    def inner(environ, start_response):
        start_response('200 OK', [])
        return [b'ok']
    return appmod.IngressOnlyMiddleware(inner)


def test_ingress_filter_accepts_only_supervisor(monkeypatch, caplog):
    mw = _middleware(monkeypatch, token=True)
    assert _call(mw, '172.30.32.2') == ('200 OK', b'ok')
    assert _call(mw, '::ffff:172.30.32.2') == ('200 OK', b'ok')   # IPv4 mappee
    with caplog.at_level(logging.WARNING):
        for remote in ('192.168.1.5', '192.168.1.5', '127.0.0.1'):
            status, body = _call(mw, remote)
            assert status.startswith('403') and b'ingress' in body
    # Un avertissement par adresse, pas un par requete
    refused = [r for r in caplog.records if 'Acces refuse' in r.getMessage()]
    assert len(refused) == 2


def test_ingress_filter_disabled_outside_home_assistant(monkeypatch):
    mw = _middleware(monkeypatch, token=False)
    assert _call(mw, '192.168.1.5') == ('200 OK', b'ok')


# --- gunicorn.conf.py ----------------------------------------------------------------
def _load_conf(monkeypatch, bind_ok):
    """Charge gunicorn.conf.py avec un bind reussi ou non. socket.socket n'est
    remplace que pendant le chargement (des fils d'autres tests peuvent en
    ouvrir ensuite)."""
    class FakeSocket:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def bind(self, addr):
            if not bind_ok:
                raise OSError('Cannot assign requested address')
    spec = importlib.util.spec_from_file_location('gunicorn_conf_test', os.path.join(DATA_DIR, 'gunicorn.conf.py'))
    mod = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as m:
        m.setattr(socket, 'socket', FakeSocket)
        spec.loader.exec_module(mod)
    return mod


def test_gunicorn_binds_internal_interface(monkeypatch):
    conf = _load_conf(monkeypatch, bind_ok=True)
    assert conf.bind == '172.30.32.1:16045' and conf.workers == 1 and conf.worker_class == 'gthread'
    log = types.SimpleNamespace(warning=lambda msg: pytest.fail('aucun avertissement attendu'))
    conf.when_ready(types.SimpleNamespace(log=log))


def test_gunicorn_falls_back_to_all_interfaces_with_warning(monkeypatch):
    conf = _load_conf(monkeypatch, bind_ok=False)
    assert conf.bind == '0.0.0.0:16045'
    warnings = []
    conf.when_ready(types.SimpleNamespace(log=types.SimpleNamespace(warning=warnings.append)))
    assert warnings and '172.30.32.1' in warnings[0]


def test_gunicorn_cleanup_on_sigterm_and_exit(monkeypatch):
    conf = _load_conf(monkeypatch, bind_ok=True)
    cleaned = []
    monkeypatch.setitem(sys.modules, 'app', types.SimpleNamespace(cleanup=lambda: cleaned.append(1)))
    previous = []
    old = signal.signal(signal.SIGTERM, lambda signum, frame: previous.append(signum))
    try:
        log = types.SimpleNamespace(warning=lambda msg: None)
        conf.post_worker_init(types.SimpleNamespace(log=log))
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)
        from helpers import wait_until
        wait_until(lambda: cleaned == [1])            # nettoyage dans un fil
        assert previous == [signal.SIGTERM]           # gestionnaire de Gunicorn chaine
        conf.worker_exit(types.SimpleNamespace(log=log), None)
        assert cleaned == [1, 1]
    finally:
        signal.signal(signal.SIGTERM, old)


def test_gunicorn_cleanup_error_is_logged(monkeypatch):
    conf = _load_conf(monkeypatch, bind_ok=True)

    def boom():
        raise RuntimeError('mqtt')
    monkeypatch.setitem(sys.modules, 'app', types.SimpleNamespace(cleanup=boom))
    warnings = []
    conf.worker_exit(types.SimpleNamespace(log=types.SimpleNamespace(warning=warnings.append)), None)
    assert warnings and 'mqtt' in warnings[0]


# --- run.sh --------------------------------------------------------------------------
PULSE_PATHS = ('/run/audio/pulse.sock', '/run/pulse/native', '/run/pulse/pulseaudio.socket', '/var/run/pulse/native')


def test_run_sh_syntax():
    assert subprocess.run(['bash', '-n', os.path.join(ROOT, 'run.sh')]).returncode == 0


@pytest.fixture
def run_sh(tmp_path):
    """run.sh hors image : bashio simule, pactl et gunicorn factices, chemin
    de l'application remplace par un dossier temporaire. Renvoie une
    fonction (pactl_info, env) -> sortie du script."""
    if shutil.which('timeout') is None:
        pytest.skip('timeout absent')
    app_dir = tmp_path / 'app'
    (app_dir / 'venv' / 'bin').mkdir(parents=True)
    (app_dir / 'venv' / 'bin' / 'activate').write_text('')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    (bin_dir / 'gunicorn').write_text('#!/bin/sh\necho "gunicorn $* PULSE_SERVER=$PULSE_SERVER PWD=$PWD"\n')
    bashio = tmp_path / 'bashio.sh'
    bashio.write_text('bashio::log.info() { echo "INFO: $*"; }\n')
    script = tmp_path / 'run.sh'
    script.write_text(open(os.path.join(ROOT, 'run.sh')).read().replace('/usr/src/app', str(app_dir)))

    def run(pactl_info='', **env):
        # pactl factice : jamais celui de la machine (reseau, delais)
        (bin_dir / 'pactl').write_text(f'#!/bin/sh\n[ "$1" = info ] && printf "%s\\n" {shlex.quote(pactl_info)}\nexit 0\n')
        for f in ('pactl', 'gunicorn'):
            os.chmod(bin_dir / f, 0o755)
        full_env = {'PATH': f"{bin_dir}:{os.environ['PATH']}", 'BASH_ENV': str(bashio), **env}
        out = subprocess.run(['bash', str(script)], env=full_env, capture_output=True, text=True, timeout=20).stdout
        return out, app_dir
    return run


@pytest.mark.skipif(any(os.path.exists(p) for p in PULSE_PATHS), reason='socket PulseAudio present sur cette machine')
def test_run_sh_finds_pulse_server_and_starts_gunicorn(run_sh):
    out, app_dir = run_sh(pactl_info='Server String: unix:/tmp/pulse.sock')
    assert 'PulseAudio: server string extrait de pactl (unix:/tmp/pulse.sock)' in out
    assert f'gunicorn -c gunicorn.conf.py app:app PULSE_SERVER=unix:/tmp/pulse.sock PWD={app_dir}' in out


def test_run_sh_keeps_existing_pulse_server(run_sh):
    out, _ = run_sh(PULSE_SERVER='tcp:audio:4713')
    assert 'PULSE_SERVER=tcp:audio:4713' in out and 'extrait de pactl' not in out
