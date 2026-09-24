#!/usr/bin/env python3
"""Verifie que data/requirements.lock satisfait data/requirements.txt, et que
data/constraints.txt (versions de test) correspond au lock. Lance par la CI."""
import os
import re
import sys

from packaging.requirements import Requirement
from packaging.version import Version

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')


def canon(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def pins(path):
    out = {}
    for line in open(path):
        m = re.match(r'^([A-Za-z0-9_.-]+)==([^\s\;]+)', line)
        if m:
            out[canon(m.group(1))] = m.group(2)
    return out


def main():
    lock = pins(os.path.join(DATA, 'requirements.lock'))
    errors = []
    for line in open(os.path.join(DATA, 'requirements.txt')):
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        req = Requirement(line)
        pinned = lock.get(canon(req.name))
        if pinned is None:
            errors.append(f"{req.name} absent du lock")
        elif not req.specifier.contains(Version(pinned), prereleases=True):
            errors.append(f"{req.name}=={pinned} ne satisfait pas « {req.specifier} »")
    constraints = pins(os.path.join(DATA, 'constraints.txt'))
    if constraints != lock:
        errors.append("constraints.txt ne correspond pas au lock (python3 scripts/lock_requirements.py freeze.txt)")
    for e in errors:
        print('ERREUR :', e)
    if errors:
        sys.exit(1)
    print(f"lock cohérent ({len(lock)} paquets)")


if __name__ == '__main__':
    main()
