"""Tests de la logique de comptage des claps (sans MediaPipe)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clap_logic import ClapTracker  # noqa: E402

GROUPS = [
    {'slug': 'clap', 'name': 'Clap', 'whitelist': {'Clapping': True}, 'threshold': 0.4},
    {'slug': 'snap', 'name': 'Snap', 'whitelist': {'Finger snapping': True}, 'threshold': 0.4},
]


class Sim:
    """Simule le flux : un bloc de 100 ms par pas, bruit de fond a 0.002."""

    def __init__(self, window=1.5):
        self.t = 1000.0
        self.tracker = ClapTracker(window=window)
        self.tracker.set_groups(GROUPS)
        self.events = []
        self.audio(50)  # stabiliser le bruit de fond

    def audio(self, n=1, peak=None):
        for _ in range(n):
            self.tracker.feed_peak(peak if peak is not None else 0.002, self.t)
            self.t += 0.1
            peak = None

    def result(self, **scores):
        cats = [(k.replace('_', ' '), v) for k, v in scores.items()]
        self.events += self.tracker.on_classification(cats, self.t)

    def settle(self, high=8, **scores):
        """YAMNet garde un score haut `high` cycles puis retombe."""
        for i in range(20):
            self.audio()
            self.result(**(scores if i < high else {k: 0.05 for k in scores}))


def test_two_claps_counted_once():
    s = Sim()
    s.audio(1, 0.3); s.audio(3); s.audio(1, 0.3); s.audio(2)
    s.result(Clapping=0.8)
    s.settle(Clapping=0.7)
    assert [(e['group']['slug'], e['clap_count'], e['ignored']) for e in s.events] == [('clap', 2, False)]


def test_old_noise_not_counted():
    s = Sim()
    s.audio(1, 0.3); s.audio(20)     # bruit 2 s avant
    s.audio(1, 0.3); s.audio(2)
    s.result(Clapping=0.8)
    s.settle(Clapping=0.7)
    assert [e['clap_count'] for e in s.events] == [1]


def test_weak_bump_below_floor_not_counted():
    s = Sim()
    s.audio(1, 0.3); s.audio(2); s.audio(1, 0.008); s.audio(2)
    s.result(Clapping=0.8)
    s.settle(Clapping=0.7)
    assert [e['clap_count'] for e in s.events] == [1]


def test_exclusivity_across_cycles():
    s = Sim()
    s.audio(1, 0.3); s.audio(2)
    s.result(Clapping=0.9)
    s.audio()
    s.result(Clapping=0.9, Finger_snapping=0.5)
    s.settle(Clapping=0.05)
    assert [(e['group']['slug'], e['ignored']) for e in s.events] == [('clap', False), ('snap', True)]


def test_no_retrigger_on_same_sound():
    """Score haut apres le declenchement, sans nouveau pic : pas de 2e evenement
    avant RETRIGGER_GUARD."""
    s = Sim(window=0.5)
    s.audio(1, 0.3)
    s.result(Clapping=0.9)
    for _ in range(8):  # 0.8 s de score haut
        s.audio()
        s.result(Clapping=0.9)
    assert len(s.events) == 1


def test_excluded_label_ignored():
    s = Sim()
    s.tracker.exclusions = {'Clapping'}
    s.audio(1, 0.3); s.audio(2)
    s.result(Clapping=0.9)
    s.settle(Clapping=0.9)
    assert s.events == []


def test_peak_ratio_relative_to_noise_floor():
    s = Sim()
    s.tracker.avg_level = 0.02   # environnement bruyant
    s.tracker.feed_peak(0.03, s.t)  # 1,5x le bruit : pas un pic (ratio 3)
    assert s.tracker.peak_times == []


def test_noisy_room_start_no_false_peaks():
    """Piece bruyante des le demarrage : aucun pic sur le bruit de fond."""
    t = ClapTracker()
    t.set_groups(GROUPS)
    now = 1000.0
    for i in range(100):  # 10 s de bruit a 0.05 +/- 20 %
        t.feed_peak(0.05 * (0.8 + 0.4 * ((i * 7) % 10) / 10), now)
        now += 0.1
    assert t.peak_times == []


def test_sustained_sound_counts_once():
    """Son tenu 1,5 s (applaudissements, aspirateur) : un seul pic."""
    t = ClapTracker()
    t.set_groups(GROUPS)
    now = 1000.0
    for _ in range(50):
        t.feed_peak(0.002, now); now += 0.1
    for i in range(15):
        t.feed_peak(0.3 + 0.03 * (i % 3), now); now += 0.1
    assert len(t.peak_times) == 1


def test_rapid_claps_with_reverb_still_counted():
    """Claps rapides dont la reverberation ne redescend pas sous le seuil."""
    t = ClapTracker()
    t.set_groups(GROUPS)
    now = 1000.0
    for _ in range(50):
        t.feed_peak(0.002, now); now += 0.1
    for _ in range(3):  # clap, 3 blocs de reverberation a 30 %, creux
        t.feed_peak(0.5, now); now += 0.1
        for _ in range(3):
            t.feed_peak(0.15, now); now += 0.1
    assert len(t.peak_times) == 3


def test_one_result_per_second_cadence():
    """Si MediaPipe ne rend qu'un resultat par ~0,975 s : 2 claps comptes une fois."""
    t = ClapTracker(window=1.5)
    t.set_groups(GROUPS)
    now, events = 1000.0, []
    for _ in range(50):
        t.feed_peak(0.002, now); now += 0.1
    t.feed_peak(0.4, now); now += 0.1
    for _ in range(3):
        t.feed_peak(0.002, now); now += 0.1
    t.feed_peak(0.4, now); now += 0.1
    for step in range(40):
        t.feed_peak(0.002, now)
        if step % 10 == 0:  # un resultat par seconde
            events += t.on_classification([('Clapping', 0.9 if step < 20 else 0.05)], now)
        now += 0.1
    assert [(e['clap_count'], e['ignored']) for e in events] == [(2, False)]


