"""Renommage des entity_id dans le registre de Home Assistant.

Renommer un groupe change ses entity_id (ils suivent son nom) sans changer
ses unique_id : ClapTrap demande a HA de renommer les entites existantes
(API WebSocket, `config/entity_registry/update`), comme un renommage a la
main dans Parametres > Entites. L'historique, les statistiques et les
personnalisations (piece, icone, nom) suivent l'entite.

Le renommage a faire est note dans le groupe (`_renamed_from`) jusqu'a ce
qu'il soit fait : il est retente si HA est injoignable (redemarrage de HA,
de l'add-on, reconnexion MQTT). Une entite dont l'id a ete choisi a la main
dans HA n'est pas touchee.
"""

import json
import logging
import os
import threading
import time

WS_URL = 'ws://supervisor/core/websocket'
RETRY_DELAY = 60.0        # s avant de retenter si HA est injoignable
SETTLE_DELAY = 2.0        # s laissees a HA pour traiter la nouvelle configuration MQTT
TIMEOUT = 10.0

_lock = threading.Lock()
_run_lock = threading.Lock()   # une reconciliation a la fois
_timer = None


class RegistryError(Exception):
    pass


def schedule_reconcile(delay=SETTLE_DELAY):
    """Renommages en attente traites un peu plus tard (une seule fois)."""
    global _timer
    with _lock:
        if _timer is not None:
            return
        _timer = threading.Timer(delay, _run)
        _timer.daemon = True
        _timer.start()


def _run():
    global _timer
    with _lock:
        _timer = None
    try:
        result = reconcile()
    except Exception as e:
        logging.warning(f"Renommage des entités HA : {e}")
        result = None
    if result is None:
        schedule_reconcile(RETRY_DELAY)


def pending_renames(settings):
    """[(cle du groupe, etat lu, [(unique_id, {anciens ids}, nouvel id)])] des
    groupes renommes dont les entites restent a renommer dans HA."""
    import ha_entities as ha
    out = []
    for ui_key, key, _label, groups, _avail, norm in ha._resolve_sources(settings)[0]:
        src_slug = ha._make_slug(key)
        for g in groups or []:
            if not isinstance(g, dict) or not g.get('_renamed_from') or g.get('slug') not in norm:
                continue
            slug, g_info = g['slug'], norm[g['slug']]
            olds = {}
            for prev in g['_renamed_from'].split():
                for uid, eid in ha.group_entity_ids(src_slug, slug, g_info, entity_slug=prev):
                    olds.setdefault(uid, set()).add(eid)
            items = [(uid, olds.get(uid, set()), new) for uid, new in ha.group_entity_ids(src_slug, slug, g_info)]
            out.append(((ui_key, slug), (g.get('entity_slug'), g['_renamed_from']), items))
    return out


class _Session:
    """Connexion WebSocket a HA via le Supervisor (jeton de l'add-on)."""

    def __init__(self, token):
        import simple_websocket
        try:
            self.ws = simple_websocket.Client.connect(WS_URL)
        except Exception as e:
            raise RegistryError(f"Home Assistant injoignable ({e})")
        self._id = 0
        if self._receive().get('type') != 'auth_required':
            raise RegistryError("réponse inattendue de Home Assistant")
        self._send({'type': 'auth', 'access_token': token})
        if self._receive().get('type') != 'auth_ok':
            raise RegistryError("authentification refusée par Home Assistant")

    def _send(self, payload):
        try:
            self.ws.send(json.dumps(payload))
        except Exception as e:
            raise RegistryError(f"connexion à Home Assistant perdue ({e})")

    def _receive(self):
        # Connexion coupee (redemarrage de HA), reponse illisible : a retenter.
        try:
            data = self.ws.receive(timeout=TIMEOUT)
            if data is None:
                raise RegistryError("pas de réponse de Home Assistant")
            return json.loads(data)
        except RegistryError:
            raise
        except Exception as e:
            raise RegistryError(f"connexion à Home Assistant perdue ({e})")

    def call(self, payload):
        self._id += 1
        self._send(dict(payload, id=self._id))
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            msg = self._receive()
            if msg.get('id') == self._id and msg.get('type') == 'result':
                return msg
        raise RegistryError("pas de réponse de Home Assistant")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def _connect():
    token = os.environ.get('SUPERVISOR_TOKEN')
    return _Session(token) if token else None


