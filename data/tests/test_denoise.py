"""Reduction du bruit de fond avant YAMNet (spectral gating)."""
import base64
import io
import wave

import numpy as np
import pytest

import denoise

RNG = np.random.default_rng(0)


def fan(n=15600, level=0.02):
    """Bruit stationnaire (souffle grave, comme une ventilation)."""
    w = np.convolve(RNG.normal(0, 1, n + 16), np.ones(8) / 8, 'valid')[:n]
    return (level * w / w.std()).astype(np.float32)


def clap(n=15600, at=7000):
    c = np.zeros(n, np.float32)
    c[at:at + 1600] = RNG.normal(0, 1, 1600) * np.exp(-np.arange(1600) / 150) * 0.6
    return c


def snr(x, ref):
    return 10 * np.log10(np.sum(ref ** 2) / np.sum((x - ref) ** 2))


def stream(gate, x, block=1600):
    """Son traite bloc par bloc, recale (retard de denoise.LATENCY)."""
    out = np.concatenate([gate.process(x[i:i + block]) for i in range(0, x.size, block)] +
                         [gate.process(np.zeros(denoise.N_FFT, np.float32))])
    return out[denoise.LATENCY:denoise.LATENCY + x.size]


def test_streaming_reconstruction_is_exact_without_gating():
    g = denoise.SpectralGate(1)
    g._params = (0.0, 0.0)                                           # aucune attenuation
    x = RNG.normal(0, 0.1, 16000).astype(np.float32)
    y = stream(g, x, block=1500)                                     # blocs non multiples de HOP
    assert y.shape == x.shape and np.abs(y - x).max() < 1e-5


@pytest.mark.parametrize('level', [1, 2, 3])
def test_steady_noise_is_reduced_and_clap_kept(level):
    g = denoise.SpectralGate(level)
    stream(g, fan(32000))                                            # 2 s de bruit : profil appris
    noise, sig = fan(), clap()
    y = stream(g, noise + sig)
    assert snr(y, sig) > snr(noise + sig, sig) + 3                   # au moins 3 dB de mieux
    peak = slice(7000, 7400)
    assert np.abs(y[peak]).max() > 0.5 * np.abs(sig[peak]).max()     # le clap reste net
    quiet = stream(g, fan())
    assert np.sqrt(np.mean(quiet ** 2)) < 0.7 * 0.02


def test_continuous_sound_is_not_learned_as_noise_before_30_s():
    """Une alarme continue n'est pas effacee au bout de quelques secondes
    (minimum du profil sur 30 s)."""
    g = denoise.SpectralGate(3)
    stream(g, fan(16000))
    t = np.arange(16000 * 10) / 16000
    alarm = (0.2 * np.sin(2 * np.pi * 3000 * t)).astype(np.float32) + fan(t.size)
    y = stream(g, alarm)
    last = slice(-16000, None)
    assert np.sqrt(np.mean(y[last] ** 2)) > 0.8 * np.sqrt(np.mean(alarm[last] ** 2))   # 10 s plus tard : intacte


def test_level_change_and_reset_keep_noise_profile():
    g = denoise.SpectralGate(1)
    stream(g, fan())
    noise = g.noise.copy()
    g.level = 3
    g.reset()
    assert g.level == 3 and np.array_equal(g.noise, noise)
    assert denoise.clamp_level('2') == 2 and denoise.clamp_level(9) == 3 and denoise.clamp_level('x') == 0
    assert denoise.SpectralGate(2).process(np.zeros(100, np.float32)).size == 0   # pas encore une tranche


def test_detector_denoises_classifier_input_only(fake_classifier):
    import time
    from audio_detector import AudioDetector
    from audio_utils import BLOCK_SAMPLES
    det = AudioDetector('model.tflite')
    det.initialize(max_results=-1, score_threshold=0.3)
    det.set_groups([])
    det.configure('s')
    det.set_denoise(3)
    gate = det.denoise
    det.set_denoise(2)
    assert det.denoise is gate and gate.level == 2                   # profil garde
    det.set_denoise(7)
    assert gate.level == 3                                            # ramene dans les bornes
    det.set_denoise(0)
    det.set_denoise(1)
    assert det.denoise is gate and gate.level == 1                   # reactivee vite : meme profil
    assert det._denoise_switched
    raw_seen, fed = [], []
    det.add_tap(lambda ev, b, level: raw_seen.append((b, level)), events={'audio_raw'})

    class Spy:
        level = 2
        delay = 0

        def process(self, x):
            fed.append(x.size)
            return np.zeros_like(x)

        def reset(self):
            pass
    det.denoise = Spy()
    det.start()
    clf = fake_classifier.instances[-1]
    noise = np.concatenate([fan() for _ in range(3)])
    for i in range(0, noise.size - BLOCK_SAMPLES, BLOCK_SAMPLES):
        det.process_audio(noise[i:i + BLOCK_SAMPLES])
        time.sleep(0.005)
    det.wait_idle()
    det.stop()
    assert len(clf.blocks) >= 3 and fed and all(n == BLOCK_SAMPLES for n in fed)   # son brut, chaque bloc
    assert not np.any(clf.blocks[-1])                                 # YAMNet recoit le son reduit
    assert raw_seen and np.any(raw_seen[-1][0]) and raw_seen[-1][1] == 2   # « avant » : meme gain, sans reduction
    assert det.tracker.avg_level > 0.005                              # le comptage mesure le brut
    det.set_denoise(0)
    assert det.denoise is None