def _quiet_tracker(**kw):
    t = ClapTracker(**kw)
    t.set_groups(GROUPS)
    now = 1000.0
    for _ in range(50):
        t.feed_peak(0.002, now); now += 0.1
    return t, now


def _decay(t, now, start, factor):
    level = start
    while level > 0.002:
        t.feed_peak(level, now); now += 0.1
        level *= factor
    return now


def test_reverberant_clap_counts_once():
    """Clap dont la reverberation decroit lentement (40 a 75 % par bloc) : un pic.
    Avant 6.37, le bloc qui passait sous le creux attendu etait recompte."""
    for factor in (0.25, 0.4, 0.5, 0.6, 0.75):
        t, now = _quiet_tracker()
        _decay(t, now, 0.8, factor)
        assert len(t.peak_times) == 1, factor


def test_double_clap_in_reverberant_room():
    t, now = _quiet_tracker()
    level = 0.8                 # 1er clap, 0,5 s de reverberation a 60 %/bloc
    for _ in range(5):
        t.feed_peak(level, now); now += 0.1; level *= 0.6
    _decay(t, now, 0.8, 0.6)
    assert len(t.peak_times) == 2


def test_clap_straddling_two_blocks_counts_once():
    t, now = _quiet_tracker()
    t.feed_peak(0.3, now); now += 0.1   # debut du clap en fin de bloc
    t.feed_peak(0.8, now); now += 0.1   # le pic dans le bloc suivant
    _decay(t, now, 0.3, 0.3)
    assert len(t.peak_times) == 1


def test_recognised_sound_without_peak_does_not_trigger_clap_entities():
    """Applaudissements continus a la tele, sans transitoire : aucun evenement
    pour un groupe qui publie des entites (avant : « 1 clap » toutes les 2,5 s)."""
    t, now = _quiet_tracker()
    events = []
    for _ in range(100):
        t.feed_peak(0.002, now)
        events += t.on_classification([('Clapping', 0.9)], now)
        now += 0.1
    assert events == []


def test_group_without_entities_triggers_without_peak_with_zero_count():
    t = ClapTracker()
    t.set_groups([{'slug': 'dog', 'name': 'Chien', 'whitelist': {'Bark': True},
                   'threshold': 0.4, 'clap_counts': []}])
    now, events = 1000.0, []
    for _ in range(50):
        t.feed_peak(0.002, now); now += 0.1
    for i in range(20):
        t.feed_peak(0.002, now)
        events += t.on_classification([('Bark', 0.9 if i < 5 else 0.05)], now)
        now += 0.1
    assert [(e['group']['slug'], e['clap_count']) for e in events] == [('dog', 0)]


