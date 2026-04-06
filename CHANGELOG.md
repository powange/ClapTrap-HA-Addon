# Changelog

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
