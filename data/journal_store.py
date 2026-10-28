"""Journal conserve : detections, « presque » et de quoi les corriger
survivent a un redemarrage ou a une mise a jour de l'add-on.

classify.py garde le journal en memoire ; ce module l'ecrit dans
/data/journal.json, au plus une fois toutes les SAVE_DELAY s apres un
changement (un clap ne declenche pas d'ecriture a lui seul), et une derniere
fois a l'arret. Un fichier illisible est mis de cote, jamais bloquant.
"""
import json
import logging
import os
import threading

import atomic_io
import settings_manager

SAVE_DELAY = 2.0
VERSION = 1

_timer = None
_timer_lock = threading.Lock()
_write_lock = threading.Lock()   # une ecriture a la fois (meme fichier temporaire)


def path():
    return os.path.join(settings_manager.PERSISTENT_DIR, 'journal.json')


def read():
    """{'history', 'near', 'feedback'} enregistres, ou None : aucun fichier,
    format plus recent (retour a une version anterieure : fichier garde),
    erreur de lecture passagere (fichier garde), ou fichier illisible (mis
    de cote)."""
    p = path()
    try:
        with open(p, encoding='utf-8') as f:
            raw = f.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        logging.warning(f"Journal non relu ({e}) : fichier gardé")
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get('version'), int) and data['version'] > VERSION:
            logging.warning("Journal d'une version plus récente de ClapTrap : non relu, fichier gardé")
            return None
        if not isinstance(data, dict) or not isinstance(data.get('history'), list) \
                or not isinstance(data.get('near'), list) or not isinstance(data.get('feedback'), dict):
            raise ValueError('format inattendu')
        return data
    except ValueError as e:
        atomic_io.set_aside(p, 'Journal illisible', e)
        return None


def write(snapshot):
    """Ecriture atomique et durable (coupure : l'ancien journal ou le nouveau)."""
    # Valeurs numpy (scores, empreintes) : en nombres ou listes simples.
    atomic_io.write_json(path(), dict(snapshot, version=VERSION), ensure_ascii=False, separators=(',', ':'),
                         default=lambda o: o.tolist() if hasattr(o, 'tolist') else str(o))


def schedule(snapshot_fn):
    """Ecriture differee : les changements des SAVE_DELAY s suivantes partent
    ensemble. `snapshot_fn` est appelee au moment d'ecrire."""
    global _timer
    with _timer_lock:
        if _timer is not None:
            return
        _timer = threading.Timer(SAVE_DELAY, _run, (snapshot_fn,))
        _timer.daemon = True
        _timer.start()


def _run(snapshot_fn):
    global _timer
    with _timer_lock:
        if _timer is threading.current_thread():   # pas une minuterie plus recente
            _timer = None
    save_now(snapshot_fn)


def flush(snapshot_fn):
    """Arret de l'add-on : ecriture tout de suite (apres celle en cours, le
    cas echeant), minuterie en attente annulee."""
    global _timer
    with _timer_lock:
        pending, _timer = _timer, None
    if pending is not None:
        pending.cancel()
    save_now(snapshot_fn)


def save_now(snapshot_fn):
    with _write_lock:
        try:
            write(snapshot_fn())
        except Exception as e:   # disque plein, /data en lecture seule : le journal reste en memoire
            logging.warning(f"Journal non enregistré : {e}")
