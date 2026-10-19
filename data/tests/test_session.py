"""Session de detection avec de fausses sources (MediaPipe simule)."""
import time
import types

import numpy as np
import pytest

from audio_utils import BLOCK_SAMPLES
from helpers import wait_until


def det_of(source_id):
    import classify
    s = classify._current()
    return s.detectors.get(source_id) if s else None


class FakeReader:
    name = 'fake'
    last_error = ''

    def __init__(self, clap_blocks=(), n=80):
        self.clap_blocks, self.n, self.runs, self.closed = set(clap_blocks), n, 0, False

    def iter_blocks(self, stop):
        self.runs += 1
        for i in range(self.n):
            if stop.is_set() or self.closed:
                return
            b = np.random.default_rng(i).normal(0, 0.002, BLOCK_SAMPLES).astype(np.float32)
            if self.runs == 1 and i in self.clap_blocks:
                b[10] = 0.5
            time.sleep(0.01)
            yield b

    def close(self):
        self.closed = True


@pytest.fixture
def session_env(settings_dir, fake_classifier, monkeypatch):
    import classify
    import ha_entities
    calls = {'claps': [], 'listening': []}
    monkeypatch.setattr(ha_entities, 'on_clap_detected', lambda *a, **k: calls['claps'].append((a, k)))
    monkeypatch.setattr(ha_entities, 'update_detection_state', lambda *a, **k: None)
    monkeypatch.setattr(ha_entities, 'set_source_listening', lambda key, on: calls['listening'].append((key, on)))
    monkeypatch.setattr(classify, '_http', types.SimpleNamespace(post=lambda *a, **k: None))
    monkeypatch.setattr(classify, 'SEEN_FLUSH_DELAY', 0.2)
    emitted = []
    sock = types.SimpleNamespace(emit=lambda ev, data=None: emitted.append((ev, data)))
    settings = {'global': {'threshold': 0.5, 'delay': 1.5}, 'microphone': {'enabled': False},
                'rtsp_sources': [{'id': 'cam1', 'name': 'Cam', 'url': 'rtsp://u:p@h/x', 'enabled': True, 'gain': 1,
                                  'sound_groups': [{'slug': 'clap', 'name': 'Clap', 'threshold': 0.5,
                                                    'ha_entities': [1, 2], 'sound_whitelist': {'Clapping': True},
                                                    'auto_add_sounds': True}]}],
                'saved_vban_sources': []}
    settings_dir.save_settings(settings)
    yield classify, sock, emitted, calls, settings_dir
    classify.stop_detection(timeout=5)


def test_session_counts_emits_and_persists(session_env, monkeypatch):
    classify, sock, emitted, calls, sm = session_env
    reader = FakeReader(clap_blocks=(30, 42))   # deux claps a 1,2 s : un geste (fenetre de 1,5 s)
    monkeypatch.setattr(classify, 'rtsp_source', lambda url, **kw: reader)
    ok, _ = classify.start_from_settings(sock)
    assert ok and classify.get_status()['running']
    wait_until(lambda: [e for e, d in emitted if e == 'clap'])
    claps = [d for e, d in emitted if e == 'clap']
    assert [(c['source_id'], c['clap_count'], c['entity_key'], c['source_name']) for c in claps] == \
        [('rtsp_cam1', 2, 'rtsp_cam1', 'Cam')]
    wait_until(lambda: calls['claps'])   # entites HA mises a jour en arriere-plan
    assert calls['claps'][0][0][0] == 'rtsp_cam1'
    statuses = [d['status'] for e, d in emitted if e == 'source_status']
    assert statuses[:2] == ['connecting', 'connected']
    assert ('rtsp_cam1', True) in calls['listening']
    assert not any('u:p' in str(d) for e, d in emitted)   # jamais l'URL
    classify.stop_detection()
    assert not classify.get_status()['running'] and reader.closed
    assert ('rtsp_cam1', False) in calls['listening']
    # son entendu enregistre (par lot)
    wait_until(lambda: sm.load_settings()['rtsp_sources'][0]['sound_groups'][0]['sound_whitelist']
               == {'Clapping': True, 'Speech': False})


