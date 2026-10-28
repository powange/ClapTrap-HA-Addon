"""Ecriture atomique des fichiers JSON (reglages, sons appris, journal,
blueprints) et mise de cote d'un fichier illisible."""
import json
import os

import pytest

import atomic_io


def test_write_replaces_and_keeps_old_file_on_error(tmp_path):
    p = str(tmp_path / 'f.json')
    atomic_io.write_json(p, {'a': 1})
    assert json.load(open(p)) == {'a': 1}
    with pytest.raises(TypeError):
        atomic_io.write_json(p, {'a': object()})             # non serialisable
    assert json.load(open(p)) == {'a': 1}                     # ancien fichier intact
    assert not os.path.exists(p + '.tmp')                     # temporaire supprime


def test_before_replace_runs_before_the_swap(tmp_path):
    p = str(tmp_path / 'f.json')
    atomic_io.write_json(p, {'v': 1})
    seen = []
    atomic_io.write_json(p, {'v': 2}, before_replace=lambda: seen.append(json.load(open(p))))
    assert seen == [{'v': 1}] and json.load(open(p)) == {'v': 2}


def test_set_aside_keeps_the_unreadable_file(tmp_path, caplog):
    p = tmp_path / 'f.json'
    p.write_text('{oups')
    atomic_io.set_aside(str(p), 'Truc illisible', 'JSON invalide')
    assert not p.exists()
    kept = [n for n in os.listdir(tmp_path) if n.startswith('f.json.corrompu-')]
    assert len(kept) == 1 and 'Truc illisible (JSON invalide) : fichier gardé sous' in caplog.text


def test_copy_file_is_atomic(tmp_path):
    src, dst = tmp_path / 'a.json', tmp_path / 'b.json'
    src.write_text('{"x": 1}')
    atomic_io.copy_file(str(src), str(dst))
    assert dst.read_text() == '{"x": 1}' and not (tmp_path / 'b.json.tmp').exists()


def test_learned_read_error_keeps_the_file(settings_dir, monkeypatch):
    """Erreur de lecture passagere : fichier garde, operation refusee (un
    enregistrement aurait reecrit le fichier sans les autres sons)."""
    import builtins
    import learned
    path = learned._path()
    with open(path, 'w') as f:
        f.write('{"sounds": []}')
    real_open = builtins.open

    def failing(p, *a, **k):
        if p == path:
            raise PermissionError('refusé')
        return real_open(p, *a, **k)
    monkeypatch.setattr(builtins, 'open', failing)
    with pytest.raises(learned.LearnError):
        learned._read()
    assert not learned.matcher()                                 # detection : aucun son, pas d'erreur
    monkeypatch.setattr(builtins, 'open', real_open)
    assert os.path.exists(path)                                  # pas mis de cote


def test_corrupt_blueprint_state_is_set_aside(settings_dir):
    import blueprints
    p = blueprints._state_path()
    with open(p, 'w') as f:
        f.write('{tronqué')
    assert blueprints._read_state() == {}
    assert not os.path.exists(p)
    assert any(n.startswith(os.path.basename(p) + '.corrompu-') for n in os.listdir(os.path.dirname(p)))
