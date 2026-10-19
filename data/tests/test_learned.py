"""Sons appris : empreintes (521 scores de YAMNet), apprentissage, detection."""
import json

import pytest

import calibration
import learned

BACKGROUND = [{'Silence': 0.9, 'Inside, small room': 0.1 + i * 0.01} for i in range(6)]


def bell(i=0):
    return {'Music': 0.9, 'Glockenspiel': 0.30 + i * 0.02, 'Chime': 0.20, 'Silence': 0.05}


def other(i=0):
    return {'Music': 0.9, 'Piano': 0.30 + i * 0.02, 'Silence': 0.05}


def cats(fp):
    return list(fp.items()) + [('Speech', 0.0001)]   # classes sous KEEP : ignorees


def _sound(name='Sonnette', n=5):
    center, ex, bg = learned._prepared([bell(i) for i in range(n)], BACKGROUND)
    floor, ceiling = learned.calibrate(ex, bg)
    return {'name': name, 'examples': [bell(i) for i in range(n)], 'center': center,
            'floor': floor, 'ceiling': ceiling}


def test_matcher_scores_learned_sound_over_similar_and_background():
    """Meme classe dominante (« Music ») : la sonnette est reconnue, une autre
    tonalite beaucoup moins, le bruit de fond pas du tout."""
    m = learned.Matcher([_sound()])
    assert m.labels() == ['Appris : Sonnette']
    score = lambda fp: dict(m.match(cats(fp)))['Appris : Sonnette']  # noqa: E731
    assert score(bell(7)) > 0.8
    assert score(other()) < 0.4
    assert score(BACKGROUND[0]) == 0.0
    assert m.match([]) == [] and not learned.Matcher([])


def test_learning_session_records_saves_and_reloads(settings_dir, monkeypatch):
    s = calibration._Session('mic', 'mic', 'mic', 'Micro', True)
    monkeypatch.setattr(calibration, '_session', s)
    applied = []
    import classify
    monkeypatch.setattr(classify, 'apply_learned', applied.append)
    monkeypatch.setattr(calibration, '_finish', lambda sess: setattr(calibration, '_session', None))
    s.purpose = 'learn'
    s.learn_background, s.learn_examples = [], []
    takes = iter([[cats(b) for b in BACKGROUND]] +
                 # chaque exemple : une fenetre de bruit de fond et une du son
                 [[cats(BACKGROUND[0]), cats(bell(i))] for i in range(4)])
    monkeypatch.setattr(calibration, 'capture_take',
                        lambda sess, d, g, scores=False: {'scores': [(c, 0.0) for c in next(takes)]})
    with pytest.raises(learned.LearnError, match="bruit de fond"):
        learned.record('exemple')
    assert learned.record('fond')['background']
    r = learned.record('exemple')
    assert r['examples'] == 1 and r['top'][0]['classe'] == 'Music'
    assert s.learn_examples[0] == learned.fingerprint(cats(bell(0)))   # la fenetre du son, pas le fond
    with pytest.raises(learned.LearnError, match='au moins 3'):
        learned.preview()
    for _ in range(3):
        learned.record('exemple')
    q = learned.preview()
    assert q['recognized'] == 4 and not q['warnings']
    saved = learned.save('  Sonnette  ')
    assert saved['name'] == 'Sonnette' and saved['label'] == 'Appris : Sonnette' and saved['examples'] == 4
    assert calibration._session is None                      # session terminee
    assert applied and applied[-1].labels() == ['Appris : Sonnette']
    assert learned.labels() == ['Appris : Sonnette']
    data = json.load(open(settings_dir.PERSISTENT_DIR + '/learned_sounds.json'))
    assert data['sounds'][0]['center'] and data['sounds'][0]['floor'] < data['sounds'][0]['ceiling']
    assert [x['name'] for x in learned.list_sounds()] == ['Sonnette']
    learned.delete(saved['id'])
    assert learned.list_sounds() == [] and learned.labels() == [] and applied[-1].labels() == []
    with pytest.raises(learned.LearnError):
        learned.delete(saved['id'])


def test_name_checks(settings_dir, monkeypatch):
    monkeypatch.setattr(learned, '_read', lambda: [{'id': 'a', 'name': 'Sonnette', 'examples': [{}]}])
    with pytest.raises(learned.LearnError, match='déjà'):
        learned._check_name('sonnette', learned._read())
    with pytest.raises(learned.LearnError, match='1 à 40'):
        learned._check_name('', [])
    assert learned._check_name('Bip  du   lave-linge', []) == 'Bip du lave-linge'