def test_stop_during_slow_initialisation_closes_detector(session_env, monkeypatch, fake_classifier):
    classify, sock, emitted, calls, sm = session_env
    fake_classifier.init_delay = 1.0
    monkeypatch.setattr(classify, 'rtsp_source', lambda url, **kw: FakeReader())
    classify.start_from_settings(sock)
    time.sleep(0.2)
    classify.stop_detection(timeout=0.5)   # l'arret n'attend pas la fin de l'init
    wait_until(lambda: fake_classifier.instances and all(c.closed for c in fake_classifier.instances))


def test_settings_changed_during_initialisation_are_applied(session_env, monkeypatch, fake_classifier):
    classify, sock, emitted, calls, sm = session_env
    fake_classifier.init_delay = 0.6
    monkeypatch.setattr(classify, 'rtsp_source', lambda url, **kw: FakeReader(n=200))
    classify.start_from_settings(sock)
    time.sleep(0.1)

    def mut(s):
        s['rtsp_sources'][0]['sound_groups'][0]['sound_whitelist']['Knock'] = True
        s['global']['delay'] = 0.4
    sm.modify_settings(mut)
    det = wait_until(lambda: det_of('rtsp_cam1'))
    wait_until(lambda: det.tracker.groups[0]['whitelist'].get('Knock') is True and det.tracker.window == 0.4)


def test_live_gain_by_source_id(session_env, monkeypatch):
    classify, sock, emitted, calls, sm = session_env
    monkeypatch.setattr(classify, 'rtsp_source', lambda url, **kw: FakeReader(n=200))
    classify.start_from_settings(sock)
    wait_until(lambda: det_of('rtsp_cam1'))
    classify.update_source_gain('rtsp_cam1', 7)
    assert classify.live_gain('rtsp_cam1', 1) == 7
    assert classify.live_gain('rtsp_other', 3) == 3


def test_all_sources_dead_stops_session(session_env, monkeypatch):
    classify, sock, emitted, calls, sm = session_env

    class Boom(FakeReader):
        def iter_blocks(self, stop):
            raise RuntimeError('boom')
    monkeypatch.setattr(classify, 'rtsp_source', lambda url, **kw: Boom())
    monkeypatch.setattr(classify, 'run_source', lambda reader, on_block, stop, **k: None)
    classify.start_from_settings(sock)
    wait_until(lambda: not classify.get_status()['running'])
    wait_until(lambda: ('detection_status', {'status': 'stopped'}) in emitted)


def test_live_gains_cleared_after_stop(session_env, monkeypatch):
    classify, sock, emitted, calls, sm = session_env
    monkeypatch.setattr(classify, 'rtsp_source', lambda url, **kw: FakeReader(n=200))
    classify.start_from_settings(sock)
    wait_until(lambda: det_of('rtsp_cam1'))
    classify.update_source_gain('rtsp_cam1', 7)
    classify.stop_detection()
    assert classify.live_gain('rtsp_cam1', 3) == 3


def test_pending_sounds_flushed_before_cleanup(session_env, monkeypatch):
    """Un son en attente d'ecriture groupee ne reapparait pas apres « Vider
    la liste des sons non coches »."""
    classify, sock, emitted, calls, sm = session_env
    from flask import Flask
    import routes.sources as rs
    monkeypatch.setattr(classify, 'SEEN_FLUSH_DELAY', 30)
    classify._queue_sound_seen('rtsp', 'cam1', 'Dog')
    app = Flask(__name__)
    app.register_blueprint(rs.sources_bp)
    r = app.test_client().post('/api/source/sound_whitelist/cleanup',
                               json={'kind': 'rtsp', 'source_key': 'cam1', 'group_slug': 'clap'})
    assert r.status_code == 200
    classify._flush_sound_seen()
    wl = sm.load_settings()['rtsp_sources'][0]['sound_groups'][0]['sound_whitelist']
    assert 'Dog' not in wl


def test_listening_off_while_reconnecting(session_env, monkeypatch):
    classify, sock, emitted, calls, sm = session_env

    class Short(FakeReader):
        def iter_blocks(self, stop):
            self.runs += 1
            for i in range(20 if self.runs == 1 else 0):
                if stop.is_set():
                    return
                time.sleep(0.01)
                yield np.zeros(BLOCK_SAMPLES, np.float32)
    monkeypatch.setattr(classify, 'rtsp_source', lambda url, **kw: Short())
    classify.start_from_settings(sock)
    # flux perdu, reconnexion en cours
    wait_until(lambda: ('rtsp_cam1', True) in calls['listening'] and calls['listening'][-1] == ('rtsp_cam1', False))


