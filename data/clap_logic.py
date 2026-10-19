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
    }


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

    def feed_peak(self, raw_peak, now, pre_level=None, gain=1.0):
        """Pic brut (avant gain et auto-gain) d'un bloc audio.

        `now` : instant du pic ; `pre_level` : niveau juste avant lui (voir
        ATTACK_RATIO) ; `gain` : gain de la source. Le plancher absolu est
        rapporte au gain : sur le signal brut, une camera faible (gain x10)
        n'atteignait jamais 0,01 et plus aucun clap n'etait compte.
        """
        floor = self.PEAK_FLOOR / max(1.0, float(gain or 1.0))
        if len(self._warmup) < self.WARMUP_BLOCKS:
            self._warmup.append(raw_peak)
            if len(self._warmup) == self.WARMUP_BLOCKS:
                self.avg_level = max(0.0001, sorted(self._warmup)[self.WARMUP_BLOCKS // 2])
            return

        # Force du pic par rapport au bruit de fond (avant que la moyenne ne
        # l'absorbe) : comparable entre sources de gains differents
        # (arbitrage entre pieces : la plus proche entend le plus fort).
        strength = raw_peak / max(self.avg_level, 1e-6)
        # Moyenne glissante du bruit de fond. Elle continue (plus lentement)
        # pendant un son : un bruit qui s'installe finit par etre absorbe.
        rate = 0.001 if self._above else 0.005
        self.avg_level = self.avg_level * (1 - rate) + raw_peak * rate
        threshold = max(floor, self.avg_level * self.peak_ratio)

        max_age = max(2.0, self.window + 1.0)
        self.peak_times = [t for t in self.peak_times if (now - t) < max_age]
        self.sustain_times = [t for t in self.sustain_times if (now - t) < max_age]
        self._strength = {t: v for t, v in self._strength.items() if (now - t) < max_age}

        if raw_peak > threshold:
            if not self._sus_above or (now - self._sus_last) >= self.SUSTAIN_RESET:
                self._sus_last = now
                self.sustain_times.append(now)
                self._strength[now] = strength
                self.sustain_count += 1
            self._sus_above = True
        elif raw_peak < threshold * 0.6:
            self._sus_above = False

        if pre_level is not None:
            attack = raw_peak > pre_level * self.ATTACK_RATIO
        else:
            attack = (not self._above
                      or (raw_peak > self._prev_peak * self.ATTACK_RATIO
                          and (now - self._last_peak_time) > self.REATTACK_GAP))
        if raw_peak > threshold and attack:
            # Front montant ou nouvelle attaque : nouveau pic
            if (now - self._last_peak_time) > self.peak_cooldown:
                self._last_peak_time = now
                self.peak_times.append(now)
                self._strength[now] = strength
                self.attack_count += 1
            self._above = True
        elif raw_peak < threshold * 0.6:
            self._above = False
        self._prev_peak = raw_peak

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
            if group['count_mode'] == 'alerte':
                # Hors des fenetres de comptage et de l'arbitrage : une alarme
                # n'empeche pas un clap, et inversement.
                ev = self._alert(group, best, hits, now)
                if ev:
                    alert_events.append(ev)
                continue
            state = self._group_state.setdefault(group['slug'], {'armed_at': 0.0, 'score': 0.0, 'labels': {}})

            if best >= group['threshold']:
                fresh = fresh_of(group)
                if not state['armed_at']:
                    if fresh or (can_arm_without_peak and not group['clap_counts']):
                        state['armed_at'] = fresh[0] if fresh else now
                        state['score'] = best
                        state['labels'] = {}
                elif best > state['score']:
                    state['score'] = best
                if state['armed_at']:
                    for name, score in hits:
                        if score >= group['threshold'] and score > state['labels'].get(name, 0):
                            state['labels'][name] = float(score)
        return alert_events + self._expire(now)

    def _times(self, group):
        return self.sustain_times if group.get('count_mode') == 'tenus' else self.peak_times

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
            candidates.append({
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
