import re
from urllib.parse import urlsplit


def is_valid_url(url):
    """
    Validate if a string is a properly formatted http(s) URL.
    Returns True if the URL is valid, False otherwise.
    Accepts None as a valid value (for optional webhooks).

    Accepte les noms d'hote sans point (http://homeassistant:8123/...,
    http://a0d7b954-nodered:1880/...) et l'IPv6 entre crochets, courants pour
    un webhook vers Home Assistant ou un autre add-on.
    """
    if url is None:
        return True
    if not isinstance(url, str) or any(c.isspace() for c in url):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port  # leve ValueError si le port est invalide
    except ValueError:
        return False
    if parts.scheme.lower() not in ('http', 'https'):
        return False
    host = parts.hostname
    if not host:
        return False
    if ':' in host:  # IPv6 (entre crochets dans l'URL)
        return True
    return re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?', host) is not None

def mask_url_credentials(url):
    """Masque 'user:pass@' dans une URL pour les logs et les messages d'erreur.

    rtsp://admin:secret@192.168.1.5:554/stream -> rtsp://***@192.168.1.5:554/stream
    """
    if not url:
        return url
    return re.sub(r'(?<=://)[^/@\s]+@', '***@', str(url))
