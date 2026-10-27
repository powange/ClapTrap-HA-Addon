"""Calibrage d'une source : reglages deduits d'ecoutes repetees.

Deroule (interface ou commandes MQTT) :
1. start(source) : la detection tourne (demarree si besoin) et la source est
   mise en sourdine (les bruits joues a repetition ne declenchent rien).
2. record(cible) : une prise de quelques secondes pendant laquelle on joue le
   bruit d'un groupe (existant ou nouveau), ou le bruit de fond ("fond").
   Mesures : niveau brut, pics comptes (attaques et sons tenus), classes
   YAMNet reconnues et leur score.
3. analyze() : gain de la source, force minimale d'un clap propre a la
   source (bruit de fond et groupes comptes en attaques), et pour chaque
   groupe calibre : sons a cocher ou retirer, seuil, mode de comptage, avec
   la reconnaissance et les fausses detections avant / apres sur les memes
   prises.
4. apply() applique (gain automatiquement) ; cancel() abandonne.

Les modifications passent par l'API (memes validations que l'interface).
"""

import logging
import threading
import time

DEFAULT_DURATION = 4.0
MIN_DURATION = 1.0
MAX_DURATION = 15.0
from recording import RESULT_GRACE, YAMNET_WINDOW, capture, saturation   # ecoute commune (calibrage, extraits)
TARGET_LEVEL = 0.5       # niveau vise (bruit le plus fort x gain) : fort, sans ecretage
MIN_TAKES = 2
BACKGROUND = 'fond'
_BACKGROUND_NAMES = {'fond', 'bruit de fond', 'silence', 'background'}
DETECTOR_WAIT = 30.0     # initialisation de YAMNet (Raspberry Pi)
IDLE_TIMEOUT = 15 * 60   # session abandonnee apres 15 min sans activite

_lock = threading.RLock()
_session = None


class CalibrationError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _call(method, path, body=None):
    from ha_control import _call as call
    return call(method, path, body)


def _api(method, path, body=None):
    from ha_control import _api_or_raise, CommandError
    try:
        return _api_or_raise(method, path, body)
    except CommandError as e:
        raise CalibrationError(str(e), e.status)


def _watchdog(session):
    """Session abandonnee (onglet ferme, client MQTT muet) : la source ne
    reste pas en sourdine indefiniment."""
    while _session is session:
        time.sleep(30)
        with _lock:
            if _session is session and time.monotonic() - session.last_activity > IDLE_TIMEOUT:
                logging.info(f"Calibrage de {session.label} abandonné (inactif)")
                _finish(session)


def _current():
    s = _session
    if s is not None and time.monotonic() - s.last_activity > IDLE_TIMEOUT:
        logging.info("Calibrage abandonné (inactif)")
        _finish(s)
        s = None
    if s is None:
        raise CalibrationError('aucun calibrage en cours : commencez par « start »', 409)
    s.last_activity = time.monotonic()
    return s


class _Session:
    def __init__(self, kind, key, source_id, label, was_running):
        self.kind, self.key, self.source_id, self.label = kind, key, source_id, label
        self.was_running = was_running
        self.takes = []
        self.take_blocks = {}   # id(prise) -> blocs bruts (force minimale), hors des reponses
        self.proposal = None
        self.last_activity = time.monotonic()
        # Calibrage du comptage (count_calibration.py) : meme session, autre but.
        self.purpose = 'source'
        self.count_takes = []
        self.count_proposal = None
        self.taking = False   # prise en cours (une a la fois)
        # Apprentissage d'un son (learned.py) : empreintes du bruit de fond et des exemples.
        self.learn_background = []
        self.learn_examples = []
        self.learn_target = None   # id du son reappris (None : nouveau son)

    def summary(self):
        per = {}
        for t in self.takes:
            per.setdefault(t['target'], 0)
            per[t['target']] += 1
        if self.purpose == 'count':
            for t in self.count_takes:
                per[t['kind']] = per.get(t['kind'], 0) + 1
        return {'active': True, 'purpose': self.purpose, 'source': self.label, 'source_ref': self.ref, 'takes': per,
                'last_take': self.takes[-1] if self.takes else None,
                'analyzed': self.proposal is not None}

    @property
    def ref(self):
        return 'mic' if self.kind == 'mic' else f'{self.kind}:{self.key}'