def two_cameras(sm):
    s = sm.load_settings()
    cam2 = dict(s['rtsp_sources'][0], id='cam2', name='Cam2', url='rtsp://h/2')
    s['rtsp_sources'].append(cam2)
    sm.save_settings(s)


def test_apply_settings_restarts_only_changed_source(session_env, monkeypatch):
    classify, sock, emitted, calls, sm = session_env
    urls = []
    monkeypatch.setattr(classify, 'rtsp_source', lambda url, **kw: urls.append(url) or FakeReader(n=400))
    two_cameras(sm)
    classify.start_from_settings(sock)
    det1, det2 = wait_until(lambda: all(d := (det_of('rtsp_cam1'), det_of('rtsp_cam2'))) and d)

    # Changement de groupe seulement : rien n'est relance
    def add_sound(s):
        s['rtsp_sources'][1]['sound_groups'][0]['sound_whitelist']['Knock'] = True
    sm.modify_settings(add_sound)
    assert classify.apply_settings_if_running() is False
    assert det_of('rtsp_cam2') is det2
    assert det2.tracker.groups[0]['whitelist'].get('Knock') is True

    # Adresse de la camera 2 : seule elle est relancee
    sm.modify_settings(lambda s: s['rtsp_sources'][1].update(url='rtsp://h/2b'))
    assert classify.apply_settings_if_running() is True
    wait_until(lambda: det_of('rtsp_cam2') not in (None, det2))
    wait_until(lambda: det2.classifier is None)
    assert det_of('rtsp_cam1') is det1 and not det1.tracker is None
    assert urls[-1] == 'rtsp://h/2b'

    # Camera 1 desactivee : arretee, la 2 continue
    sm.modify_settings(lambda s: s['rtsp_sources'][0].update(enabled=False))
    classify.apply_settings_if_running()
    assert det_of('rtsp_cam1') is None
    wait_until(lambda: det1.classifier is None)   # ferme en arriere-plan (la requete n'attend plus)
    assert classify.get_status()['sources'] == ['rtsp_cam2']
    assert ('rtsp_cam1', False) in calls['listening']

    # Plus aucune source : la session s'arrete d'elle-meme
    sm.modify_settings(lambda s: s['rtsp_sources'][1].update(enabled=False))
    classify.apply_settings_if_running()
    wait_until(lambda: not classify.get_status()['running'])


def test_no_live_emits_without_clients(session_env, monkeypatch):
    classify, sock, emitted, calls, sm = session_env
    monkeypatch.setattr(classify, 'rtsp_source', lambda url, **kw: FakeReader(n=100))
    monkeypatch.setattr(classify, '_clients', 0)
    classify.start_from_settings(sock)
    det = wait_until(lambda: det_of('rtsp_cam1'))
    wait_until(lambda: det.classifier is not None and len(det.classifier.blocks) >= 10)   # blocs traites
    assert not [e for e, d in emitted if e in ('source_level', 'group_scores', 'labels')]
    classify.client_connected(+1)
    wait_until(lambda: [e for e, d in emitted if e == 'source_level'])
    classify.client_connected(-1)


def test_restarting_only_source_keeps_session_alive(session_env, monkeypatch):
    """Relance de l'unique source avec un arret lent (auto-volume, init YAMNet) :
    la session survit et l'etat HA reste « en cours » (elle s'arretait pendant
    la fenetre sans source, en 6.51-6.52)."""
    classify, sock, emitted, calls, sm = session_env
    import ha_entities
    states = []
    monkeypatch.setattr(ha_entities, 'update_detection_state', lambda running, *a: states.append(running))
    monkeypatch.setattr(classify, 'rtsp_source', lambda url, **kw: FakeReader(n=400))
    classify.start_from_settings(sock)
    old = wait_until(lambda: det_of('rtsp_cam1'))
    slow = classify.DetectionSession._set_listening
    monkeypatch.setattr(classify.DetectionSession, '_set_listening',
                        staticmethod(lambda src, on: (time.sleep(1.0), slow(src, on))))
    sm.modify_settings(lambda s: s['rtsp_sources'][0].update(url='rtsp://h/nouvelle'))
    assert classify.apply_settings_if_running() is True
    wait_until(lambda: det_of('rtsp_cam1') not in (None, old))
    time.sleep(0.7)   # au-dela d'une ronde de surveillance (0,5 s) : jamais vue arretee
    assert classify.get_status()['running'] and det_of('rtsp_cam1') is not None
    assert states == [True]   # jamais « arretee » puis « en cours »


