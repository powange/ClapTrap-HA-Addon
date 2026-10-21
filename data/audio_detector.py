"""Detecteur d'une source audio : pretraitement + YAMNet (MediaPipe) + logique
de claps (clap_logic.ClapTracker). Un detecteur = une source.

YAMNet analyse une fenetre glissante de 0,975 s, toutes les 0,5 s (mode
« clips » de MediaPipe, appele directement). Le mode flux de MediaPipe
n'analysait que des fenetres disjointes : un resultat par seconde, et un
clap a cheval sur deux fenetres n'etait entendu qu'a moitie dans chacune."""

import collections
import logging
import queue
import threading
import time

import numpy as np

from audio_utils import BLOCK_SAMPLES

_mediapipe = None


def _load_mediapipe():
    """Import de mediapipe au premier detecteur cree (il ralentissait le
    demarrage de l'add-on meme si la detection n'etait jamais lancee), sans
    OpenCV ni matplotlib (voir mp_light)."""
    global _mediapipe
    if _mediapipe is None:
        import mp_light
        mp_light.install_stubs()
        from mediapipe.tasks import python
        from mediapipe.tasks.python import audio
        from mediapipe.tasks.python.components import containers
        _mediapipe = (python, audio, containers)
    return _mediapipe

from clap_logic import ClapTracker

