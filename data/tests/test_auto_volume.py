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


def test_stop_is_immediate_and_device_change_restarts(monkeypatch):
    import time
    import auto_volume
    monkeypatch.setattr(auto_volume, 'load_settings', lambda: {'microphone': {'volume': 100}})
    monkeypatch.setattr(AutoVolume, '_persist_volume', lambda self, v: None)
    av = AutoVolume()
    av.start('alsa.a', None)
    av.start('alsa.b', None)          # autre micro : relance sur le nouveau
    assert av._pulse_name == 'alsa.b'
    t0 = time.monotonic()
    av.stop()
    assert time.monotonic() - t0 < 0.5   # attendait jusqu'a 2 s (sleep)


# --- Boucle d'ajustement (fil reel, intervalle raccourci) -------------------------
def test_adjust_loop_applies_notifies_and_persists(settings_dir, monkeypatch):
    import types
    import audio_utils
    import auto_volume
    from helpers import wait_until
    monkeypatch.setattr(auto_volume, 'ADJUST_INTERVAL', 0.05)
    applied, emitted = [], []
    monkeypatch.setattr(audio_utils, 'set_pulse_volume', lambda name, vol: applied.append((name, vol)))
    settings_dir.modify_settings(lambda s: s.setdefault('microphone', {}).update(volume=100))
    av = AutoVolume()

    def feed_until(cond, peak):
        av.feed_peak(peak)
        return cond()
    sio = types.SimpleNamespace(emit=lambda ev, data: emitted.append((ev, data)))
    av.start('alsa.mic', sio)
    try:
        # Saturation : baisse appliquee, annoncee et enregistree (1re fois)
        wait_until(lambda: feed_until(lambda: applied, CLIP_LEVEL))
        assert applied[0] == ('alsa.mic', 100 - STEP_DOWN)
        assert emitted[0] == ('auto_volume_update', {'volume': 100 - STEP_DOWN})
        wait_until(lambda: settings_dir.load_settings()['microphone']['volume'] == 100 - STEP_DOWN)
        # 2e baisse dans les 30 s : appliquee mais pas encore ecrite...
        wait_until(lambda: feed_until(lambda: len(applied) >= 2, CLIP_LEVEL))
        assert settings_dir.load_settings()['microphone']['volume'] == 100 - STEP_DOWN
    finally:
        av.stop()
    # ... ecrite a l'arret
    assert settings_dir.load_settings()['microphone']['volume'] == applied[-1][1]
    assert not av.running


def test_stop_without_loop_keeps_user_volume(settings_dir):
    settings_dir.modify_settings(lambda s: s.setdefault('microphone', {}).update(volume=73))
    AutoVolume().stop()   # jamais demarre : ne reecrit pas le volume par defaut
    assert settings_dir.load_settings()['microphone']['volume'] == 73


def test_apply_and_persist_errors_are_contained(monkeypatch, caplog):
    import audio_utils
    import settings_manager

    def boom(*a, **k):
        raise RuntimeError('panne')
    monkeypatch.setattr(audio_utils, 'set_pulse_volume', boom)
    monkeypatch.setattr(settings_manager, 'atomic_update', boom)
    av = AutoVolume()
    av._apply_volume(50)
    av._persist_volume(50)
    assert caplog.text.count('panne') == 2
