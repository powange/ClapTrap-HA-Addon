"""Calibrage de l'arbitrage entre pieces : correction par source.

L'arbitrage compare la force d'un clap (pic / bruit ambiant de la source).
Une camera qui reduit elle-meme son bruit a un fond presque nul : le clap
d'une autre piece y parait tres fort, et elle gagne de loin. Le calibrage
mesure, pour des claps faits dans chaque piece, la force vue par toutes les
sources du groupe, puis propose une correction (poids) par source telle que
la source de la piece ou l'on a tape gagne, avec la plus grande marge.

Deroule : start(groupe) (detection lancee si besoin, sources du groupe en
sourdine), record(source) pour chaque piece (3 claps pres de cette source,
captes en meme temps par toutes), analyze(), apply().
"""
import logging
import math
import statistics
import threading
import time

import calibration

CLAPS = 3               # claps par prise
DURATION = 5.0          # s par prise
GRACE = 0.3
HOME_OVER_FLOOR = 3.0   # la source de la piece doit entendre ses claps nettement
MAX_MARGIN = math.log(10)   # marge visee au plus (x 10) : borne la recherche
from settings_manager import ROOM_WEIGHT_LIMITS as WEIGHT_LIMITS

# Verrou du calibrage d'une source : un seul calibrage a la fois (les
# sourdines ne se comptent pas, deux sessions se les retireraient).
_lock = calibration._lock
_session = None


class _Session:
    def __init__(self, zone, sources, was_running):
        self.zone, self.sources, self.was_running = zone, sources, was_running
        self.takes = []        # [{'home': source_id, 'forces': {source_id: force}}]
        self.proposal = None
        self.taking = False
        self.last_activity = time.monotonic()

    def summary(self):
        per = {s['source_id']: sum(1 for t in self.takes if t['home'] == s['source_id']) for s in self.sources}
        return {'active': True, 'zone': self.zone, 'sources': self.sources, 'takes': per,
                'claps': CLAPS, 'duration': DURATION, 'analyzed': self.proposal is not None}


def _names(sources):
    return {s['source_id']: s['name'] for s in sources}


def candidates(settings):
    """Groupes calibrables : [{'zone': nom ou None, 'sources': [{source_id,
    name}]}], deux sources actives au moins (sans groupe : toutes)."""
    import classify
    active = [{'source_id': s['source_id'], 'name': s['name']} for s in classify.build_sources_from_settings(settings)]
    zones = classify.room_zone_map(settings.get('global') or {})
    if zones is None:
        return [{'zone': None, 'sources': active}] if len(active) >= 2 else []
    out = []
    for z in (settings.get('global') or {}).get('room_zones') or []:
        members = [s for s in active if zones.get(s['source_id']) == z.get('name')]
        if len(members) >= 2:
            out.append({'zone': z['name'], 'sources': members})
    return out


def status():
    s = _session
    return s.summary() if s is not None else {'active': False}


def _current():
    return calibration.touch(_session, _finish, "aucun calibrage de l'arbitrage en cours : commencez par « start »",
                             "Calibrage de l'arbitrage")


def start(zone=None):
    """Commence le calibrage d'un groupe de sources (`zone` : son nom ; sans
    groupe de sources, toutes). Remplace un calibrage en cours."""
    global _session
    from settings_manager import load_settings
    with _lock:
        settings = load_settings()
        if not (settings.get('global') or {}).get('room_arbitration'):
            raise calibration.CalibrationError('activez d\'abord « Seule la source la plus proche déclenche »')
        groups = candidates(settings)
        group = next((g for g in groups if zone is None or (g['zone'] or '').casefold() == str(zone).casefold()), None)
        if group is None:
            raise calibration.CalibrationError('aucun groupe d\'au moins deux sources actives' if not groups
                                               else f'groupe « {zone} » introuvable ou avec moins de deux sources actives')
        if _session is not None:
            _finish(_session)
        calibration.stop_quietly()   # un calibrage a la fois (sourdines)
        names = _names(group['sources'])
        was_running = calibration.acquire_sources(
            list(names), lambda missing: 'ces sources n\'écoutent pas (flux absent ?) : '
                                         + ', '.join(names[sid] for sid in missing))
        _session = _Session(group['zone'], group['sources'], was_running)
        calibration.watch_idle(_session, lambda s: _session is s, _finish, "Calibrage de l'arbitrage",
                               'room-calibration-watchdog')
        logging.info(f"Calibrage de l'arbitrage commencé ({', '.join(s['name'] for s in group['sources'])})")
        return _session.summary()


