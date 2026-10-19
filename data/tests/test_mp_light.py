"""mp_light : modules vides pour cv2 et matplotlib, seulement s'ils manquent."""
import importlib.util
import sys
import types

import pytest

import mp_light

NAMES = ('cv2', 'matplotlib', 'matplotlib.pyplot')


@pytest.fixture
def clean_modules():
    """sys.modules restaure apres le test (les modules vides ne fuient pas
    vers les tests suivants)."""
    saved = {n: sys.modules[n] for n in NAMES if n in sys.modules}
    for n in NAMES:
        sys.modules.pop(n, None)
    yield
    for n in NAMES:
        sys.modules.pop(n, None)
    sys.modules.update(saved)


def test_stubs_installed_only_when_missing(monkeypatch, clean_modules):
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, 'find_spec',
                        lambda name, *a: None if name in ('cv2', 'matplotlib') else real(name, *a))
    mp_light.install_stubs()
    import cv2
    import matplotlib.pyplot as plt
    assert isinstance(cv2, types.ModuleType) and plt is sys.modules['matplotlib'].pyplot
    assert cv2.__spec__ is not None   # find_spec ne leve pas sur le module vide


def test_real_modules_kept(clean_modules):
    real_mod = types.ModuleType('cv2')
    sys.modules['cv2'] = real_mod
    mp_light.install_stubs()
    assert sys.modules['cv2'] is real_mod