def test_removing_last_source_stops_session_once(session_env, monkeypatch):
    classify, sock, emitted, calls, sm = session_env
    import ha_entities
    states = []
    monkeypatch.setattr(ha_entities, 'update_detection_state', lambda running, *a: states.append(running))
    monkeypatch.setattr(classify, 'rtsp_source', lambda url, **kw: FakeReader(n=400))
    classify.start_from_settings(sock)
    wait_until(lambda: det_of('rtsp_cam1'))
    sm.modify_settings(lambda s: s['rtsp_sources'][0].update(enabled=False))
    assert classify.apply_settings_if_running() is None
    wait_until(lambda: not classify.get_status()['running'])
    wait_until(lambda: states == [True, False])


def test_sound_seen_not_duplicated_after_live_change(session_env, monkeypatch):
    """Son en attente d'ecriture : une mise a jour en direct ne le rend pas
    « nouveau » une seconde fois."""
    classify, sock, emitted, calls, sm = session_env
    monkeypatch.setattr(classify, 'rtsp_source', lambda url, **kw: FakeReader(n=400))
    monkeypatch.setattr(classify, 'SEEN_FLUSH_DELAY', 30)
    classify.start_from_settings(sock)
    wait_until(lambda: 'Speech' in [d['label'] for e, d in emitted if e == 'sound_seen'])
    before = [d['label'] for e, d in emitted if e == 'sound_seen']
    assert before.count('Speech') == 1
    classify.apply_settings_if_running()
    time.sleep(0.5)
    assert [d['label'] for e, d in emitted if e == 'sound_seen'].count('Speech') == 1


def test_entity_key_change_keeps_listening(session_env, monkeypatch):
    """Cle d'entite changee par un import (capture identique, pas de relance) :
    l'ancienne cle passe hors ligne, la nouvelle en ligne."""
    classify, sock, emitted, calls, sm = session_env
    monkeypatch.setattr(classify, 'get_vban_detector', lambda: object())
    monkeypatch.setattr(classify, 'VbanSource', lambda listener, ip, name, on_idle=None: FakeReader(n=400))
    sm.modify_settings(lambda s: s.update(rtsp_sources=[], saved_vban_sources=[
        {'id': 'v1', 'entity_key': 'vban_salon', 'ip': '1.1.1.1', 'name': 'Salon', 'stream_name': 'S',
         'enabled': True, 'sound_groups': [{'slug': 'clap', 'name': 'Clap', 'ha_entities': [1]}]}]))
    classify.start_from_settings(sock)
    wait_until(lambda: ('vban_salon', True) in calls['listening'])
    calls['listening'].clear()
    sm.modify_settings(lambda s: s['saved_vban_sources'][0].update(entity_key='vban_bureau'))
    assert classify.apply_settings_if_running() is False   # aucune relance
    assert calls['listening'] == [('vban_salon', False), ('vban_bureau', True)]


def test_apply_settings_during_shutdown_is_ignored(session_env, monkeypatch):
    classify, sock, emitted, calls, sm = session_env
    monkeypatch.setattr(classify, 'rtsp_source', lambda url, **kw: FakeReader(n=400))
    classify.start_from_settings(sock)
    wait_until(lambda: det_of('rtsp_cam1'))
    session = classify._current()
    session.stop_event.set()
    assert session.apply_settings(sm.load_settings()) is None
    assert session._applying == 0


def test_mic_runner_stopped_during_prepare(session_env, monkeypatch):
    """Micro arrete pendant la resolution PulseAudio : aucun auto-volume ni
    volume applique apres coup."""
    classify, sock, emitted, calls, sm = session_env
    import threading
    stop = threading.Event()
    stop.set()
    session = classify.DetectionSession([], sock)
    started = []
    import auto_volume
    monkeypatch.setattr(auto_volume.auto_volume_mgr, 'start', lambda *a, **k: started.append(a))
    import audio_utils
    monkeypatch.setattr(audio_utils, 'set_pulse_volume', lambda *a: None)
    monkeypatch.setattr(classify, 'mic_source', lambda name: FakeReader())
    sm.modify_settings(lambda s: s['microphone'].update(enabled=True, auto_volume=True, pulse_name='alsa.x'))
    session._prepare_mic(stop)
    assert started == []


