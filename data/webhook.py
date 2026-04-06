import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


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
        try:
            response = self.session.post(url, json=data, timeout=5)
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            logging.error(f"Webhook failed: {str(e)}")
            raise
