"""Logique de detection des claps, sans E/S ni thread (testable seule).

ClapTracker recoit :
- le pic brut de chaque bloc audio de 100 ms (`feed_peak`) : suivi du bruit
  de fond et detection des fronts montants (les "pics" comptes comme claps) ;
- chaque resultat de classification YAMNet (`on_classification`) : armement
  des groupes de sons, fenetre multi-clap, arbitrage entre groupes.

Il renvoie les evenements de detection a declencher. L'horloge est passee en
parametre (`now`) pour pouvoir tester la logique de facon deterministe.
"""


class ClapTracker:
    # Plancher absolu (signal brut, avant auto-gain) pour qu'un front compte
    # comme un pic.
    PEAK_FLOOR = 0.01
    # Un pic n'est rattache a une detection que s'il date de moins de
    # PEAK_LOOKBACK s : fenetre d'analyse de YAMNet (~0.975 s) + marge.
    PEAK_LOOKBACK = 1.2
    # Apres un declenchement, le score YAMNet reste haut ~1 s sur le meme son :
    # un groupe ne se re-arme qu'avec un nouveau pic ou apres ce delai.
    RETRIGGER_GUARD = 1.0

    def __init__(self, window=1.5, peak_cooldown=0.08, peak_ratio=3.0, peak_reset=0.3):
        self.window = window
        self.peak_cooldown = peak_cooldown
        self.peak_ratio = peak_ratio
        self.peak_reset = peak_reset
        self.groups = []         # [{slug, name, whitelist, threshold, clap_counts}]
        self.exclusions = set()
        self.avg_level = 0.001   # niveau moyen du bruit de fond (signal brut)
        self._above = False
        self._last_peak_time = 0.0
        self.peak_times = []
        self._consumed_until = 0.0
        self._last_trigger = 0.0
        self._group_state = {}   # slug -> {armed_at, score, labels}

    # --- Configuration ----------------------------------------------------

    def set_params(self, window=None, peak_cooldown=None, peak_ratio=None, peak_reset=None):
        if window is not None:
            self.window = float(window)
        if peak_cooldown is not None:
            self.peak_cooldown = float(peak_cooldown)
        if peak_ratio is not None:
            self.peak_ratio = float(peak_ratio)
        if peak_reset is not None:
            self.peak_reset = float(peak_reset)

    def set_groups(self, groups, default_threshold=0.3):
        normalised = []
        for idx, g in enumerate(groups or []):
            if not isinstance(g, dict):
                continue
            normalised.append({
                'slug': g.get('slug') or f'group{idx + 1}',
                'name': g.get('name') or g.get('slug') or f'Groupe {idx + 1}',
                'whitelist': dict(g.get('whitelist') or g.get('sound_whitelist') or {}),
                'threshold': float(g.get('threshold', default_threshold)),
                'clap_counts': list(g.get('clap_counts') or g.get('ha_entities') or [1, 2]),
            })
        self.groups = normalised

    @property
    def min_threshold(self):
        return min((g['threshold'] for g in self.groups), default=None)

    # --- Pics -------------------------------------------------------------

    def feed_peak(self, raw_peak, now):
        """Pic brut (avant auto-gain) d'un bloc audio."""
        # Moyenne glissante lente du bruit de fond, hors pics.
        if not self._above:
            self.avg_level = self.avg_level * 0.995 + raw_peak * 0.005
        threshold = max(self.PEAK_FLOOR, self.avg_level * self.peak_ratio)

        max_age = max(2.0, self.window + 1.0)
        self.peak_times = [t for t in self.peak_times if (now - t) < max_age]

        if raw_peak > threshold and not self._above:
            # Front montant : nouveau pic
            if (now - self._last_peak_time) > self.peak_cooldown:
                self._last_peak_time = now
                self.peak_times.append(now)
            self._above = True
        elif raw_peak < threshold * 0.6:
            self._above = False
        elif self._above and (now - self._last_peak_time) > self.peak_reset:
            # Un son qui dure : on le considere fini pour compter les suivants.
            self._above = False
        return threshold

    # --- Classification -----------------------------------------------------

    def on_classification(self, categories, now):
        """`categories` : [(label, score)]. Retourne la liste des evenements a
        declencher : dicts {group, clap_count, score, labels, ignored}."""
        fresh = [t for t in self.peak_times
                 if t > self._consumed_until and t >= now - self.PEAK_LOOKBACK]
        can_arm = bool(fresh) or (now - self._last_trigger) >= self.RETRIGGER_GUARD

        any_expired = False
        for group in self.groups:
            hits = [(name, score) for name, score in categories
                    if group['whitelist'].get(name, False) and name not in self.exclusions]
            best = max((score for _, score in hits), default=0.0)
            state = self._group_state.setdefault(group['slug'], {'armed_at': 0.0, 'score': 0.0, 'labels': {}})

            if best >= group['threshold']:
                if not state['armed_at']:
                    if can_arm:
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
                'clap_count': max(1, len(recent)),
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