def test_three_groups_single_winner():
    """3 groupes reconnaissent le meme son : un seul gagnant, les autres ignores."""
    t = ClapTracker(window=0.5)
    t.set_groups([
        {'slug': 'a', 'name': 'A', 'whitelist': {'Clapping': True}, 'threshold': 0.3},
        {'slug': 'b', 'name': 'B', 'whitelist': {'Hands': True}, 'threshold': 0.3},
        {'slug': 'c', 'name': 'C', 'whitelist': {'Applause': True}, 'threshold': 0.3},
    ])
    now, events = 1000.0, []
    for _ in range(50):
        t.feed_peak(0.002, now); now += 0.1
    t.feed_peak(0.5, now); now += 0.1
    for _ in range(10):
        t.feed_peak(0.002, now)
        events += t.on_classification([('Clapping', 0.6), ('Hands', 0.9), ('Applause', 0.4)], now)
        now += 0.1
    assert sorted((e['group']['slug'], e['ignored']) for e in events) == [('a', True), ('b', False), ('c', True)]


def test_params_changed_while_armed():
    """Fenetre raccourcie pendant qu'un groupe est arme : la detection se
    termine avec la nouvelle fenetre, sans erreur."""
    s = Sim(window=1.5)
    s.audio(1, 0.3); s.audio(2)
    s.result(Clapping=0.8)
    s.tracker.set_params(window=0.3)
    s.settle(Clapping=0.7)
    assert [e['clap_count'] for e in s.events] == [1]


def test_set_groups_keeps_empty_clap_counts():
    t = ClapTracker()
    t.set_groups([{'slug': 'x', 'whitelist': {}, 'ha_entities': []},
                  {'slug': 'y', 'whitelist': {}, 'clap_counts': [1, 9, True, 3]},
                  {'slug': 'z', 'whitelist': {}}])
    assert [g['clap_counts'] for g in t.groups] == [[], [1, 3], [1, 2]]


def test_lookback_window_is_configurable():
    """Resultat 1,4 s apres le pic : hors fenetre par defaut (1,2 s), rattache
    avec une fenetre de rattachement plus large (reglage du tracker)."""
    for lookback, expected in ((1.2, []), (1.5, [1])):
        t, now = _quiet_tracker()
        t.peak_lookback = lookback
        t.feed_peak(0.5, now)
        events = []
        for _ in range(14):
            now += 0.1
            t.feed_peak(0.002, now)
        events += t.on_classification([('Clapping', 0.9)], now)
        for _ in range(20):
            now += 0.1
            t.feed_peak(0.002, now)
            events += t.on_classification([('Clapping', 0.05)], now)
        assert [e['clap_count'] for e in events] == expected, lookback


def test_sustained_sound_counted_in_sustain_mode():
    """Sifflement (son tenu, montee lente) : 0 attaque, mais un pic toutes les
    0,3 s en mode « sons tenus » (comportement d'avant la 6.37)."""
    from clap_logic import ClapTracker
    tr = ClapTracker(window=1.5)
    tr.set_groups([{'slug': 'siffler', 'name': 'Siffler', 'threshold': 0.5, 'ha_entities': [3],
                    'sound_whitelist': {'Whistling': True}, 'count_mode': 'tenus'}])
    t = 0.0
    for _ in range(10):
        t += 0.1
        tr.feed_peak(0.002, t, pre_level=0.002)
    level = 0.002
    for _ in range(15):     # montee lente puis son tenu ~1 s
        t += 0.1
        level = min(0.05, level * 1.4)
        tr.feed_peak(level, t, pre_level=level * 0.95)
    assert tr.attack_count == 0 and len(tr.sustain_times) >= 3
    events = []
    for _ in range(25):
        t += 0.1
        events += tr.on_classification([('Whistling', 0.8)], t)
        tr.feed_peak(0.002, t, pre_level=0.002)
    assert events and events[0]['group']['slug'] == 'siffler' and events[0]['clap_count'] >= 3


