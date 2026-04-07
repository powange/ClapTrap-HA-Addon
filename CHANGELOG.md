# Changelog

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