def test_sounds_not_added_without_auto_add(session_env, monkeypatch):
    """Groupe sans « Ajouter les sons entendus » : aucun son ajouté ni annoncé."""
    classify, sock, emitted, calls, sm = session_env
    sm.modify_settings(lambda s: s['rtsp_sources'][0]['sound_groups'][0].update(auto_add_sounds=False))
    monkeypatch.setattr(classify, 'rtsp_source', lambda url, **kw: FakeReader(n=100))
    classify.start_from_settings(sock)
    det = wait_until(lambda: det_of('rtsp_cam1'))
    wait_until(lambda: det.classifier is not None and len(det.classifier.blocks) >= 20)   # « Speech » entendu
    time.sleep(0.4)   # au-dela de l'ecriture groupee (0,2 s)
    classify.stop_detection(timeout=5)
    assert not [d for e, d in emitted if e == 'sound_seen']
    assert sm.load_settings()['rtsp_sources'][0]['sound_groups'][0]['sound_whitelist'] == {'Clapping': True}


def test_sounds_added_only_to_auto_add_groups(session_env, monkeypatch):
    classify, sock, emitted, calls, sm = session_env
    sm.modify_settings(lambda s: s['rtsp_sources'][0]['sound_groups'].append(
        {'slug': 'toc', 'name': 'Toc', 'ha_entities': [1], 'sound_whitelist': {}}))
    monkeypatch.setattr(classify, 'rtsp_source', lambda url, **kw: FakeReader(n=100))
    classify.start_from_settings(sock)
    wait_until(lambda: sm.load_settings()['rtsp_sources'][0]['sound_groups'][0]['sound_whitelist'].get('Speech') is False)
    classify.stop_detection(timeout=5)
    clap, toc = sm.load_settings()['rtsp_sources'][0]['sound_groups']
    assert clap['sound_whitelist'].get('Speech') is False and toc['sound_whitelist'] == {}


def test_discovery_seen_intersection():
    import classify
    assert classify._discovery_seen([{'whitelist': {'A': True}, 'auto_add': False}]) is None
    groups = [{'whitelist': {'A': True, 'B': False}, 'auto_add': True},
              {'whitelist': {}, 'auto_add': True},       # option tout juste activee
              {'whitelist': {'C': True}, 'auto_add': False}]
    assert classify._discovery_seen(groups) == set()   # A et B manquent au 2e groupe


def test_zero_clap_event_dropped_for_group_with_entities(session_env, monkeypatch):
    classify, sock, emitted, calls, sm = session_env
    session = classify.DetectionSession([], sock)
    src = {'source_id': 'rtsp_cam1', 'entity_key': 'rtsp_cam1', 'name': 'Cam', 'label': 'RTSP: Cam'}
    posted = []
    monkeypatch.setattr(classify, '_side_effects', types.SimpleNamespace(submit=lambda *a, **k: posted.append(a)))
    base = {'timestamp': 1, 'score': 0.8, 'labels': [], 'group_slug': 'clap', 'group_name': 'Clap', 'ignored': False}
    session._on_detection(src, dict(base, clap_count=0, group_clap_counts=[1, 2]))
    assert posted == [] and not [e for e, d in emitted if e == 'clap']
    session._on_detection(src, dict(base, clap_count=0, group_clap_counts=[]))   # groupe sans entites
    assert len(posted) == 1


