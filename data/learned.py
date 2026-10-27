"""Sons appris : un son precis (sa sonnette, le bip du lave-linge) reconnu a
partir de quelques exemples enregistres.

Empreinte d'un son : les scores des 521 classes de YAMNet pour une fenetre
(meme modele, aucun calcul de plus), compresses (logarithme : la classe
dominante, « Music » a 0,9 pour toute tonalite, n'ecrase plus les autres) et
centres sur le bruit de fond enregistre avec les exemples (ce qui distingue
le son, pas ce qui est toujours la). Un son appris est reconnu quand
l'empreinte d'une fenetre ressemble (similarite cosinus) a l'un de ses
exemples plus que le bruit de fond. Il apparait comme une
classe de plus, « Appris : <nom> », avec un score de 0 a 1 : les groupes le
cochent comme n'importe quel son (seuil, comptage, alerte, exclusions).

Apprentissage (session du calibrage, but « learn » : detection lancee si
besoin, source en sourdine) :
1. bruit de fond (5 s, calme) ;
2. 3 a 10 exemples (une prise de 3 s par exemple, le son joue une fois) : la
   fenetre la plus differente du bruit de fond est gardee ;
3. save(nom) : bilan (exemples reconnus entre eux, ecart avec le bruit de
   fond) et enregistrement dans learned_sounds.json.

Reapprendre (start(source, sound_id)) : memes etapes, le son garde son id
et son nom (les groupes qui le cochent restent lies), ses contre-exemples
aussi.

Contre-exemples : empreintes de fausses detections (« Ce n'etait pas ca »
dans l'historique). Une fenetre plus proche d'un contre-exemple que de tous
les exemples a un score nul.
"""
import json
import logging
import math
import os
import re
import threading
import time
import uuid

import calibration

PREFIX = 'Appris : '
MIN_EXAMPLES = 3
MAX_EXAMPLES = 10
MAX_SOUNDS = 20
MAX_NEGATIVES = 20       # contre-exemples par son (les plus anciens remplaces)
BACKGROUND_SECONDS = 5.0
EXAMPLE_SECONDS = 3.0
GRACE = 1.6              # derniers resultats YAMNet apres la prise
SCORE_AT_EXAMPLE = 0.9   # score d'un son aussi proche que les exemples entre eux
SHARPNESS = 4            # courbe du score : un son voisin (meme classe dominante) tombe bas
MIN_SPREAD = 0.02        # ecart minimal (similarite) entre exemples et bruit de fond
WEAK_SPREAD = 0.15       # en dessous : son trop proche du bruit de fond (avertissement)
KEEP = 0.001             # scores plus faibles : non stockes (empreinte creuse)
# Essai (vrai YAMNet, sons de synthese) : sonnette 0,9-1 (0,75-0,9 tres
# faible), autre tonalite « Music » 0,2-0,4, bips et bruit de fond 0.

_lock = threading.Lock()
_file_lock = threading.RLock()   # lecture-modification-ecriture du fichier (enregistrer, supprimer)
_matcher = None          # (fichier, Matcher des sons enregistres) ; None : a relire


class LearnError(calibration.CalibrationError):
    pass


def _path():
    import settings_manager
    return os.path.join(settings_manager.PERSISTENT_DIR, 'learned_sounds.json')


# ===== Empreintes ===============================================================

def fingerprint(categories):
    """{classe: score} creux (scores >= KEEP) d'une liste [(classe, score)]."""
    return {name: round(float(score), 4) for name, score in categories if score >= KEEP}


def _norm(fp):
    return math.sqrt(sum(v * v for v in fp.values()))


def cosine(a, b, na=None, nb=None):
    """Similarite cosinus de deux empreintes (normes deja connues : na, nb)."""
    na = _norm(a) if na is None else na
    nb = _norm(b) if nb is None else nb
    if not na or not nb:
        return 0.0
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    return sum(v * big.get(k, 0.0) for k, v in small.items()) / (na * nb)


def _best(fp, examples):
    return max((cosine(fp, e) for e in examples), default=0.0)


def _log(fp):
    return {k: math.log1p(v / KEEP) for k, v in fp.items()}


def center_of(background):
    """Empreinte moyenne (compressee) du bruit de fond."""
    total = {}
    for fp in background:
        for k, v in _log(fp).items():
            total[k] = total.get(k, 0.0) + v
    n = max(len(background), 1)
    return {k: round(v / n, 4) for k, v in total.items() if v / n >= 1e-3}


def centered(fp, center, logged=None):
    """Empreinte compressee (`logged` : deja calculee) moins celle du bruit de fond."""
    out = dict(logged) if logged is not None else _log(fp)
    for k, m in center.items():
        out[k] = out.get(k, 0.0) - m
    return out


