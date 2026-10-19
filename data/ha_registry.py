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
RETRY_DELAYS = (60.0, 300.0, 900.0)   # s avant de retenter si HA est injoignable
MAX_MESSAGE = None        # aucune limite cote client (la limite 1009 venait du proxy)
RECONNECTS = 3            # reprises apres une coupure pendant une operation
BATCH = 100               # entites par appel get_entries
# Entites de ClapTrap (event compris : celle d'un groupe renomme, a l'entity_id
# personnalise, etait comptee « introuvable » au lieu de « conservee »).
DOMAINS = {'binary_sensor', 'event', 'number', 'switch', 'sensor', 'text'}
SETTLE_DELAY = 2.0        # s laissees a HA pour traiter la nouvelle configuration MQTT
TIMEOUT = 10.0

_lock = threading.Lock()
_run_lock = threading.Lock()   # une reconciliation a la fois
_timer = None
_timer_at = 0.0           # instant prevu de la tentative programmee
_failures = 0             # echecs consecutifs (delai de la tentative suivante)
_retry = False            # erreurs passageres lors de la derniere operation


class RegistryError(Exception):
    pass


class _Timeout(RegistryError):
    """Pas de reponse dans le delai (requete lente, pas une coupure)."""


def schedule_reconcile(delay=SETTLE_DELAY):
    """Renommages en attente traites un peu plus tard (une seule fois) ; une
    demande plus proche (nouveau renommage, HA de retour) avance une
    tentative lointaine programmee apres des echecs."""
    global _timer, _timer_at
    with _lock:
        at = time.monotonic() + delay
        if _timer is not None:
            if at >= _timer_at:
                return
            _timer.cancel()
        _timer, _timer_at = threading.Timer(delay, _run), at
        _timer.daemon = True
        _timer.start()


def _run():
    global _timer
    with _lock:
        _timer = None
    global _failures
    try:
        result = reconcile()
    except Exception as e:
        logging.warning(f"Renommage des entités HA : {e}")
        result = None
    if result is None or _retry:
        _failures += 1
        schedule_reconcile(RETRY_DELAYS[min(_failures, len(RETRY_DELAYS)) - 1])
    else:
        _failures = 0


def old_ids(src_slug, slug, g_info, renamed_from):
    """{unique_id: {ids que l'entite peut encore avoir dans HA}} d'apres
    les noms precedents du groupe."""
    import ha_entities as ha
    olds = {}
    for prev in (renamed_from or '').split():
        for uid, eid in ha.group_entity_ids(src_slug, slug, g_info, entity_slug=prev):
            olds.setdefault(uid, set()).add(eid)
    return olds


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
            olds = old_ids(src_slug, slug, g_info, g['_renamed_from'])
            items = [(uid, olds.get(uid, set()), new) for uid, new in ha.group_entity_ids(src_slug, slug, g_info)]
            out.append(((ui_key, slug), (g.get('entity_slug'), g['_renamed_from']), items))
    return out


class _Session:
    """Connexion WebSocket a HA via le Supervisor (jeton de l'add-on)."""

    def __init__(self, token):
        import simple_websocket
        try:
            self.ws = simple_websocket.Client.connect(WS_URL, max_message_size=MAX_MESSAGE)
        except Exception as e:
            raise RegistryError(f"Home Assistant injoignable ({e})")
        self._id = 0
        try:
            if self._receive().get('type') != 'auth_required':
                raise RegistryError("réponse inattendue de Home Assistant")
            self._send({'type': 'auth', 'access_token': token})
            if self._receive().get('type') != 'auth_ok':
                raise RegistryError("authentification refusée par Home Assistant")
        except Exception:
            # Socket ferme : il fuyait a chaque tentative (60, 300, 900 s).
            try:
                self.ws.close()
            except Exception:
                pass
            raise

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
                raise _Timeout("pas de réponse de Home Assistant")
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
        raise _Timeout("pas de réponse de Home Assistant")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def _connect():
    token = os.environ.get('SUPERVISOR_TOKEN')
    return _Session(token) if token else None


