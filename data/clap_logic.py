"""Logique de detection des claps, sans E/S ni thread (testable seule).

ClapTracker recoit :
- le pic brut de chaque bloc audio de 100 ms (`feed_peak`) : suivi du bruit
  de fond et detection des fronts montants (les "pics" comptes comme claps) ;
- chaque resultat de classification YAMNet (`on_classification`) : armement
  des groupes de sons, fenetre multi-clap, arbitrage entre groupes.

Il renvoie les evenements de detection a declencher. L'horloge est passee en
parametre (`now`) pour pouvoir tester la logique de facon deterministe.
"""


DEFAULT_WHITELIST = {"Clapping": True, "Hands": True, "Applause": True}


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
    # Marge de 0,2 s pour la duree d'inference ; portee a 1,5 s si MediaPipe
    # ne rend qu'un resultat par seconde (cadence mesuree par le detecteur).
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
        self._consumed_until = 0.0
        self._last_trigger = 0.0
        self._group_state = {}   # slug -> {armed_at, score, labels}

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

        # Moyenne glissante du bruit de fond. Elle continue (plus lentement)
        # pendant un son : un bruit qui s'installe finit par etre absorbe.
        rate = 0.001 if self._above else 0.005
        self.avg_level = self.avg_level * (1 - rate) + raw_peak * rate
        threshold = max(floor, self.avg_level * self.peak_ratio)

        max_age = max(2.0, self.window + 1.0)
        self.peak_times = [t for t in self.peak_times if (now - t) < max_age]

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
            self._above = True
        elif raw_peak < threshold * 0.6:
            self._above = False
        self._prev_peak = raw_peak

    # --- Classification -----------------------------------------------------

    def on_classification(self, categories, now):
        """`categories` : [(label, score)]. Retourne la liste des evenements a
        declencher : dicts {group, clap_count, score, labels, ignored}."""
        fresh = [t for t in self.peak_times
                 if t > self._consumed_until and t >= now - self.peak_lookback]
        # Sans pic, seul un groupe sans entites (evenement et webhook seulement)
        # peut s'armer : une tele qui diffuse des applaudissements declenchait
        # sinon « 1 clap » toutes les 2,5 s.
        can_arm_without_peak = (now - self._last_trigger) >= self.RETRIGGER_GUARD

        any_expired = False
        for group in self.groups:
            hits = [(name, score) for name, score in categories
                    if group['whitelist'].get(name, False) and name not in self.exclusions]
            best = max((score for _, score in hits), default=0.0)
            state = self._group_state.setdefault(group['slug'], {'armed_at': 0.0, 'score': 0.0, 'labels': {}})

            if best >= group['threshold']:
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

            if state['armed_at'] and (now - state['armed_at']) >= self.window:
                any_expired = True

        if not any_expired:
            return []

        # Des qu'une fenetre expire, TOUS les groupes armes entrent en
        # arbitrage : un seul gagnant par evenement sonore.
        candidates = []
        for group in self.groups:
            state = self._group_state.get(group['slug'])
            if not state or not state['armed_at']:
                continue
            recent = [t for t in self.peak_times
                      if state['armed_at'] <= t <= now and t > self._consumed_until]
            candidates.append({
                'group': group,
                # 0 : son reconnu sans aucun pic (groupe sans entites)
                'clap_count': len(recent),
                'score': float(state['score']),
                'labels': sorted(({'label': n, 'score': s} for n, s in state['labels'].items()),
                                 key=lambda x: x['score'], reverse=True),
            })
            state.update(armed_at=0.0, score=0.0, labels={})

        winner = max(candidates, key=lambda c: c['score'])
        for c in candidates:
            c['ignored'] = c is not winner
        # Les pics comptes ici ne serviront plus a une autre detection.
        self._consumed_until = now
        self._last_trigger = now
        return candidates
