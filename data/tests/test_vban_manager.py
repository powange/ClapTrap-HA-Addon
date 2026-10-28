"""vban_manager : un seul detecteur VBAN, echec de bind signale, arret."""
import logging

import pytest

import vban_manager


class FakeDetector:
    busy = False
    created = 0

    def __init__(self, port=6980):
        FakeDetector.created += 1
        self.port = port
        self.listening = False

    def start_listening(self):
        if FakeDetector.busy:
            raise OSError('Address already in use')
        self.listening = True

    def stop_listening(self):
        if self.listening == 'boom':
            raise RuntimeError('socket')
        self.listening = False


@pytest.fixture
def options(monkeypatch):
    """Options de l'add-on (/data/options.json)."""
    import ha_entities
    opts = {}
    monkeypatch.setattr(ha_entities, '_addon_options', lambda: opts)
    return opts


@pytest.fixture(autouse=True)
def fake(monkeypatch, options):
    FakeDetector.busy, FakeDetector.created = False, 0
    monkeypatch.setattr(vban_manager, 'VBANDetector', FakeDetector)
    monkeypatch.setattr(vban_manager, 'vban_detector', None)
    return FakeDetector


def test_single_detector_created_and_reused():
    assert vban_manager.init_vban_detector() is True
    det = vban_manager.get_vban_detector()
    assert det.listening and vban_manager.init_vban_detector() is True
    assert vban_manager.get_vban_detector() is det and FakeDetector.created == 1


def test_busy_port_reported_and_retried_lazily(caplog):
    FakeDetector.busy = True
    with caplog.at_level(logging.WARNING):
        assert vban_manager.init_vban_detector() is False
    assert 'port 6980' in caplog.text and vban_manager.get_vban_detector() is None
    FakeDetector.busy = False                       # port libere : get relance l'ecoute
    assert vban_manager.get_vban_detector().listening


def test_cleanup_stops_and_forgets_detector():
    det = vban_manager.get_vban_detector()
    vban_manager.cleanup_vban_detector()
    assert not det.listening and vban_manager.vban_detector is None
    vban_manager.cleanup_vban_detector()            # idempotent


def test_cleanup_tolerates_stop_error():
    det = vban_manager.get_vban_detector()
    det.listening = 'boom'
    vban_manager.cleanup_vban_detector()
    assert vban_manager.vban_detector is None


def test_listen_port_from_addon_option(options, caplog):
    """Option vban_port : 6981 quand Wyoming VBAN occupe deja le 6980."""
    assert vban_manager.listen_port() == 6980                  # absente : port standard
    options['vban_port'] = 6981
    assert vban_manager.listen_port() == 6981
    assert vban_manager.init_vban_detector() is True
    assert vban_manager.get_vban_detector().port == 6981
    FakeDetector.busy = True
    vban_manager.cleanup_vban_detector()
    with caplog.at_level(logging.WARNING):
        assert vban_manager.init_vban_detector() is False
    assert 'port 6981' in caplog.text                           # message avec le port reel
    FakeDetector.busy = False
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        for bad in (0, 70000, 'abc', 6981.5, True, 'abc'):
            options['vban_port'] = bad
            assert vban_manager.configured_port() == 6980
    assert caplog.text.count("'abc'") == 1                     # signale une fois, pas a chaque appel
    options['vban_port'] = '6982'                               # texte numerique accepte
    assert vban_manager.configured_port() == 6982


def test_listen_port_is_the_bound_one(options):
    """Options changees sans redemarrage : on affiche et enregistre le port
    du socket ouvert, pas celui des options."""
    options['vban_port'] = 6981
    vban_manager.init_vban_detector()
    options['vban_port'] = 7000
    assert vban_manager.listen_port() == 6981 and vban_manager.configured_port() == 7000
    vban_manager.cleanup_vban_detector()
    assert vban_manager.listen_port() == 7000