def test_arbitration_prefers_group_with_matching_entity():
    """Deux groupes reconnaissent le meme son : « Fort » a le meilleur score
    mais aucune entite pour 1 clap ; « Clap » (1 clap) gagne au lieu d'etre
    eteint pour rien."""
    t, now = _quiet_tracker()
    t.set_groups([{'slug': 'fort', 'name': 'Fort', 'whitelist': {'Applause': True}, 'threshold': 0.4,
                   'ha_entities': [3]},
                  {'slug': 'clap', 'name': 'Clap', 'whitelist': {'Clapping': True}, 'threshold': 0.4,
                   'ha_entities': [1]}])
    t.feed_peak(0.5, now)
    events = []
    for _ in range(25):
        now += 0.1
        t.feed_peak(0.002, now)
        events += t.on_classification([('Applause', 0.9), ('Clapping', 0.6)], now)
    winner = next(e for e in events if not e['ignored'])
    assert winner['group']['slug'] == 'clap' and winner['clap_count'] == 1


def test_event_only_group_without_peak_does_not_beat_matching_clap():
    """Groupe sans entite arme sans pic (applaudissements de la tele) : il ne
    passe pas devant un vrai clap d'un groupe a entite."""
    t, now = _quiet_tracker()
    t.set_groups([{'slug': 'tv', 'name': 'TV', 'whitelist': {'Applause': True}, 'threshold': 0.4,
                   'ha_entities': []},
                  {'slug': 'clap', 'name': 'Clap', 'whitelist': {'Clapping': True}, 'threshold': 0.4,
                   'ha_entities': [1]}])
    events = []
    for i in range(30):
        now += 0.1
        if i == 12:
            t.feed_peak(0.5, now)
        else:
            t.feed_peak(0.002, now)
        events += t.on_classification([('Applause', 0.9)] + ([('Clapping', 0.6)] if i >= 12 else []), now)
    winners = [e for e in events if not e['ignored']]
    assert any(w['group']['slug'] == 'clap' and w['clap_count'] == 1 for w in winners)



def _realistic(window, claps, duration=6.0, result_every=0.975):
    """Blocs de 100 ms, resultats YAMNet toutes les 975 ms (haut tant que des
    claps recents)."""
    tracker = ClapTracker(window=window)
    tracker.set_groups([dict(GROUPS[0], clap_counts=[1, 2, 3])])
    t, events, next_result = 1000.0, [], 1000.0 + 5.0
    for _ in range(50):                      # bruit de fond
        tracker.feed_peak(0.002, t)
        t += 0.1
    start = t
    pending = sorted(start + c for c in claps)
    while t < start + duration:
        peak = 0.5 if pending and abs(pending[0] - t) < 0.05 else 0.002
        if peak > 0.002:
            pending.pop(0)
        tracker.feed_peak(peak, t)
        if t >= next_result:
            recent = any(0 <= t - (start + c) <= 1.2 for c in claps)
            events += tracker.on_classification([('Clapping', 0.9 if recent else 0.02)], t)
            next_result += result_every
        t = round(t + 0.1, 3)
    return events


def test_window_counts_only_its_own_claps():
    """Deux claps puis, 1,8 s apres le premier, un clap isole : deux gestes
    (« 2 claps » puis « 1 clap »), et non un « 3 claps » sans entite."""
    events = _realistic(1.5, [0.0, 0.5, 1.8])
    counts = [e['clap_count'] for e in events if not e['ignored']]
    assert counts == [2, 1]


def test_three_quick_claps_still_three():
    events = _realistic(1.5, [0.0, 0.4, 0.8])
    assert [e['clap_count'] for e in events if not e['ignored']] == [3]


def test_removed_armed_group_does_not_break_expiry():
    """Groupe desactive pendant que sa fenetre est ouverte : son etat est
    oublie (il faisait echouer l'expiration et bloquait la source)."""
    tracker = ClapTracker(window=1.5)
    tracker.set_groups([dict(GROUPS[0], clap_counts=[1])])
    t = 1000.0
    for _ in range(50):
        tracker.feed_peak(0.002, t)
        t += 0.1
    tracker.feed_peak(0.5, t)
    assert tracker.on_classification([('Clapping', 0.9)], t + 0.2) == []   # arme
    tracker.set_groups([])                                                  # groupe retire
    assert tracker.on_classification([('Clapping', 0.9)], t + 3.0) == []   # aucune erreur


