"""Calibrage du comptage des claps : fenetre, ecart minimal, force minimale.

Ces trois reglages valent pour toutes les sources ; on les mesure sur une
source choisie (session du calibrage, but « count » : detection lancee si
besoin, source en sourdine). Deroule :
1. bruit de fond (calme) : niveau ambiant et pics parasites ;
2. claps simples : force de vos claps face au bruit, rebonds ;
3. « 2 claps » et « 3 claps » : duree de vos gestes ;
4. (facultatif) « 2 claps, pause, 1 clap » : la fenetre ne doit pas fusionner
   deux commandes.
analyze() propose les trois valeurs et rejoue chaque prise avec les reglages
actuels puis proposes ; apply() les enregistre.
"""
import logging
import math
import statistics

import calibration
from clap_logic import ClapTracker

KINDS = {
    'fond': {'label': 'Bruit de fond', 'expected': [], 'duration': 5.0},
    'simple': {'label': 'Clap simple', 'expected': [1], 'duration': 3.0},
    'double': {'label': '2 claps', 'expected': [2], 'duration': 4.0},
    'triple': {'label': '3 claps', 'expected': [3], 'duration': 4.0},
    'separe': {'label': '2 claps, pause, 1 clap', 'expected': [2, 1], 'duration': 7.0},
}
GRACE = 0.3              # fin de prise : derniers blocs (aucun resultat YAMNet necessaire)
MIN_GAP = 0.12           # deux pics de clap distincts : au moins cet ecart
CLAP_OVER_NOISE = 1.5    # un pic de clap depasse le plus fort pic parasite d'au moins ce facteur
WINDOW_MARGIN = 0.4      # marge ajoutee a la duree du geste le plus long
# Bornes des propositions (plus etroites que celles des reglages : valeurs utiles).
LIMITS = {'delay': (0.6, 5.0), 'peak_cooldown': (0.05, 0.3), 'peak_ratio': (1.5, 50.0)}


def _params(s=None):
    """Reglages du comptage simules : globaux, et la force minimale propre a
    la source de la session `s` si elle en a une."""
    from settings_manager import load_settings
    from classify import detection_params_from_settings, source_peak_ratio
    settings = load_settings()
    p = detection_params_from_settings(settings)
    src = calibration._source(settings, s.kind, s.key) if s is not None else None
    return {'delay': float(p['delay']), 'peak_cooldown': float(p['peak_cooldown']),
            'peak_ratio': source_peak_ratio(src, p)}


def _source_ratio(s):
    """Force minimale propre a la source calibree (None : reglage global)."""
    from settings_manager import load_settings
    src = calibration._source(load_settings(), s.kind, s.key) or {}
    return src.get('peak_ratio')


def start(source_ref):
    """Commence un calibrage du comptage sur une source (remplace un
    calibrage en cours, comme celui d'une source)."""
    return calibration.start(source_ref, purpose='count')


def _session():
    """Session en cours, a condition qu'elle soit un calibrage du comptage
    (un calibrage de source lance ailleurs l'a peut-etre remplacee)."""
    s = calibration._current()
    if s.purpose != 'count':
        raise calibration.CalibrationError('un autre calibrage est en cours : recommencez le calibrage du comptage', 409)
    return s


def record(kind, duration=None):
    """Une prise du comptage (bloque la duree + 0,3 s)."""
    from settings_manager import to_number
    if not isinstance(kind, str) or kind not in KINDS:
        raise calibration.CalibrationError('kind : ' + ', '.join(KINDS) + ' attendu')
    with calibration._lock:
        s = _session()
        duration = to_number(duration if duration is not None else KINDS[kind]['duration'], 'duration', 1, 15)
    blocks = calibration.capture_take(s, duration, GRACE)['blocks']   # hors du verrou (Annuler)
    with calibration._lock:
        if calibration._session is not s:
            raise calibration.CalibrationError('calibrage terminé pendant la prise', 409)
        if kind == 'fond':
            s.count_takes[:] = [t for t in s.count_takes if t['kind'] != 'fond']   # un seul bruit de fond
        s.count_takes.append({'kind': kind, 'blocks': blocks})
        s.count_proposal = None
        bg = _background(s.count_takes)
        found = _simulate(bg, blocks, _params(s)) if bg else None
        return {'kind': kind, 'label': KINDS[kind]['label'],
                'take_number': sum(1 for t in s.count_takes if t['kind'] == kind),
                'claps_found': sum(found) if found is not None else None, 'gestures': found,
                'counts': {k: sum(1 for t in s.count_takes if t['kind'] == k) for k in KINDS}}


def _background(takes):
    return next((t['blocks'] for t in takes if t['kind'] == 'fond'), None)


