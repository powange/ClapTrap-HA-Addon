# Changelog

## 3.0.3

- Fix : le test micro utilise maintenant le device selectionne dans le dropdown (pas celui sauvegarde). Permet de tester un micro avant de sauvegarder.

## 3.0.2

- Fix PulseAudio : le socket est a /run/audio/pulse.sock (pas /run/pulse/native) dans les addons HA
- Fallback : si aucun socket trouve, extrait le Server String de `pactl info` (qui fonctionne via la config interne du conteneur)
- Applique dans run.sh et app.py

## 3.0.1

- Fix : quand la detection demarre en auto-start, l'UI affiche maintenant le bouton "Arreter" au lieu de "Demarrer"
- L'UI verifie l'etat de detection au chargement de la page via /status
- L'auto-start emet un event socket `detection_status` pour synchroniser les clients connectes

## 3.0.0

### Nouvelles fonctionnalites

- **Detection multi-claps** : compte le nombre de claps dans une fenetre de 1.5s. Les events incluent `clap_count` (1, 2, 3...). Permet des automations differenciees (2 claps = lumiere, 3 claps = scene).
- **Auto-start** : toggle dans la config pour demarrer la detection automatiquement au boot de l'addon (delai de 3s pour PulseAudio).
- **Events Home Assistant natifs** : chaque clap emet un evenement `claptrap_clap` via l'API Supervisor. Plus besoin de webhooks pour les automations HA simples.
- **Webhook avec payload** : les webhooks envoient maintenant un JSON `{event, source_id, timestamp, score, clap_count}` au lieu d'un POST vide.
- **Historique des detections** : endpoint GET /api/detections/history retourne les 50 dernieres detections.
- **Seuil en temps reel** : le score du classifier s'affiche a cote du slider Precision pour aider au reglage.

### Ameliorations detection

- **Scoring pondere** : labels Hands (0.8) et Clapping (1.0) avec penalite Finger snapping (0.5) et Writing (0.3). Suppression de "Cap gun" (faux positifs).
- **Seuil configurable** : le score_threshold et le delay utilisateur sont maintenant effectivement passes au classifier (avant ils etaient ignores).

### Architecture

