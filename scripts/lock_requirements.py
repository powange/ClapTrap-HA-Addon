#!/usr/bin/env python3
"""Genere data/requirements.lock (versions exactes + empreintes sha256).

1. Construire l'image avec data/requirements.txt (contraintes lisibles) ;
2. relever les versions installees :
     docker run --rm --entrypoint /usr/src/app/venv/bin/pip <image> freeze > freeze.txt
3. python3 scripts/lock_requirements.py freeze.txt > data/requirements.lock

Les empreintes couvrent TOUS les fichiers publies sur PyPI pour chaque
version (roues amd64 et aarch64 comprises) : le meme fichier sert aux deux
architectures, et pip refuse tout fichier modifie ou toute autre version.
"""
import json
import sys
import urllib.request


def main(path):
    print("# Genere par scripts/lock_requirements.py : ne pas modifier a la main.")
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
        print(f"{name}=={version} \\")
        print(" \\\n".join(f"    --hash=sha256:{h}" for h in hashes))


if __name__ == '__main__':
    main(sys.argv[1])