def calibrate(examples, background):
    """(plancher, plafond) de la similarite d'un son (empreintes centrees) :
    plus forte ressemblance du bruit de fond a un exemple, et ressemblance
    mediane d'un exemple aux autres (exemple retire)."""
    floor = max((_best(b, examples) for b in background), default=0.0)
    loo = sorted(_best(e, examples[:i] + examples[i + 1:]) for i, e in enumerate(examples))
    ceiling = loo[len(loo) // 2] if loo else 1.0
    return floor, ceiling


def score_of(sim, floor, ceiling):
    """Similarite -> score 0..1 : 0 au niveau du bruit de fond,
    SCORE_AT_EXAMPLE au niveau des exemples entre eux, et chute rapide entre
    les deux (SHARPNESS)."""
    spread = max(ceiling - floor, MIN_SPREAD)
    ratio = max(0.0, (sim - floor) / spread)
    return min(1.0, SCORE_AT_EXAMPLE * ratio ** SHARPNESS)


def _prepared(examples, background):
    """(centre, exemples centres, bruit de fond centre)."""
    center = center_of(background)
    return center, [centered(e, center) for e in examples], [centered(b, center) for b in background]


class Matcher:
    """Sons appris pour le detecteur (lecture seule : partage entre fils)."""

    def __init__(self, sounds):
        # Exemples centres et leurs normes calcules une fois (match tourne a
        # chaque analyse de chaque source).
        self.sounds = []
        for s in sounds:
            center = s.get('center') or {}
            examples = [centered(e, center) for e in s['examples']]
            negatives = [centered(e, center) for e in s.get('negatives') or []]
            self.sounds.append((PREFIX + s['name'], center, [(e, _norm(e)) for e in examples],
                                [(e, _norm(e)) for e in negatives], s['floor'], s['ceiling']))

    def __bool__(self):
        return bool(self.sounds)

    def labels(self):
        return [label for label, *_ in self.sounds]

    def match(self, categories):
        """[(classe apprise, score)] d'un resultat complet (521 classes)."""
        return self.match_full(categories)[1]

    def match_full(self, categories):
        """(empreinte, [(classe apprise, score)]) : l'empreinte sert de
        contre-exemple si la detection etait fausse."""
        fp = fingerprint(categories)
        if not fp:
            return fp, []
        logged = _log(fp)
        out = []
        for label, center, examples, negatives, floor, ceiling in self.sounds:
            c = centered(fp, center, logged)
            nc = _norm(c)
            sim = max((cosine(c, e, nc, ne) for e, ne in examples), default=0.0)
            if negatives and max(cosine(c, e, nc, ne) for e, ne in negatives) > sim:
                out.append((label, 0.0))   # plus proche d'une fausse detection signalee
                continue
            out.append((label, round(score_of(sim, floor, ceiling), 4)))
        return fp, out


# ===== Stockage =================================================================

def _fp_ok(fp):
    return isinstance(fp, dict) and all(isinstance(v, (int, float)) for v in fp.values())


def _valid(s):
    """Entree utilisable (un fichier modifie a la main ne casse pas tout).
    Un contre-exemple invalide est retire, pas tout le son (l'ecriture
    suivante l'aurait efface du fichier)."""
    if isinstance(s, dict) and 'negatives' in s:
        negatives = s['negatives'] if isinstance(s['negatives'], list) else []
        s['negatives'] = [e for e in negatives if _fp_ok(e)]
    return (isinstance(s, dict) and isinstance(s.get('id'), str) and isinstance(s.get('name'), str) and s['name']
            and isinstance(s.get('examples'), list) and s['examples'] and all(_fp_ok(e) for e in s['examples'])
            and isinstance(s.get('floor'), (int, float)) and isinstance(s.get('ceiling'), (int, float))
            and _fp_ok(s.get('center') or {}))


def _read():
    path = _path()
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        # Mis de cote : l'enregistrement suivant ne l'ecrase pas en silence.
        aside = f"{path}.corrompu-{time.strftime('%Y%m%d-%H%M%S')}"
        try:
            os.replace(path, aside)
            logging.error(f"Sons appris illisibles ({e}) : fichier gardé sous {os.path.basename(aside)}")
        except OSError:
            logging.error(f"Sons appris illisibles : {e}")
        return []
    sounds = data.get('sounds') if isinstance(data, dict) else None
    good = [s for s in sounds or [] if _valid(s)]
    if len(good) != len(sounds or []):
        logging.warning(f"Sons appris : {len(sounds or []) - len(good)} entrée(s) invalide(s) ignorée(s)")
    return good


def _write(sounds):
    """Ecriture atomique et durable (coupure de courant : l'ancien fichier
    ou le nouveau, jamais un fichier tronque)."""
    path = _path()
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'sounds': sounds}, f, ensure_ascii=False, separators=(',', ':'))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        fd = os.open(os.path.dirname(path) or '.', os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def matcher():
    """Matcher des sons enregistres (relu apres chaque modification)."""
    global _matcher
    path = _path()
    with _lock:
        if _matcher is None or _matcher[0] != path:
            _matcher = (path, Matcher(_read()))
        return _matcher[1]


def labels():
    return matcher().labels()


def _changed():
    global _matcher
    with _lock:
        _matcher = None
    try:
        import classify
        classify.apply_learned(matcher())
    except Exception as e:
        logging.warning(f"Sons appris non transmis à la détection : {e}")


def _source_noise_reduction(session):
    try:
        import classify
        det = classify.detector_for(session.source_id)
        gate = det.denoise if det is not None else None
        return gate.level if gate is not None else 0
    except Exception:
        return 0


def summary(sound):
    return {'id': sound['id'], 'name': sound['name'], 'label': PREFIX + sound['name'],
            'examples': len(sound['examples']), 'source': sound.get('source', ''),
            'noise_reduction': sound.get('noise_reduction', 0), 'source_ref': sound.get('source_ref', ''),
            'created': sound.get('created'), 'updated': sound.get('updated'), 'quality': sound.get('quality'),
            'negatives': len(sound.get('negatives') or [])}


def list_sounds():
    return [summary(s) for s in _read()]


def delete(sound_id):
    with _file_lock:
        sounds = _read()
        kept = [s for s in sounds if s.get('id') != sound_id]
        if len(kept) == len(sounds):
            raise LearnError('son appris introuvable', 404)
        _write(kept)
    _changed()
    return {'deleted': sound_id}


_NAME_RE = re.compile(r'^[^\x00-\x1f]{1,40}$')


def _check_name(name, sounds, exclude=None):
    if not isinstance(name, str) or not _NAME_RE.match(name.strip()):
        raise LearnError('nom : 1 à 40 caractères')
    name = ' '.join(name.split())
    if any(s['name'].casefold() == name.casefold() and s.get('id') != exclude for s in sounds):
        raise LearnError(f'un son appris s\'appelle déjà « {name} »', 409)
    return name


# ===== Apprentissage (session du calibrage) =======================================

def start(source_ref, sound_id=None):
    """Apprentissage d'un nouveau son, ou d'un son existant (`sound_id` :
    reapprendre, memes nom et id)."""
    if sound_id is not None and not any(s['id'] == sound_id for s in _read()):
        raise LearnError('son appris introuvable', 404)
    return dict(calibration.start(source_ref, purpose='learn', learn_target=sound_id), relearn=sound_id)


def _session():
    s = calibration._current()
    if s.purpose != 'learn':
        raise LearnError('un autre calibrage est en cours : recommencez l\'apprentissage', 409)
    return s


def _status(s):
    return {'background': bool(s.learn_background), 'examples': len(s.learn_examples),
            'min_examples': MIN_EXAMPLES, 'max_examples': MAX_EXAMPLES}


def record(kind):
    """Une prise : « fond » (5 s de calme) ou « exemple » (3 s, le son joue
    une fois). Bloque la duree + ~1,6 s."""
    if kind not in ('fond', 'exemple'):
        raise LearnError('kind : « fond » ou « exemple » attendu')
    with calibration._lock:
        s = _session()
        if kind == 'exemple':
            if not s.learn_background:
                raise LearnError('enregistrez d\'abord le bruit de fond', 409)
            if len(s.learn_examples) >= MAX_EXAMPLES:
                raise LearnError(f'{MAX_EXAMPLES} exemples au plus', 409)
    seconds = BACKGROUND_SECONDS if kind == 'fond' else EXAMPLE_SECONDS
    take = calibration.capture_take(s, seconds, GRACE, scores=True)   # hors du verrou (Annuler)
    fps = [fingerprint(cats) for cats, _now in take['scores']]
    fps = [fp for fp in fps if fp]
    with calibration._lock:
        if calibration._session is not s:
            raise LearnError('apprentissage terminé pendant la prise', 409)
        if not fps:
            raise LearnError('aucune analyse pendant la prise : la source est-elle à l\'écoute ?', 409)
        if kind == 'fond':
            s.learn_background = fps
            return dict(_status(s), kind=kind)
        # Fenetre la plus differente du bruit de fond : celle du son.
        center, _ex, bg = _prepared([], s.learn_background)
        best = min(fps, key=lambda fp: _best(centered(fp, center), bg))
        distinct = 1.0 - _best(centered(best, center), bg)
        s.learn_examples.append(best)
        top = sorted(best.items(), key=lambda kv: kv[1], reverse=True)[:3]
        return dict(_status(s), kind=kind, distinct=round(distinct, 3),
                    top=[{'classe': n, 'score': round(v, 2)} for n, v in top])


def _quality(examples, floor, ceiling):
    """Bilan : exemples reconnus par les autres (seuil 0,5) et ecart avec le
    bruit de fond. Un son trop proche du bruit de fond est signale."""
    recognized = sum(1 for i, e in enumerate(examples)
                     if score_of(_best(e, examples[:i] + examples[i + 1:]), floor, ceiling) >= 0.5)
    spread = ceiling - floor
    warnings = []
    if spread < WEAK_SPREAD:
        warnings.append('ce son ressemble beaucoup au bruit de fond pour YAMNet : '
                        'des fausses détections sont probables (essayez plus près de la source, ou un son plus fort)')
    if recognized < len(examples) * 0.7:
        warnings.append('les exemples se ressemblent peu : refaites-les dans les mêmes conditions')
    return {'recognized': recognized, 'examples': len(examples), 'spread': round(spread, 3),
            'warnings': warnings}


def preview():
    with calibration._lock:
        s = _session()
        if len(s.learn_examples) < MIN_EXAMPLES:
            raise LearnError(f'au moins {MIN_EXAMPLES} exemples', 409)
        _center, ex, bg = _prepared(s.learn_examples, s.learn_background)
        floor, ceiling = calibrate(ex, bg)
        return _quality(ex, floor, ceiling)


def save(name=None):
    """Enregistre le son appris et termine la session (son reappris : nom
    inchange, `name` ignore)."""
    with calibration._lock:
        s = _session()
        if len(s.learn_examples) < MIN_EXAMPLES:
            raise LearnError(f'au moins {MIN_EXAMPLES} exemples', 409)
        center, ex, bg = _prepared(s.learn_examples, s.learn_background)
        floor, ceiling = calibrate(ex, bg)
        quality = _quality(ex, floor, ceiling)
        sound = {'id': uuid.uuid4().hex[:12], 'name': '', 'source': s.label,
                 # Reduction du bruit de la source pendant l'apprentissage :
                 # les empreintes en dependent (l'interface signale un
                 # reglage different sur cette source).
                 'noise_reduction': _source_noise_reduction(s), 'source_ref': s.ref,
                 'created': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                 'floor': round(floor, 4), 'ceiling': round(ceiling, 4), 'center': center,
                 'examples': s.learn_examples, 'quality': quality}
        target = s.learn_target
        with _file_lock:
            sounds = _read()
            if target is not None:
                i = next((i for i, x in enumerate(sounds) if x['id'] == target), None)
                if i is None:
                    raise LearnError('son appris supprimé pendant l\'apprentissage', 404)
                old = sounds[i]
                sound.update(id=old['id'], name=old['name'], created=old.get('created'),
                             updated=sound['created'], negatives=old.get('negatives') or [])
                sounds[i] = sound
                _write(sounds)
            else:
                if len(sounds) >= MAX_SOUNDS:
                    raise LearnError(f'{MAX_SOUNDS} sons appris au plus : supprimez-en un', 409)
                sound['name'] = _check_name(name, sounds)
                _write(sounds + [sound])
        calibration._finish(s)
    logging.info(f"Son appris {'réappris' if target else 'enregistré'} : {sound['name']} "
                 f"({len(sound['examples'])} exemples, écart {quality['spread']})")
    _changed()
    return summary(sound)



# ===== Contre-exemples ==========================================================

def add_negative(label, fp):
    """Ajoute l'empreinte d'une fausse detection a un son appris (`label` :
    « Appris : <nom> »). Avertit si elle ressemble aux exemples autant
    qu'ils se ressemblent entre eux (le vrai son risque de ne plus etre
    reconnu)."""
    if not isinstance(label, str) or not label.startswith(PREFIX) or not fp:
        raise LearnError('son appris attendu')
    with _file_lock:
        sounds = _read()
        sound = next((s for s in sounds if PREFIX + s['name'] == label), None)
        if sound is None:
            raise LearnError('son appris introuvable (supprimé ou renommé ?)', 404)
        center = sound.get('center') or {}
        sim = _best(centered(fp, center), [centered(e, center) for e in sound['examples']])
        sound['negatives'] = ((sound.get('negatives') or []) + [fp])[-MAX_NEGATIVES:]
        _write(sounds)
    _changed()
    warning = None
    if sim >= sound['ceiling']:
        warning = (f'ce son ressemble beaucoup aux exemples de « {sound["name"]} » : '
                   'le vrai son risque de ne plus être reconnu (réapprenez-le si c\'est le cas)')
    logging.info(f"Son appris {sound['name']} : contre-exemple ajouté "
                 f"({len(sound['negatives'])}, ressemblance {sim:.2f})")
    return {'negatives': len(sound['negatives']), 'warning': warning}