def reconcile():
    """Renomme dans HA les entites des groupes renommes. Renvoie le compte
    rendu (lignes), ou None si HA est injoignable (a retenter)."""
    with _run_lock:
        return _reconcile()


def _reconcile():
    from settings_manager import load_settings, atomic_update, NO_CHANGE
    pending = pending_renames(load_settings())
    if not pending:
        return []
    try:
        session = _connect()
    except RegistryError as e:
        logging.warning(f"Renommage des entités HA reporté : {e}")
        return None
    report = []
    if session is None:
        # Hors de Home Assistant (pas de Supervisor) : pas de registre a mettre
        # a jour, les nouvelles entites prennent directement le nouvel id.
        logging.info("Renommage des entités : hors de Home Assistant, rien à renommer")
    else:
        try:
            entries = session.call({'type': 'config/entity_registry/list'})
            if not entries.get('success'):
                raise RegistryError((entries.get('error') or {}).get('message') or 'registre indisponible')
            by_uid = {e.get('unique_id'): e.get('entity_id') for e in entries.get('result') or []
                      if e.get('platform') == 'mqtt'}
            todo = []
            for _group, _snap, items in pending:
                for uid, olds, new in items:
                    current = by_uid.get(uid)
                    if current is None or current == new:
                        continue   # pas encore creee (elle le sera sous le nouvel id) ou deja renommee
                    if current not in olds:
                        line = f"{current} : id choisi dans Home Assistant, conservé"
                        logging.info(f"Renommage HA : {line}")
                        report.append(line)
                        continue
                    todo.append((current, new))
            # Plusieurs passes : un id libere par un renommage peut en
            # debloquer un autre (deux groupes renommes l'un apres l'autre).
            errors = {}
            while todo:
                left = []
                for old, new in todo:
                    res = session.call({'type': 'config/entity_registry/update',
                                        'entity_id': old, 'new_entity_id': new})
                    if res.get('success'):
                        logging.info(f"Renommage HA : {old} → {new}")
                        report.append(f"{old} → {new}")
                    else:
                        errors[(old, new)] = (res.get('error') or {}).get('message') or 'refusé'
                        left.append((old, new))
                if len(left) == len(todo):
                    break
                todo = left
            for old, new in todo:
                # Id deja pris par une autre entite de HA : a regler dans HA.
                logging.warning(f"Renommage HA impossible : {old} → {new} ({errors[(old, new)]})")
                report.append(f"{old} → {new} : impossible ({errors[(old, new)]})")
        except RegistryError as e:
            logging.warning(f"Renommage des entités HA interrompu : {e}")
            return None
        finally:
            session.close()

    done = {group: snap for group, snap, _items in pending}

    def _clear(settings):
        import ha_entities as ha
        changed = False
        for ui_key, _key, _l, groups, _a, _n in ha._resolve_sources(settings)[0]:
            for g in groups or []:
                if not isinstance(g, dict):
                    continue
                snap = done.get((ui_key, g.get('slug')))
                # Renomme a nouveau pendant la reconciliation : garder pour la suivante.
                if snap and snap == (g.get('entity_slug'), g.get('_renamed_from')):
                    g.pop('_renamed_from', None)
                    changed = True
        return settings if changed else NO_CHANGE
    atomic_update(_clear)
    if report:
        try:
            from ha_control import _publish_result
            _publish_result({'action': 'rename_entities', 'success': not any('impossible' in r for r in report),
                             'renamed': report})
        except Exception:
            pass
    return report
