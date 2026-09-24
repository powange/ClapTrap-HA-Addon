"""Socle des tests : faux MediaPipe, reglages dans un dossier temporaire,
faux client MQTT. Aucun materiel, aucun reseau, aucun Home Assistant."""
import os
import sys
import types

import numpy as np
import pytest

DATA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, DATA_DIR)


# --- Faux MediaPipe ------------------------------------------------------------
# Classifieur : repond « Clapping » tant que le bloc recu est fort, et garde les
# blocs recus pour les tests (gain applique, ecretage...).
class FakeClassifier:
    instances = []
    init_delay = 0.0  # secondes (simule une initialisation lente sur Pi)

    def __init__(self, callback):
        self.callback = callback
        self.blocks = []
        self.closed = False
        self.hot = 0
        FakeClassifier.instances.append(self)

    def classify_async(self, block, timestamp_ms):
        assert not self.closed, 'classify_async apres close()'
        block = np.asarray(block)
        self.blocks.append(block.copy())
        if np.abs(block).max() > 0.2:
            self.hot = 6
        score = 0.9 if self.hot > 0 else 0.02
        self.hot = max(0, self.hot - 1)
        cat = lambda n, s: types.SimpleNamespace(category_name=n, score=s)  # noqa: E731
        result = types.SimpleNamespace(classifications=[types.SimpleNamespace(
            categories=[cat('Clapping', score), cat('Speech', 0.6)])])
        self.callback(result, timestamp_ms)

    def close(self):
        self.closed = True


def _create_classifier(options):
    import time
    if FakeClassifier.init_delay:
        time.sleep(FakeClassifier.init_delay)
    return FakeClassifier(options['result_callback'])


def _install_mediapipe_stub():
    mp = types.ModuleType('mediapipe')
    tasks = types.ModuleType('mediapipe.tasks')
    py = types.ModuleType('mediapipe.tasks.python')
    audio = types.ModuleType('mediapipe.tasks.python.audio')
    comp = types.ModuleType('mediapipe.tasks.python.components')
    cont = types.ModuleType('mediapipe.tasks.python.components.containers')
    cont.AudioData = types.SimpleNamespace(create_from_array=lambda a, sr: a)
    py.BaseOptions = lambda **k: None
    audio.AudioClassifierOptions = lambda **k: k
    audio.RunningMode = types.SimpleNamespace(AUDIO_STREAM=1)
    audio.AudioClassifier = types.SimpleNamespace(create_from_options=_create_classifier)
    py.audio = audio
    comp.containers = cont
    py.components = comp
    tasks.python = py
    mp.tasks = tasks
    for name, mod in {'mediapipe': mp, 'mediapipe.tasks': tasks, 'mediapipe.tasks.python': py,
                      'mediapipe.tasks.python.audio': audio,
                      'mediapipe.tasks.python.components': comp,
                      'mediapipe.tasks.python.components.containers': cont}.items():
        sys.modules.setdefault(name, mod)


_install_mediapipe_stub()


@pytest.fixture
def fake_classifier():
    FakeClassifier.instances = []
    FakeClassifier.init_delay = 0.0
    yield FakeClassifier
    FakeClassifier.init_delay = 0.0


# --- Reglages dans un dossier temporaire ---------------------------------------------
@pytest.fixture
def settings_dir(tmp_path, monkeypatch):
    import settings_manager as sm
    monkeypatch.setattr(sm, 'PERSISTENT_DIR', str(tmp_path))
    monkeypatch.setattr(sm, 'SETTINGS_FILE', str(tmp_path / 'settings.json'))
    monkeypatch.setattr(sm, 'SETTINGS_BACKUP', str(tmp_path / 'settings.json.backup'))
    monkeypatch.setattr(sm, 'SETTINGS_TEMP', str(tmp_path / 'settings.json.tmp'))
    monkeypatch.setattr(sm, '_cache', None)
    monkeypatch.setattr(sm, '_cache_time', 0)
    yield sm


# --- Faux client MQTT ----------------------------------------------------------------
class FakeMqtt:
    def __init__(self):
        self.published = []  # (topic, payload, retain)

    def publish(self, topic, payload, retain=False):
        self.published.append((topic, payload, retain))
        return types.SimpleNamespace(wait_for_publish=lambda *a: None)

    def subscribe(self, topic):
        pass

    def configs(self):
        return [t for t, p, r in self.published if t.endswith('/config') and p]

    def deleted(self):
        return [t for t, p, r in self.published if t.endswith('/config') and p == '']


@pytest.fixture
def mqtt(monkeypatch):
    import ha_entities as ha
    client = FakeMqtt()
    monkeypatch.setattr(ha, '_mqtt_client', client)
    monkeypatch.setattr(ha, '_source_info', {})
    monkeypatch.setattr(ha, '_listening', set())
    monkeypatch.setattr(ha, '_discovered', set())
    monkeypatch.setattr(ha, '_availability_seen', set())
    monkeypatch.setattr(ha, '_pending_removal', set())
    monkeypatch.setattr(ha, '_expected_keys', None)
    monkeypatch.setattr(ha, '_warned_collisions', set())
    ha._mqtt_connected.set()
    yield client
    ha._mqtt_connected.clear()
