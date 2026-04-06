# Changelog

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
