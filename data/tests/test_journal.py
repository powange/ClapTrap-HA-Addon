"""Journal conserve : detections, « presque » et corrections rechargees
apres un redemarrage."""
import json
import os

import numpy as np

import pytest

import classify
import journal_store


@pytest.fixture(autouse=True)
def real_read(monkeypatch):
    monkeypatch.setattr(journal_store, 'read', journal_store.real_read)


def _clap(det_id, score=0.8, evidence=None):
    classify._remember({'source_id': 'mic', 'group_slug': 'clap', 'group_name': 'Clap', 'ignored': False,
                        'labels': [{'label': 'Clapping', 'score': score}], 'timestamp': 1e9,
                        '_feedback': {'id': det_id, 'kind': 'mic', 'source_key': '0', 'group_slug': 'clap',
                                      'evidence': evidence or {}}}, score)


def test_journal_survives_a_restart(settings_dir):
    classify.clear_detection_history()
    _clap('a1')
    _clap('a2', evidence={'Appris : Bip': {'Music': np.float32(0.9)}})   # valeurs numpy : ecrites en nombres
    with classify._history_lock:
        classify._near_misses.appendleft({'kind': 'count', 'group': 'Clap', 'source_id': 'mic', 'timestamp': 1e9})
    classify.mark_feedback('a1', 'threshold')
    journal_store.save_now(classify._journal_snapshot)
    saved = json.load(open(journal_store.path()))
    assert saved['version'] == 1 and [e['id'] for e in saved['history']] == ['a2', 'a1']
    # Redemarrage : memoire vide, journal relu (le plus recent d'abord)
    with classify._history_lock:
        classify._detection_history.clear()
        classify._near_misses.clear()
        classify._feedback_info.clear()
    assert classify.load_journal() == 3
    hist = classify.get_detection_history()
    assert [e['id'] for e in hist] == ['a2', 'a1'] and hist[1]['feedback'] == [{'action': 'threshold', 'label': None}]
    assert classify.get_near_misses()[0]['kind'] == 'count'
    entry, info = classify.feedback_target('a2')   # « Ce n'etait pas ça » encore possible
    assert info['evidence'] == {'Appris : Bip': {'Music': 0.8999999761581421}}


def test_unreadable_journal_is_set_aside(settings_dir):
    with open(journal_store.path(), 'w') as f:
        f.write('{pas du json')
    classify.clear_detection_history()
    assert classify.load_journal() == 0 and classify.get_detection_history() == []
    assert not os.path.exists(journal_store.path())
    assert any(n.startswith('journal.json.corrompu-') for n in os.listdir(os.path.dirname(journal_store.path())))
    assert classify.load_journal() == 0   # aucun fichier : rien a faire
    with open(journal_store.path(), 'w') as f:   # version plus recente : gardee telle quelle
        f.write('{"version": 9, "history": [], "near": [], "feedback": {}}')
    assert classify.load_journal() == 0 and os.path.exists(journal_store.path())


def test_writes_are_grouped_and_flushed_at_shutdown(settings_dir, monkeypatch):
    real = journal_store
    monkeypatch.setattr(real, 'schedule', real.real_schedule)   # ecriture differee reelle
    monkeypatch.setattr(real, 'SAVE_DELAY', 30)
    writes = []
    monkeypatch.setattr(real, 'write', lambda snap: writes.append(snap))
    classify.clear_detection_history()   # programme une ecriture
    _clap('b1')
    _clap('b2')
    assert writes == [] and real._timer is not None   # une seule ecriture en attente
    classify.flush_journal()
    assert len(writes) == 1 and [e['id'] for e in writes[0]['history']] == ['b2', 'b1'] and real._timer is None
    classify.flush_journal()
    assert len(writes) == 2   # arret : toujours ecrit (attend une ecriture en cours)