def test_stopped_mic_launcher_stops_only_its_auto_volume(session_env, monkeypatch):
    """Relance rapide du micro (meme peripherique) : l'ancien lanceur, arrete
    pendant son demarrage, ne coupe pas l'auto-volume repris par le nouveau."""
    import threading
    import audio_utils
    import auto_volume
    classify, sock, emitted, calls, sm = session_env
    mgr = auto_volume.auto_volume_mgr
    state = {'running': False, 'stops': 0}

    def fake_start(pulse, sio, owner=None):
        mgr._owner = owner
        mgr._pulse_name = pulse
        state['running'] = True
        if owner is old_stop:
            # pendant le demarrage de l'ancien : arret de la source, puis le
            # nouveau lanceur reprend l'auto-volume (meme micro)
            old_stop.set()
            mgr._owner = new_stop
    monkeypatch.setattr(mgr, 'start', fake_start)
    monkeypatch.setattr(mgr, 'stop', lambda: state.__setitem__('stops', state['stops'] + 1))
    monkeypatch.setattr(type(mgr), '_running', property(lambda self: state['running']), raising=False)
    monkeypatch.setattr(audio_utils, 'set_pulse_volume', lambda *a: None)
    monkeypatch.setattr(classify, 'mic_source', lambda name: FakeReader())
    sm.modify_settings(lambda s: s['microphone'].update(enabled=True, auto_volume=True, pulse_name='alsa.x'))
    session = classify.DetectionSession([], sock)
    old_stop, new_stop = threading.Event(), threading.Event()
    reader, av = session._prepare_mic(old_stop)
    assert av is None and state['stops'] == 0          # le nouveau proprietaire garde l'auto-volume
    mgr._owner = old_stop
    mgr.stop_if(old_stop)
    assert state['stops'] == 1                          # encore proprietaire : arrete


def test_sound_seen_tolerates_list_disabled_meanwhile(session_env):
    classify, sock, emitted, calls, sm = session_env
    session = classify.DetectionSession([], sock)
    src = {'source_id': 'x', 'kind': 'rtsp', 'source_key': 'x'}
    session.seen['x'] = None        # dernier « Ajouter les sons entendus » desactive
    session._on_sound_seen(src, {'label': 'Speech'})
    session.seen.pop('x')           # source arretee
    session._on_sound_seen(src, {'label': 'Speech'})
    assert not [e for e, d in emitted if e == 'sound_seen']


def test_audio_format_reported_and_kept_in_status(session_env, caplog):
    import logging
    classify, sock, emitted, calls, sm = session_env
    session = classify.DetectionSession([], sock)
    src = {'source_id': 'rtsp_cam1', 'label': 'RTSP: Cam'}
    low = {'codec': 'pcm_mulaw', 'sample_rate': 8000, 'channels': 'mono'}
    with caplog.at_level(logging.INFO):
        session._set_format(src, low)
        session._set_format(src, low)                       # meme format a la reconnexion : rien de plus
        session._set_format(src, dict(low, codec='aac', sample_rate=16000))
    formats = [d for e, d in emitted if e == 'source_format']
    assert [f['format']['sample_rate'] for f in formats] == [8000, 16000]
    assert 'coupés au-dessus de 4 kHz' in caplog.text
    assert session.audio_formats['rtsp_cam1']['codec'] == 'aac'


def test_audio_format_from_stale_reader_ignored(session_env):
    """Format lu par le fil d'un ffmpeg deja remplace (source retiree ou
    relancee) : ignore, rien n'est garde ni annonce."""
    classify, sock, emitted, calls, sm = session_env
    session = classify.DetectionSession([], sock)
    old, new = object(), object()
    session._runners['rtsp_cam1'] = {'reader': new, 'stop': __import__('threading').Event()}
    session._set_format({'source_id': 'rtsp_cam1', 'label': 'Cam'}, {'codec': 'aac', 'sample_rate': 16000}, old)
    session._set_format({'source_id': 'rtsp_gone', 'label': 'X'}, {'codec': 'aac', 'sample_rate': 16000}, old)
    assert session.audio_formats == {} and not [e for e, d in emitted if e == 'source_format']


def test_vban_unavailable_reported_as_source_error(session_env, monkeypatch):
    """Port VBAN occupe : erreur explicite sur la source, sans initialiser
    YAMNet pour rien (la carte restait sur « Connexion… »)."""
    classify, sock, emitted, calls, sm = session_env
    monkeypatch.setattr(classify, 'get_vban_detector', lambda: None)
    created = []
    monkeypatch.setattr(classify.DetectionSession, '_create_detector', lambda self, *a: created.append(1))
    session = classify.DetectionSession([], sock)
    session._run_one({'type': 'vban', 'source_id': 'vban_v1', 'label': 'VBAN: Salon'}, {'stop': __import__('threading').Event()})
    assert created == [] and session.source_errors['vban_v1'].startswith('écoute VBAN indisponible')
    assert ('source_status', {'source_id': 'vban_v1', 'status': 'error',
                              'error': session.source_errors['vban_v1']}) in emitted


