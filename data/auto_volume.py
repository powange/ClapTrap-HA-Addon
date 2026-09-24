"""Auto-volume (AGC) pour le microphone PulseAudio.

Ajuste le volume de la source PulseAudio pendant la detection :
- baisse si le son sature (pic >= CLIP_LEVEL dans les 2 dernieres secondes) ;
- monte doucement si le fond est tres faible ET qu'aucun son fort n'a ete
  entendu depuis 30 s ;
- sinon ne touche a rien.

L'ancien algorithme travaillait sur la MOYENNE des pics : dans une piece calme
il montait jusqu'a 150 % et ne redescendait jamais (un clap bref ne fait pas
monter une moyenne sur 1 s au-dela de 0,3), d'ou des claps ecretes.

Usage (par la session de detection) :
    auto_volume_mgr.start(pulse_name, socketio)
    auto_volume_mgr.feed_peak(0.05)   # pic brut de chaque bloc de 100 ms
    auto_volume_mgr.stop()
"""

import collections
import threading
import time
import logging
from settings_manager import load_settings

CLIP_LEVEL = 0.9        # pic considere comme sature
QUIET_FLOOR = 0.002     # fond (mediane des pics) sous lequel on peut monter
LOUD_LEVEL = 0.3        # un son a ce niveau dans les 30 s : ne pas monter
VOLUME_MIN = 10
VOLUME_MAX = 150
ADJUST_INTERVAL = 2.0   # secondes entre ajustements
LONG_WINDOW = 30.0      # memoire des sons forts
STEP_UP = 5             # % d'augmentation
STEP_DOWN = 10          # % de diminution


class AutoVolume:
    def __init__(self):
        self._running = False
        self._pulse_name = ''
        self._socketio = None
        self._current_volume = 100
        self._peaks = collections.deque()
        self._lock = threading.Lock()
        self._thread = None
        self._wake = threading.Event()   # arret immediat (plus d'attente de sleep)
        self._last_save_time = 0
        self._SAVE_DEBOUNCE = 30

    @property
    def running(self):
        return self._running

    def start(self, pulse_name, socketio):
        if self._running:
            if pulse_name == self._pulse_name:
                return
            # Autre peripherique (micro change) : ne pas garder l'ancien.
            self.stop()
        self._pulse_name = pulse_name
        self._socketio = socketio
        self._running = True
        self._wake.clear()
        self._peaks = collections.deque()

        # Lire le volume actuel depuis les settings
        settings = load_settings()
        self._current_volume = settings.get('microphone', {}).get('volume', 100)

        self._thread = threading.Thread(target=self._adjust_loop, daemon=True)
        self._thread.start()
        logging.info(f"Auto-volume demarre (volume initial: {self._current_volume}%)")

    def stop(self):
        # stop() est appele a chaque arret de la detection, auto-volume actif ou
        # non : ne persister que si la boucle a reellement tourne, sinon on
        # ecrasait le volume choisi par l'utilisateur avec la valeur par defaut.
        was_running = self._thread is not None
        self._running = False
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        if not was_running:
            return
        self._persist_volume(self._current_volume)
        logging.info("Auto-volume arrete")

    def feed_peak(self, peak):
        """Pic brut de chaque bloc de 100 ms du micro."""
        if not self._running:
            return
        with self._lock:
            self._peaks.append((time.monotonic(), peak))

    @staticmethod
    def decide(volume, recent, long_max):
        """Nouveau volume a partir des pics des 2 dernieres secondes et du pic
        maximal des 30 dernieres secondes."""
        if not recent:
            return volume
        if max(recent) >= CLIP_LEVEL:
            return max(volume - STEP_DOWN, VOLUME_MIN)
        floor = sorted(recent)[len(recent) // 2]
        if floor < QUIET_FLOOR and long_max < LOUD_LEVEL:
            return min(volume + STEP_UP, VOLUME_MAX)
        return volume

    def _adjust_loop(self):
        while self._running:
            # Event.wait : stop() reveille la boucle aussitot (sleep(2) faisait
            # durer l'arret d'une source micro jusqu'a 2 s).
            if self._wake.wait(ADJUST_INTERVAL) or not self._running:
                break

            now = time.monotonic()
            with self._lock:
                while self._peaks and now - self._peaks[0][0] > LONG_WINDOW:
                    self._peaks.popleft()
                recent = [p for t, p in self._peaks if now - t <= ADJUST_INTERVAL]
                long_max = max((p for _, p in self._peaks), default=0.0)

            new_volume = self.decide(self._current_volume, recent, long_max)

            if new_volume != self._current_volume:
                self._current_volume = new_volume
                self._apply_volume(new_volume)

                # Persister sur disque seulement toutes les _SAVE_DEBOUNCE secondes
                now = time.time()
                if (now - self._last_save_time) >= self._SAVE_DEBOUNCE:
                    self._last_save_time = now
                    self._persist_volume(new_volume)

    def _persist_volume(self, volume):
        """Persiste le volume actuel dans les settings sur disque.

        Via atomic_update : ce thread ecrit periodiquement en parallele des
        sauvegardes de l'UI -> load+save atomique pour ne pas se clobberer.
        """
        try:
            from settings_manager import atomic_update

            def _mutate(settings):
                settings.setdefault('microphone', {})['volume'] = volume
                return settings

            atomic_update(_mutate)
        except Exception as e:
            logging.warning(f"Auto-volume: erreur sauvegarde settings: {e}")

    def _apply_volume(self, volume):
        try:
            from audio_utils import set_pulse_volume
            set_pulse_volume(self._pulse_name, volume)
            logging.info(f"Auto-volume: ajuste a {volume}%")

            # Notifier le frontend
            if self._socketio:
                self._socketio.emit('auto_volume_update', {'volume': volume})

        except Exception as e:
            logging.warning(f"Auto-volume: erreur pactl: {e}")


# Singleton
auto_volume_mgr = AutoVolume()