def _source(settings, kind, key):
    if kind == 'mic':
        return settings.get('microphone') or {}
    lst = settings.get('rtsp_sources' if kind == 'rtsp' else 'saved_vban_sources') or []
    return next((s for s in lst if str(s.get('id')) == str(key)), None)


def status():
    # Sans le verrou : lisible pendant une prise (qui le garde plusieurs secondes).
    s = _session
    return s.summary() if s is not None else {'active': False}


def start(source_ref, purpose='source', learn_target=None):
    """Commence le calibrage d'une source (remplace un calibrage en cours).
    `purpose` : 'source', 'count' (comptage) ou 'learn' (son appris), fixe
    sous le verrou (une autre requete ne voit jamais une session sans but),
    comme `learn_target` (son reappris)."""
    global _session
    import classify
    import room_calibration
    from ha_control import _resolve_source, CommandError
    from routes.sources import _source_id_for
    from settings_manager import load_settings
    with _lock:
        room_calibration.stop_quietly()   # un calibrage a la fois (sourdines)
        if _session is not None:
            _finish(_session)
        try:
            kind, key, src = _resolve_source(load_settings(), source_ref)
        except CommandError as e:
            raise CalibrationError(str(e), e.status)
        if not src.get('enabled'):
            raise CalibrationError('source désactivée : activez-la avant de la calibrer')
        source_id = _source_id_for(kind, None if kind == 'mic' else key)
        was_running = classify.get_status()['running']
        if not was_running:
            status_code, data = _call('POST', '/api/detection/start')
            if status_code >= 400 and status_code != 409:
                raise CalibrationError((data or {}).get('error') or 'démarrage de la détection impossible')
        deadline = time.monotonic() + DETECTOR_WAIT
        while classify.detector_for(source_id) is None:
            if time.monotonic() > deadline:
                if not was_running:
                    _call('POST', '/api/detection/stop')
                raise CalibrationError("la source n'écoute pas (flux absent ?) : calibrage impossible", 503)
            time.sleep(0.2)
        from ha_entities import source_label
        _session = _Session(kind, key, source_id, source_label(kind, src), was_running)
        _session.purpose = purpose
        _session.learn_target = learn_target
        classify.mute_source(source_id, True)
        threading.Thread(target=_watchdog, args=(_session,), daemon=True, name='calibration-watchdog').start()
        logging.info(f"Calibrage de {_session.label} commencé")
        return _session.summary()


def _finish(session):
    global _session
    import classify
    classify.mute_source(session.source_id, False)
    if not session.was_running:
        try:
            _call('POST', '/api/detection/stop')
        except Exception as e:
            logging.warning(f"Calibrage : arrêt de la détection impossible : {e}")
    if _session is session:
        _session = None


def cancel():
    with _lock:
        s = _current()
        _finish(s)
        logging.info(f"Calibrage de {s.label} annulé")
        return {'active': False, 'cancelled': True}


def _resolve_target(session, target, settings):
    """('fond'|'group'|'new', slug, nom) d'une cible de prise."""
    if not isinstance(target, str) or not target.strip():
        raise CalibrationError('target : nom du groupe (existant ou nouveau) ou « fond » attendu')
    name = target.strip()
    if name.lower() in _BACKGROUND_NAMES:
        return 'fond', None, BACKGROUND
    src = _source(settings, session.kind, session.key) or {}
    for g in src.get('sound_groups') or []:
        if isinstance(g, dict) and name.lower() in (str(g.get('slug', '')).lower(), str(g.get('name', '')).lower()):
            return 'group', g['slug'], g.get('name') or g['slug']
    if len(name) > 80:
        raise CalibrationError('nom du groupe : 80 caractères au plus')
    return 'new', None, name


