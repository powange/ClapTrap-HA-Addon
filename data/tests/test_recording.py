"""Enregistrement d'un extrait (son donne a YAMNet + classes reconnues)."""
import base64
import io
import threading
import time
import types
import wave

import numpy as np
import pytest

import recording
from audio_utils import BLOCK_SAMPLES


class FakeDetector:
    """Detecteur qui envoie, une fois ecoute, des blocs et des resultats sur
    son horloge audio (comme le vrai : resultat date a la fin de la fenetre).
    `jump` : saut d'horloge (flux coupe puis repris) apres ce bloc."""
    def __init__(self, raw=0.1, gain=2.0, jump=None):
        self._clock_end = 100.0
        self.taps, self.raw, self.gain, self.jump = [], raw, gain, jump
        self._stop = threading.Event()

    def add_tap(self, fn, events=None):
        self.taps.append((fn, events))
        threading.Thread(target=self._run, daemon=True).start()

    def remove_tap(self, fn):
        self.taps = [t for t in self.taps if t[0] is not fn]
        self._stop.set()

    def _emit(self, event, *data):
        for fn, events in list(self.taps):
            if events is None or event in events:
                fn(event, *data)

    def _run(self):
        n = 0
        while not self._stop.is_set():
            n += 1
            self._clock_end += 0.1
            self._emit('block', self.raw, self.gain, self._clock_end)
            self._emit('audio', np.full(BLOCK_SAMPLES, min(0.9, self.raw * self.gain), np.float32))
            if n == self.jump:
                self._clock_end += 1000.0          # recalage de l'horloge (coupure)
            if n % 10 == 0:
                self._emit('result', [('Finger snapping', 0.71), ('Animal', 0.4), ('Speech', 0.1), ('X', 0.01)],
                           self._clock_end)
            time.sleep(0.01)


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(recording, 'RESULT_GRACE', 0.3)
    # Attentes de l'enregistrement 10x plus courtes (module seulement)
    monkeypatch.setattr(recording, 'time', types.SimpleNamespace(sleep=lambda s: time.sleep(s / 10)))


def use(monkeypatch, det):
    import classify
    monkeypatch.setattr(classify, 'detector_for', lambda sid: det)


def test_record_returns_wav_and_timeline(monkeypatch, fast):
    det = FakeDetector()
    use(monkeypatch, det)
    out = recording.record('rtsp_cam', 2.0)
    with wave.open(io.BytesIO(base64.b64decode(out['wav']))) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)
        pcm = np.frombuffer(w.readframes(w.getnframes()), '<i2')
    assert pcm.size == out['seconds'] * 16000 and abs(pcm.max() - 0.2 * 32767) < 2
    assert out['timeline'] and out['timeline'][0]['top'][0] == {'classe': 'Finger snapping', 'score': 0.71}
    assert len(out['timeline'][0]['top']) == 3                      # 3 classes par fenetre
    assert all(0 <= row['t'] <= out['seconds'] for row in out['timeline'])
    assert out['clipped'] is False and out['limited'] is False and det.taps == []   # ecoute retiree


def test_timeline_follows_the_sound_across_a_clock_jump(monkeypatch, fast):
    """Coupure du flux (horloge recalee) : les lignes restent placees dans
    l'extrait, pas a des milliers de secondes."""
    use(monkeypatch, FakeDetector(jump=5))
    out = recording.record('rtsp_cam', 3.0)
    assert out['timeline'] and all(0 <= row['t'] <= out['seconds'] for row in out['timeline'])
    starts = [row['t'] for row in out['timeline']]
    assert starts == sorted(starts)


def test_record_flags_input_saturation_and_limiter(monkeypatch, fast):
    use(monkeypatch, FakeDetector(raw=1.0, gain=1.0))
    out = recording.record('rtsp_cam', 1.0)
    assert out['clipped'] is True and out['limited'] is True     # sature des l'entree
    use(monkeypatch, FakeDetector(raw=0.2, gain=10.0))
    out = recording.record('rtsp_cam', 1.0)
    assert out['clipped'] is False and out['limited'] is True    # reduit par le limiteur (gain eleve)


def test_record_needs_a_listened_source(monkeypatch):
    use(monkeypatch, None)
    with pytest.raises(recording.RecordingError) as e:
        recording.record('rtsp_cam', 1.0)
    assert e.value.status == 409 and 'démarrez la détection' in str(e.value)


def test_one_recording_per_source(monkeypatch, fast):
    use(monkeypatch, FakeDetector())
    recording._busy.add('rtsp_cam')
    try:
        with pytest.raises(recording.RecordingError, match='déjà en cours'):
            recording.record('rtsp_cam', 1.0)
        assert recording.record('rtsp_autre', 1.0)['timeline'] is not None   # autre source : possible
    finally:
        recording._busy.discard('rtsp_cam')


def test_record_route_validates_and_returns(monkeypatch, settings_dir):
    from flask import Flask
    import routes.testing as t
    app = Flask(__name__)
    app.register_blueprint(t.testing_bp)
    c = app.test_client()
    assert c.post('/api/source/record', json={'seconds': 5}).status_code == 400
    assert c.post('/api/source/record', json={'source_id': 'x', 'seconds': 60}).status_code == 400
    monkeypatch.setattr(recording, 'record', lambda sid, s: {'wav': 'AA==', 'timeline': [], 'seconds': s})
    r = c.post('/api/source/record', json={'source_id': 'rtsp_cam', 'seconds': 2})
    assert r.status_code == 200 and r.json == {'success': True, 'wav': 'AA==', 'timeline': [], 'seconds': 2}

    def busy(sid, s):
        raise recording.RecordingError('Un enregistrement de cette source est déjà en cours.', 409)
    monkeypatch.setattr(recording, 'record', busy)
    assert c.post('/api/source/record', json={'source_id': 'rtsp_cam'}).status_code == 409


def test_detector_taps_receive_only_subscribed_events(fake_classifier):
    """Abonnement par evenement : le calibrage ne recoit pas le son, et le
    son n'est fourni que si quelqu'un l'a demande."""
    from audio_detector import AudioDetector
    det = AudioDetector('model.tflite')
    det.initialize(max_results=5, score_threshold=0.3)
    det.set_groups([{'slug': 'clap', 'name': 'Clap', 'whitelist': {'Clapping': True}, 'threshold': 0.5}])
    det.configure('s', detection_callback=lambda e: None)
    det.start()
    seen = []
    det.add_tap(lambda event, *d: seen.append(event), events={'block', 'peak', 'result'})
    det.process_audio(np.full(BLOCK_SAMPLES, 0.01, np.float32))
    assert 'audio' not in seen and 'block' in seen and not det._wants('audio')