PRE_WINDOW = 320   # 20 ms : niveau mesure juste avant un pic (attaque)
# Fenetre d'analyse de YAMNet (15 600 echantillons a 16 kHz), analysee tous
# les HOP_BLOCKS blocs (0,5 s, proche du pas natif de YAMNet, 0,48 s) : deux
# resultats par seconde, chacun date a la fin de sa fenetre.
YAMNET_WINDOW_MS = 975
WINDOW_SAMPLES = YAMNET_WINDOW_MS * 16   # 15 600 a 16 kHz (seule frequence du modele)
HOP_BLOCKS = 5
ANALYSIS_HOP = HOP_BLOCKS * BLOCK_SAMPLES / 16000   # s entre deux analyses
WINDOW_BLOCKS = -(-WINDOW_SAMPLES // BLOCK_SAMPLES)   # blocs gardes pour une fenetre
JOBS_MAX = 2       # fenetres en attente d'analyse (au-dela, la plus ancienne est jetee)
# Classes gardees par resultat (groupes, decouverte, capteurs) ; le classifieur
# rend les 521 (max_results=-1) pour les sons appris.
TOP_RESULTS = 30
DENOISE_KEEP = 120.0   # s : reduction du bruit reactivee dans ce delai : profil repris
# Horloge audio : coupure si elle a plus de CUT_GAP s de retard sur l'heure ;
# sinon, ecart corrige en douceur (SLEW_STEP s par bloc au plus, au-dela de
# SLEW_DEADBAND) sans vider la fenetre d'analyse.
CUT_GAP = 0.5
SLEW_DEADBAND = 0.05
SLEW_STEP = 0.002
PRE_GAP = 80       # 5 ms laisses entre cette mesure et le pic
AGC_MEMORY = 30    # blocs (3 s) de pics pris en compte par l'auto-gain
AGC_TARGET = 0.3   # niveau vise pour le son le plus fort recent
AGC_MAX = 5.0
AGC_CEILING = 0.9  # niveau maximal avant le classifieur (pas d'ecretage)


class AudioDetector:
    def __init__(self, model_path, sample_rate=16000):
        self.model_path = model_path
        self.sample_rate = sample_rate
        self.source_id = None
        self.label = None
        self.classifier = None
        self.running = False
        self.score_threshold = 0.3
        self.tracker = ClapTracker()
        # Protege l'etat du tracker (fil audio vs fil d'analyse)
        self.lock = threading.Lock()
        # Serialise classify et close() : close() pendant une classification
        # n'est pas sur cote natif.
        self._clf_lock = threading.Lock()
        self._blocks = collections.deque(maxlen=WINDOW_BLOCKS)   # derniere seconde (classifieur)
        self._since_hop = 0       # blocs depuis la derniere analyse
        # Analyse dans un fil a part (comme le fil de MediaPipe en mode flux) :
        # l'inference et les rappels (socketio, MQTT) ne retardent jamais la
        # lecture du son (file VBAN d'une seconde sur un Raspberry Pi).
        self._jobs = queue.Queue(maxsize=JOBS_MAX)
        self._worker = None
        self._dropped = 0
        # Diagnostic (carte de la source, capteur HA) : analyses sautees,
        # coupures du flux, duree des analyses.
        self._cuts = 0
        self._lost_blocks = 0
        self._analyses = 0
        self._analysis_total = 0.0
        self._analysis_max = 0.0
        # Faut-il analyser ? (session : un groupe actif, ou un onglet ouvert
        # pour la decouverte des sons). None : toujours.
        self.analysis_wanted = None
        self.learned = None       # sons appris (learned.Matcher), remplace d'un bloc
        self.denoise = None       # reduction du bruit de fond (denoise.SpectralGate), entree de YAMNet seulement
        self._denoise_errors = 0
        self._denoise_kept = None     # (reduction coupee, instant) : profil repris si on la reactive vite
        self._denoise_switched = False
        self._pending = np.zeros(0, dtype=np.float32)
        self._dc = 0.0            # composante continue, suivie en douceur
        self._agc = 1.0           # auto-gain lisse (pour le classifieur seulement)
        self._last_total = 1.0    # gain total applique au bloc precedent
        self._recent_peaks = collections.deque(maxlen=AGC_MEMORY)
        self._prev_tail = np.zeros(0, dtype=np.float32)  # fin du bloc precedent (|x|)
        self._clock_end = 0.0     # horloge audio : fin du dernier bloc recu
        self._taps = []           # ecoutes du calibrage : fn(evenement, *donnees)
        self._errors_logged = 0
        self._detection_callback = None
        self._labels_callback = None
        self._sound_seen_callback = None
        self._scores_callback = None
        self._analysis_callback = None

    # --- Ecoutes (calibrage) ------------------------------------------------

    def add_tap(self, fn, events=None):
        """fn('block', pic_brut, gain, instant, niveau_avant_pic), fn('peak', 'attack'|'sustain',
        instant), fn('result', [(classe, score)], instant) : instants sur
        l'horloge audio ; fn('scores', [(classe, score)] des 521 classes,
        instant) : empreinte des sons appris ; fn('audio_raw', bloc) : le meme
        sans reduction du bruit (si active) ; fn('audio', bloc) : le son donne a YAMNet (gain,
        auto-gain et limiteur appliques), pour l'enregistrement d'extraits.
        `events` : evenements voulus (tous si None)."""
        self._taps = self._taps + [(fn, frozenset(events) if events else None)]

    def remove_tap(self, fn):
        self._taps = [t for t in self._taps if t[0] is not fn]

    def _wants(self, event):
        return any(ev is None or event in ev for _fn, ev in self._taps)

    def _tap(self, event, *data):
        for fn, ev in self._taps:
            if ev is not None and event not in ev:
                continue
            try:
                fn(event, *data)
            except Exception as e:
                logging.debug(f"tap: {e}")

    # --- Configuration ------------------------------------------------------

    def configure(self, source_id, label=None, detection_callback=None,
                  labels_callback=None, sound_seen_callback=None,
                  scores_callback=None, analysis_callback=None):
        self.source_id = source_id
        self.label = label or source_id
        self._detection_callback = detection_callback
        self._labels_callback = labels_callback
        self._sound_seen_callback = sound_seen_callback
        # Retour en direct pour l'interface : meilleur score de chaque groupe.
        self._scores_callback = scores_callback
        # Diagnostic Home Assistant : 3 classes les plus probables (meme sous
        # le seuil) et score de chaque groupe, a chaque resultat.
        self._analysis_callback = analysis_callback

    def set_groups(self, groups):
        """Groupes de sons (modifiables en direct). Le seuil d'auto-decouverte
        et d'emission des labels est le minimum des seuils de groupe."""
        with self.lock:
            self.tracker.set_groups(groups, default_threshold=self.score_threshold)
            if self.tracker.min_threshold is not None:
                self.score_threshold = self.tracker.min_threshold
            events = self.tracker.drop_alerts()   # alerte d'un groupe retire : eteinte
        self._dispatch(events)

    def stop_alerts(self):
        """Arret de la source : alertes allumees eteintes (capteurs HA)."""
        with self.lock:
            events = self.tracker.stop_alerts()
        self._dispatch(events)

    def set_exclusions(self, labels):
        with self.lock:
            self.tracker.exclusions = set(labels or [])

    def set_params(self, **params):
        with self.lock:
            self.tracker.set_params(**params)

    def initialize(self, max_results=10, score_threshold=0.3, clap_window=1.5,
                   peak_cooldown=0.08, peak_ratio=3.0):
        """Cree le classifieur YAMNet (mode clips : une fenetre par appel)."""
        self.score_threshold = score_threshold
        self.tracker.set_params(window=clap_window, peak_cooldown=peak_cooldown,
                                peak_ratio=peak_ratio)
        python, audio, containers = _load_mediapipe()
        self._containers = containers
        # Pas de category_allowlist : les labels hors groupes sont remontes
        # pour l'auto-decouverte dans l'UI.
        options = audio.AudioClassifierOptions(
            base_options=python.BaseOptions(model_asset_path=self.model_path),
            running_mode=audio.RunningMode.AUDIO_CLIPS,
            max_results=max_results,
            score_threshold=0,
        )
        self.classifier = audio.AudioClassifier.create_from_options(options)
        logging.info(f"Classificateur audio initialisé (max_results={max_results})")

    def start(self):
        self.running = True
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._work, daemon=True,
                                            name=f'yamnet-{self.source_id or "source"}')
            self._worker.start()

    def stop(self):
        self.running = False
        self._submit(None)   # reveille le fil d'analyse, qui s'arrete
        with self._clf_lock:
            classifier, self.classifier = self.classifier, None
        if classifier:
            try:
                classifier.close()
                logging.info(f"Classificateur arrêté ({self.label})")
            except Exception as e:
                logging.error(f"Erreur lors de l'arrêt du classificateur: {e}")

    # --- Audio ----------------------------------------------------------------

    def process_audio(self, audio_data, gain=1.0):
        """Bloc audio mono 16 kHz, AVANT gain. Ne relance jamais un detecteur
        arrete.

        Les pics sont comptes sur le signal brut ; le gain de la source et
        l'auto-gain ne s'appliquent qu'a l'entree du classifieur. Amplifie et
        ecrete avant le comptage (gain RTSP par defaut x10), le bruit de fond
        d'une camera depassait 1/ratio et plus aucun pic n'etait detecte.
        """
        if not self.running:
            return 0.0
        raw_peak = 0.0
        try:
            audio_data = np.asarray(audio_data, dtype=np.float32).reshape(-1)
            if not np.isfinite(audio_data).all():
                # Un bloc corrompu empoisonnait sinon le niveau moyen du bruit.
                audio_data = np.nan_to_num(audio_data, nan=0.0, posinf=0.0, neginf=0.0)
            if audio_data.size:
                # Composante continue suivie en douceur (une soustraction par
                # bloc creait des marches a chaque frontiere de 100 ms).
                self._dc = 0.95 * self._dc + 0.05 * float(audio_data.mean())
                audio_data = audio_data - np.float32(self._dc)
            raw_peak, peak_time, pre_level = self._locate_peak(audio_data)

            with self.lock:
                attacks, sustains = self.tracker.attack_count, self.tracker.sustain_count
                self.tracker.feed_peak(raw_peak, peak_time, pre_level=pre_level, gain=gain)
                attacks, sustains = self.tracker.attack_count - attacks, self.tracker.sustain_count - sustains
                pending = self.tracker.pending()
            if pending and self._jobs.empty():
                # Fenetre ouverte : fin sur l'horloge audio et decision
                # anticipee, verifiees dans le fil d'analyse (les rappels ne
                # retardent pas la lecture, et une analyse en attente passe
                # avant : elle couvre peut-etre la fin de la fenetre).
                try:
                    self._jobs.put_nowait((None, self._clock_end))
                except queue.Full:
                    pass
            if self._taps:
                self._tap('block', raw_peak, gain, peak_time, pre_level)
                if attacks:
                    self._tap('peak', 'attack', peak_time)
                if sustains:
                    self._tap('peak', 'sustain', peak_time)
            # Auto-gain (classifieur seulement) sur le son le plus fort des 3
            # dernieres secondes : il reste eleve sur un clap faible isole (le
            # bruit qui suit ne le fait plus retomber) et baisse aussitot sur
            # un son fort, qui n'est donc pas ecrete.
            self._recent_peaks.append(raw_peak * gain)
            loudest = max(self._recent_peaks)
            target = min(AGC_TARGET / loudest, AGC_MAX) if 0 < loudest < AGC_TARGET / 2 else 1.0
            self._agc = target if target < self._agc else 0.95 * self._agc + 0.05 * target
            total = gain * self._agc
            raw = audio_data
            if self._denoise_switched:
                # Reduction activee ou coupee : pas de fenetre melant les deux
                # (son decale de 24 ms, ou tranches en attente perdues).
                self._denoise_switched = False
                self._reset_window()
                self._pending = np.zeros(0, dtype=np.float32)
            clean = self._denoised(raw)
            delay = self.denoise.delay if clean is not None else 0
            if clean is not None:
                audio_data = clean   # reduction du bruit : entree du classifieur seulement
            # Pic du son effectivement amplifie (traite : en retard de 24 ms,
            # un clap de la fin du bloc precedent peut y etre).
            out_peak = float(np.abs(audio_data).max()) if clean is not None and audio_data.size else raw_peak
            if out_peak * total > AGC_CEILING:
                # Bloc trop fort pour le gain de la source (camera a x10) :
                # reduit plutot qu'ecrete (un clap sature est moins bien
                # reconnu). Ce bloc seulement : les sons suivants ne sont pas
                # attenues par un bruit fort isole.
                total = AGC_CEILING / out_peak
            last_total = self._last_total
            audio_data = self._amplify(audio_data, last_total, total)
            self._last_total = total
            if self._taps and self._wants('audio'):
                self._tap('audio', audio_data)   # tableau neuf, non modifie ensuite
            if self._taps and self._wants('audio_raw'):
                # Extrait « avant » : meme gain sans reduction du bruit (limiteur
                # sur son propre pic), et le niveau de reduction du bloc.
                if clean is None:
                    self._tap('audio_raw', audio_data, 0)
                else:
                    raw_total = min(total, AGC_CEILING / raw_peak) if raw_peak > 0 else total
                    self._tap('audio_raw', self._amplify(raw, min(last_total, raw_total), raw_total),
                              self.denoise.level)

            if self._pending.size:
                audio_data = np.concatenate((self._pending, audio_data))
            n_blocks = audio_data.size // BLOCK_SAMPLES
            for i in range(n_blocks):
                # Fin de ce bloc sur l'horloge audio (les blocs suivants du
                # meme envoi sont encore a venir).
                # Son reduit : en retard de `delay` echantillons sur l'horloge.
                rest = audio_data.size - (i + 1) * BLOCK_SAMPLES + delay
                self._push(audio_data[i * BLOCK_SAMPLES:(i + 1) * BLOCK_SAMPLES],
                           self._clock_end - rest / self.sample_rate)
            self._pending = audio_data[n_blocks * BLOCK_SAMPLES:].copy()
        except Exception:
            logging.exception(f"Erreur dans le traitement audio ({self.label})")
        return raw_peak

    @staticmethod
    def _amplify(x, last_total, total):
        if total > last_total + 1e-3 and x.size:
            # Remontee en rampe sur le bloc : pas de marche toutes les 100 ms.
            return np.clip(x * np.linspace(last_total, total, x.size, dtype=np.float32), -1.0, 1.0)
        if abs(total - 1.0) > 1e-3:
            return np.clip(x * np.float32(total), -1.0, 1.0)
        return x

    def _denoised(self, raw):
        """Son reduit (denoise.SpectralGate), ou None (desactivee, erreur)."""
        gate = self.denoise
        if gate is None:
            return None
        try:
            return gate.process(raw)
        except Exception as e:
            # Limite : une erreur a chaque bloc ferait 10 lignes/s.
            self._denoise_errors += 1
            if self._denoise_errors <= 3 or self._denoise_errors % 3000 == 0:
                logging.warning(f"[{self.label}] réduction du bruit en échec ({self._denoise_errors}x), "
                                f"son brut utilisé : {e}")
            return None

    def _locate_peak(self, x):
        """(pic, instant du pic, niveau juste avant lui) d'un bloc.

        L'instant suit une horloge audio (duree des blocs recus) et non l'heure
        de traitement : des blocs livres en rafale par ffmpeg gardent leur
        espacement reel. Elle n'est recalee que si le flux s'est interrompu
        (plus de CUT_GAP s de retard), et suit l'heure en douceur sinon. Les
        resultats de YAMNet sont dates sur la meme horloge.
        """
        now = time.monotonic()
        n = x.size
        dur = n / self.sample_rate
        if not self._clock_end:
            start = now - dur   # 1er bloc : il vient de se terminer
        else:
            start = self._clock_end
            lag = now - (start + dur)   # > 0 : horloge en retard sur l'heure
            if lag > CUT_GAP:
                # Flux interrompu (reconnexion) : recalage, et la seconde
                # d'avant la coupure n'est pas analysee avec le son d'apres.
                # (Avant 6.80, l'horloge etait calee pile sur ce seuil : la
                # moindre gigue passait pour une coupure et videait la fenetre.)
                self._on_gap(self._clock_end, lag)
                start = now - dur
            elif abs(lag) > SLEW_DEADBAND:
                # Source un peu lente ou rapide, rafale de ffmpeg : rattrapage
                # progressif, borne dans les deux sens.
                start += max(-SLEW_STEP, min(SLEW_STEP, lag))
        self._clock_end = start + dur
        if not n:
            return 0.0, now, None
        mag = np.abs(x)
        idx = int(mag.argmax())
        peak = float(mag[idx])
        buf = np.concatenate((self._prev_tail, mag)) if self._prev_tail.size else mag
        j = idx + self._prev_tail.size
        lo, hi = max(0, j - PRE_GAP - PRE_WINDOW), max(0, j - PRE_GAP)
        pre = float(buf[lo:hi].max()) if hi > lo else None
        self._prev_tail = mag[-(PRE_GAP + PRE_WINDOW):].copy()
        return peak, start + idx / self.sample_rate, pre

    def skip_audio(self, samples):
        """Audio perdu en amont (blocs VBAN jetes) : l'horloge audio avance
        d'autant, l'espacement des claps suivants reste juste."""
        gap = samples / self.sample_rate
        # File pleine (consommateur en retard, souvent le processeur) : compte
        # a part, ce n'est pas une coupure du flux.
        self._lost_blocks += samples // BLOCK_SAMPLES
        self._on_gap(self._clock_end, gap, cut=False)
        self._clock_end += gap

    def _on_gap(self, last_clock, gap, cut=True):
        """Coupure apres `last_clock` : pas de fenetre a cheval sur le trou, et
        les fenetres de comptage terminees sont closes (ou abandonnees si la
        coupure est longue) au lieu de partir au resultat suivant."""
        if cut:
            self._cuts += 1
        self._reset_window()
        self._prev_tail = np.zeros(0, dtype=np.float32)   # niveau d'avant le trou : sans objet
        gate = self.denoise
        if gate is not None:
            gate.reset()   # pas de tranche a cheval sur le trou
        with self.lock:
            events = self.tracker.interrupt(last_clock, gap)
        self._dispatch(events)

    def stats(self):
        """Compteurs de diagnostic depuis le demarrage de la source."""
        n = self._analyses
        return {'analyses_sautees': self._dropped, 'blocs_perdus': self._lost_blocks, 'coupures': self._cuts,
                'analyses': n,
                'analyse_ms': round(self._analysis_total / n * 1000, 1) if n else None,
                'analyse_max_ms': round(self._analysis_max * 1000, 1) if n else None}

    def _reset_window(self):
        self._blocks.clear()
        self._since_hop = 0

    def _push(self, block, end_clock):
        """Bloc de 100 ms vers la fenetre glissante ; une fenetre a analyser
        toutes les HOP_BLOCKS blocs, une fois la premiere seconde recue."""
        self._blocks.append(block)
        self._since_hop += 1
        if len(self._blocks) < WINDOW_BLOCKS or self._since_hop < HOP_BLOCKS:
            return
        self._since_hop = 0
        if self.analysis_wanted is not None and not self.analysis_wanted(end_clock):
            return   # aucun groupe actif et aucun onglet ouvert : rien a reconnaitre
        self._submit((np.concatenate(self._blocks)[-WINDOW_SAMPLES:], end_clock))

    def _submit(self, job):
        try:
            self._jobs.put_nowait(job)
            return
        except queue.Full:
            pass
        try:
            old = self._jobs.get_nowait()   # analyse en retard (Pi surcharge) : la plus ancienne saute
            self._jobs.task_done()
            if job is not None and old is not None and old[0] is not None:
                self._dropped += 1
                if self._dropped <= 5 or self._dropped % 100 == 0:
                    logging.warning(f"[{self.label}] analyse en retard : fenêtre ignorée ({self._dropped}x)")
        except queue.Empty:
            pass
        try:
            self._jobs.put_nowait(job)
        except queue.Full:
            pass

    def _work(self):
        while True:
            try:
                # Delai : le marqueur d'arret peut etre perdu (file pleine,
                # fenetre deposee au meme moment) ; le fil s'arrete quand meme.
                job = self._jobs.get(timeout=1.0)
            except queue.Empty:
                if not self.running:
                    return
                continue
            try:
                if job is None:
                    if not self.running:
                        return
                    continue
                if job[0] is None:
                    self._poll(job[1])
                else:
                    self._analyze(*job)
            finally:
                self._jobs.task_done()

    def _poll(self, clock):
        if not self.running:
            return
        try:
            with self.lock:
                events = self.tracker.poll(clock)
            self._dispatch(events)
        except Exception:
            logging.exception("Erreur dans la fin des fenêtres de comptage")

    def wait_idle(self, timeout=5.0):
        """Attend que les fenetres en attente soient analysees (tests, autotest)."""
        with self._jobs.all_tasks_done:
            self._jobs.all_tasks_done.wait_for(lambda: not self._jobs.unfinished_tasks, timeout)

    def set_denoise(self, level):
        """Reduction du bruit de fond avant YAMNet : 0 (desactivee) a 3
        (valeur hors bornes ramenee). Le profil du bruit est garde quand seul
        le niveau change."""
        from denoise import SpectralGate, clamp_level
        level = clamp_level(level)
        if not level:
            if self.denoise is not None:
                self._denoise_kept = (self.denoise, time.monotonic())
                self.denoise = None
                self._denoise_switched = True
        elif self.denoise is None:
            kept, since = self._denoise_kept or (None, 0.0)
            # Coupee il y a peu (comparaison avec/sans) : profil du bruit
            # repris plutot que reappris sur le son du moment.
            gate = kept if kept is not None and time.monotonic() - since < DENOISE_KEEP else SpectralGate(level)
            gate.level = level
            gate.reset()
            self._denoise_kept = None
            self.denoise = gate
            self._denoise_switched = True
        else:
            self.denoise.level = level

    def _analyze(self, window, end_clock):
        container = self._containers.AudioData.create_from_array(window, self.sample_rate)
        with self._clf_lock:
            if not self.running or not self.classifier:
                return
            try:
                t0 = time.monotonic()
                results = self.classifier.classify(container)
                spent = time.monotonic() - t0
                self._analyses += 1
                self._analysis_total += spent
                self._analysis_max = max(self._analysis_max, spent)
            except Exception as e:
                # Limite : une erreur persistante produisait 2 lignes/s.
                self._errors_logged += 1
                if self._errors_logged <= 5 or self._errors_logged % 600 == 0:
                    logging.error(f"Erreur lors de la classification ({self._errors_logged}x): {e}")
                return
        if results:
            # Une fenetre exacte : un seul resultat (verifie par l'autotest), date a sa fin.
            self._handle_result(results[0], end_clock)

    # --- Resultats -------------------------------------------------------------

    def _categories(self, result):
        """(521 classes, classes gardees) d'un resultat : les TOP_RESULTS
        meilleures et les sons appris (« Appris : <nom> »), cochees dans les
        groupes comme les autres."""
        full = [(c.category_name, float(c.score)) for c in result.classifications[0].categories]
        categories = sorted(full, key=lambda c: c[1], reverse=True)[:TOP_RESULTS]
        learned = self.learned
        if learned:
            categories = sorted(categories + learned.match(full), key=lambda c: c[1], reverse=True)
        return full, categories

    def classify_window(self, window):
        """Classes d'une fenetre hors du flux de la detection (extrait
        enregistre), comme pour une analyse : None si le classifieur est
        arrete."""
        container = self._containers.AudioData.create_from_array(window, self.sample_rate)
        with self._clf_lock:
            if not self.running or not self.classifier:
                return None
            results = self.classifier.classify(container)
        if not results or not results[0].classifications:
            return []
        return self._categories(results[0])[1]

    def _handle_result(self, result, now):
        """Resultat d'une fenetre se terminant a `now` (horloge audio). Appele
        dans le fil d'analyse, hors du verrou du classifieur."""
        if not self.running:
            return   # detecteur arrete pendant l'analyse
        try:
            if not result or not result.classifications:
                return
            full, categories = self._categories(result)
            if self._taps and self._wants('scores'):
                self._tap('scores', full, now)
            with self.lock:
                exclusions = set(self.tracker.exclusions)
                threshold = self.score_threshold
                if logging.getLogger().isEnabledFor(logging.DEBUG):
                    logging.debug(f"[{self.label}] labels={[(n, round(s, 3)) for n, s in categories]}")
                events = self.tracker.on_classification(categories, now)
            if self._taps:
                self._tap('result', categories, now)
            with self.lock:
                group_scores = {
                    g['slug']: max((s for n, s in categories
                                    if g['whitelist'].get(n) and n not in exclusions), default=0.0)
                    for g in self.tracker.groups
                }
            if self._scores_callback:
                try:
                    self._scores_callback(group_scores)
                except Exception:
                    pass
            if self._analysis_callback:
                try:
                    top = sorted((c for c in categories if c[0] not in exclusions),
                                 key=lambda c: c[1], reverse=True)[:3]
                    self._analysis_callback(top, group_scores)
                except Exception as e:
                    logging.debug(f"analysis: {e}")

            # Callbacks HORS verrou (E/S : socketio, MQTT, HTTP).
            if self._sound_seen_callback:
                for name, score in categories:
                    if score >= threshold and name not in exclusions:
                        try:
                            self._sound_seen_callback({'label': name, 'score': score})
                        except Exception as e:
                            logging.debug(f"sound_seen: {e}")

            if self._labels_callback:
                top = sorted((c for c in categories if c[0] not in exclusions),
                             key=lambda c: c[1], reverse=True)[:3]
                labels = [{'label': n, 'score': s} for n, s in top if s >= threshold]
                if labels:
                    try:
                        self._labels_callback(labels)
                    except Exception as e:
                        logging.error(f"Erreur callback labels ({self.label}): {e}")

            self._dispatch(events)
        except Exception:
            logging.exception("Erreur dans le traitement du résultat")

    def _dispatch(self, events):
        """Evenements de detection (arbitrage fait) vers la session, hors verrou."""
        if events and any(not e['ignored'] and not e.get('alert') for e in events):
            winner = next(e for e in events if not e['ignored'] and not e.get('alert'))
            logging.debug(f"[{self.label}] CLAP groupe={winner['group']['name']}: "
                          f"{winner['clap_count']} pic(s), score={winner['score']:.2f}")
        for ev in events:
            if not self._detection_callback:
                break
            group = ev['group']
            try:
                self._detection_callback({
                    'timestamp': time.time(),
                    # Instant du 1er clap (et non de l'envoi, 1,5 a 2,5 s plus
                    # tard) : horloge audio (monotone) ramenee a l'heure reelle.
                    'clap_time': time.time() - max(0.0, time.monotonic() - ev['start']) if ev.get('start') else None,
                    'score': ev['score'],
                    'source_id': self.source_id,
                    'clap_count': ev['clap_count'],
                    'labels': ev['labels'],
                    'group_slug': group['slug'],
                    'group_name': group['name'],
                    'group_clap_counts': list(group['clap_counts']),   # garanti par normalize_group
                    'ignored': ev['ignored'],
                    'alert': ev.get('alert'),   # mode alerte : 'on' / 'off'
                    'strength': ev.get('strength', 0.0),   # pic le plus fort / bruit de fond
                })
            except Exception as e:
                logging.error(f"Erreur callback détection ({self.label}): {e}")