def test_other_calibration_session_is_refused(monkeypatch):
    s = calibration._Session('mic', 'mic', 'mic', 'Micro', True)
    s.purpose = 'count'
    monkeypatch.setattr(calibration, '_session', s)
    with pytest.raises(learned.LearnError, match='un autre calibrage'):
        learned.record('fond')


def test_detector_adds_learned_classes_that_groups_can_use(fake_classifier):
    """Classe apprise ajoutee aux resultats : un groupe qui la coche se
    declenche (ici, le faux YAMNet repond « Clapping » sur un son fort)."""
    import numpy as np
    import time
    from audio_detector import AudioDetector
    from audio_utils import BLOCK_SAMPLES

    class Stub:
        def __bool__(self):
            return True

        def match(self, full):
            return [('Appris : Bip', 0.95 if dict(full).get('Clapping', 0) > 0.5 else 0.0)]
    det = AudioDetector('model.tflite')
    det.initialize(max_results=-1, score_threshold=0.3)
    det.set_groups([{'slug': 'bip', 'name': 'Bip', 'whitelist': {'Appris : Bip': True}, 'threshold': 0.5,
                     'clap_counts': [1]}])
    events, scores = [], []
    det.configure('s', detection_callback=events.append)
    det.add_tap(lambda ev, full, now: scores.append(full), events={'scores'})
    det.learned = Stub()
    det.start()
    for i in range(40):
        b = np.random.default_rng(i).normal(0, 0.002, BLOCK_SAMPLES).astype(np.float32)
        if i == 15:
            b[300] = 0.6
        det.process_audio(b)
        time.sleep(0.02)
    det.wait_idle()
    for _ in range(25):
        det.process_audio(np.zeros(BLOCK_SAMPLES, np.float32))
        time.sleep(0.02)
    det.wait_idle()
    det.stop()
    assert [e['group_name'] for e in events if not e.get('ignored')][:1] == ['Bip']
    assert scores and all(n != 'Appris : Bip' for n, _ in scores[0])   # empreinte : classes de YAMNet seules


def test_routes_and_known_labels(settings_dir, monkeypatch):
    from flask import Flask
    from routes.calibration import calibration_bp
    import routes.sources as rs
    app = Flask(__name__)
    app.register_blueprint(calibration_bp)
    app.register_blueprint(rs.sources_bp)
    c = app.test_client()
    r = c.get('/api/learned')
    assert r.json['sounds'] == [] and r.json['limits']['min_examples'] == 3
    assert c.delete('/api/learned/nope').status_code == 404
    assert c.post('/api/learned/record', json={'kind': 'fond'}).status_code == 409   # aucune session
    monkeypatch.setattr(learned, 'labels', lambda: ['Appris : Sonnette'])
    monkeypatch.setattr(rs, 'yamnet_labels', lambda: ['Clapping', 'Music'])
    assert 'Appris : Sonnette' in c.get('/api/sounds/known').json['labels']
    assert rs.canonical_label('Appris : Sonnette') == 'Appris : Sonnette'


def test_unreadable_file_is_kept_aside_and_bad_entries_ignored(settings_dir, caplog):
    import os
    path = settings_dir.PERSISTENT_DIR + '/learned_sounds.json'
    open(path, 'w').write('{"sounds": [')                         # coupure pendant une ecriture
    assert learned._read() == []
    assert not os.path.exists(path) and any('.corrompu-' in f for f in os.listdir(settings_dir.PERSISTENT_DIR))
    good = dict(_sound(), id='a')
    json.dump({'sounds': [good, {'id': 'b', 'name': 'Sans plancher', 'examples': [{'Music': 0.9}]}, 'x']},
              open(path, 'w'))
    assert [s['id'] for s in learned._read()] == ['a']
    assert learned.labels() == ['Appris : Sonnette']             # aucune erreur pour l'entree invalide


def test_session_purpose_is_set_atomically(monkeypatch):
    """Le but est fixe a la creation de la session (aucune fenetre ou une
    autre requete verrait un calibrage « source » a la place)."""
    import inspect
    assert 'purpose' in inspect.signature(calibration.start).parameters
    s = calibration._Session('mic', 'mic', 'mic', 'Micro', True)
    assert s.learn_background == [] and s.learn_examples == []
