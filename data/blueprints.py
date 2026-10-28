"""Blueprints Home Assistant fournis avec ClapTrap (onglet Blueprints).

Les fichiers de blueprints/automation/claptrap/ sont livres dans l'image :
la version installee correspond toujours a celle de l'add-on. Leur etat dans
Home Assistant vient de l'API WebSocket (blueprint/list) : un blueprint de
ClapTrap y est reconnu par son source_url (qu'il ait ete importe par le
bouton de la documentation ou installe d'ici). Installer ou mettre a jour
passe par blueprint/save, au meme chemin que l'existant : les automatisations
qui l'utilisent le gardent, puis sont rechargees.

Etat : empreinte du fichier enregistree a chaque installation par ClapTrap
(/data/blueprints_state.json), confrontee a ce que Home Assistant en montre
(nom, description, entrees). Un blueprint importe ailleurs (bouton de la
documentation) qui differe est « different » (peut-etre plus recent : on ne
parle pas de mise a jour). Home Assistant n'expose pas le contenu complet :
« Reinstaller » reste possible sur un blueprint a jour.
"""
import functools
import hashlib
import json
import logging
import os
import re
import threading

import atomic_io

DOMAIN = 'automation'
_lock = threading.Lock()


class BlueprintError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _dir():
    """Dossier des blueprints : a cote du code dans l'image, a la racine du
    depot en developpement."""
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (here, os.path.dirname(here)):
        path = os.path.join(base, 'blueprints', DOMAIN, 'claptrap')
        if os.path.isdir(path):
            return path
    return os.path.join(here, 'blueprints', DOMAIN, 'claptrap')


def _state_path():
    import settings_manager
    return os.path.join(settings_manager.PERSISTENT_DIR, 'blueprints_state.json')


# ===== Lecture des fichiers livres ================================================

def parse_header(text):
    """Nom, description, source_url et entrees du bloc `blueprint:` (sous-
    ensemble YAML de nos fichiers : valeurs simples, description repliee
    `>-`). Verifie par les tests contre un vrai parseur YAML."""
    meta = {'name': '', 'description': '', 'source_url': '', 'input': []}
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n and lines[i].rstrip() != 'blueprint:':
        i += 1
    i += 1
    while i < n:
        line = lines[i]
        if line.lstrip().startswith('#'):
            i += 1
            continue
        if line and not line.startswith(' '):
            break                                   # fin du bloc blueprint
        m = re.match(r'^  (\w+):\s*(.*)$', line)
        if m:
            key, value = m.group(1), m.group(2).strip()
            if key == 'description' and value in ('>-', '>', '|', '|-'):
                parts = []
                i += 1
                while i < n and (lines[i].startswith('    ') or not lines[i].strip()):
                    parts.append(lines[i].strip())
                    i += 1
                sep = '\n' if value.startswith('|') else ' '
                meta['description'] = sep.join(p for p in parts if p) if sep == ' ' else '\n'.join(parts).strip()
                continue
            if key == 'input':
                i += 1
                while i < n and (lines[i].startswith('    ') or not lines[i].strip() or lines[i].lstrip().startswith('#')):
                    k = re.match(r'^    ([\w-]+):', lines[i])
                    if k:
                        meta['input'].append(k.group(1))
                    i += 1
                continue
            if key in ('name', 'source_url', 'description'):
                meta[key] = value.strip('"\'')
        i += 1
    return meta


def bundled():
    """Blueprints livres : [{file, yaml, sha, name, description, source_url, input}]."""
    return [dict(b) for b in _read_bundled(_dir())]


@functools.lru_cache(maxsize=2)
def _read_bundled(folder):
    """Fichiers de l'image (inchanges pendant sa vie) : lus une fois."""
    out = []
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if not name.endswith('.yaml'):
            continue
        with open(os.path.join(folder, name), encoding='utf-8') as f:
            text = f.read()
        out.append(dict(parse_header(text), file=name, yaml=text,
                        sha=hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]))
    return tuple(out)


# ===== Etat dans Home Assistant ====================================================

