"""vban_manager : un seul detecteur VBAN, echec de bind signale, arret."""
import logging

import pytest

import vban_manager


class FakeDetector:
    busy = False
    created = 0

    def __init__(self):
        FakeDetector.created += 1
        self.listening = False

    def start_listening(self):
        if FakeDetector.busy:
            raise OSError('Address already in use')
        self.listening = True

    def stop_listening(self):
        if self.listening == 'boom':
            raise RuntimeError('socket')
        self.listening = False


@pytest.fixture(autouse=True)
def fake(monkeypatch):
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
