import logging
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from url_validator import is_valid_url


def validate_webhook_url(url):
    """Valide une URL webhook. Retourne True si valide ou None, False sinon."""
    if url is None or url == '':
        return True
    if not is_valid_url(url):
        return False
    if not url.startswith(('http://', 'https://')):
        return False
    return True


class WebhookManager:
    def __init__(self):
        self.session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504]
        )
        self.session.mount('http://', HTTPAdapter(max_retries=retry_strategy))
        self.session.mount('https://', HTTPAdapter(max_retries=retry_strategy))

    def send_webhook(self, url, data):
        """Envoie une requête webhook et retourne la réponse"""
        if not validate_webhook_url(url):
            raise ValueError(f"URL webhook invalide: {url}")
        try:
            response = self.session.post(url, json=data, timeout=5)
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            logging.error(f"Webhook failed: {str(e)}")
            raise


# --- Async dispatch shared by all clap sources ------------------------------
_shared_manager = WebhookManager()
_shared_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="webhook")


def send_webhook_async(url, payload):
    """Envoie un webhook en arriere-plan (pool partage) avec retry via la
    Session de `WebhookManager`. Tous les chemins de detection (mic/RTSP/VBAN)
    passent par cette fonction pour homogeneiser:
    validation d'URL, retry, timeouts, logging.
    """
    if not url:
        return
    if not validate_webhook_url(url):
        logging.warning(f"URL webhook invalide, ignoree: {url}")
        return

    def _send():
        try:
            _shared_manager.send_webhook(url, payload)
        except Exception as exc:
            logging.debug(f"Webhook async vers {url} a echoue: {exc}")

    _shared_executor.submit(_send)
