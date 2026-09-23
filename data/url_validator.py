import re

def is_valid_url(url):
    """
    Validate if a string is a properly formatted URL.
    Returns True if the URL is valid, False otherwise.
    Accepts None as a valid value (for optional webhooks).
    """
    if url is None:
        return True
        
    url_pattern = re.compile(
        r'^https?://'  # http:// or https://
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|'  # domain...
        r'localhost|'  # localhost...
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'  # ...or ip
        r'(?::\d+)?'  # optional port
        r'(?:/?|[/?]\S+)$', re.IGNORECASE)
    
    return bool(url_pattern.match(url)) 

def mask_url_credentials(url):
    """Masque 'user:pass@' dans une URL pour les logs et les messages d'erreur.

    rtsp://admin:secret@192.168.1.5:554/stream -> rtsp://***@192.168.1.5:554/stream
    """
    if not url:
        return url
    return re.sub(r'(?<=://)[^/@\s]+@', '***@', str(url))
