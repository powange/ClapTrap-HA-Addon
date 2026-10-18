"""Outils communs aux tests."""
import time


def wait_until(condition, timeout=5.0, interval=0.01, message=None):
    """Attend que `condition()` soit vraie et renvoie sa valeur. Remplace les
    time.sleep fixes : rapide quand la machine l'est, tolerant sur un runner
    lent ; echoue avec `message` apres `timeout` secondes."""
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(message or f'condition non atteinte en {timeout} s')
        time.sleep(interval)
