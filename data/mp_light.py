"""MediaPipe sans OpenCV ni matplotlib.

`import mediapipe` charge ses outils de dessin video (vision/drawing_utils.py),
qui importent cv2 et matplotlib.pyplot. ClapTrap n'utilise que la
classification audio : des modules vides suffisent, et l'image perd environ
250 Mo (OpenCV, matplotlib, fontTools, Pillow...) ; l'import de MediaPipe
passe de ~0,6 s a ~0,2 s et de ~83 a ~30 Mo de memoire.

Si ces paquets sont installes (developpement), les vrais sont utilises.
L'autotest du build (selftest_yamnet.py) verifie que la classification
fonctionne bien avec ces modules vides.
"""
import importlib.machinery
import importlib.util
import sys
import types


def install_stubs():
    """Modules vides pour cv2 et matplotlib(.pyplot) s'ils sont absents.
    A appeler avant le premier import de mediapipe."""
    for name in ('cv2', 'matplotlib'):
        if name in sys.modules or importlib.util.find_spec(name) is not None:
            continue
        sys.modules[name] = _stub(name)
        if name == 'matplotlib':
            pyplot = _stub('matplotlib.pyplot')
            sys.modules['matplotlib.pyplot'] = pyplot
            sys.modules['matplotlib'].pyplot = pyplot


def _stub(name):
    # __spec__ renseigne : importlib.util.find_spec(name) ne leve pas
    # « __spec__ is None » sur un module vide.
    module = types.ModuleType(name)
    module.__spec__ = importlib.machinery.ModuleSpec(name, None)
    return module