def test_failing_gate_falls_back_to_raw_with_warning(fake_classifier, caplog):
    from audio_detector import AudioDetector
    from audio_utils import BLOCK_SAMPLES

    class Broken:
        level = 1

        def process(self, x):
            raise ValueError('boum')
    det = AudioDetector('model.tflite')
    det.initialize(max_results=-1, score_threshold=0.3)
    det.configure('s')
    det.denoise = Broken()
    det.start()
    for _ in range(5):
        det.process_audio(fan(BLOCK_SAMPLES))
    det.stop()
    warnings = [r for r in caplog.records if 'réduction du bruit en échec' in r.getMessage()]
    assert len(warnings) == 3                                         # limite
    assert det._blocks and np.any(det._blocks[-1])


def test_recording_adds_denoised_clip(monkeypatch):
    """Reduction active : extrait avant et apres, pour comparer."""
    import types
    import time
    import recording
    from test_recording import FakeDetector
    import classify
    det = FakeDetector()
    det.denoise = denoise.SpectralGate(2)
    emit = det._emit

    def with_raw(event, *data):
        emit(event, *data)
        if event == 'audio':
            emit('audio_raw', data[0] * 2, 2)                         # « avant » : plus fort, niveau 2
    det._emit = with_raw
    monkeypatch.setattr(classify, 'detector_for', lambda sid: det)
    monkeypatch.setattr(recording, 'RESULT_GRACE', 0.3)
    monkeypatch.setattr(recording, 'time', types.SimpleNamespace(sleep=lambda s: time.sleep(s / 10)))
    out = recording.record('rtsp_cam', 2.0)
    assert out['noise_reduction'] == 2

    def peak(b64):
        with wave.open(io.BytesIO(base64.b64decode(b64))) as w:
            return np.abs(np.frombuffer(w.readframes(w.getnframes()), '<i2')).max()
    assert peak(out['wav']) > peak(out['wav_denoised'])               # avant / apres
    det2 = FakeDetector()
    monkeypatch.setattr(classify, 'detector_for', lambda sid: det2)
    assert 'wav_denoised' not in recording.record('rtsp_cam', 1.0)


def test_denoised_blocks_are_dated_with_the_gate_delay(fake_classifier, monkeypatch):
    """Son reduit en retard de gate.delay : chaque fenetre est datee a la fin
    de son contenu, pas 24 a 32 ms plus tard."""
    from audio_detector import AudioDetector
    from audio_utils import BLOCK_SAMPLES
    det = AudioDetector('model.tflite')
    det.initialize(max_results=-1, score_threshold=0.3)
    det.configure('s')
    pushed = []
    monkeypatch.setattr(det, '_push', lambda block, end: pushed.append((block.size, end, det._clock_end, det.denoise.delay)))
    det.start()
    det.set_denoise(2)
    for _ in range(3):
        det.process_audio(fan(BLOCK_SAMPLES))
    det.stop()
    delay = det.denoise.delay
    assert denoise.LATENCY <= delay < denoise.LATENCY + denoise.HOP
    produced = sum(p[0] for p in pushed) + det._pending.size
    assert produced == 3 * BLOCK_SAMPLES + denoise.LATENCY - delay    # tout rendu (24 ms de silence au depart)
    # Fin de chaque bloc : au moins `delay` echantillons avant la fin du son recu.
    assert pushed and all((clk - end) * 16000 >= d - 1e-6 for _n, end, clk, d in pushed)


def test_noise_reduction_levels_match_everywhere():
    import re
    import os
    import settings_manager
    assert settings_manager.MAX_NR_LEVEL == denoise.MAX_LEVEL == len(denoise.LEVELS)
    core = open(os.path.join(os.path.dirname(denoise.__file__), 'static', 'js', 'core.js'), encoding='utf-8').read()
    names = re.search(r"CT\.NR_NAMES = \[([^\]]*)\]", core).group(1)
    assert names.count("'") == 2 * (denoise.MAX_LEVEL + 1)


def test_noise_reduction_setting_routes(settings_dir, monkeypatch):
    from flask import Flask
    import routes.sources as rs
    applied = []
    monkeypatch.setattr(rs, '_apply_live', lambda: applied.append(1))
    monkeypatch.setattr(rs, '_find_pulse_name', lambda s: ('alsa.usb', None))
    app = Flask(__name__)
    app.register_blueprint(rs.sources_bp)
    c = app.test_client()
    sid = c.post('/api/rtsp/stream', json={'name': 'Cam', 'url': 'rtsp://c/1', 'enabled': True}).json['stream']['id']
    applied.clear()
    assert c.patch(f'/api/sources/rtsp/{sid}', json={'noise_reduction': 2}).status_code == 200
    assert settings_dir.load_settings()['rtsp_sources'][0]['noise_reduction'] == 2 and applied
    assert c.patch(f'/api/sources/rtsp/{sid}', json={'noise_reduction': 4}).status_code == 400
    assert c.patch('/api/sources/mic/mic', json={'noise_reduction': 1}).status_code == 200
    assert settings_dir.load_settings()['microphone']['noise_reduction'] == 1
    from classify import build_sources_from_settings
    built = {s['kind']: s for s in build_sources_from_settings(settings_dir.load_settings())}
    assert built['rtsp']['noise_reduction'] == 2
