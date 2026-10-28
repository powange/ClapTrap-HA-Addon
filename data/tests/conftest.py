"""Socle des tests : faux MediaPipe, reglages dans un dossier temporaire,
faux client MQTT. Aucun materiel, aucun reseau, aucun Home Assistant."""
import os
import sys
import threading
import types

import numpy as np
import pytest

DATA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, DATA_DIR)


# --- Faux MediaPipe ------------------------------------------------------------
# Classifieur (mode clips) : repond « Clapping » si la fenetre recue (0,975 s)
# contient un son fort, et garde les fenetres recues pour les tests (gain
# applique, ecretage...).
class FakeClassifier:
    instances = []
    init_delay = 0.0  # secondes (simule une initialisation lente sur Pi)

    def __init__(self):
        self.blocks = []      # fenetres analysees
        self.closed = False
        FakeClassifier.instances.append(self)

    def classify(self, window):
        assert not self.closed, 'classify apres close()'
        window = np.asarray(window)
        self.blocks.append(window.copy())
        score = 0.9 if np.abs(window).max() > 0.2 else 0.02
        cat = lambda n, s: types.SimpleNamespace(category_name=n, score=s)  # noqa: E731
        return [types.SimpleNamespace(classifications=[types.SimpleNamespace(
            categories=[cat('Clapping', score), cat('Speech', 0.6)])])]

    def close(self):
        self.closed = True


def _create_classifier(options):
    import time
    if FakeClassifier.init_delay:
        time.sleep(FakeClassifier.init_delay)
    return FakeClassifier()


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
    audio.RunningMode = types.SimpleNamespace(AUDIO_CLIPS=2)
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


def _no_vban_socket():
    """Pas de vrai socket UDP 6980 pendant les tests : importer app.py lancait
    l'ecoute VBAN (sources du reseau local detectees, fil qui survit aux tests,
    echec silencieux si le port est pris). Le detecteur existe, sans ecouter ;
    un test du vrai bind restaure `real_start_listening` (monkeypatch)."""
    import vban_listener

    def start_listening(self):
        self.running = False

    cls = vban_listener.VBANDetector
    cls.real_start_listening = cls.start_listening
    cls.start_listening = start_listening


_no_vban_socket()


@pytest.fixture
def fake_classifier():
    FakeClassifier.instances = []
    FakeClassifier.init_delay = 0.0
    yield FakeClassifier
    FakeClassifier.init_delay = 0.0


# --- Reglages dans un dossier temporaire ---------------------------------------------
@pytest.fixture(autouse=True)
def no_journal_writes(monkeypatch):
    """Journal conserve : aucune ecriture differee pendant les tests (sans
    settings_dir, elle irait dans le dossier du code). test_journal ecrit
    explicitement (journal_store.real_schedule et real_read : les vraies).
    Pas de lecture non plus : importer app relirait le journal du dossier
    du code."""
    import journal_store
    if not hasattr(journal_store, 'real_schedule'):
        journal_store.real_schedule, journal_store.real_read = journal_store.schedule, journal_store.read
    monkeypatch.setattr(journal_store, 'schedule', lambda snapshot_fn: None)
    monkeypatch.setattr(journal_store, 'read', lambda: None)


@pytest.fixture
def settings_dir(tmp_path, monkeypatch):
    import settings_manager as sm
    monkeypatch.setattr(sm, 'PERSISTENT_DIR', str(tmp_path))
    monkeypatch.setattr(sm, 'SETTINGS_FILE', str(tmp_path / 'settings.json'))
    monkeypatch.setattr(sm, 'SETTINGS_BACKUP', str(tmp_path / 'settings.json.backup'))
    monkeypatch.setattr(sm, 'SETTINGS_TEMP', str(tmp_path / 'settings.json.tmp'))
    monkeypatch.setattr(sm, '_cache', None)
    monkeypatch.setattr(sm, '_cache_time', 0)
    monkeypatch.setattr(sm, '_degraded', False)   # mode degrade d'un test : pas transmis aux suivants
    # Pas de republication HA en arriere-plan (enregistree par l'import d'app.py).
    monkeypatch.setattr(sm, '_change_listeners', [])
    yield sm


