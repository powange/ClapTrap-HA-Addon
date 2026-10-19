"""Blueprints Home Assistant fournis avec ClapTrap (onglet Blueprints).

Les fichiers de blueprints/automation/claptrap/ sont livres dans l'image :
la version installee correspond toujours a celle de l'add-on. Leur etat dans
Home Assistant vient de l'API WebSocket (blueprint/list) : un blueprint de
ClapTrap y est reconnu par son source_url (qu'il ait ete importe par le
bouton de la documentation ou installe d'ici). Installer ou mettre a jour
passe par blueprint/save, au meme chemin que l'existant : les automatisations
qui l'utilisent le gardent, puis sont rechargees.

Version installee : empreinte du fichier enregistree a chaque installation
par ClapTrap (/data/blueprints_state.json) ; pour un blueprint importe
ailleurs, comparaison de ce que Home Assistant en montre (nom, description,
entrees).
"""
import hashlib
import json
import logging
import os
import re
import threading

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
                while i < n and (lines[i].startswith('    ') or not lines[i].strip()):
                    k = re.match(r'^    ([\w-]+):\s*$', lines[i])
                    if k:
                        meta['input'].append(k.group(1))
                    i += 1
                continue
            if key in ('name', 'source_url'):
                meta[key] = value.strip('"\'')
        i += 1
    return meta


def bundled():
    """Blueprints livres : [{file, yaml, sha, name, description, source_url, input}]."""
    out = []
    folder = _dir()
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if not name.endswith('.yaml'):
            continue
        with open(os.path.join(folder, name), encoding='utf-8') as f:
            text = f.read()
        out.append(dict(parse_header(text), file=name, yaml=text,
                        sha=hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]))
    return out


# ===== Etat dans Home Assistant ====================================================

def _read_state():
    try:
        with open(_state_path(), encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(state):
    path = _state_path()
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _connect():
    import ha_registry
    session = ha_registry._connect()
    if session is None:
        raise BlueprintError("Home Assistant injoignable : l'onglet ne fonctionne que dans l'add-on.", 503)
    return ha_registry._Conn(session)


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


def _status(bp, installed, state):
    """'absent', 'a_jour' ou 'mise_a_jour' (+ chemin dans HA)."""
    path, meta = next(((p, m) for p, m in installed.items() if _same_file(m.get('source_url'), bp['file'])),
                      (None, None))
    if path is None:
        return 'absent', None
    known = state.get(bp['file'])
    if known and known.get('path') == path:
        return ('a_jour' if known.get('sha') == bp['sha'] else 'mise_a_jour'), path
    # Importe ailleurs (bouton de la documentation) : ce que HA en montre.
    same = (meta.get('name') == bp['name'] and ' '.join((meta.get('description') or '').split()) == bp['description']
            and sorted((meta.get('input') or {}).keys()) == sorted(bp['input']))
    return ('a_jour' if same else 'mise_a_jour'), path


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
            # Automatisations qui l'utilisent : nouvelle version tout de suite.
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