def _simulate(bg_blocks, blocks, params):
    """Nombre de claps par geste d'une prise, comme le detecteur : bruit de
    fond pour le niveau ambiant, puis la prise ; un geste commence au premier
    pic et compte ceux de sa fenetre (meme limite que ClapTracker)."""
    tracker = ClapTracker(window=params['delay'], peak_cooldown=params['peak_cooldown'],
                          peak_ratio=params['peak_ratio'])
    t0 = blocks[0][2]
    for i, (raw, gain, t, pre, *rest) in enumerate(bg_blocks):
        # Bruit de fond recale juste avant la prise (memes attaques, decalees).
        shift = t0 - (len(bg_blocks) - i) * 0.1 - t
        ons = [(pk, ot + shift, op) for pk, ot, op in rest[0]] if rest and rest[0] is not None else None
        tracker.feed_peak(raw, t + shift, pre_level=pre, gain=gain or 1.0, onsets=ons)
    attacks = []
    for raw, gain, t, pre, *rest in blocks:
        # Attaques du bloc (tranches de 5 ms) : comme la detection.
        attacks += tracker.feed_peak(raw, t, pre_level=pre, gain=gain or 1.0, onsets=rest[0] if rest else None)
    gestures, i = [], 0
    while i < len(attacks):
        end = attacks[i] + params['delay'] + ClapTracker.WINDOW_EPS
        n = 0
        while i < len(attacks) and attacks[i] <= end:
            n += 1
            i += 1
        gestures.append(n)
    return gestures


def _is_attack(block):
    raw, pre = block[0], block[3]
    return pre is None or raw > pre * ClapTracker.ATTACK_RATIO


def _candidates(blocks):
    """Pics candidats d'une prise (pic, gain, instant, niveau avant) : les
    attaques de chaque bloc (tranches de 5 ms) quand elles sont connues,
    sinon le pic du bloc."""
    for b in blocks:
        if len(b) > 4 and b[4] is not None:
            yield from ((pk, b[1], t, pre) for pk, t, pre in b[4])
        else:
            yield tuple(b[:4])


def _clap_peaks(blocks, n, min_raw):
    """Les `n` pics de clap d'une prise : attaques nettement au-dessus du
    bruit (min_raw), espacees d'au moins MIN_GAP, les plus fortes, dans
    l'ordre du temps : [(instant, pic brut)]. Moins de `n` : prise ratee."""
    picked = []
    for raw, g, t, pre in sorted(_candidates(blocks), key=lambda b: b[0], reverse=True):
        if raw < min_raw:
            break
        if _is_attack((raw, g, t, pre)) and all(abs(t - pt) >= MIN_GAP for pt, _ in picked):
            picked.append((t, raw))
        if len(picked) == n:
            break
    return sorted(picked)


def _clamp(key, value):
    lo, hi = LIMITS[key]
    return min(hi, max(lo, value))