def capture_take(s, duration, grace, scores=False):
    """Une prise sur la source de la session : capture, puis blocs, pics et
    resultats limites a la prise. Commun au calibrage d'une source et a celui
    du comptage.

    Appelee hors de _lock : la prise dure jusqu'a 16 s, et « Annuler » doit
    repondre tout de suite. Une seconde prise simultanee (double clic) est
    refusee au lieu d'occuper un fil de plus ; l'appelant revoit la session.
    """
    with _lock:
        if s.taking:
            raise CalibrationError('une prise est déjà en cours', 409)
        s.taking = True
    try:
        return _capture_take(s, duration, grace, scores)
    except CalibrationError:
        if _session is not s:   # « aucun son reçu » d'une prise annulee
            raise CalibrationError('calibrage terminé pendant la prise', 409)
        raise
    finally:
        s.taking = False


def _capture_take(s, duration, grace, scores=False):
    import classify
    det = classify.detector_for(s.source_id)
    if det is None:
        raise CalibrationError("la source n'écoute plus (détection arrêtée ?)", 409)
    cap = capture(det, duration, grace=grace, scores=scores)
    t0, t1 = cap['t0'], cap['t1']
    blocks = [tuple(b) + (None,) * (4 - len(b)) for b in cap['blocks'] if t0 < b[2] <= t1]
    if not blocks:
        raise CalibrationError("aucun son reçu pendant la prise : la source est-elle à l'écoute ?", 409)
    half = YAMNET_WINDOW / 2
    return {'blocks': blocks, 'peaks': [p for p in cap['peaks'] if t0 < p[1] <= t1],
            # Fenetres dont la moitie au moins tombe dans la prise.
            'results': [r for r in cap['results'] if t0 + half < r[1] <= t1 + half],
            'scores': [r for r in cap['scores'] if t0 + half < r[1] <= t1 + half]}


def record(target, expected=None, duration=DEFAULT_DURATION):
    """Une prise : le bruit de `target` est joue pendant `duration` s, a
    partir de maintenant. Bloque duration + ~1,6 s."""
    from settings_manager import load_settings, to_number
    with _lock:
        s = _current()
        kind, slug, name = _resolve_target(s, target, load_settings())
        duration = to_number(duration if duration is not None else DEFAULT_DURATION, 'duration', MIN_DURATION, MAX_DURATION)
        if expected is not None:
            expected = to_number(expected, 'expected', 1, 10, integer=True)
    take_data = capture_take(s, duration, RESULT_GRACE)
    with _lock:
        if _session is not s:
            raise CalibrationError('calibrage terminé pendant la prise', 409)
        blocks, peaks, results = take_data['blocks'], take_data['peaks'], take_data['results']
        classes = {}
        for categories, _now in results:
            for label, score in categories:
                if score > classes.get(label, 0.0):
                    classes[label] = float(score)
        max_raw = max((b[0] for b in blocks), default=0.0)
        gain = blocks[-1][1] if blocks else 1.0
        take = {
            'target': name, 'kind': kind, 'group': slug, 'expected': expected,
            'duration': duration, 'blocks': len(blocks), 'results': len(results),
            'max_raw': round(max_raw, 5), 'gain': gain,
            # Comme l'extrait (le limiteur empeche l'ecretage par le gain :
            # « sature » pour un son simplement limite trompait).
            'clipped': saturation(max_raw, gain)[0],
            'limited': saturation(max_raw, gain)[1],
            'attacks': sum(1 for p in peaks if p[0] == 'attack'),
            'sustains': sum(1 for p in peaks if p[0] == 'sustain'),
            'classes': {k: round(v, 3) for k, v in classes.items()},
            'top': [{'classe': k, 'score': round(v, 2)}
                    for k, v in sorted(classes.items(), key=lambda kv: kv[1], reverse=True)[:3]],
        }
        s.takes.append(take)
        s.take_blocks[id(take)] = blocks
        s.proposal = None
        n = sum(1 for t in s.takes if t['target'] == name)
        return dict(take, take_number=n)


# ===== Analyse ================================================================

