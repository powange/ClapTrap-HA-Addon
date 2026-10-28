"""Fichiers JSON de l'add-on (reglages, sons appris, journal, blueprints) :
ecriture atomique et durable, et mise de cote d'un fichier illisible.

Un seul code pour tous : avant, quatre copies divergeaient (synchronisation
du dossier oubliee, fichier temporaire laisse en cas d'erreur).
"""
import json
import logging
import os
import shutil
import time


def fsync_dir(path):
    """Le renommage lui-meme sur le disque (carte SD : coupure de courant)."""
    try:
        fd = os.open(path or '.', os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def write_json(path, obj, tmp=None, before_replace=None, **dump_kw):
    """Ecrit `obj` dans `path` : l'ancien fichier ou le nouveau, jamais un
    fichier tronque. `before_replace` : appele juste avant le remplacement
    (copie de sauvegarde des reglages). En cas d'erreur (valeur non
    serialisable, disque plein), le fichier temporaire est supprime et
    l'ancien fichier reste intact."""
    tmp = tmp or path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(obj, f, **dump_kw)
            f.flush()
            os.fsync(f.fileno())
        if before_replace is not None:
            before_replace()
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    fsync_dir(os.path.dirname(path))


def copy_file(src, dst):
    """Copie atomique et durable (sauvegarde des reglages : le fichier qui
    doit survivre si l'original est abime)."""
    tmp = dst + '.tmp'
    try:
        shutil.copy2(src, tmp)
        with open(tmp, 'rb') as f:
            os.fsync(f.fileno())
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    fsync_dir(os.path.dirname(dst))


def set_aside(path, what, error):
    """Fichier illisible garde sous `<nom>.corrompu-<date>` : l'ecriture
    suivante ne l'ecrase pas en silence. `what` : debut du message
    (« Sons appris illisibles »)."""
    aside = f"{path}.corrompu-{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        os.replace(path, aside)
        logging.error(f"{what} ({error}) : fichier gardé sous {os.path.basename(aside)}")
    except OSError:
        logging.error(f"{what} ({error})")