class _Conn:
    """Session reprise apres une coupure (redemarrage de HA, proxy) : les
    operations etant idempotentes, l'appel coupe est simplement refait."""

    def __init__(self, session):
        self.session, self.reconnects = session, 0

    def call(self, payload):
        while True:
            try:
                return self.session.call(payload)
            except _Timeout:
                raise   # requete lente : la renvoyer ne ferait qu'ajouter de la charge
            except RegistryError as e:
                if self.reconnects >= RECONNECTS:
                    raise
                self.reconnects += 1
                logging.info(f"Renommage HA : connexion perdue ({e}), reprise {self.reconnects}/{RECONNECTS}")
                self.session.close()
                time.sleep(min(2.0 * self.reconnects, 5.0))
                session = _connect()
                if session is None:
                    raise
                self.session = session

    def close(self):
        self.session.close()


def _get(conn, entity_id):
    """Entree du registre (None si l'entite n'existe pas)."""
    res = conn.call({'type': 'config/entity_registry/get', 'entity_id': entity_id})
    return res.get('result') if res.get('success') else None


def _ours(entry, uid):
    return bool(entry) and entry.get('unique_id') == uid and entry.get('platform') == 'mqtt'


class _UidIndex:
    """Dernier recours : entity_id actuel d'apres le unique_id, sans charger
    le registre complet (un message de plusieurs Mo, coupe par le proxy du
    Supervisor : erreur 1009). Liste compacte (sans unique_id), puis lecture
    par lots des seules entites MQTT des domaines de ClapTrap (un id choisi
    a la main ne commence pas forcement par « claptrap_ »). Construit une
    fois par operation, et seulement si besoin."""

    def __init__(self, conn):
        self.conn, self._map = conn, None

    def lookup(self, uid):
        if self._map is None:
            self._map = {}
            res = self.conn.call({'type': 'config/entity_registry/list_for_display'})
            if not res.get('success'):
                raise RegistryError('liste du registre indisponible')
            ids = [e.get('ei') for e in (res.get('result') or {}).get('entities') or []
                   if e.get('pl') == 'mqtt' and str(e.get('ei', '')).split('.', 1)[0] in DOMAINS]
            for i in range(0, len(ids), BATCH):
                got = self.conn.call({'type': 'config/entity_registry/get_entries', 'entity_ids': ids[i:i + BATCH]})
                for eid, entry in ((got.get('result') or {}) if got.get('success') else {}).items():
                    if entry and entry.get('platform') == 'mqtt':
                        self._map[entry.get('unique_id')] = eid
        return self._map.get(uid)


# Elements de HA qui citent une entite et ne suivent pas son renommage.
USAGE_TYPES = {'automation': 'automatisation', 'script': 'script', 'scene': 'scène'}


def _usage(conn, entity_ids):
    """[{id, nom, type, entites}] des automatisations, scripts et scenes qui
    utilisent ces entites (recherche « elements lies » de HA ; un nom lu
    une seule fois par element)."""
    found = {}
    for eid in entity_ids:
        res = conn.call({'type': 'search/related', 'item_type': 'entity', 'item_id': eid})
        if not res.get('success'):
            continue
        for kind in USAGE_TYPES:
            for item in (res.get('result') or {}).get(kind) or []:
                entry = found.get(item)
                if entry is None:
                    reg = _get(conn, item) or {}
                    entry = found[item] = {'id': item, 'nom': reg.get('name') or reg.get('original_name') or item,
                                           'type': USAGE_TYPES[kind], 'entites': []}
                entry['entites'].append(eid)
    return sorted(found.values(), key=lambda e: (e['type'], e['nom']))