def test_yamnet_init_failure_reported(session_env, monkeypatch):
    classify, sock, emitted, calls, sm = session_env

    def boom(self, *a):
        raise RuntimeError('modèle illisible')
    monkeypatch.setattr(classify.DetectionSession, '_create_detector', boom)
    session = classify.DetectionSession([], sock)
    runner = {'stop': __import__('threading').Event()}
    session._runners['rtsp_c'] = runner
    session._run_one({'type': 'rtsp', 'source_id': 'rtsp_c', 'label': 'Cam'}, runner)
    assert 'modèle illisible' in session.source_errors['rtsp_c']
    # Source retiree pendant l'initialisation : rien a afficher
    session.source_errors.clear()
    session._runners.pop('rtsp_c')
    session._run_one({'type': 'rtsp', 'source_id': 'rtsp_c', 'label': 'Cam'}, runner)
    assert session.source_errors == {}


def test_error_before_reading_reported_and_detector_freed(session_env, monkeypatch):
    """Exception apres l'initialisation de YAMNet (reglages, auto-volume) :
    erreur sur la carte, detecteur arrete et retire (il tournait pour rien)."""
    classify, sock, emitted, calls, sm = session_env
    stopped = []
    fake_det = types.SimpleNamespace(stop=lambda: stopped.append(1), stop_alerts=lambda: stopped.append('alerts'))

    def create(self, src, stop=None):
        self.detectors[src['source_id']] = fake_det
        return fake_det
    monkeypatch.setattr(classify.DetectionSession, '_create_detector', create)
    monkeypatch.setattr(classify.DetectionSession, '_prepare_mic',
                        lambda self, stop: (_ for _ in ()).throw(RuntimeError('pactl absent')))
    session = classify.DetectionSession([], sock)
    runner = {'stop': __import__('threading').Event()}
    session._runners['mic'] = runner
    session._run_guarded({'type': 'mic', 'source_id': 'mic', 'label': 'Micro'}, runner)
    assert 'pactl absent' in session.source_errors['mic']
    assert stopped == ['alerts', 1] and 'mic' not in session.detectors   # alertes eteintes d'abord


def test_alert_event_drives_sensor_ha_event_and_webhook(session_env, monkeypatch):
    """Mode alerte : capteur allume puis eteint, evenement claptrap_alert,
    webhook « alert », historique au debut seulement."""
    classify, sock, emitted, calls, sm = session_env
    import ha_entities
    alerts, posts, hooks = [], [], []
    monkeypatch.setattr(ha_entities, 'set_alert', lambda key, slug, on: alerts.append((key, slug, on)))
    monkeypatch.setattr(classify, '_http', types.SimpleNamespace(post=lambda url, **k: posts.append((url, k['json']))))
    monkeypatch.setattr(classify, 'send_webhook_async', lambda url, payload: hooks.append(payload))
    monkeypatch.setenv('SUPERVISOR_TOKEN', 't')
    session = classify.DetectionSession([], sock)
    session.webhooks['rtsp_cam1'] = 'http://ha/api/webhook/x'
    src = {'source_id': 'rtsp_cam1', 'entity_key': 'rtsp_cam1', 'name': 'Cam', 'label': 'RTSP: Cam'}
    base = {'timestamp': 1, 'score': 0.7, 'labels': [], 'group_slug': 'fumee', 'group_name': 'Fumée',
            'ignored': False, 'clap_count': 0, 'group_clap_counts': []}
    before = len(classify.get_detection_history())
    session._on_detection(src, dict(base, alert='on'))
    session._on_detection(src, dict(base, alert='off'))
    wait_until(lambda: len(hooks) == 2)
    assert alerts == [('rtsp_cam1', 'fumee', True), ('rtsp_cam1', 'fumee', False)]
    assert [u.rsplit('/', 1)[1] for u, _ in posts] == ['claptrap_alert', 'claptrap_alert']
    assert [h['event'] for h in hooks] == ['alert', 'alert'] and [h['alert'] for h in hooks] == ['on', 'off']
    assert len(classify.get_detection_history()) == before + 1
    assert [d['alert'] for e, d in emitted if e == 'alert'] == ['on', 'off']