def _finish(session):
    global _session
    calibration.release_sources(session, [s['source_id'] for s in session.sources], "Calibrage de l'arbitrage")
    if _session is session:
        _session = None


def stop_quietly():
    """Un autre calibrage commence : celui-ci s'arrete (sources rendues)."""
    with _lock:
        if _session is not None:
            _finish(_session)


def cancel():
    with _lock:
        s = _current()
        _finish(s)
        return {'active': False, 'cancelled': True}


def force_of(blocks):
    """(force des claps, attaques nettes) d'une prise sur une source, comme
    l'arbitrage : pic / bruit ambiant (mediane des blocs, claps compris :
    quelques blocs sur 50). Force : mediane des 3 plus fortes attaques (un
    bruit isole de la piece, porte ou television, ne la fixe pas seul) ;
    sans attaque nette (son lisse par la camera), le plus fort bloc."""
    import count_calibration as cc
    if not blocks:
        return None, 0
    floor = max(1e-6, statistics.median(b[0] for b in blocks))
    claps = cc.clap_peaks(blocks, CLAPS, floor * HOME_OVER_FLOOR)
    top = [raw for _, raw in cc.clap_peaks(blocks, CLAPS, 0.0)] or [max(b[0] for b in blocks)]
    return round(statistics.median(top) / floor, 2), len(claps)