def group_usage(settings, ui_key, slug):
    """Automatisations, scripts et scenes a adapter si le groupe est renomme
    (avant le renommage, pour la confirmation). Rapide : seulement les ids
    attendus (`get`), sans recherche dans le registre. None si HA ne peut
    pas etre interroge (ou groupe inconnu)."""
    import ha_entities as ha
    for key_ui, key, _l, groups, _a, norm in ha._resolve_sources(settings)[0]:
        if key_ui != ui_key or slug not in norm:
            continue
        try:
            session = _connect()
        except RegistryError:
            return None
        if session is None:
            return None
        conn = _Conn(session)
        try:
            src_slug = ha._make_slug(key)
            g = next((x for x in groups if isinstance(x, dict) and x.get('slug') == slug), {})
            olds = old_ids(src_slug, slug, norm[slug], g.get('_renamed_from'))
            current = []
            for uid, eid in ha.group_entity_ids(src_slug, slug, norm[slug]):
                for candidate in [eid] + sorted(olds.get(uid, ())):
                    if _ours(_get(conn, candidate), uid):
                        current.append(candidate)
                        break
            return _usage(conn, current)
        except RegistryError:
            return None
        finally:
            conn.close()
    return None


def reconcile():
    """Renomme dans HA les entites des groupes renommes. Renvoie le compte
    rendu (lignes), ou None si HA est injoignable (a retenter)."""
    with _run_lock:
        return _reconcile()


STATUSES = ('renommées', 'déjà à jour', 'conservées (id personnalisé)', 'conflits', 'introuvables', 'erreurs')


def _locate(conn, index, uid, olds, new):
    """Etat d'une entite : ('rename', ancien id), ('a_jour',), ('conflit', unique_id
    de l'autre entite), ('conservee', id actuel) ou ('introuvable',)."""
    for old in olds:
        if _ours(_get(conn, old), uid):
            return ('rename', old)
    entry = _get(conn, new)
    if _ours(entry, uid):
        return ('a_jour',)
    if entry:
        return ('conflit', entry.get('unique_id'))
    current = index.lookup(uid)
    return ('conservee', current) if current else ('introuvable',)


