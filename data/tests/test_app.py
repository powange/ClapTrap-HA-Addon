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
