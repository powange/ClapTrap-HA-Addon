"""Processeur et memoire de l'add-on (diagnostic) : tout le conteneur
(Gunicorn, ffmpeg, parecord) par les compteurs de son cgroup s'il contient
bien ce processus, sinon la somme des processus visibles dans /proc (espace
de noms du conteneur : ceux de l'add-on seulement). Sans psutil (absent de
l'image)."""

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


PROC = '/proc'


def _owns(path):
    """Le cgroup contient-il ce processus ? Sinon (hierarchie de l'hote
    montee dans le conteneur, cgroup v1) ses compteurs seraient ceux d'un
    autre groupe, voire de tout l'hote : l'en-tete affichait l'usage de tout
    Home Assistant."""
    try:
        with open(os.path.join(path, 'cgroup.procs')) as f:
            return str(os.getpid()) in f.read().split()
    except OSError:
        return False


def _cgroup_dir():
    """Dossier du cgroup du conteneur (v2) s'il contient ce processus, sinon
    None (somme des processus de /proc). Espace de noms prive : la racine ;
    partage avec l'hote : le chemin indique par /proc/self/cgroup."""
    try:
        with open(os.path.join(PROC, 'self', 'cgroup')) as f:
            for line in f:
                if line.startswith('0::'):
                    path = os.path.join(CGROUP_ROOT, line.strip()[3:].lstrip('/'))
                    if os.path.exists(os.path.join(path, 'cpu.stat')) and _owns(path):
                        return path
    except OSError:
        pass
    return None


MAX_PROCS = 64   # au-dela, /proc montre l'hote (pas d'espace de noms prive) : pas une somme de l'add-on
PROC_MODE = 'proc'   # mesure par la somme des processus de /proc (pas de cgroup fiable)


def _proc_totals():
    """(temps processeur en s, memoire en Mo) des processus visibles dans
    /proc, en un passage, ou None si /proc est illisible ou montre l'hote.
    Temps : propre et des enfants termines (cutime, cstime : un ffmpeg
    relance ne fait pas chuter le total). Memoire : PSS (pages partagees
    comptees une fois), a defaut RSS."""
    try:
        pids = [d for d in os.listdir(PROC) if d.isdigit()]
    except OSError:
        return None
    if not pids or len(pids) > MAX_PROCS:
        return None
    tick, page = os.sysconf('SC_CLK_TCK'), os.sysconf('SC_PAGE_SIZE')
    cpu, mem_kb = 0, 0
    for pid in pids:
        base = os.path.join(PROC, pid)
        try:
            with open(os.path.join(base, 'stat')) as f:
                fields = f.read().rsplit(')', 1)[1].split()
            cpu += sum(int(x) for x in fields[11:15])   # utime, stime, cutime, cstime
        except (OSError, ValueError, IndexError):
            continue   # termine entre-temps
        try:
            with open(os.path.join(base, 'smaps_rollup')) as f:
                mem_kb += next(int(line.split()[1]) for line in f if line.startswith('Pss:'))
        except (OSError, ValueError, IndexError, StopIteration):
            try:
                with open(os.path.join(base, 'statm')) as f:
                    mem_kb += int(f.read().split()[1]) * page // 1024
            except (OSError, ValueError, IndexError):
                pass
    return cpu / tick, mem_kb / 1024


def _measure_mode():
    """Une fois au demarrage : cgroup du conteneur s'il contient ce
    processus, sinon somme de /proc si elle ne montre que l'add-on, sinon
    None (ce seul processus, comme avant)."""
    cg = _cgroup_dir()
    if cg is not None:
        return cg
    return PROC_MODE if _proc_totals() is not None else None


def _self_cpu_seconds():
    t = os.times()
    return t.user + t.system + t.children_user + t.children_system


def _self_memory_mb():
    try:
        with open('/proc/self/status') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _cpu_seconds(cg):
    """Temps processeur du cgroup `cg`, a defaut de ce seul processus."""
    try:
        with open(os.path.join(cg, 'cpu.stat')) as f:
            for line in f:
                parts = line.split()
                if len(parts) == 2 and parts[0] == 'usage_usec':
                    return int(parts[1]) / 1e6
    except (OSError, ValueError, TypeError):
        pass
    return _self_cpu_seconds()


def _memory_mb(cg):
    """Memoire du cgroup `cg`, sans le cache disque recuperable
    (inactive_file), comme « docker stats » : sinon elle grimpait a chaque
    fichier lu. A defaut, celle de ce seul processus."""
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
    except (OSError, ValueError, TypeError):
        pass
    return _self_memory_mb()


def _read(mode):
    """(temps processeur en s, memoire en Mo) selon le mode de mesure."""
    if mode == PROC_MODE:
        totals = _proc_totals()   # un seul passage pour les deux
        if totals is not None:
            return totals
        mode = None   # /proc devenu illisible : ce seul processus
    return _cpu_seconds(mode), _memory_mb(mode)


def cpu_percent(prev, cur):
    """% d'un coeur entre deux mesures (instant, temps processeur)."""
    if not prev or cur[0] <= prev[0]:
        return None
    return round(max(0.0, (cur[1] - prev[1]) / (cur[0] - prev[0]) * 100), 1)


_AUTO = object()


def sample(cg=_AUTO):
    """{'processeur': % d'un coeur depuis la mesure precedente (None a la
    premiere), 'memoire_mo': Mo}. 100 % = un coeur entier occupe."""
    global _last
    if cg is _AUTO:
        cg = _measure_mode()
    cpu, mem = _read(cg)
    cur = (time.monotonic(), cpu)
    with _lock:
        prev, _last = _last, cur
    return {'processeur': cpu_percent(prev, cur), 'memoire_mo': round(mem, 1) if mem is not None else None}


def _loop():
    global _latest
    cg = _measure_mode()   # une fois : cgroup, somme de /proc ou ce seul processus
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
