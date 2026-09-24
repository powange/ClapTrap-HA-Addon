"""Algorithme de l'auto-volume (fonction pure)."""
from auto_volume import AutoVolume, CLIP_LEVEL, STEP_DOWN, STEP_UP, VOLUME_MAX, VOLUME_MIN

decide = AutoVolume.decide


def test_no_data_keeps_volume():
    assert decide(100, [], 0.0) == 100


def test_clipping_lowers_volume():
    assert decide(100, [0.1, CLIP_LEVEL], 0.95) == 100 - STEP_DOWN
    assert decide(VOLUME_MIN, [1.0], 1.0) == VOLUME_MIN


def test_quiet_room_raises_slowly():
    assert decide(100, [0.001] * 20, 0.01) == 100 + STEP_UP
    assert decide(VOLUME_MAX, [0.001] * 20, 0.01) == VOLUME_MAX


def test_no_raise_after_loud_sound():
    """Un clap fort dans les 30 s : ne pas monter (l'ancien algorithme montait
    jusqu'a 150 % dans une piece calme et ecretait les claps)."""
    assert decide(100, [0.001] * 20, 0.5) == 100


def test_normal_level_unchanged():
    assert decide(100, [0.05] * 20, 0.2) == 100
