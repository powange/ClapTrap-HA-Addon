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
