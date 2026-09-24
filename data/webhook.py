"""Envoi des webhooks (claps et test), avec une seule session HTTP partagee."""
import logging
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from url_validator import is_valid_url, mask_webhook_url


def _make_session():
    session = requests.Session()
    # Au plus 1 nouvel essai rapide, et seulement si la connexion a echoue
    # (la requete n'est alors jamais partie). Rejouer un POST sur 502/503/504
    # pouvait declencher l'automation deux fois : un proxy renvoie 502 apres
    # avoir deja transmis la requete.
    retry_kwargs = dict(total=1, connect=1, read=0, status=0, other=0, backoff_factor=0.3)
    try:
        retry_strategy = Retry(allowed_methods=frozenset({'POST'}), **retry_kwargs)
    except TypeError:  # urllib3 < 1.26
        retry_strategy = Retry(method_whitelist=frozenset({'POST'}), **retry_kwargs)
    session.mount('http://', HTTPAdapter(max_retries=retry_strategy))
    session.mount('https://', HTTPAdapter(max_retries=retry_strategy))
    return session


_session = _make_session()
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="webhook")


def send_webhook(url, data, follow_redirects=True):
    """POST JSON synchrone ; retourne la reponse, leve ValueError (URL
    invalide) ou requests.RequestException."""
    if not url or not is_valid_url(url):
        raise ValueError("URL de webhook invalide (http:// ou https:// attendu)")
    try:
        # timeout = (connect, read) : echec rapide si l'hote ne repond pas.
        response = _session.post(url, json=data, timeout=(3, 5), allow_redirects=follow_redirects)
        response.raise_for_status()
        return response
    except requests.exceptions.RequestException as e:
        # Le message de requests contient l'URL complete (donc le secret).
        status = getattr(getattr(e, 'response', None), 'status_code', None)
        logging.error(f"Webhook vers {mask_webhook_url(url)} en échec"
                      + (f" (HTTP {status})" if status else f" ({type(e).__name__})"))
        raise


def send_webhook_async(url, payload):
    """Envoie un webhook de clap en arriere-plan (pool partage par toutes les
    sources)."""
    if not url:
        return
    if not is_valid_url(url):
        logging.warning(f"URL webhook invalide, ignorée : {mask_webhook_url(url)}")
        return

    def _send():
        try:
            send_webhook(url, payload)
        except Exception as exc:
            logging.debug(f"Webhook vers {mask_webhook_url(url)} en échec : {type(exc).__name__}")

    _executor.submit(_send)