def _score(take, classes):
    return max((take['classes'].get(c, 0.0) for c in classes), default=0.0)


def _evaluate(pos, neg, classes, threshold):
    if not classes:
        return {'recall': 0.0, 'false_positive': 0.0, 'missed': len(pos), 'false_alarms': 0}
    hits = sum(1 for t in pos if _score(t, classes) >= threshold)
    fps = sum(1 for t in neg if _score(t, classes) >= threshold)
    return {'recall': round(hits / len(pos), 2) if pos else 0.0,
            'false_positive': round(fps / len(neg), 2) if neg else 0.0,
            'missed': len(pos) - hits, 'false_alarms': fps}


def _best_threshold(pos_scores, neg_scores):
    """Seuil qui reconnait le plus de prises du groupe en declenchant le
    moins sur les autres (une fausse detection pese 1,5 prise manquee) ;
    au milieu de la plage des meilleurs seuils (marge des deux cotes)."""
    best, best_obj = [], None
    for i in range(10, 91):
        th = i / 100
        recall = sum(1 for p in pos_scores if p >= th) / len(pos_scores)
        fp = sum(1 for n in neg_scores if n >= th) / len(neg_scores) if neg_scores else 0.0
        obj = recall - 1.5 * fp
        if best_obj is None or obj > best_obj + 1e-9:
            best, best_obj = [th], obj
        elif abs(obj - best_obj) <= 1e-9:
            best.append(th)
    return best[len(best) // 2], best_obj


def _median(values):
    v = sorted(values)
    return v[len(v) // 2] if v else 0.0


def analyze():
    """Propositions de reglages a partir des prises."""
    from settings_manager import load_settings
    with _lock:
        s = _current()
        settings = load_settings()
        src = _source(settings, s.kind, s.key) or {}
        exclusions = set(settings.get('global', {}).get('sound_exclusions') or [])
        targets = []
        for t in s.takes:
            if t['kind'] != 'fond' and t['target'] not in targets:
                targets.append(t['target'])
        if not targets:
            raise CalibrationError('aucune prise de groupe : enregistrez le bruit d\'au moins un groupe')
        takes_of = {name: [t for t in s.takes if t['target'] == name] for name in targets}
        for name, takes in takes_of.items():
            if len(takes) < MIN_TAKES:
                raise CalibrationError(f'« {name} » : au moins {MIN_TAKES} prises nécessaires ({len(takes)})')
        groups_by_slug = {g['slug']: g for g in src.get('sound_groups') or [] if isinstance(g, dict) and g.get('slug')}
        calibrated = {takes_of[n][0]['group'] for n in targets if takes_of[n][0]['group']}
        # Sons coches dans un groupe non calibre : intouchables (un son ne peut
        # etre coche que dans un groupe par source).
        reserved = {}
        for slug, g in groups_by_slug.items():
            if slug not in calibrated:
                for label, on in (g.get('sound_whitelist') or {}).items():
                    if on:
                        reserved[label] = g.get('name') or slug
        # Candidats de chaque cible : classes presentes (>= 0,1) dans au moins
        # 60 % de ses prises ; chaque classe revient a la cible ou elle est la
        # plus forte.
        candidates, medians = {}, {}
        for name in targets:
            takes = takes_of[name]
            labels = {label for t in takes for label in t['classes']}
            cands = []
            for label in labels:
                if label in exclusions or label == 'Silence':
                    continue
                scores = [t['classes'].get(label, 0.0) for t in takes]
                if sum(1 for x in scores if x >= 0.1) >= 0.6 * len(takes):
                    cands.append(label)
                    medians[(name, label)] = _median(scores)
            candidates[name] = cands
        owner = {}
        for (name, label), med in medians.items():
            if label not in owner or med > medians[(owner[label], label)]:
                owner[label] = name

        groups, warnings = [], []
        for name in targets:
            takes = takes_of[name]
            first = takes[0]
            pos = takes
            neg = [t for t in s.takes if t['target'] != name]
            g = groups_by_slug.get(first['group']) if first['group'] else None
            if first['kind'] == 'group' and g is None:
                groups.append({'target': name, 'group': first['group'], 'new': False, 'takes': len(takes),
                               'warnings': [], 'error': 'groupe supprimé ou renommé depuis les prises'})
                continue
            mine = [c for c in candidates[name] if owner.get(c) == name and c not in reserved]
            mine.sort(key=lambda c: medians[(name, c)], reverse=True)
            blocked = sorted(c for c in candidates[name] if c in reserved)
            res = {'target': name, 'group': first['group'], 'new': first['kind'] == 'new',
                   'takes': len(takes), 'warnings': []}
            if blocked:
                res['warnings'].append('déjà cochés dans un autre groupe : ' +
                                       ', '.join(f'{c} ({reserved[c]})' for c in blocked))
            chosen, best = [], None
            for c in mine[:6]:
                trial = chosen + [c]
                th, obj = _best_threshold([_score(t, trial) for t in pos], [_score(t, trial) for t in neg])
                if best is None or obj > best[1] + 1e-9:
                    chosen, best = trial, (th, obj)
            if not chosen:
                res['error'] = 'aucun son reconnu de façon régulière sur ses prises'
                groups.append(res)
                continue
            threshold = best[0]
            current = sorted(l for l, on in ((g or {}).get('sound_whitelist') or {}).items() if on)
            cur_th = (g or {}).get('threshold', settings.get('global', {}).get('threshold', 0.5))
            res.update({
                'sounds': chosen,
                'add': [c for c in chosen if c not in current],
                'remove': [c for c in current if c not in chosen],
                'threshold': {'current': cur_th if g else None, 'proposed': threshold},
                'before': _evaluate(pos, neg, current, cur_th) if g else None,
                'after': _evaluate(pos, neg, chosen, threshold),
            })
            expected = next((t['expected'] for t in takes if t['expected']), None)
            counts = {'attaques': [t['attacks'] for t in takes], 'tenus': [t['sustains'] for t in takes]}
            cur_mode = (g or {}).get('count_mode', 'attaques')
            mode = cur_mode
            if expected:
                ok_att = sum(1 for n in counts['attaques'] if n == expected)
                ok_sus = sum(1 for n in counts['tenus'] if n == expected)
                mode = 'tenus' if ok_sus > ok_att else 'attaques' if ok_att > ok_sus else cur_mode
                if max(ok_att, ok_sus) < len(takes) / 2:
                    res['warnings'].append(f'{expected} pic(s) attendu(s), comptés : attaques {counts["attaques"]}, '
                                           f'sons tenus {counts["tenus"]}')
            res.update({'expected': expected, 'counts': counts,
                        'count_mode': {'current': cur_mode if g else None, 'proposed': mode}})
            if res['new']:
                res['ha_entities'] = [expected] if expected and 1 <= expected <= 4 else [1, 2]
            elif expected and expected not in (g.get('ha_entities') or []):
                res['warnings'].append(f'aucune entité Home Assistant pour {expected} clap(s) dans ce groupe')
            if res['after']['false_positive'] > 0:
                res['warnings'].append('ce groupe se déclencherait encore sur d\'autres prises')
            groups.append(res)

        gain = _gain_proposal(s, src)
        peak_ratio = _peak_ratio_proposal(s, src, settings, groups)
        warnings += peak_ratio.pop('warnings')
        if not any(t['kind'] == 'fond' for t in s.takes):
            warnings.append('aucune prise du bruit de fond : les fausses détections ne sont mesurées '
                            'que sur les autres groupes')
        clipped = sum(1 for t in s.takes if t['clipped'])
        if clipped:
            warnings.append(f'{clipped} prise(s) saturée(s) dès le micro ou la caméra : éloignez le son '
                            'ou baissez le volume du micro (le gain n\'y peut rien)')
        s.proposal = {'source': s.label, 'gain': gain, 'peak_ratio': peak_ratio, 'groups': groups, 'warnings': warnings,
                      'background_takes': sum(1 for t in s.takes if t['kind'] == 'fond')}
        return s.proposal


def _gain_proposal(s, src):
    levels = sorted(t['max_raw'] for t in s.takes if t['kind'] != 'fond' and t['max_raw'] > 0)
    if not levels:
        return {'field': None, 'note': 'aucun niveau mesuré'}
    loud = levels[int(0.9 * (len(levels) - 1))]
    if s.kind == 'mic':
        if src.get('auto_volume'):
            return {'field': None, 'note': 'volume automatique actif : réglé par l\'add-on'}
        current = int(src.get('volume', 100))
        factor = max(0.5, min(2.0, TARGET_LEVEL / loud))
        proposed = int(max(10, min(150, round(current * factor / 5) * 5)))
        field = 'volume'
    else:
        current = float(src.get('gain', 10 if s.kind == 'rtsp' else 1))
        proposed = int(max(1, min(100, round(TARGET_LEVEL / loud))))
        field = 'gain'
    change = not (0.67 <= proposed / max(current, 1e-6) <= 1.5)
    return {'field': field, 'current': current, 'proposed': proposed if change else current,
            'change': change, 'loudest_raw': round(loud, 4)}


def _peak_ratio_proposal(s, src, settings, groups):
    """Force minimale d'un clap propre a la source : entre les pics parasites
    du bruit de fond et le plus faible des sons comptes des `groups` (claps
    des groupes en attaques, le nombre de pics attendu comme le calibrage du
    comptage ; sommet de chaque prise des groupes en sons tenus, que le meme
    seuil filtre)."""
    import count_calibration as cc
    from classify import detection_params_from_settings, source_peak_ratio
    current = source_peak_ratio(src, detection_params_from_settings(settings))
    base = {'field': None, 'current': current, 'own': src.get('peak_ratio') is not None, 'warnings': []}
    bg = [b for t in s.takes if t['kind'] == 'fond' for b in s.take_blocks.get(id(t)) or []]
    counted = [g for g in groups if not g.get('error') and g.get('expected')]
    if not counted:
        return dict(base, note='aucun nombre de pics attendu : force minimale d\'un clap inchangée')
    if not bg:
        return dict(base, note='ajoutez une prise du bruit de fond pour régler la force minimale d\'un clap')
    floor, noise_max = cc.noise_levels(bg)
    min_raw = noise_max * cc.CLAP_OVER_NOISE
    levels, ignored = [], {}
    for g in counted:
        held = g['count_mode']['proposed'] == 'tenus'
        for t in s.takes:
            blocks = s.take_blocks.get(id(t))
            if t['target'] != g['target'] or not blocks:
                continue
            n = t['expected'] or g['expected']
            if held:
                top = max(b[0] for b in blocks)
                found = [top] if top >= min_raw else []
                n = 1
            else:
                found = [raw for _, raw in cc.clap_peaks(blocks, n, min_raw)]
            if len(found) == n:
                levels += found
            else:   # prise ratee (son oublie, trop faible) : ecartee
                ignored[g['target']] = ignored.get(g['target'], 0) + 1
    if ignored:
        base['warnings'].append('force minimale : prises ignorées (son trop proche du bruit de fond ou nombre de pics '
                                'non retrouvé) : ' + ', '.join(f'{k} ({v})' for k, v in ignored.items()))
    if not levels:
        return dict(base, note='sons trop proches du bruit de fond : force minimale d\'un clap inchangée')
    proposed, warning = cc.ratio_between(noise_max / floor, min(levels) / floor)
    if warning:
        base['warnings'].append(warning)
    # Ecart sensible seulement : une valeur propre a la source n'est pas
    # creee pour quelques dixiemes du reglage global.
    change = abs(proposed - current) > 0.15 * current
    if change and proposed > current:
        names = {g.get('group') for g in counted}
        others = [g.get('name') or g['slug'] for g in src.get('sound_groups') or []
                  if isinstance(g, dict) and g.get('slug') and g['slug'] not in names]
        if others:
            base['warnings'].append('force minimale plus haute : vérifiez ensuite les groupes non calibrés ici ('
                                    + ', '.join(others) + '), leurs sons plus faibles pourraient ne plus être comptés')
    return dict(base, field='peak_ratio', proposed=proposed if change else current, change=change,
                weak=warning is not None, sounds=len(levels))


# ===== Application ==============================================================

def apply(gain=True, groups=None, peak_ratio=True):
    """Applique la proposition (toute, ou le gain, la force minimale et les
    groupes choisis par leur nom) puis termine le calibrage."""
    with _lock:
        s = _current()
        p = s.proposal or analyze()
        kind_key = None if s.kind == 'mic' else s.key
        done, errors = [], []

        def step(label, method, path, body=None):
            try:
                data = _api(method, path, body)
                done.append(label)
                return data
            except CalibrationError as e:
                errors.append(f'{label} : {e}')
                return None

        wanted = [r for r in p['groups'] if not r.get('error')
                  and (groups is None or r['target'] in groups or r.get('group') in groups)]
        from settings_manager import load_settings
        r = p.get('peak_ratio') or {}
        if peak_ratio and groups is not None and r.get('field'):
            # Groupes ecartes : leurs prises ne fixent pas la force minimale.
            settings = load_settings()
            r = _peak_ratio_proposal(s, _source(settings, s.kind, s.key) or {}, settings, wanted)
        g = p['gain']
        changes, labels = {}, []
        if gain and g.get('field') and g.get('change'):
            changes[g['field']] = g['proposed']
            labels.append(f"{g['field']} {g['current']} → {g['proposed']}")
        if peak_ratio and r.get('field') and r.get('change'):
            changes['peak_ratio'] = r['proposed']
            labels.append(f"force minimale × {r['current']} → × {r['proposed']} (propre à la source)")
        if changes:   # une seule ecriture (et une seule mise a jour des sources)
            step(', '.join(labels), 'PATCH', f'/api/sources/{s.kind}/{s.key}', changes)
        base = {'kind': s.kind, 'source_key': kind_key}
        for r in wanted:
            if r['new']:
                data = step(f"groupe « {r['target']} » créé", 'POST', '/api/source/sound_groups',
                            {**base, 'name': r['target'], 'threshold': r['threshold']['proposed'],
                             'ha_entities': r['ha_entities']})
                r['group'] = (data or {}).get('group', {}).get('slug')
        # Retraits d'abord : un son passe d'un groupe a l'autre sans conflit.
        for r in wanted:
            for label in r['remove'] if r.get('group') else []:
                step(f"{r['target']} : {label} retiré", 'PUT', '/api/source/sound_whitelist',
                     {**base, 'group_slug': r['group'], 'label': label, 'enabled': False})
        src = _source(load_settings(), s.kind, s.key) or {}
        for r in wanted:
            if not r.get('group'):
                continue
            for label in r['add']:
                # Encore coche dans un groupe non retenu (decoche dans la
                # fenetre, ou en erreur) : un son par groupe et par source.
                holder = next((g.get('name') or g['slug'] for g in src.get('sound_groups') or []
                               if isinstance(g, dict) and g.get('slug') != r['group']
                               and (g.get('sound_whitelist') or {}).get(label) is True), None)
                if holder:
                    errors.append(f"{r['target']} : {label} non coché, il reste dans « {holder} »")
                    continue
                step(f"{r['target']} : {label} coché", 'PUT', '/api/source/sound_whitelist',
                     {**base, 'group_slug': r['group'], 'label': label, 'enabled': True})
            step(f"{r['target']} : seuil {r['threshold']['proposed']}, comptage {r['count_mode']['proposed']}",
                 'PUT', '/api/source/sound_groups',
                 {**base, 'group_slug': r['group'], 'threshold': r['threshold']['proposed'],
                  'count_mode': r['count_mode']['proposed']})
        _finish(s)
        logging.info(f"Calibrage de {s.label} appliqué : {len(done)} modification(s), {len(errors)} erreur(s)")
        return {'applied': done, 'errors': errors, 'active': False}
