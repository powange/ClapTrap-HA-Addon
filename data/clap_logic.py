"""Logique de detection des claps, sans E/S ni thread (testable seule).

ClapTracker recoit :
- le pic brut de chaque bloc audio de 100 ms (`feed_peak`) : suivi du bruit
  de fond et detection des fronts montants (les "pics" comptes comme claps) ;
- chaque resultat de classification YAMNet (`on_classification`) : armement
  des groupes de sons, fenetre multi-clap, arbitrage entre groupes.

Il renvoie les evenements de detection a declencher. L'horloge est passee en
parametre (`now`) pour pouvoir tester la logique de facon deterministe.
"""


import collections

# Sons de type clap : un groupe qui ne coche qu'eux ne compte que les pics
# qui ont la forme d'un clap (bref, aigu).
CLAP_LABELS = frozenset({'Clapping', 'Hands', 'Applause', 'Finger snapping'})
MAX_MARGIN = 0.5
LEARNED_PREFIX = 'Appris : '   # sons appris (learned.PREFIX)

DEFAULT_WHITELIST = {"Clapping": True, "Hands": True, "Applause": True}
ALERT_WINDOW = 4      # analyses (0,5 s chacune) sur lesquelles compter les reconnaissances
ALERT_OFF_DELAY = 5.0  # s sans reconnaissance avant d'eteindre une alerte


def normalize_group(g, idx=0, default_threshold=0.5):
    """Groupe au format du detecteur, depuis les reglages (sound_whitelist,
    ha_entities) ou deja normalise (whitelist, clap_counts). Point unique :
    la normalisation etait ecrite trois fois, avec des regles differentes
    (nombres de claps filtres ou non). Une liste vide = aucune entite."""
    counts = g.get('clap_counts')
    if counts is None:
        counts = g.get('ha_entities')
    if counts is None:
        counts = [1, 2]
    return {
        'slug': g.get('slug') or f'group{idx + 1}',
        'name': g.get('name') or g.get('slug') or f'Groupe {idx + 1}',
        'whitelist': dict(g.get('whitelist') or g.get('sound_whitelist') or {}),
        'threshold': float(g.get('threshold', default_threshold)),
        'clap_counts': [n for n in counts if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= 4],
        # Sons entendus ajoutes a la liste du groupe (desactive par defaut).
        'auto_add': bool(g.get('auto_add', g.get('auto_add_sounds', False))),
        # Comptage : « attaques » (claps, toc : un front net par geste),
        # « tenus » (sifflement : recompte toutes les SUSTAIN_RESET s tant que
        # le son dure, comme avant la 6.37) ou « alerte » (alarme, pleurs : un
        # capteur allume tant que le son est reconnu, sans compter de pics).
        'count_mode': g.get('count_mode') if g.get('count_mode') in ('tenus', 'alerte') else 'attaques',
        # Alerte : reconnaissances sur les ALERT_WINDOW dernieres analyses
        # pour l'allumer (1 : son bref comme un bris de glace).
        'alert_confirm': min(ALERT_WINDOW, max(1, int(g.get('alert_confirm') or 2))),
        # Seuil relatif : le son du groupe doit battre les autres sons de cette
        # marge (0 : desactive). Contre une piece bruyante ou une camera qui
        # ecrase tous les scores.
        'margin': min(MAX_MARGIN, max(0.0, float(g.get('margin') or 0.0))),
    }


# Rythme d'un geste : groupes de claps rapides separes par des pauses
# (« 2+1 » : deux claps, une pause, un clap). Un ecart est une pause a partir
# de PAUSE s, ou de PAUSE_MIN s s'il fait au moins PAUSE_FACTOR fois le plus
# court ecart du geste (pause marquee apres des claps tres rapides). Claps
# reguliers : « 3 » s'ils sont rapides, « 1+1+1 » s'ils sont lents.
PAUSE = 0.45
PAUSE_MIN = 0.3
PAUSE_FACTOR = 1.6


def rhythm_of(times):
    """(rythme, ecarts en s) des instants des claps d'un geste : ('2+1',
    [0.21, 0.68]) ; ('', []) sans clap."""
    if not times:
        return '', []
    gaps = [b - a for a, b in zip(times, times[1:])]
    if not gaps:
        return '1', []
    shortest = min(gaps)
    sizes = [1]
    for g in gaps:
        if g >= PAUSE or (g >= PAUSE_MIN and g >= PAUSE_FACTOR * shortest):
            sizes.append(1)
        else:
            sizes[-1] += 1
    return '+'.join(str(n) for n in sizes), [round(g, 2) for g in gaps]


