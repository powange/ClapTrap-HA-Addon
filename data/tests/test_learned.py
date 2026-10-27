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

        def match_full(self, full):
            return {}, [('Appris : Bip', 0.95 if dict(full).get('Clapping', 0) > 0.5 else 0.0)]
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
    json.dump({'sounds': [dict(good, negatives=[fake(), {'Music': 'x'}, 3])]}, open(path, 'w'))
    assert learned._read()[0]['negatives'] == [fake()]            # contre-exemple invalide retire, pas le son


def test_session_purpose_is_set_atomically(monkeypatch):
    """Le but est fixe a la creation de la session (aucune fenetre ou une
    autre requete verrait un calibrage « source » a la place)."""
    import inspect
    assert 'purpose' in inspect.signature(calibration.start).parameters
    s = calibration._Session('mic', 'mic', 'mic', 'Micro', True)
    assert s.learn_background == [] and s.learn_examples == []


# ===== Contre-exemples et reapprentissage =========================================

def fake():
    """Fausse detection : meme famille que la sonnette, un peu differente."""
    return {'Music': 0.9, 'Glockenspiel': 0.30, 'Bell': 0.25, 'Silence': 0.05}


def test_negative_example_silences_close_windows_only():
    m = learned.Matcher([_sound()])
    score = lambda m, fp: dict(m.match(cats(fp)))['Appris : Sonnette']  # noqa: E731
    assert score(m, fake()) > 0.1                       # reconnue a tort
    m2 = learned.Matcher([dict(_sound(), negatives=[fake()])])
    assert score(m2, fake()) == 0.0                     # plus proche du contre-exemple
    assert score(m2, bell(7)) > 0.8                     # le vrai son reste reconnu


def _stored(settings_dir, **extra):
    sound = dict(_sound(), id='s1', created='2026-10-01T10:00:00', **extra)
    json.dump({'sounds': [sound]}, open(settings_dir.PERSISTENT_DIR + '/learned_sounds.json', 'w'))
    learned._changed()
    return sound


def test_add_negative_stores_warns_and_caps(settings_dir, monkeypatch):
    import classify
    applied = []
    monkeypatch.setattr(classify, 'apply_learned', applied.append)
    _stored(settings_dir)
    r = learned.add_negative('Appris : Sonnette', fake())
    assert r == {'negatives': 1, 'warning': None}
    assert dict(applied[-1].match(cats(fake())))['Appris : Sonnette'] == 0.0   # detection a jour
    assert learned.list_sounds()[0]['negatives'] == 1
    assert 'risque' in learned.add_negative('Appris : Sonnette', bell(2))['warning']   # le vrai son
    for i in range(learned.MAX_NEGATIVES):
        learned.add_negative('Appris : Sonnette', dict(fake(), Chime=0.01 * (i + 1)))
    assert learned.list_sounds()[0]['negatives'] == learned.MAX_NEGATIVES
    with pytest.raises(learned.LearnError) as e:
        learned.add_negative('Appris : Inconnu', fake())
    assert e.value.status == 404
    with pytest.raises(learned.LearnError):
        learned.add_negative('Clapping', fake())


def test_relearn_keeps_id_name_and_negatives(settings_dir, monkeypatch):
    import classify
    monkeypatch.setattr(classify, 'apply_learned', lambda m: None)
    _stored(settings_dir, negatives=[fake()])
    s = calibration._Session('mic', 'mic', 'mic', 'Micro', True)

    def fake_start(ref, purpose='source', learn_target=None):
        s.purpose, s.learn_target = purpose, learn_target
        calibration._session = s
        return s.summary()
    monkeypatch.setattr(calibration, '_session', None)
    monkeypatch.setattr(calibration, 'start', fake_start)
    monkeypatch.setattr(calibration, '_finish', lambda sess: setattr(calibration, '_session', None))
    with pytest.raises(learned.LearnError) as e:
        learned.start('mic', 'absent')
    assert e.value.status == 404
    assert learned.start('mic', 's1')['relearn'] == 's1' and s.learn_target == 's1'
    s.learn_background = [learned.fingerprint(cats(b)) for b in BACKGROUND]
    s.learn_examples = [learned.fingerprint(cats(other(i))) for i in range(4)]   # nouveau son
    saved = learned.save('Autre nom')                                           # nom ignore
    assert (saved['id'], saved['name'], saved['examples'], saved['negatives']) == ('s1', 'Sonnette', 4, 1)
    stored = learned._read()
    assert len(stored) == 1 and stored[0]['created'] == '2026-10-01T10:00:00' and stored[0]['updated']
    assert dict(learned.matcher().match(cats(other(6))))['Appris : Sonnette'] > 0.8


def test_relearn_of_a_sound_deleted_meanwhile_fails(settings_dir, monkeypatch):
    import classify
    monkeypatch.setattr(classify, 'apply_learned', lambda m: None)
    s = calibration._Session('mic', 'mic', 'mic', 'Micro', True)
    s.purpose, s.learn_target = 'learn', 'gone'
    s.learn_background = [learned.fingerprint(cats(b)) for b in BACKGROUND]
    s.learn_examples = [learned.fingerprint(cats(bell(i))) for i in range(3)]
    monkeypatch.setattr(calibration, '_session', s)
    with pytest.raises(learned.LearnError, match='supprimé') as e:
        learned.save()
    assert e.value.status == 404 and learned._read() == []


def test_detector_keeps_recent_learned_evidence():
    import time
    from audio_detector import AudioDetector, LEARNED_LOOKBACK
    det = AudioDetector('model.tflite')
    now = time.monotonic()
    det._learned_seen.extend([(now - LEARNED_LOOKBACK - 1, {'old': 1.0}, {'Appris : A': 0.99}),
                              (now - 1, {'a': 1.0}, {'Appris : A': 0.6, 'Appris : B': 0.05}),
                              (now, {'b': 1.0}, {'Appris : A': 0.8})])
    assert det.learned_evidence('Appris : A') == {'b': 1.0}       # meilleur score recent
    assert det.learned_evidence('Appris : B') is None              # trop faible
    assert det.learned_evidence('Appris : C') is None