- **Fix import circulaire** : `get_audio_input_devices()` extrait dans `audio_utils.py` (plus d'import app.py depuis classify.py).
- **Drain stderr parecord** : thread daemon qui vide stderr pour eviter que parecord se bloque quand le pipe est plein.
- **Debounce ecriture auto-volume** : le volume n'est persiste sur disque que toutes les 30s (au lieu de chaque seconde). Reduit l'usure SD card.
- **Code mort supprime** : `vban_signal_processor.py` (300 lignes jamais importees), route dupliquee `/refresh_vban`.
- **Types normalises** : threshold et delay stockes en float (plus en string), device_index en int.

## 2.5.0

### Nouvelle fonctionnalite : Volume automatique du micro (AGC)

- Toggle "Auto" a cote du slider de volume du micro
- Quand active, le systeme ajuste automatiquement le volume PulseAudio pour maintenir un niveau de signal optimal pour la detection de claps
- Algorithme : si le signal est trop faible (peak < 0.005) le volume augmente de 5%, si trop fort (peak > 0.3) il diminue de 10%, intervalle d'ajustement de 1 seconde
- Le slider se met a jour en temps reel via socket.io quand l'AGC ajuste
- Le slider manuel est desactive quand l'auto-volume est actif
- L'AGC piggybacke sur les donnees parecord de la detection (pas de second processus)
- Nouvel endpoint API PUT /api/microphone/auto-volume
- L'auto-volume s'arrete automatiquement quand la detection s'arrete

## 2.4.0

### Fix : 6 bugs critiques dans le pipeline de detection

- **detection_running jamais reset** : quand le thread de detection mourait (parecord crash, erreur...), `detection_running` restait a `True`, empechant tout redemarrage sans restart de l'addon. Ajout d'un `finally` qui reset le flag.
- **Collision de timestamps** : `time.time()` dans le calcul de timestamp causait des collisions qui faisaient silencieusement dropper des resultats du classifier. Remplace par un compteur monotone.
- **score_threshold ignore** : `detector.initialize()` etait appele sans les parametres utilisateur (`max_results`, `score_threshold`), utilisant toujours les valeurs par defaut. Le seuil configure dans l'UI n'avait aucun effet.
- **stderr.read() bloquant** : si parecord fermait stdout sans fermer stderr, `proc.stderr.read()` bloquait indefiniment le thread de detection. Limite a 4096 bytes.
- **detection.js ecoutait le mauvais event** : ecoutait `'detection_event'` au lieu de `'clap'` (ce que le backend emet). L'animation de detection dans l'UI ne se declenchait jamais via ce module.
- Ajout de logging diagnostique dans le pipeline de detection

## 2.3.7

- Ajout de logging INFO dans le flux de detection micro : nombre de blocs lus, peak, etat du detector
- Ajout de logging INFO dans le classifier : resultats recus, top labels, mapping source
- Permet de diagnostiquer si parecord envoie des donnees et si le classifier les traite

## 2.3.6

- Fix "Connection refused" : ne plus surcharger PULSE_SERVER si deja defini par le systeme HA (s6/contenv)
- Ajout diagnostic : listing de /run/pulse/, pactl info, pactl list sources au demarrage
- Suppression du fallback TCP qui ne fonctionnait pas

## 2.3.5

- Remplacement de sounddevice/PortAudio par parecord pour la capture audio micro
- parecord utilise directement libpulse (comme pactl) et bypass la chaine PortAudio/ALSA qui ne voyait aucun device dans le conteneur HA
- Le device TONOR selectionne est passe via --device=pulse_name a parecord
- Applique au test micro ET a la detection

## 2.3.4

- Fix "Error querying device -1" : sounddevice ne trouvait aucun device par defaut. Le code cherche maintenant explicitement un device "pulse" ou "default" dans la liste des devices disponibles, au lieu de passer device=None (qui echoue quand PortAudio n'a pas de default configure).
- Meme fix applique au test micro et a la detection.

## 2.3.3

- Recherche du socket PulseAudio a plusieurs chemins (/run/pulse/native, /run/pulse/pulseaudio.socket, /var/run/pulse/native)
- Fallback TCP vers 172.30.32.1 (host audio HA) si aucun socket unix trouve
- Ajout de timeout sur pactl pour eviter de bloquer le demarrage
- Meme logique de fallback dans app.py

## 2.3.2

- Fix crash au demarrage : le diagnostic pactl dans run.sh pouvait echouer si PulseAudio n'etait pas pret, causant un exit sous bashio (set -e implicite). Le diagnostic est maintenant non-bloquant.

## 2.3.1

- Fix barre VU-metre invisible : fond de track plus contraste, gradient fixe (vert→jaune→rouge proportionnel a la largeur totale), hauteur augmentee, min-width pour toujours afficher un indicateur

## 2.3.0

### Fix majeur : la detection micro fonctionne enfin

- **Bridge ALSA→PulseAudio** : ajout de `libpulse0` dans le Dockerfile et creation de `/etc/asound.conf` pour router ALSA default vers PulseAudio. Sans ca, sounddevice/PortAudio parlait directement a ALSA et ignorait PULSE_SOURCE, recevant du silence.
- **PULSE_SERVER** : `run.sh` configure automatiquement `PULSE_SERVER=unix:/run/pulse/native` pour se connecter au daemon PulseAudio de Home Assistant. Fallback dans `app.py` si non defini.
- **Diagnostic audio au demarrage** : `run.sh` log les infos PulseAudio et les sources disponibles.
- **Fix priorite sources** : RTSP ne prend plus priorite sur le micro quand le micro est active.
- **Fix pulse_name stale** : `_resolve_pulse_name()` verifie maintenant que le pulse_name en cache correspond toujours au device selectionne, et le re-resout sinon.
- **Fix double-reload settings** : `start_detection()` n'ecrase plus l'audio_source avec une relecture du disque.

## 2.2.35

- Layout passe en single-column (flex) pour s'adapter aux panneaux HA ingress etroits
- Detection circle + boutons sur une meme ligne horizontale
- Max-width 720px centre, responsive jusqu'a 320px
- Breakpoint 480px : cards compactes, inputs empiles, header reduit
- Cercle de detection reduit (100px, 80px sur mobile)

## 2.2.34

- Refonte complete de l'interface : design moderne, palette de couleurs plus douce, layout plus compact
- CSS entierement reecrit : suppression de 500+ lignes de code duplique
- Header sticky et plus fin, cards avec ombres subtiles, typographie amelioree
- Slider volume et range inputs avec nouveau style unifie
- Toggle switches plus compacts, animations plus fluides
- Responsive ameliore pour tablettes et mobiles
- Ajout de la meta viewport pour un meilleur rendu mobile

## 2.2.33

- Fix : le slider volume du micro fonctionne maintenant via le script fallback inline
- Le label pourcentage se met a jour en temps reel lors du deplacement du slider
- L'appel API PUT /api/microphone/volume est envoye au relachement du slider

## 2.2.32

- Ajout d'un slider de volume du micro dans l'interface (0% a 150%)
- Le volume est sauvegarde dans les settings et applique via pactl
- Nouvel endpoint API PUT /api/microphone/volume
- Ajout de logging diagnostique dans le callback du test micro

## 2.2.31

- Ajout de pulseaudio-utils (pactl) dans le Dockerfile
- Le volume du micro PulseAudio est automatiquement mis a 100% au demarrage du test et de la detection
- Logging des sources PulseAudio disponibles pour le diagnostic

## 2.2.30

- Fix : resolution automatique du pulse_name depuis l'API Supervisor si absent des settings
- Le pulse_name est sauvegarde automatiquement apres resolution
- Gain VU-metre augmente a 50x pour le diagnostic

## 2.2.29

- Ajout gain 10x sur le VU-metre pour compenser le niveau faible PulseAudio
- Ajout de logging du pulse_name lors du test micro

## 2.2.28

- Fix : affichage de la barre VU-metre (largeur 100% + min-width sur le track)

## 2.2.27

- Fix : ajout d'un script inline fallback pour le bouton test micro (independant des modules ES)

## 2.2.26

- Fix : bouton "Tester le micro" remis dans la section conditionnelle (cache quand micro desactive)
- Fix : delegation d'evenements pour le bouton test micro (fonctionne meme si cache au chargement)

## 2.2.25

- Fix : ajout cache-buster sur script.js pour forcer le rechargement apres mise a jour

## 2.2.24

- Fix : initialisation du test micro et Socket.IO independante de initSettings (evite que le bouton soit inactif)
- Ajout de logging console pour diagnostiquer le test micro

## 2.2.23

- Fix : bouton "Tester le micro" deplace hors de la zone conditionnelle (visible meme si le micro est desactive)

## 2.2.22

- Ajout d'un VU-metre en temps reel pour tester le microphone (bouton "Tester le micro")
- Affichage du niveau audio en dB via WebSocket

## 2.2.21

- Les parametres utilisateur (settings.json) sont maintenant stockes dans /data (volume persistant HA)
- La configuration survit aux mises a jour et rebuilds de l'addon

## 2.2.20

- Fix : utilisation de PULSE_SOURCE pour router PulseAudio vers le bon micro USB dans le container HA
- Le pulse_name du device est maintenant stocke dans les settings et passe au backend

## 2.2.19

- Fix : race condition sur detection_running avec threading.Lock (evite double demarrage)
- Fix : validation des parametres avant de modifier l'etat global
- Fix : webhook avec timeout=5s (evite blocage indefini du ThreadPoolExecutor)
- Fix : source_callback VBAN unifie sur une seule signature
- Fix : escaping JSON dans le template via tojson (securite XSS)
- Fix : load_settings retourne un deepcopy du cache (evite mutations silencieuses)
- Fix : double prefixe rtsp:// sur les URLs RTSP
- Fix : acces thread-safe a self.sources dans audio_detector._handle_result
- Fix : clean_vban_name sentinel ambigu (None au lieu de 0)
- Fix : shallow merge dans audioSources.js preservait les champs microphone existants
- Fix : fallback sur device par defaut PulseAudio si le device n'est pas visible par sounddevice

## 2.2.18

- Fix : ouverture du micro par nom au lieu de l'index numerique (PortAudio ne peut pas ouvrir les devices par index Supervisor)
- Ajout de logging des devices visibles par sounddevice pour le diagnostic

## 2.2.17

- Fix : resolution du micro par nom au lieu de l'index (les index Supervisor et sounddevice different)

## 2.2.16

- Reset de settings.json aux valeurs par defaut (suppression des donnees de test embarquees dans l'image Docker)

## 2.2.15

- Fix : la sauvegarde des parametres ne duplique plus les flux RTSP (sources RTSP exclues du syncWithDOM)

## 2.2.14

- Ajout de `libegl1` dans le Dockerfile pour corriger le crash MediaPipe (`libEGL.so.1` manquant)

## 2.2.13

- Ajout de `libgles2` dans le Dockerfile pour corriger le crash MediaPipe (`libGLESv2.so.2` manquant)

## 2.2.12

- Build 15-25 min plus rapide : ajout --prefer-binary pour pip, .dockerignore, pytest retire du runtime
- Retrait des dependances inutilisees : psutil, pyvban
- Ring buffer numpy pre-alloue dans AudioDetector (zero allocation dans le hot path)
- Import scipy.signal au niveau module (plus de resolution dynamique par appel)
- Webhooks non-bloquants via ThreadPoolExecutor (ne bloque plus le thread MediaPipe)
- Cache TTL 60s sur get_audio_input_devices() et load_flux()
- Eviction automatique des entrees perimees dans _timestamp_to_source
- Guard isEnabledFor sur les logs debug numpy
- VBAN : resample_poly au lieu de FFT, ring buffer numpy au lieu de deque
- Singleton WebhookManager (reutilise le pool de connexions HTTP)

## 2.2.11

- Ajout de la permission `hassio_api` pour corriger le 403 sur l'endpoint Supervisor `/audio/info`

## 2.2.10

- Ajout de logging pour diagnostiquer la detection des peripheriques audio via l'API Supervisor
- Meilleure gestion de la structure de reponse de l'API /audio/info

## 2.2.9

- Suppression du fichier events.py (instance SocketIO morte, code jamais appele)
- Centralisation de la gestion des parametres dans settings_manager.py (cache TTL, une seule source de verite)
- Remplacement du buffer deque par numpy array (suppression allocation list intermediaire dans le hot path)
- Validation des URLs webhook (protection SSRF dans webhook.py)
- Logs frontend gates derriere le flag debug (plus de console.log en production)
- Multi-stage Docker build (build-essential retire de l'image finale, -200 MB)
- Resampling audio anti-aliase via scipy.signal.resample_poly
- Remplacement de tous les print() par logging dans app.py et vban_manager.py
- CORS SocketIO configurable via variable d'environnement CORS_ORIGINS

## 2.2.8

- Correction d'une race condition dans AudioDetector (mauvais routage des detections entre sources)
- YAMNet n'est plus instancie deux fois (economie ~200-400 MB de RAM)
- Suppression de la lecture de settings.json a chaque requete HTTP (init VBAN au demarrage uniquement)
- Reconnexion automatique des flux RTSP avec backoff exponentiel en cas de coupure

## 2.2.7

- Securite : cle secrete Flask generee aleatoirement au lieu d'une valeur en dur
- Performance : suppression du blocage de 2 secondes lors du rafraichissement VBAN
- Docker : retrait de opencv-python et pyaudio (non utilises, -100 MB d'image)
- Securite : suppression de la route /run_tests exposee sans authentification
- Code : WebhookManager factorise dans un module partage (webhook.py)
- Nettoyage des imports morts (cv2, pyaudio, HTTPAdapter, Retry)

## 2.2.6

- Rattrapage du changelog pour toutes les versions precedentes

## 2.2.5

- Correction du cache Docker qui empechait la reconstruction
- Suppression des architectures obsoletes (armhf, armv7, i386)
- Mise a jour de pip avant installation des dependances

## 2.2.4

- Ajout de build-essential et python3-dev pour la compilation des dependances
- Versions des dependances Python relachees pour compatibilite avec l'image de base HA

## 2.2.3

- Mise en conformite avec les conventions HAOS
- Dockerfile base sur l'image HA Debian (`ghcr.io/hassio-addons/debian-base`)
- Ajout de build.yaml, run.sh, CHANGELOG.md, DOCS.md
- Labels Docker Home Assistant

## 2.2.2

- Liste les vrais peripheriques audio via l'API Supervisor HA (micro USB, etc.)
- Fallback sur sounddevice si l'API n'est pas disponible

## 2.2.1

- Ajout du support audio (`audio: true`) et USB (`usb: true`) pour la detection des micros USB
- Ajout de `libasound2-plugins` pour le pont ALSA/PulseAudio

## 2.2.0

- Ajout du support ingress Home Assistant (acces via le panneau HA en HTTPS)
- Suppression du lien webui direct
- Middleware WSGI pour gerer le prefixe ingress sur tous les assets et routes
- Toutes les URLs API et connexions SocketIO supportent l'ingress

## 2.1.0

- Version initiale avec support microphone, RTSP et VBAN
- Interface web de configuration
- Detection en temps reel via YAMNet
- Webhooks configurables par source