def record(source_id):
    """Une prise : 3 claps pres de `source_id`, captes en meme temps par
    toutes les sources du groupe (bloque ~5,3 s)."""
    import classify
    from recording import capture
    with _lock:
        s = _current()
        names = _names(s.sources)
        if source_id not in names:
            raise calibration.CalibrationError('source : une des sources du groupe attendue')
        if s.taking:
            raise calibration.CalibrationError('une prise est déjà en cours', 409)
        dets = {sid: classify.detector_for(sid) for sid in names}
        absent = [names[sid] for sid, d in dets.items() if d is None]
        if absent:
            raise calibration.CalibrationError('ces sources n\'écoutent plus : ' + ', '.join(absent), 409)
        s.taking = True
    try:
        caps = {}

        def one(sid, det):
            cap = capture(det, DURATION, grace=GRACE)
            caps[sid] = [b for b in cap['blocks'] if cap['t0'] < b[2] <= cap['t1']]
        threads = [threading.Thread(target=one, args=(sid, det), daemon=True) for sid, det in dets.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        s.taking = False
    with _lock:
        if _session is not s:
            raise calibration.CalibrationError('calibrage terminé pendant la prise', 409)
        forces, heard = {}, 0
        for sid in names:
            force, n = force_of(caps.get(sid))
            forces[sid] = force
            if sid == source_id:
                heard = n
        if heard < CLAPS or not forces[source_id]:
            raise calibration.CalibrationError(f'{heard} clap(s) net(s) sur « {names[source_id]} » au lieu de {CLAPS} : '
                                               'tapez plus près de cette source, après le début de la prise')
        s.takes.append({'home': source_id, 'forces': forces})
        s.proposal = None
        return {'home': source_id, 'forces': {names[k]: v for k, v in forces.items()},
                'loudest': names[max((k for k in forces if forces[k]), key=lambda k: forces[k])],
                'takes': s.summary()['takes']}


def _feasible(takes, ids, margin):
    """Poids (log) ou la source de chaque prise depasse les autres d'au moins
    `margin` (log), ou None : contraintes de difference (Bellman-Ford)."""
    edges = []
    for t in takes:
        lh = math.log(t['forces'][t['home']])
        for o, f in t['forces'].items():
            if o != t['home'] and f:
                # x_o - x_h <= log F_h - log F_o - margin
                edges.append((t['home'], o, lh - math.log(f) - margin))
    dist = {i: 0.0 for i in ids}
    for _ in range(len(ids)):
        changed = False
        for a, b, w in edges:
            if dist[a] + w < dist[b] - 1e-12:
                dist[b] = dist[a] + w
                changed = True
        if not changed:
            return dist
    return None


def solve(takes, ids):
    """(poids par source, marge) : la plus grande marge (rapport de forces
    ponderees, x 10 au plus) qui fait gagner la source de chaque prise ; le
    plus fort poids vaut 1 (les autres sources sont baissees). Marge < 1 :
    impossible de departager toutes les prises."""
    lo, hi = -MAX_MARGIN, MAX_MARGIN
    if _feasible(takes, ids, hi) is not None:
        lo = hi
    else:
        for _ in range(40):
            mid = (lo + hi) / 2
            if _feasible(takes, ids, mid) is not None:
                lo = mid
            else:
                hi = mid
    dist = _feasible(takes, ids, lo) or {i: 0.0 for i in ids}
    top = max(dist.values())
    w_lo, w_hi = WEIGHT_LIMITS
    weights = {i: round(min(w_hi, max(w_lo, math.exp(d - top))), 2) for i, d in dist.items()}
    return weights, margin_of(takes, weights)


def margin_of(takes, weights):
    """Marge reelle des poids (bornes et arrondis) : plus petit rapport,
    sur les prises, entre la source de la piece et la plus forte des autres."""
    worst = None
    for t in takes:
        home = t['forces'][t['home']] * weights.get(t['home'], 1.0)
        others = [f * weights.get(sid, 1.0) for sid, f in t['forces'].items() if sid != t['home'] and f]
        if others:
            ratio = home / max(others)
            worst = ratio if worst is None else min(worst, ratio)
    return worst if worst is not None else math.exp(MAX_MARGIN)


def _winner(t, weights):
    return max((sid for sid, f in t['forces'].items() if f), key=lambda sid: t['forces'][sid] * weights.get(sid, 1.0))


def analyze():
    from settings_manager import load_settings
    with _lock:
        s = _current()
        names = _names(s.sources)
        ids = list(names)
        lacking = [names[sid] for sid in ids if not any(t['home'] == sid for t in s.takes)]
        if lacking:
            raise calibration.CalibrationError('faites au moins une prise près de : ' + ', '.join(lacking))
        current = {sid: float(((load_settings().get('global') or {}).get('room_weights') or {}).get(sid, 1.0)) for sid in ids}
        proposed, margin = solve(s.takes, ids)
        rows = []
        for t in s.takes:
            cur, new = _winner(t, current), _winner(t, proposed)
            rows.append({'home': names[t['home']],
                         'forces': [{'source': names[k], 'force': v} for k, v in t['forces'].items()],
                         'current': names[cur], 'proposed': names[new],
                         'current_ok': cur == t['home'], 'proposed_ok': new == t['home']})
        warnings = []
        if margin <= 1.0:
            warnings.append('certaines prises restent mal départagées : une source entend aussi fort le clap d\'une '
                            'autre pièce que le sien. Rapprochez-la de sa pièce, ou retirez-la du groupe')
        elif margin < 1.5:
            warnings.append(f'marge faible (× {margin:.1f}) : des claps entre deux pièces pourront encore hésiter')
        s.proposal = {
            'zone': s.zone,
            'sources': [{'source_id': sid, 'name': names[sid], 'current': current[sid], 'proposed': proposed[sid]}
                        for sid in ids],
            'margin': round(margin, 2), 'rows': rows, 'warnings': warnings,
            'score': {'current': sum(r['current_ok'] for r in rows), 'proposed': sum(r['proposed_ok'] for r in rows),
                      'takes': len(rows)}}
        return s.proposal


def apply(weights=None):
    """Enregistre les poids proposes (ou `weights` : {source_id: poids}) et
    termine la session."""
    from settings_manager import to_number
    with _lock:
        s = _current()
        if not s.proposal and weights is None:
            raise calibration.CalibrationError('lancez d\'abord l\'analyse')
        ids = {x['source_id'] for x in s.sources}
        chosen = {x['source_id']: x['proposed'] for x in (s.proposal or {}).get('sources') or []}
        for sid, w in (weights or {}).items():
            if sid not in ids:
                raise calibration.CalibrationError(f'{sid} : source hors du groupe calibré')
            chosen[sid] = to_number(w, sid, *WEIGHT_LIMITS)
        calibration._api('PUT', '/api/settings/rooms', {'weights': chosen})
        _finish(s)
        logging.info(f"Calibrage de l'arbitrage appliqué : {chosen}")
        return {'applied': chosen, 'active': False}
