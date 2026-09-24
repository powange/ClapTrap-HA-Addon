"""Validation et masquage des URL, envoi des webhooks."""
import pytest

from url_validator import is_valid_url, mask_url_credentials, mask_webhook_url
import webhook


@pytest.mark.parametrize('url, ok', [
    ('http://homeassistant:8123/api/webhook/x', True),
    ('https://example.com/hook', True),
    ('http://[fe80::1]:1880/x', True),
    ('http://a0d7b954-nodered:1880/x', True),
    ('ftp://x', False), ('http://', False), ('http://exa mple.com', False),
    ('http://host:99999/x', False), ('javascript:alert(1)', False), (None, True),
])
def test_is_valid_url(url, ok):
    assert is_valid_url(url) is ok


def test_masking():
    assert mask_url_credentials('rtsp://admin:secret@192.168.1.5:554/s') == 'rtsp://***@192.168.1.5:554/s'
    assert mask_webhook_url('http://ha:8123/api/webhook/SECRET') == 'http://ha:8123/…'
    assert 'pw' not in mask_webhook_url('https://u:pw@h/x')


def test_invalid_webhook_url_refused():
    with pytest.raises(ValueError):
        webhook.send_webhook('ftp://x', {})


def test_post_not_replayed_on_server_error():
    """Un POST n'est rejoue qu'en cas d'echec de connexion : jamais sur une
    reponse 502/503/504 (l'automation se declenchait deux fois)."""
    retry = webhook._session.get_adapter('http://x').max_retries
    assert retry.total == 1 and retry.connect == 1 and retry.status == 0 and retry.read == 0


def test_async_ignores_invalid_url(caplog):
    webhook.send_webhook_async('not a url', {})
    assert 'invalide' in caplog.text
