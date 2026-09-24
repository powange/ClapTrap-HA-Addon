#!/usr/bin/env python3
"""Genere data/requirements.lock (versions exactes + empreintes sha256) et
data/constraints.txt (memes versions, sans empreintes, pour les tests).

1. Resoudre data/requirements.txt dans le meme Python que l'image (3.13,
   Debian 13) :
     docker run --rm -v "$PWD/data:/d:ro" python:3.13-slim \\
       sh -c "pip install -q -r /d/requirements.txt && pip freeze" > freeze.txt
2. python3 scripts/lock_requirements.py freeze.txt
3. Construire l'image et lancer les tests (la CI verifie la coherence avec
   scripts/check_lock.py).

Les empreintes couvrent TOUS les fichiers publies sur PyPI pour chaque
version (roues amd64 et aarch64 comprises) : le meme fichier sert aux deux
architectures, et pip refuse tout fichier modifie ou toute autre version.
"""
import json
import os
import sys
import urllib.request


def main(path):
    data = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
    lock = ["# Genere par scripts/lock_requirements.py : ne pas modifier a la main."]
    constraints = ["# Genere par scripts/lock_requirements.py : versions de requirements.lock",
                   "# sans empreintes, pour installer les dependances de test (requirements-dev.txt)."]
    for line in open(path):
        line = line.strip()
        if not line or line.startswith('#') or '==' not in line:
            continue
        name, version = line.split('==', 1)
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json") as r:
            files = json.load(r)['urls']
        hashes = sorted({f['digests']['sha256'] for f in files})
        if not hashes:
            sys.exit(f"{name}=={version} : aucun fichier sur PyPI")
        lock.append(f"{name}=={version} \\")
        lock.append(" \\\n".join(f"    --hash=sha256:{h}" for h in hashes))
        constraints.append(f"{name}=={version}")
    open(os.path.join(data, 'requirements.lock'), 'w').write("\n".join(lock) + "\n")
    open(os.path.join(data, 'constraints.txt'), 'w').write("\n".join(constraints) + "\n")


if __name__ == '__main__':
    main(sys.argv[1])