def default_group(threshold=0.5):
    return normalize_group({'slug': 'clap', 'name': 'Clap', 'whitelist': dict(DEFAULT_WHITELIST),
                            'threshold': threshold})


class ClapTracker:
    # Plancher absolu (signal brut, avant auto-gain) pour qu'un front compte
    # comme un pic.
    PEAK_FLOOR = 0.01
    # Un pic n'est rattache a une detection que s'il date de moins de
    # PEAK_LOOKBACK s, la fenetre d'analyse de YAMNet (~0.975 s) : au-dela,
    # ce n'est pas le son que YAMNet est en train de reconnaitre (choix assume :
    # un bruit sans rapport dans cette seconde peut encore etre compte).
    # Marge de 0,2 s : les resultats sont dates a la fin de leur fenetre
    # (glissante, analysee toutes les 0,5 s), donc un pic au debut de la
    # fenetre date de ~0,975 s.
    PEAK_LOOKBACK = 1.2
    # Blocs (100 ms) servant a mesurer le bruit de fond au demarrage : aucun
    # pic n'est compte pendant ce temps. Sans cela, le niveau partait de 0.001
    # et chaque bloc d'une piece bruyante etait un "pic" pendant ~40 s.
    WARMUP_BLOCKS = 10
    # Apres un declenchement, le score YAMNet reste haut ~1 s sur le meme son :
    # un groupe ne se re-arme qu'avec un nouveau pic ou apres ce delai.
    RETRIGGER_GUARD = 1.0
    # Un pic n'est compte que sur une vraie attaque : ATTACK_RATIO fois plus
    # fort que le niveau qui le precede. La decroissance d'un son
    # (reverberation) ne remonte jamais : elle ne peut pas compter pour un 2e
    # clap.
    # - Avec `pre_level` (detecteur) : niveau des ~20 ms juste avant le pic, et
    #   instant precis du pic dans le bloc. Deux claps dans des blocs voisins
    #   comptent pour deux, un clap a cheval sur deux blocs pour un (pics a
    #   quelques ms d'ecart, sous peak_cooldown).
    # - Sans (pics de blocs seuls) : bloc precedent, et REATTACK_GAP s apres le
    #   dernier pic pendant un son deja au-dessus du seuil.
    ATTACK_RATIO = 1.5
    REATTACK_GAP = 0.15
    # Mode « sons tenus » : un son qui reste au-dessus du seuil compte un
    # nouveau pic toutes les SUSTAIN_RESET s (un sifflement monte lentement :
    # aucune attaque nette, donc 0 pic en mode « attaques »).
    SUSTAIN_RESET = 0.3

    WINDOW_EPS = 1e-6   # pic pile a la fin de la fenetre : compte (arrondis)
    EARLY_SETTLE = 0.5  # s de calme apres le dernier clap avant une decision anticipee
    LATE_MAX = 5.0      # s : coupure au-dela de laquelle un geste en cours est abandonne (interrupt)

    def __init__(self, window=1.5, peak_cooldown=0.08, peak_ratio=3.0):
        self.window = window
        self.peak_cooldown = peak_cooldown
        self.peak_ratio = peak_ratio
        self.peak_lookback = self.PEAK_LOOKBACK
        self.groups = []         # [{slug, name, whitelist, threshold, clap_counts}]
        self.exclusions = set()
        self.avg_level = 0.001   # niveau moyen du bruit de fond (signal brut)
        self._warmup = []
        self._above = False
        self._prev_peak = 0.0    # pic du bloc precedent (detection d'attaque)
        self._last_peak_time = 0.0
        self.peak_times = []
        self.sustain_times = []  # pics du mode « sons tenus »
        self._sus_above = False
        self._sus_last = 0.0
        self.attack_count = 0    # compteurs cumules (calibrage)
        self.sustain_count = 0
        self._consumed_until = 0.0
        self._last_trigger = 0.0
        self._last_result = 0.0  # fin (horloge audio) du dernier resultat YAMNet traite
        self._always_results = False   # un groupe a besoin de toutes les analyses
        # Decision anticipee (desactivee avec l'arbitrage entre pieces : les
        # sources doivent finir en meme temps pour etre comparees).
        self.early_decision = True
        self._group_state = {}   # slug -> {armed_at, score, labels}
        self._alerts = {}        # slug -> {hits, on, last_hit, score, labels} (mode alerte)
        self._strength = {}      # instant d'un pic -> force (pic / bruit de fond)
        # Journal des « presque » (gestes qui ont failli declencher) : lu et
        # vide par le detecteur (near_misses).
        self.near_misses = []
        self._recent = collections.deque(maxlen=40)   # (instant, force, force exigee) des derniers blocs
        self._peak_eval = {}     # pic en attente -> {slug: (score, classe)} meilleur score vu
        self._used_peaks = set() # pics ayant arme un groupe
        self._windows = []       # (debut, fin) des fenetres de comptage ouvertes recemment
        self._last_weak = {}     # slug -> instant du dernier « son reconnu, pic trop faible »
        # Forme des pics (clap_shape) : les groupes qui ne cochent que des sons
        # de clap ne comptent que les pics qui en ont la forme.
        self.shape_check = True
        self._shape = {}         # instant d'un pic -> (est un clap, raison, mesures)
        self._rejected = {}      # pic ecarte -> note dans les « presque » ? (une fois, son reconnu)
        self.replaced = []       # pics comptes puis remplaces par un plus fort (bloc suivant) : a oublier (take_replaced)
        self._taken = None       # sons coches dans un groupe (cache, refait par set_groups)

    # --- Configuration ----------------------------------------------------

    def set_params(self, window=None, peak_cooldown=None, peak_ratio=None):
        if window is not None:
            self.window = float(window)
        if peak_cooldown is not None:
            self.peak_cooldown = float(peak_cooldown)
        if peak_ratio is not None:
            self.peak_ratio = float(peak_ratio)

    def set_groups(self, groups, default_threshold=0.3):
        self.groups = [normalize_group(g, idx, default_threshold)
                       for idx, g in enumerate(groups or []) if isinstance(g, dict)]
        # Etat d'un groupe retire (desactive, supprime, renomme) : oublie.
        self._taken = None
        for g in self.groups:
            sounds = [name for name, on in g['whitelist'].items() if on]
            g['_shape'] = bool(sounds) and all(name in CLAP_LABELS for name in sounds)
        slugs = {g['slug'] for g in self.groups}
        self._always_results = any(g['count_mode'] == 'alerte' or not g['clap_counts'] or g['auto_add']
                                   for g in self.groups)
        for slug in [s for s in self._group_state if s not in slugs]:
            del self._group_state[slug]

    @staticmethod
    def _alert_event(group, state, score=0.0, labels=(), start=None):
        """Evenement du mode alerte (un seul format pour tous les cas)."""
        ev = {'group': group, 'alert': state, 'clap_count': 0, 'score': float(score),
              'labels': list(labels), 'ignored': False}
        if start is not None:
            ev['start'] = start
        return ev

    def drop_alerts(self):
        """Alertes allumees de groupes retires, desactives ou passes a un autre
        mode : evenements d'extinction (sinon le capteur restait allume)."""
        alert_slugs = {g['slug'] for g in self.groups if g['count_mode'] == 'alerte'}
        events = []
        for slug in [s for s in self._alerts if s not in alert_slugs]:
            st = self._alerts.pop(slug)
            if st['on']:
                events.append(self._alert_event({'slug': slug, 'name': st['name'], 'clap_counts': []}, 'off'))
        return events

    def stop_alerts(self):
        """Arret de la detection (ou longue coupure) : alertes allumees eteintes."""
        events = []
        for slug, st in self._alerts.items():
            if st['on']:
                st['on'] = False
                st['hits'].clear()
                events.append(self._alert_event({'slug': slug, 'name': st['name'], 'clap_counts': []}, 'off'))
        return events

    def _alert(self, group, best, hits, now):
        """Mode alerte : allume apres `alert_confirm` reconnaissances sur les
        ALERT_WINDOW dernieres analyses, eteint apres ALERT_OFF_DELAY s sans
        reconnaissance. Retourne un evenement au changement d'etat."""
        st = self._alerts.setdefault(group['slug'], {'hits': collections.deque(maxlen=ALERT_WINDOW), 'on': False,
                                                     'last_hit': 0.0, 'name': group['name']})
        st['name'] = group['name']
        heard = best >= group['threshold']
        st['hits'].append(heard)
        if heard:
            st['last_hit'] = now
        if not st['on'] and sum(st['hits']) >= group['alert_confirm']:
            st['on'] = True
            labels = sorted(({'label': n, 'score': float(sc)} for n, sc in hits if sc >= group['threshold']),
                            key=lambda x: x['score'], reverse=True)
            return self._alert_event(group, 'on', best, labels, start=now)
        if st['on'] and now - st['last_hit'] >= ALERT_OFF_DELAY:
            st['on'] = False
            st['hits'].clear()
            return self._alert_event(group, 'off', best)
        return None

    @property
    def min_threshold(self):
        return min((g['threshold'] for g in self.groups), default=None)

    # --- Pics -------------------------------------------------------------

    def feed_peak(self, raw_peak, now, pre_level=None, gain=1.0, onsets=None):
        """Pic brut (avant gain et auto-gain) d'un bloc audio. Rend les
        instants des nouveaux pics (attaques) comptes.

        `now` : instant du pic ; `pre_level` : niveau juste avant lui (voir
        ATTACK_RATIO) ; `gain` : gain de la source. Le plancher absolu est
        rapporte au gain : sur le signal brut, une camera faible (gain x10)
        n'atteignait jamais 0,01 et plus aucun clap n'etait compte.
        `onsets` : attaques candidates du bloc [(pic, instant, niveau avant)],
        dans l'ordre du temps (detecteur, tranches de 5 ms) ; chacune peut
        compter. Le pic du bloc sert alors au bruit de fond et aux sons tenus.
        """
        floor = self.PEAK_FLOOR / max(1.0, float(gain or 1.0))
        if len(self._warmup) < self.WARMUP_BLOCKS:
            self._warmup.append(raw_peak)
            if len(self._warmup) == self.WARMUP_BLOCKS:
                self.avg_level = max(0.0001, sorted(self._warmup)[self.WARMUP_BLOCKS // 2])
            return []

        # Force du pic par rapport au bruit de fond (avant que la moyenne ne
        # l'absorbe) : comparable entre sources de gains differents
        # (arbitrage entre pieces : la plus proche entend le plus fort).
        noise = self.avg_level
        strength = raw_peak / max(noise, 1e-6)
        # Moyenne glissante du bruit de fond. Elle continue (plus lentement)
        # pendant un son : un bruit qui s'installe finit par etre absorbe.
        rate = 0.001 if self._above else 0.005
        self.avg_level = self.avg_level * (1 - rate) + raw_peak * rate
        threshold = max(floor, self.avg_level * self.peak_ratio)
        self._recent.append((now, strength, threshold / max(self.avg_level, 1e-6)))

        max_age = max(2.0, self.window + 1.0)
        self.peak_times = [t for t in self.peak_times if (now - t) < max_age]
        self.sustain_times = [t for t in self.sustain_times if (now - t) < max_age]
        self._strength = {t: v for t, v in self._strength.items() if (now - t) < max_age}
        self._used_peaks = {t for t in self._used_peaks if (now - t) < max_age}
        self._windows = [w for w in self._windows if (now - w[1]) < max_age]
        self._shape = {t: v for t, v in self._shape.items() if (now - t) < max_age}
        self._rejected = {t: v for t, v in self._rejected.items() if (now - t) < max_age}

        if raw_peak > threshold:
            if not self._sus_above or (now - self._sus_last) >= self.SUSTAIN_RESET:
                self._sus_last = now
                self.sustain_times.append(now)
                self._strength[now] = strength
                self.sustain_count += 1
            self._sus_above = True
        elif raw_peak < threshold * 0.6:
            self._sus_above = False

        # Attaques candidates (pic, instant, est une attaque) : celles du
        # detecteur (tranches de 5 ms), sinon le pic du bloc.
        if onsets is None and pre_level is not None:
            onsets = [(raw_peak, now, pre_level)]
        if onsets is not None:
            candidates = [(pk, t, pk > pre * self.ATTACK_RATIO if pre is not None else not self._above)
                          for pk, t, pre in onsets]
        else:
            # Pics de blocs seuls : bloc precedent, et REATTACK_GAP s apres le
            # dernier pic pendant un son deja au-dessus du seuil.
            candidates = [(raw_peak, now, not self._above
                           or (raw_peak > self._prev_peak * self.ATTACK_RATIO
                               and (now - self._last_peak_time) > self.REATTACK_GAP))]
        counted, attacked = [], False
        for peak, t, attack in candidates:
            if peak <= threshold or not attack:
                continue
            attacked = True
            force = peak / max(noise, 1e-6)
            last = counted[-1] if counted else (self.peak_times[-1] if self.peak_times else None)
            if last is not None and 0 < t - last <= self.peak_cooldown:
                # Meme geste : le plus fort l'emporte, dans ce bloc comme a
                # cheval sur le precedent (un petit bruit juste avant le clap
                # ne prend pas sa place, sa force ni sa forme, mesuree sinon
                # sur le clap qui le suit et jugee « tenue »). Pas un pic qui a
                # deja arme un groupe : son geste est en cours.
                if force > self._strength.get(last, 0.0) and last not in self._used_peaks:
                    if counted:
                        counted.pop()
                    else:
                        self.replaced.append(last)
                    self.peak_times.remove(last)
                    for d in (self._strength, self._shape, self._rejected, self._peak_eval):
                        d.pop(last, None)
                    self._last_peak_time = t
                    self.peak_times.append(t)
                    self._strength[t] = force
                    counted.append(t)
                continue
            if (t - self._last_peak_time) > self.peak_cooldown:
                self._last_peak_time = t
                self.peak_times.append(t)
                self._strength[t] = force
                self.attack_count += 1
                counted.append(t)
        if attacked:
            self._above = True
        elif raw_peak < threshold * 0.6:
            self._above = False
        self._prev_peak = raw_peak
        return counted

    # --- Classification -----------------------------------------------------

    def on_classification(self, categories, now):
        """`categories` : [(label, score)]. Retourne la liste des evenements a
        declencher : dicts {group, clap_count, score, labels, ignored}."""
        def fresh_of(group):
            return [t for t in self._times(group) if t > self._consumed_until and t >= now - self.peak_lookback]
        # Sans pic, seul un groupe sans entites (evenement et webhook seulement)
        # peut s'armer : une tele qui diffuse des applaudissements declenchait
        # sinon « 1 clap » toutes les 2,5 s.
        can_arm_without_peak = (now - self._last_trigger) >= self.RETRIGGER_GUARD
        self._last_result = max(self._last_result, now)

        alert_events = []
        for group in self.groups:
            hits = [(name, score) for name, score in categories
                    if group['whitelist'].get(name, False) and name not in self.exclusions]
            best = max((score for _, score in hits), default=0.0)
            best_label = max(hits, key=lambda h: h[1])[0] if hits else ''
            state_now = self._group_state.get(group['slug'])
            if best >= group['threshold'] and not (state_now and state_now['armed_at']):
                rival = self.margin_rival(group, categories, best)
                if rival:
                    self._note_margin(group, best, best_label, rival, now, fresh_of(group))
                    best = 0.0   # pas reconnu pour ce groupe (un geste deja en cours continue)
            if group['count_mode'] == 'alerte':
                # Hors des fenetres de comptage et de l'arbitrage : une alarme
                # n'empeche pas un clap, et inversement.
                ev = self._alert(group, best, hits, now)
                if ev:
                    alert_events.append(ev)
                continue
            state = self._group_state.setdefault(group['slug'], {'armed_at': 0.0, 'score': 0.0, 'labels': {}})
            fresh = fresh_of(group)
            if group['clap_counts']:
                self._note_scores(group, fresh, best, best_label)
                if self._rejected and self._shaped(group) and best >= group['threshold'] * self.NEAR_FRACTION:
                    for t in [t for t in self._rejected if now - self.peak_lookback <= t <= now]:
                        self._note_rejected(group, t, best)

            if best >= group['threshold']:
                if not state['armed_at']:
                    if fresh or (can_arm_without_peak and not group['clap_counts']):
                        state['armed_at'] = fresh[0] if fresh else now
                        state['score'] = best
                        state['labels'] = {}
                        self._used_peaks.update(fresh)
                        self._windows.append((state['armed_at'], state['armed_at'] + self.window + self.WINDOW_EPS))
                    elif group['clap_counts']:
                        self._note_weak(group, best, best_label, now)
                elif best > state['score']:
                    state['score'] = best
                if state['armed_at']:
                    for name, score in hits:
                        if score >= group['threshold'] and score > state['labels'].get(name, 0):
                            state['labels'][name] = float(score)
        self._missed_peaks(now)
        return alert_events + self._expire(now)

    # --- Journal des « presque » ------------------------------------------

    NEAR_FRACTION = 0.5      # « presque » : au moins la moitie du seuil du groupe (sinon du bruit)
    WEAK_REPEAT = 2.5        # s : un meme son reconnu sans pic n'est note qu'une fois

    def _counted(self, t):
        """Pic deja pris par un geste : il a arme un groupe, il est dans une
        fenetre de comptage (2e clap d'un geste), ou un evenement l'a consomme."""
        return (t in self._used_peaks or t <= self._consumed_until
                or any(a <= t <= b for a, b in self._windows))

    def _note_scores(self, group, fresh, best, label):
        """Meilleur score de chaque groupe a nombre de claps pendant que des
        pics attendent un son reconnu."""
        for t in fresh:
            if self._counted(t):
                continue
            seen = self._peak_eval.setdefault(t, {})
            if best > seen.get(group['slug'], (0.0, ''))[0]:
                seen[group['slug']] = (best, label)

    def margin_rival(self, group, categories, best):
        """Seuil relatif : (son, score) du meilleur concurrent trop proche du
        son du groupe, ou None. Concurrents : les sons coches dans aucun groupe
        de la source (un son d'un autre groupe se departage par l'arbitrage),
        hors sons appris (derives du meme vecteur) et sons ignores."""
        if not group['margin'] or group['count_mode'] == 'alerte':
            return None
        taken = self._taken_labels()
        for name, score in categories:   # tries par score decroissant
            if name in taken or name in self.exclusions or name.startswith(LEARNED_PREFIX):
                continue
            return (name, score) if best - score < group['margin'] - 1e-9 else None
        return None

    def _taken_labels(self):
        if self._taken is None:
            self._taken = {n for g in self.groups for n, on in g['whitelist'].items() if on}
        return self._taken

    def _note_margin(self, group, best, label, rival, now, fresh):
        """Son du groupe au-dessus du seuil mais trop proche d'un autre son,
        avec un pic qui attendait : « presque » (une fois par geste)."""
        if not fresh or now - self._last_weak.get(('m', group['slug']), -1e9) < self.WEAK_REPEAT:
            return
        self._last_weak[('m', group['slug'])] = now
        self.near_misses.append({'kind': 'margin', 'group': group['name'], 'slug': group['slug'], 'time': fresh[0],
                                 'label': label, 'score': round(best, 3), 'rival': rival[0],
                                 'rival_score': round(rival[1], 3), 'margin': group['margin']})

    def _note_weak(self, group, best, label, now):
        """Son du groupe reconnu sans pic assez fort pour compter un clap."""
        if now - self._last_weak.get(group['slug'], -1e9) < self.WEAK_REPEAT:
            return
        lo = now - self.peak_lookback
        if any(lo <= t <= now for t in self._times(group)):
            return   # un pic est la (consomme par le geste precedent, ou ignore expres) : pas « trop faible »
        recent = [r for r in self._recent if lo <= r[0] <= now]
        strongest = max(recent, key=lambda r: r[1], default=None)
        if strongest is not None and strongest[1] >= strongest[2]:
            return   # assez fort, mais pas compte (attaque, ecart minimal) : la force minimale n'y est pour rien
        self._last_weak[group['slug']] = now
        self.near_misses.append({
            'kind': 'weak', 'group': group['name'], 'slug': group['slug'], 'time': now,
            'label': label, 'score': round(best, 3), 'threshold': group['threshold'],
            'strength': round(strongest[1], 2) if strongest else None,
            'required': round(strongest[2], 2) if strongest else round(self.peak_ratio, 2)})

    def _missed_peaks(self, now):
        """Pics qui n'ont arme aucun groupe une fois leurs resultats passes :
        le son le plus proche du seuil est note (« pic sans son reconnu »)."""
        expired = [t for t in self._peak_eval if now - t > self.peak_lookback]
        if not expired:
            return
        groups = {g['slug']: g for g in self.groups if g['clap_counts']}
        for t in expired:
            seen = self._peak_eval.pop(t)
            if self._counted(t):
                continue
            # Groupes actuels seulement (un groupe retire depuis ne masque pas les autres),
            # sous leur seuil et au moins a sa moitie.
            cands = [(sl, sc, lb) for sl, (sc, lb) in seen.items() if sl in groups
                     and groups[sl]['threshold'] * self.NEAR_FRACTION <= sc < groups[sl]['threshold']]
            if not cands:
                continue
            slug, score, label = max(cands, key=lambda c: c[1] / groups[c[0]]['threshold'])
            group = groups[slug]
            self.near_misses.append({
                'kind': 'low_score', 'group': group['name'], 'slug': slug, 'time': t,
                'label': label, 'score': round(score, 3), 'threshold': group['threshold'],
                'strength': round(self._strength.get(t, 0.0), 2)})

    def _times(self, group):
        if group.get('count_mode') == 'tenus':
            return self.sustain_times
        if self.shape_check and group.get('_shape') and self._rejected:
            # Pic dont la forme n'est pas celle d'un clap : pas compte (forme
            # pas encore mesuree : compte, jusqu'a la mesure).
            return [t for t in self.peak_times if t not in self._rejected]
        return self.peak_times

    def _shaped(self, group):
        return (self.shape_check and group.get('_shape') and group['clap_counts']
                and group.get('count_mode') not in ('tenus', 'alerte'))

    def set_peak_shape(self, t, ok, reason='', metrics=None):
        """Forme du pic de l'instant `t` (mesuree un bloc plus tard, avec le
        son qui suit). Pic ecarte : un groupe de claps deja arme sur lui se
        reporte sur le pic suivant de sa fenetre (ou se desarme) ; il est
        note dans les « presque » si le son du groupe est reconnu."""
        self._shape[t] = (ok, reason, metrics or {})
        if ok:
            return
        self._rejected[t] = False
        for g in self.groups:
            st = self._group_state.get(g['slug'])
            if not self._shaped(g) or not st or st['armed_at'] != t:
                continue
            nxt = [p for p in self._times(g) if t < p <= t + self.window + self.WINDOW_EPS]
            if nxt:
                st['armed_at'] = nxt[0]
            else:
                score = st['score']
                st.update(armed_at=0.0, score=0.0, labels={})
                self._note_rejected(g, t, score)

    def _note_rejected(self, group, t, score):
        """Pic ecarte pour sa forme alors que le son du groupe est reconnu :
        « presque » (une fois par pic)."""
        if self._rejected.get(t, True):
            return
        self._rejected[t] = True
        _ok, reason, metrics = self._shape.get(t, (False, '', {}))
        self.near_misses.append({'kind': 'shape', 'group': group['name'], 'slug': group['slug'],
                                 'time': t, 'reason': reason, **metrics, 'score': round(score, 3),
                                 'strength': round(self._strength.get(t, 0.0), 2)})

    def needs_results(self, now, hop):
        """Les resultats de YAMNet d'une fenetre finissant a `now` peuvent-ils
        servir ? Sinon (piece calme) l'analyse est sautee : un groupe avec des
        nombres de claps ne s'arme qu'avec un pic d'au plus peak_lookback s
        (plus un pas d'analyse `hop` de marge). Toujours oui pour un groupe
        d'alerte, sans nombre de claps (son reconnu seul) ou qui ajoute les
        sons entendus."""
        if self._always_results:
            return True
        last = max(self.peak_times[-1:] + self.sustain_times[-1:], default=None)
        return last is not None and now - last < self.peak_lookback + hop

    def take_replaced(self):
        """Pics comptes dans un bloc precedent puis remplaces par un plus fort :
        le detecteur les retire de ses mesures de forme et analyses centrees."""
        out, self.replaced = self.replaced, []
        return out

    def pending(self):
        """Une fenetre de comptage est-elle ouverte ?"""
        return any(st['armed_at'] for st in self._group_state.values())

    def poll(self, now):
        """Appele a chaque bloc audio (100 ms) : fenetres arrivees a leur fin
        sur l'horloge audio (au lieu d'attendre le resultat YAMNet suivant,
        jusqu'a 0,5 s plus tard), et decision anticipee."""
        return self._early(now) or self._expire(now)

    def _early(self, now):
        """Decision anticipee : un seul groupe arme, qui a deja son nombre
        maximal de claps (un groupe « 1 clap » des le premier), suivi de
        EARLY_SETTLE s de calme ET d'un resultat YAMNet couvrant ce calme (un
        autre groupe a eu sa chance de reconnaitre le son) : l'evenement part
        sans attendre la fin de la fenetre (environ 1 s de latence en moins).
        Les claps de la fin de la fenetre sont ensuite ignores : un double
        clap lent sur un groupe « 1 clap » ne bascule pas deux fois."""
        if not self.early_decision:
            return []
        armed = [g for g in self.groups if (self._group_state.get(g['slug']) or {}).get('armed_at')]
        if len(armed) != 1 or not armed[0]['clap_counts']:
            return []
        group = armed[0]
        state = self._group_state[group['slug']]
        counted = [t for t in self._times(group) if state['armed_at'] <= t <= now and t > self._consumed_until]
        if (len(counted) != max(group['clap_counts']) or now - counted[-1] < self.EARLY_SETTLE
                or self._last_result < counted[-1] + self.EARLY_SETTLE):
            return []
        window_end = state['armed_at'] + self.window + self.WINDOW_EPS
        events = self._expire(now, early=True)
        self._consumed_until = max(self._consumed_until, window_end)
        return events

    def _expire(self, now, early=False):
        """Fenetres arrivees a leur fin (ou decision anticipee `early`) :
        arbitrage et evenements."""
        states = [self._group_state.get(g['slug']) for g in self.groups]   # groupes actuels seulement
        if not early and not any(st and st['armed_at'] and (now - st['armed_at']) >= self.window for st in states):
            return []

        # Des qu'une fenetre expire, TOUS les groupes armes entrent en
        # arbitrage : un seul gagnant par evenement sonore.
        candidates = []
        for group in self.groups:
            state = self._group_state.get(group['slug'])
            if not state or not state['armed_at']:
                continue
            # Pics de la fenetre reglee seulement : jusqu'a sa fin, et non
            # jusqu'au resultat qui la clot (un clap isole 1,8 s apres deux
            # claps faisait « 3 claps » avec une fenetre de 1,5 s).
            end = min(now, state['armed_at'] + self.window + self.WINDOW_EPS)
            recent = [t for t in self._times(group)
                      if state['armed_at'] <= t <= end and t > self._consumed_until]
            # Rythme des claps seulement (sons tenus : un pic toutes les 0,3 s).
            rhythm, gaps = rhythm_of(recent) if group.get('count_mode', 'attaques') == 'attaques' else ('', [])
            candidates.append({
                'rhythm': rhythm,   # « 2+1 » : deux claps, une pause, un clap
                'gaps': gaps,
                '_end': end,
                'start': state['armed_at'],   # instant du 1er clap (horloge audio)
                'group': group,
                # 0 : son reconnu sans aucun pic (groupe sans entites)
                'clap_count': len(recent),
                'strength': round(max((self._strength.get(t, 0.0) for t in recent), default=0.0), 2),
                'score': float(state['score']),
                'labels': sorted(({'label': n, 'score': s} for n, s in state['labels'].items()),
                                 key=lambda x: x['score'], reverse=True),
            })
            state.update(armed_at=0.0, score=0.0, labels={})

        # Un seul gagnant : en priorite un groupe dont le nombre de claps a une
        # entite (ou sans entites : evenement et webhook seulement). Avant, le
        # meilleur score gagnait meme s'il ne pouvait rien declencher (3 claps
        # pour [1, 2], 0 pic) et eteignait un groupe qui, lui, aurait declenche.
        def rank(c):
            counts = c['group']['clap_counts']
            if counts and c['clap_count'] in counts:
                tier = 2          # peut declencher une entite
            elif not counts and c['clap_count']:
                tier = 1          # evenement et webhook seulement, sur un vrai pic
            else:
                tier = 0          # rien a declencher (ou son reconnu sans pic)
            return (tier, c['clap_count'] > 0, c['score'])
        winner = max(candidates, key=rank)
        for c in candidates:
            c['ignored'] = c is not winner
        if rank(winner)[0] < 2:
            # Rien n'a declenche d'entite : son reconnu, mais un nombre de
            # claps sans entite (3 pour [1, 2]), pour chaque groupe concerne.
            for c in candidates:
                counts = c['group']['clap_counts']
                if counts and c['clap_count'] and c['clap_count'] not in counts:
                    self.near_misses.append({
                        'kind': 'count', 'group': c['group']['name'], 'slug': c['group']['slug'],
                        'time': c['start'], 'counted': c['clap_count'],
                        'expected': list(counts), 'score': round(c['score'], 3)})
        # Les pics de la fenetre gagnante ne serviront plus ; ceux qui la
        # suivent restent pour l'evenement suivant (geste distinct).
        self._consumed_until = winner['_end']
        self._last_trigger = now
        for c in candidates:
            c.pop('_end', None)
        return candidates

    def interrupt(self, last_clock, gap):
        """Flux interrompu apres `last_clock` (horloge audio) pendant `gap` s.

        Un groupe arme n'expirait qu'au resultat YAMNet suivant : apres une
        coupure (camera qui se reconnecte, emetteur VBAN arrete), l'evenement
        partait des dizaines de secondes apres le geste, et la fenetre
        d'analyse videe repoussait encore le resultat d'une seconde. A la
        reprise, les fenetres terminees (pendant la coupure ou avant) sont
        closes aussitot avec les pics deja connus (evenements retournes) ; une
        fenetre encore ouverte continue. Coupure de plus de LATE_MAX s : un
        geste envoye si tard surprendrait plus qu'il ne servirait, abandonne."""
        # Alertes : plus de son depuis plus que le delai d'extinction.
        alerts = self.stop_alerts() if gap >= ALERT_OFF_DELAY else []
        if gap > self.LATE_MAX:
            for st in self._group_state.values():
                st.update(armed_at=0.0, score=0.0, labels={})
            return alerts
        return alerts + self._expire(last_clock + gap)
