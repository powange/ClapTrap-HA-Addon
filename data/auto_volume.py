"""Auto-volume (AGC) pour le microphone PulseAudio.

Ajuste automatiquement le volume de la source PulseAudio pour maintenir
un niveau de signal optimal pour la detection de claps.

Usage:
    from auto_volume import auto_volume_mgr
    auto_volume_mgr.start(pulse_name, socketio)
    auto_volume_mgr.feed_peak(0.05)   # appele depuis la boucle parecord
    auto_volume_mgr.stop()
"""

import subprocess
import threading
import time
import logging
from settings_manager import load_settings, save_settings

# Seuils de peak brut (avant amplification x50)
PEAK_TOO_LOW = 0.005
PEAK_TOO_HIGH = 0.3
VOLUME_MIN = 10
VOLUME_MAX = 150
ADJUST_INTERVAL = 1.0  # secondes entre ajustements
STEP_UP = 5            # % d'augmentation
STEP_DOWN = 10         # % de diminution


class AutoVolume:
    def __init__(self):
        self._running = False
        self._pulse_name = ''
        self._socketio = None
        self._current_volume = 100
        self._peaks = []
        self._lock = threading.Lock()
        self._thread = None
        self._last_save_time = 0
        self._SAVE_DEBOUNCE = 30

    @property
    def running(self):
        return self._running

    def start(self, pulse_name, socketio):
        if self._running:
            return
        self._pulse_name = pulse_name
        self._socketio = socketio
        self._running = True
        self._peaks = []

        # Lire le volume actuel depuis les settings
        settings = load_settings()
        self._current_volume = settings.get('microphone', {}).get('volume', 100)

        self._thread = threading.Thread(target=self._adjust_loop, daemon=True)
        self._thread.start()
        logging.info(f"Auto-volume demarre (volume initial: {self._current_volume}%)")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        # Persister le volume final avant l'arret
        self._persist_volume(self._current_volume)
        logging.info("Auto-volume arrete")

    def feed_peak(self, peak):
        """Appele depuis la boucle parecord avec chaque peak brut."""
        if not self._running:
            return
        with self._lock:
            self._peaks.append(peak)

    def _adjust_loop(self):
        while self._running:
            time.sleep(ADJUST_INTERVAL)
            if not self._running:
                break

            with self._lock:
                if not self._peaks:
                    continue
                avg_peak = sum(self._peaks) / len(self._peaks)
                self._peaks.clear()

            new_volume = self._current_volume

            if avg_peak < PEAK_TOO_LOW and self._current_volume < VOLUME_MAX:
                new_volume = min(self._current_volume + STEP_UP, VOLUME_MAX)
            elif avg_peak > PEAK_TOO_HIGH and self._current_volume > VOLUME_MIN:
                new_volume = max(self._current_volume - STEP_DOWN, VOLUME_MIN)

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