def _reconcile():
    global _retry
    from settings_manager import load_settings, atomic_update, NO_CHANGE
    _retry = False
    pending = pending_renames(load_settings())
    if not pending:
        return []
    try:
        session = _connect()
    except RegistryError as e:
        logging.warning(f"Renommage des entités HA reporté : {e}")
        return None
    report, failed_groups = [], set()
    counts = {k: 0 for k in STATUSES}
    if session is None:
        # Hors de Home Assistant (pas de Supervisor) : pas de registre a mettre
        # a jour, les nouvelles entites prennent directement le nouvel id.
        logging.info("Renommage des entités : hors de Home Assistant, rien à renommer")
    else:
        conn = _Conn(session)
        index = _UidIndex(conn)
        try:
            # 1. Etat des lieux, entite par entite (sans charger le registre).
            todo = []      # (groupe, unique_id, ancien id, nouvel id)
            for group, _snap, items in pending:
                for uid, olds, new in items:
                    try:
                        state = _locate(conn, index, uid, sorted(olds), new)
                    except _Timeout as e:
                        state = ('erreur', str(e))
                    if state[0] == 'rename':
                        todo.append((group, uid, state[1], new))
                    elif state[0] == 'a_jour':
                        logging.info(f"Renommage HA : {new} déjà à jour")
                        counts['déjà à jour'] += 1
                    elif state[0] == 'conflit':
                        # Nouvel id pris par une AUTRE entite : rien n'est ecrase (pas de doublon « _2 »).
                        logging.warning(f"Renommage HA : {new} est déjà pris par une autre entité "
                                        f"(unique_id {state[1]}), rien n'est modifié")
                        report.append(f"{new} : déjà pris par une autre entité, rien n'est modifié")
                        counts['conflits'] += 1
                    elif state[0] == 'conservee':
                        logging.info(f"Renommage HA : {state[1]} conservé (id choisi dans Home Assistant, attendu {new})")
                        report.append(f"{state[1]} : id choisi dans Home Assistant, conservé")
                        counts['conservées (id personnalisé)'] += 1
                    elif state[0] == 'introuvable':
                        logging.info(f"Renommage HA : {new} introuvable (sera créée sous ce nom)")
                        counts['introuvables'] += 1
                    else:
                        logging.warning(f"Renommage HA : {new} : {state[1]}")
                        counts['erreurs'] += 1
                        failed_groups.add(group)
            # 2. Elements a adapter, cherches une fois, avant de renommer (ils
            #    citent l'ancien id).
            usage = _usage(conn, [old for _g, _u, old, _n in todo]) if todo else []
            # 3. Renommages, en plusieurs passes : un id libere par un renommage
            #    peut en debloquer un autre.
            renamed, last_error = {}, {}
            while todo:
                left = []
                for group, uid, old, new in todo:
                    res = conn.call({'type': 'config/entity_registry/update', 'entity_id': old, 'new_entity_id': new})
                    # Echec apparent apres une reprise : la mise a jour avait pu
                    # passer avant la coupure.
                    if res.get('success') or _ours(_get(conn, new), uid):
                        logging.info(f"Renommage HA : {old} -> {new}")
                        report.append(f"{old} -> {new}")
                        counts['renommées'] += 1
                        renamed[old] = new
                    else:
                        last_error[old] = ((res.get('error') or {}).get('message') or 'refusé', _get(conn, new))
                        left.append((group, uid, old, new))
                if len(left) == len(todo):
                    break
                todo = left
            for group, uid, old, new in todo:
                msg, taken = last_error[old]
                if taken and not _ours(taken, uid):
                    logging.warning(f"Renommage HA : {new} est déjà pris par une autre entité "
                                    f"(unique_id {taken.get('unique_id')}), {old} n'est pas modifié")
                    report.append(f"{old} -> {new} : impossible, id déjà pris par une autre entité")
                    counts['conflits'] += 1
                else:
                    logging.warning(f"Renommage HA impossible : {old} -> {new} ({msg}), nouvelle tentative plus tard")
                    report.append(f"{old} -> {new} : impossible ({msg})")
                    counts['erreurs'] += 1
                    failed_groups.add(group)
            for item in usage:
                olds = [e for e in item['entites'] if e in renamed]
                if olds:
                    line = (f"à adapter : {item['type']} « {item['nom']} » ({item['id']}) utilise "
                            + ', '.join(f"{e} (→ {renamed[e]})" for e in olds))
                    logging.warning(f"Renommage HA : {line}")
                    report.append(line)
        except RegistryError as e:
            logging.warning(f"Renommage des entités HA interrompu : {e} (nouvelle tentative plus tard)")
            return None
        finally:
            conn.close()
        summary = ', '.join(f"{counts[k]} {k}" for k in STATUSES)
        logging.info(f"Renommage des entités HA terminé : {summary}")
        report.insert(0, f"bilan : {summary}")

    # Erreurs passageres : le groupe reste en attente (nouvelle tentative).
    _retry = bool(failed_groups)
    done = {group: snap for group, snap, _items in pending if group not in failed_groups}

    def _clear(settings):
        import ha_entities as ha
        changed = False
        for ui_key, _key, _l, groups, _a, _n in ha._resolve_sources(settings)[0]:
            for g in groups or []:
                if not isinstance(g, dict):
                    continue
                snap = done.get((ui_key, g.get('slug')))
                # Renomme a nouveau pendant l'operation : garder pour la suivante.
                if snap and snap == (g.get('entity_slug'), g.get('_renamed_from')):
                    g.pop('_renamed_from', None)
                    changed = True
        return settings if changed else NO_CHANGE
    atomic_update(_clear)
    if report:
        try:
            from ha_control import _publish_result
            _publish_result({'action': 'rename_entities', 'success': not (counts['conflits'] or counts['erreurs']),
                             'counts': counts, 'renamed': report})
        except Exception:
            pass
    return report