def _read_state():
    try:
        with open(_state_path(), encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except ValueError as e:
        # Mis de cote : l'installation suivante ne l'ecrase pas en silence.
        atomic_io.set_aside(_state_path(), 'État des blueprints illisible', e)
        return {}
    except OSError:
        return {}


def _write_state(state):
    atomic_io.write_json(_state_path(), state)


def _connect():
    """Session WebSocket simple : sans nouvel envoi automatique apres une
    coupure (un blueprint/save renvoye repondrait « existe deja » alors qu'il
    a ete enregistre)."""
    import ha_registry
    try:
        session = ha_registry._connect()
    except ha_registry.RegistryError as e:
        raise BlueprintError(str(e), 503)
    if session is None:
        raise BlueprintError("Home Assistant injoignable : l'onglet ne fonctionne que dans l'add-on.", 503)
    return session


def _call(conn, payload):
    import ha_registry
    try:
        msg = conn.call(payload)
    except ha_registry.RegistryError as e:
        raise BlueprintError(str(e), 503)
    if not msg.get('success'):
        err = msg.get('error') or {}
        raise BlueprintError(f"Home Assistant : {err.get('message') or err.get('code') or 'refus'}", 502)
    return msg.get('result')


def _same_file(url, file):
    """source_url d'un blueprint de ClapTrap (meme nom de fichier, depot
    ClapTrap, branche quelconque ou lien brut)."""
    return bool(url) and url.rstrip('/').rsplit('/', 1)[-1] == file and 'ClapTrap-HA-Addon' in url


def _same_meta(meta, bp):
    return (meta.get('name') == bp['name'] and ' '.join((meta.get('description') or '').split()) == bp['description']
            and sorted((meta.get('input') or {}).keys()) == sorted(bp['input']))


def _status(bp, installed, state):
    """(etat, chemin dans HA). Etats : 'absent' ; 'a_jour' ; 'mise_a_jour'
    (installe par ClapTrap dans une version plus ancienne) ; 'different'
    (importe ailleurs, ou modifie : version inconnue, peut-etre plus recente)."""
    path, meta = next(((p, m) for p, m in installed.items() if _same_file(m.get('source_url'), bp['file'])),
                      (None, None))
    if path is None:
        default = f"claptrap/{bp['file']}"
        # Meme nom de fichier, autre origine (copie, fork) : ne pas l'ecraser
        # en silence, mais permettre de le remplacer.
        return ('different', default) if default in installed else ('absent', None)
    known = state.get(bp['file'])
    if known and known.get('path') == path:
        if known.get('sha') != bp['sha']:
            return 'mise_a_jour', path
        # Installe par ClapTrap : HA montre-t-il encore la meme chose
        # (sauvegarde restauree, modifie dans HA) ?
        return ('a_jour' if _same_meta(meta, bp) else 'different'), path
    return ('a_jour' if _same_meta(meta, bp) else 'different'), path


def _installed(conn):
    result = _call(conn, {'type': 'blueprint/list', 'domain': DOMAIN}) or {}
    return {path: (info or {}).get('metadata') or {} for path, info in result.items()
            if isinstance(info, dict) and 'error' not in info}


def list_blueprints():
    """(blueprints livres et leur etat dans Home Assistant, erreur). Home
    Assistant injoignable : la liste quand meme (etat inconnu), pour
    l'import par le site my.home-assistant."""
    items = bundled()
    installed, error = None, None
    try:
        conn = _connect()
        try:
            installed = _installed(conn)
        finally:
            conn.close()
    except BlueprintError as e:
        error = str(e)
    state = _read_state()
    out = []
    for bp in items:
        status, path = _status(bp, installed, state) if installed is not None else ('inconnu', None)
        out.append({'file': bp['file'], 'name': bp['name'], 'description': bp['description'],
                    'source_url': bp['source_url'], 'status': status, 'path': path})
    return out, error


def install(file):
    """Ajoute ou met a jour un blueprint dans Home Assistant (au chemin
    existant s'il y est deja), puis recharge les automatisations."""
    bp = next((b for b in bundled() if b['file'] == file), None)
    if bp is None:
        raise BlueprintError('blueprint inconnu', 404)
    with _lock:
        conn = _connect()
        try:
            installed = _installed(conn)
            _status_, path = _status(bp, installed, _read_state())
            target = path or f"claptrap/{file}"
            _call(conn, {'type': 'blueprint/save', 'domain': DOMAIN, 'path': target[:-5] if target.endswith('.yaml') else target,
                         'yaml': bp['yaml'], 'source_url': bp['source_url'], 'allow_override': path is not None})
            if path is not None:
                # Automatisations qui l'utilisent : nouvelle version tout de
                # suite (un premier ajout n'en a aucune).
                try:
                    _call(conn, {'type': 'call_service', 'domain': 'automation', 'service': 'reload'})
                except BlueprintError as e:
                    logging.warning(f"Blueprint {file} installé, automatisations non rechargées : {e}")
        finally:
            conn.close()
        state = _read_state()
        state[file] = {'sha': bp['sha'], 'path': target if target.endswith('.yaml') else target + '.yaml'}
        _write_state(state)
    logging.info(f"Blueprint {'mis à jour' if path else 'ajouté'} dans Home Assistant : {bp['name']}")
    return {'file': file, 'status': 'a_jour', 'path': state[file]['path'], 'updated': path is not None}
