"""Application : erreurs HTTP en JSON et nettoyage a l'arret."""
import threading
import time

from werkzeug.exceptions import NotFound, RequestEntityTooLarge


def test_http_errors_json_and_cleanup_waits(settings_dir, monkeypatch):
    import app as appmod
    with appmod.app.test_request_context('/api/inconnue'):
        resp, code = appmod._api_http_error(NotFound())
        assert code == 404 and resp.json['success'] is False
        resp, code = appmod._api_http_error(RequestEntityTooLarge())
        assert code == 413 and resp.json['error']
    with appmod.app.test_request_context('/page'):
        assert isinstance(appmod._api_http_error(NotFound()), NotFound)

    # Arret sans onglet ouvert : le second appel (worker_exit) attend la fin du
    # premier (thread du SIGTERM) au lieu de laisser le processus sortir.
    done = []
    monkeypatch.setattr(appmod, '_do_cleanup', lambda: (time.sleep(0.5), done.append(1)))
    appmod._cleaned = False
    monkeypatch.setattr(appmod, '_cleanup_done', threading.Event())
    first = threading.Thread(target=appmod.cleanup)
    first.start()
    time.sleep(0.05)
    appmod.cleanup()
    assert done == [1]
    first.join()
    appmod._cleaned = True   # pas de vrai nettoyage a la sortie de pytest


def test_internal_api_passes_ingress_filter(settings_dir, monkeypatch):
    """Commandes venues de HA (ha_control) : routes appelees en interne, a
    travers le filtre ingress actif comme en production."""
    import app as appmod
    monkeypatch.setattr(appmod.app.wsgi_app, 'enabled', True)
    status, data = appmod._internal_api('GET', '/api/settings')
    assert status == 200 and 'microphone' in data
    status, data = appmod._internal_api('PUT', '/api/settings/advanced', {'delay': 99})
    assert status == 400 and data['success'] is False


def test_index_page_and_versioned_static_files(settings_dir, monkeypatch):
    """Vraie route « / » (le test d'interface rend le gabarit avec son propre
    contexte) et fichiers versionnes : cache long, traversee refusee."""
    import app as appmod
    monkeypatch.setattr(appmod, 'get_audio_input_devices', lambda: [{'index': 1, 'name': 'USB', 'pulse_name': 'u'}])
    monkeypatch.setenv('CLAPTRAP_VERSION', '9.9.9')
    client = appmod.app.test_client()
    page = client.get('/', environ_base={'REMOTE_ADDR': next(iter(appmod.IngressOnlyMiddleware.ALLOWED))},
                      headers={'X-Ingress-Path': '/api/hassio_ingress/abc'})
    html = page.get_data(as_text=True)
    assert page.status_code == 200 and '/js/9.9.9/core.js' in html and '/css/9.9.9/' in html
    js = client.get('/js/9.9.9/core.js')
    assert js.status_code == 200 and js.mimetype == 'application/javascript'
    assert 'immutable' in js.headers['Cache-Control'] and b'CT' in js.data
    css = client.get('/css/1/style.css')
    assert css.status_code == 200 and css.mimetype == 'text/css'
    for bad in ('/js/1/../../app.py', '/js/1/%2e%2e/%2e%2e/app.py', '/css/1/absent.css'):
        assert client.get(bad).status_code == 404, bad


def test_auto_start_only_when_enabled(settings_dir, monkeypatch):
    import app as appmod
    import classify
    started = []
    monkeypatch.setattr(classify, 'start_from_settings',
                        lambda sio: started.append(1) or (True, [{'label': 'Micro'}]))
    import types
    threads = []

    def spy(*a, **k):
        t = threading.Thread(*a, **k)
        threads.append(t)
        return t
    # Dans app.py seulement (pas les modules time et threading de tout le processus).
    monkeypatch.setattr(appmod, 'time', types.SimpleNamespace(sleep=lambda s: None))
    monkeypatch.setattr(appmod, 'threading', types.SimpleNamespace(Thread=spy))
    settings_dir.save_settings({'microphone': {'configured': True, 'auto_start': False}})
    appmod._auto_start_if_configured()
    assert threads == [] and started == []
    settings_dir.save_settings({'microphone': {'configured': True, 'auto_start': True}})
    appmod._auto_start_if_configured()
    threads[0].join(2)
    assert started == [1]