# --- Faux client MQTT ----------------------------------------------------------------
class FakeMqtt:
    def __init__(self):
        self.published = []  # (topic, payload, retain)
        self.qos = {}        # topic -> [qos de chaque publication]
        self.devices = {}    # topic d'appareil -> dernier message (dict)
        self._cmps = {}      # topic d'appareil -> {composant: plateforme}

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))
        self.qos.setdefault(topic, []).append(qos)
        if topic.startswith('homeassistant/device/'):
            self._expand_device(topic, payload)
        return types.SimpleNamespace(wait_for_publish=lambda *a: None)

    def _expand_device(self, topic, payload):
        """Message d'appareil traduit en configurations par entite, comme HA
        l'interprete (topics virtuels homeassistant/<plateforme>/claptrap/<id>/config) :
        les assertions portent sur les entites obtenues."""
        import json as _json
        known = self._cmps.setdefault(topic, {})
        if not payload:
            self.devices.pop(topic, None)
            for cid, platform in known.items():
                self.published.append((f'homeassistant/{platform}/claptrap/{cid}/config', '', True))
            known.clear()
            return
        msg = _json.loads(payload)
        self.devices[topic] = msg
        for cid, c in msg['cmps'].items():
            platform = c['p']
            virtual = f'homeassistant/{platform}/claptrap/{cid}/config'
            if len(c) == 1:   # {"p": …} : retrait
                self.published.append((virtual, '', True))
                known.pop(cid, None)
                continue
            import ha_entities
            cfg = {k: v for k, v in ha_entities.expand_component(c).items() if k != 'p'}
            cfg.update(device=msg['dev'], origin=msg['o'])
            self.published.append((virtual, _json.dumps(cfg), True))
            known[cid] = platform

    def subscribe(self, topic):
        pass

    # Entites clap (binary_sensor) ; les entites de reglage (ha_control) ont
    # leurs propres tests.
    # Capteurs binaires de clap (le capteur « Flux connecté » d'une source,
    # *_flux, a ses propres tests).
    def configs(self, component='binary_sensor'):
        return [t for t, p, r in self.published if t.endswith('/config') and p and f'/{component}/' in t
                and not t.endswith('_flux/config')]

    def deleted(self, component='binary_sensor'):
        return [t for t, p, r in self.published if t.endswith('/config') and p == '' and f'/{component}/' in t
                and not t.endswith('_flux/config')]


@pytest.fixture
def mqtt(monkeypatch):
    import ha_entities as ha
    client = FakeMqtt()
    monkeypatch.setattr(ha, '_mqtt_client', client)
    monkeypatch.setattr(ha, '_source_info', {})
    monkeypatch.setattr(ha, '_listening', set())
    monkeypatch.setattr(ha, '_availability_seen', set())
    monkeypatch.setattr(ha, '_pending_devices', set())
    monkeypatch.setattr(ha, '_pending_topics', set())
    monkeypatch.setattr(ha, '_legacy', {})
    monkeypatch.setattr(ha, '_seen_devices', {})
    monkeypatch.setattr(ha, '_device_cmps', {})
    ready = threading.Event()
    ready.set()   # migration supposee faite : les configurations sont publiees
    monkeypatch.setattr(ha, '_discovery_ready', ready)
    import ha_control
    monkeypatch.setattr(ha_control, '_published', {})
    monkeypatch.setattr(ha_control, '_analysis_last', {})
    monkeypatch.setattr(ha, '_expected_keys', None)
    monkeypatch.setattr(ha, '_warned_collisions', set())
    ha._mqtt_connected.set()
    yield client
    ha._mqtt_connected.clear()
