"""Processeur et memoire de l'add-on (diagnostic) : tout le conteneur
(Gunicorn, ffmpeg, parecord) par les compteurs de son cgroup, a defaut le seul
processus Python. Sans psutil (absent de l'image)."""

import logging
import os
import threading
import time

CGROUP_ROOT = '/sys/fs/cgroup'
INTERVAL = 10.0   # s entre deux mesures
_lock = threading.Lock()
_last = None      # (instant monotone, temps processeur en s)
_latest = {'processeur': None, 'memoire_mo': None}
_thread = None


def _cgroup_dir():
    """Dossier du cgroup du conteneur (v2). Espace de noms prive : la racine ;
    partage avec l'hote : le chemin indique par /proc/self/cgroup (la racine
    serait alors tout l'hote)."""
    try:
        with open('/proc/self/cgroup') as f:
            for line in f:
                if line.startswith('0::'):
                    path = os.path.join(CGROUP_ROOT, line.strip()[3:].lstrip('/'))
                    if os.path.exists(os.path.join(path, 'cpu.stat')):
                        return path
    except OSError:
        pass
    return CGROUP_ROOT


def _cpu_seconds(cg):
    try:
        with open(os.path.join(cg, 'cpu.stat')) as f:
            for line in f:
                parts = line.split()
                if len(parts) == 2 and parts[0] == 'usage_usec':
                    return int(parts[1]) / 1e6
    except (OSError, ValueError):
        pass
    t = os.times()
    return t.user + t.system + t.children_user + t.children_system


def _memory_mb(cg):
    """Memoire utilisee, sans le cache disque recuperable (inactive_file),
    comme « docker stats » : sinon elle grimpait a chaque fichier lu."""
    try:
        with open(os.path.join(cg, 'memory.current')) as f:
            used = int(f.read().strip())
        try:
            with open(os.path.join(cg, 'memory.stat')) as f:
                for line in f:
                    parts = line.split()
                    if len(parts) == 2 and parts[0] == 'inactive_file':
                        used -= int(parts[1])
        except (OSError, ValueError):
            pass
        return max(0, used) / (1024 * 1024)
    except (OSError, ValueError):
        pass
    try:
        with open('/proc/self/status') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def cpu_percent(prev, cur):
    """% d'un coeur entre deux mesures (instant, temps processeur)."""
    if not prev or cur[0] <= prev[0]:
        return None
    return round(max(0.0, (cur[1] - prev[1]) / (cur[0] - prev[0]) * 100), 1)


def sample(cg=None):
    """{'processeur': % d'un coeur depuis la mesure precedente (None a la
    premiere), 'memoire_mo': Mo}. 100 % = un coeur entier occupe."""
    global _last
    cg = cg or _cgroup_dir()
    cur = (time.monotonic(), _cpu_seconds(cg))
    with _lock:
        prev, _last = _last, cur
    mem = _memory_mb(cg)
    return {'processeur': cpu_percent(prev, cur), 'memoire_mo': round(mem, 1) if mem is not None else None}


def _loop():
    global _latest
    cg = _cgroup_dir()
    while True:
        try:
            _latest = sample(cg)
        except Exception as e:   # le fil ne doit jamais mourir (capteurs figes)
            logging.debug(f"Mesure de performance impossible: {e}")
        time.sleep(INTERVAL)


def latest():
    """Derniere mesure (prise toutes les INTERVAL s par un fil a part :
    plusieurs lecteurs ne faussent pas l'intervalle)."""
    global _thread
    with _lock:
        if _thread is None:
            _thread = threading.Thread(target=_loop, daemon=True, name='perf')
            _thread.start()
    return dict(_latest)