def test_interrupt_fires_finished_gesture_and_drops_after_long_cut():
    from clap_logic import ClapTracker
    def armed_tracker():
        t = ClapTracker(window=1.5)
        t.set_groups([{'slug': 'clap', 'name': 'Clap', 'whitelist': {'Clapping': True},
                       'threshold': 0.5, 'clap_counts': [1, 2]}])
        t._group_state['clap'] = {'armed_at': 10.0, 'score': 0.9, 'labels': {}}
        t.peak_times = [10.0, 10.4]
        return t
    t = armed_tracker()
    ev = t.interrupt(11.7, 3.0)                          # geste termine avant la reprise
    assert [e['clap_count'] for e in ev if not e['ignored']] == [2] and ev[0]['start'] == 10.0
    t = armed_tracker()
    assert t.interrupt(10.6, 0.6) == [] and t._group_state['clap']['armed_at'] == 10.0   # fenetre encore ouverte
    t = armed_tracker()
    assert t.interrupt(11.7, 30.0) == [] and not t._group_state['clap']['armed_at']    # trop tard : abandonne


def _alert_tracker(confirm=3):
    from clap_logic import ClapTracker
    t = ClapTracker(window=1.5)
    t.set_groups([{'slug': 'clap', 'name': 'Clap', 'whitelist': {'Clapping': True}, 'threshold': 0.5, 'clap_counts': [1, 2]},
                  {'slug': 'fumee', 'name': 'Détecteur de fumée', 'whitelist': {'Smoke detector, smoke alarm': True},
                   'threshold': 0.3, 'count_mode': 'alerte', 'alert_confirm': confirm}])
    return t


def test_alert_turns_on_after_confirmations_and_off_after_silence():
    from clap_logic import ALERT_OFF_DELAY
    t = _alert_tracker(confirm=3)
    smoke = [('Smoke detector, smoke alarm', 0.6)]
    states = []
    now = 100.0
    for cats in (smoke, [], smoke, smoke):                # 3 reconnaissances sur 4 analyses
        states += [(e['alert'], e['group']['slug']) for e in t.on_classification(cats, now)]
        now += 0.5
    assert states == [('on', 'fumee')]
    for _ in range(int(ALERT_OFF_DELAY / 0.5) - 1):      # silence, pas encore assez long
        assert t.on_classification([], now) == []
        now += 0.5
    ev = t.on_classification([], now + 0.5)
    assert [(e['alert'], e['group']['slug']) for e in ev] == [('off', 'fumee')]


def test_alert_group_never_counts_claps_nor_blocks_them():
    t = _alert_tracker(confirm=1)
    t.feed_peak(0.001, 99.0)
    for i in range(12):
        t.feed_peak(0.001, 99.0 + i * 0.01)               # bruit de fond (echauffement)
    t.feed_peak(0.8, 100.0, pre_level=0.001)
    ev = t.on_classification([('Clapping', 0.9), ('Smoke detector, smoke alarm', 0.9)], 100.2)
    assert [e['alert'] for e in ev if e.get('alert')] == ['on']
    ev = t.on_classification([('Clapping', 0.9)], 101.8)
    claps = [e for e in ev if not e.get('alert') and not e['ignored']]
    assert claps and claps[0]['group']['slug'] == 'clap' and claps[0]['clap_count'] == 1


def test_alert_switched_off_when_group_removed_or_detection_stops():
    t = _alert_tracker(confirm=1)
    t.on_classification([('Smoke detector, smoke alarm', 0.9)], 100.0)
    assert [e['alert'] for e in t.stop_alerts()] == ['off'] and t.stop_alerts() == []
    t.on_classification([('Smoke detector, smoke alarm', 0.9)], 101.0)
    t.on_classification([('Smoke detector, smoke alarm', 0.9)], 101.5)
    t.set_groups([{'slug': 'clap', 'name': 'Clap', 'whitelist': {'Clapping': True}, 'threshold': 0.5}])
    assert [(e['alert'], e['group']['slug']) for e in t.drop_alerts()] == [('off', 'fumee')]


def test_long_cut_switches_alerts_off():
    t = _alert_tracker(confirm=1)
    t.on_classification([('Smoke detector, smoke alarm', 0.9)], 100.0)
    ev = t.interrupt(100.0, 60.0)
    assert [e['alert'] for e in ev] == ['off']