def analyze():
    with calibration._lock:
        s = _session()
        takes = s.count_takes
        bg = _background(takes)
        if not bg:
            raise calibration.CalibrationError('commencez par la prise du bruit de fond')
        current = _params(s)
        own = _source_ratio(s)
        warnings = []

        floor = max(1e-5, statistics.median(b[0] for b in bg))
        noise_max = max(b[0] for b in bg)
        noise_ratio = noise_max / floor
        # Prises de claps exploitables : le nombre attendu de vrais claps
        # (attaque, au-dessus du bruit). Une prise ratee (clap trop tot,
        # oublie) faussait les trois valeurs : ecartee et signalee.
        peaks, ignored = {}, []
        for t in takes:
            n = sum(KINDS[t['kind']]['expected'])
            if not n:
                continue
            found = _clap_peaks(t['blocks'], n, noise_max * CLAP_OVER_NOISE)
            if len(found) == n:
                peaks[id(t)] = found
            else:
                ignored.append(f"{KINDS[t['kind']]['label']} ({len(found)} clap(s) trouvé(s) au lieu de {n})")
        if ignored:
            warnings.append('prises ignorées, à refaire si besoin : ' + ', '.join(ignored))
        usable = [t for t in takes if id(t) in peaks]
        if not any(t['kind'] == 'simple' for t in usable) or not any(t['kind'] in ('double', 'triple') for t in usable):
            raise calibration.CalibrationError('il faut au moins une prise « clap simple » et une prise « 2 claps » ou '
                                               '« 3 claps » où vos claps sont nettement plus forts que le bruit de fond')

        # Force minimale : entre le pic parasite le plus fort et le clap le plus faible.
        weakest = min(raw for t in usable for _, raw in peaks[id(t)]) / floor
        lo, hi = noise_ratio * 1.3, weakest * 0.7
        if hi > lo:
            ratio = math.sqrt(lo * hi)
        else:
            ratio = hi
            warnings.append('vos claps les plus faibles sont à peine plus forts que le bruit de fond : rapprochez-vous '
                            'du micro ou montez le gain de la source, sinon des bruits seront comptés')
        ratio = round(_clamp('peak_ratio', ratio), 1)

        # Ecart minimal : au-dela des rebonds (vraies attaques apres le clap,
        # pas sa decroissance), en deca de l'ecart entre deux claps.
        rebounds = []
        for t in usable:
            if t['kind'] != 'simple':
                continue
            (tc, _), = peaks[id(t)]
            # Toutes les attaques (tranches de 5 ms), comme le comptage : un
            # rebond dans le bloc du clap compte aussi.
            rebounds += [b[2] - tc for b in _candidates(t['blocks'])
                         if 0 < b[2] - tc <= 0.3 and b[0] > ratio * floor and b[3] is not None and _is_attack(b)]
        intervals = [b[0] - a[0] for t in usable if t['kind'] in ('double', 'triple')
                     for a, b in zip(peaks[id(t)], peaks[id(t)][1:])]
        cooldown = max(rebounds) + 0.03 if rebounds else current['peak_cooldown']
        if intervals:
            cooldown = min(cooldown, min(intervals) * 0.6)
        cooldown = round(_clamp('peak_cooldown', cooldown), 2)

        # Fenetre : le geste le plus long, plus une marge (arrondie au dixieme
        # superieur ; round avant ceil : 12,000000000000002 ne donne pas 13).
        spans = [peaks[id(t)][-1][0] - peaks[id(t)][0][0] for t in usable if t['kind'] in ('double', 'triple')]
        window = round(_clamp('delay', math.ceil(round((max(spans) + WINDOW_MARGIN) * 10, 6)) / 10), 1)
        for t in usable:
            if t['kind'] == 'separe':
                pause = peaks[id(t)][2][0] - peaks[id(t)][0][0]
                if window >= pause:
                    warnings.append(f'entre deux commandes, laissez au moins {window + 0.3:.1f} s après le premier clap : '
                                    f'avec {pause:.1f} s, elles seraient comptées ensemble')
        proposed = {'delay': window, 'peak_cooldown': cooldown, 'peak_ratio': ratio}

        # Rejeu des prises avec les deux jeux de reglages.
        rows = []
        for t in takes:
            expected = KINDS[t['kind']]['expected']
            cur = _simulate(bg, t['blocks'], current)
            new = _simulate(bg, t['blocks'], proposed)
            rows.append({'kind': t['kind'], 'label': KINDS[t['kind']]['label'], 'expected': expected,
                         'current': cur, 'proposed': new, 'usable': t['kind'] == 'fond' or id(t) in peaks,
                         'current_ok': cur == expected, 'proposed_ok': new == expected})
        s.count_proposal = {'current': current, 'proposed': proposed, 'rows': rows, 'warnings': warnings,
                            'peak_ratio_scope': 'source' if own is not None else 'global',
                            'limits': LIMITS,
                            'score': {'current': sum(r['current_ok'] for r in rows),
                                      'proposed': sum(r['proposed_ok'] for r in rows), 'takes': len(rows)},
                            'measures': {'noise_ratio': round(noise_ratio, 1), 'weakest_clap_ratio': round(weakest, 1),
                                         'longest_gesture': round(max(spans), 2),
                                         'rebound': round(max(rebounds), 2) if rebounds else None}}
        return s.count_proposal


def _save_on_source(s, chosen):
    from settings_manager import modify_settings, SettingsSaveError
    from routes.sources import _apply_live

    def _mut(settings):
        src = calibration._source(settings, s.kind, s.key)
        if src is None:
            raise calibration.CalibrationError('source supprimée pendant le calibrage', 404)
        src['peak_ratio'] = chosen['peak_ratio']
        settings.setdefault('global', {}).update({k: v for k, v in chosen.items() if k != 'peak_ratio'})
    try:
        modify_settings(_mut)
    except SettingsSaveError as e:
        raise calibration.CalibrationError(f'réglages non enregistrés : {e}', 500)
    _apply_live()


def apply(values=None):
    """Enregistre les reglages proposes (ou `values`) et termine la session."""
    from settings_manager import to_number
    with calibration._lock:
        s = _session()
        if not s.count_proposal and not values:
            raise calibration.CalibrationError('lancez d\'abord l\'analyse')
        chosen = dict(s.count_proposal['proposed'] if s.count_proposal else {})
        for key, val in (values or {}).items():
            if key not in LIMITS:
                raise calibration.CalibrationError(f'{key} : réglage inconnu')
            chosen[key] = to_number(val, key, *LIMITS[key])
        scope = (s.count_proposal or {}).get('peak_ratio_scope', 'global')
        if scope == 'source' and 'peak_ratio' in chosen:
            # Force minimale propre a la source (comme analysee) : enregistree
            # sur elle, le reste en global, en une seule ecriture.
            _save_on_source(s, chosen)
        else:
            calibration._api('PUT', '/api/settings/advanced', chosen)
        calibration._finish(s)
        logging.info(f"Calibrage du comptage appliqué sur {s.label} : {chosen}")
        return {'applied': chosen, 'active': False}
