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


def test_lookback_extended_for_slow_cadence():
    """Resultat 1,4 s apres le pic : hors fenetre par defaut, rattache avec la
    fenetre elargie (cadence d'un resultat par seconde)."""
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
