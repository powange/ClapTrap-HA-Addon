# Image construite par .github/workflows/ci.yaml (actions Home Assistant, qui
# fournissent BUILD_ARCH et BUILD_VERSION) : base definie ici, epinglee par
# digest pour chaque architecture (build reproductible ; Dependabot propose
# les nouvelles versions).
ARG BUILD_ARCH=amd64
FROM ghcr.io/hassio-addons/debian-base/amd64:9.4.0@sha256:d76281c74089ccebd7a91a2872b902365015f8a5e831a925d1db33cac677d14b AS base-amd64
FROM ghcr.io/hassio-addons/debian-base/aarch64:9.4.0@sha256:4fa84082956a06c4df61d825bc4b4769a86657e14126e412a2d64bf6f563f8bc AS base-aarch64

# Stage 1: Build des dépendances Python
FROM base-${BUILD_ARCH} AS builder

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src/app
ENV VIRTUAL_ENV=/usr/src/app/venv
RUN python3 -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Versions exactes et empreintes (data/requirements.lock, genere depuis
# data/requirements.txt par scripts/lock_requirements.py) : une nouvelle
# version d'une dependance ne peut plus casser l'installation.
COPY data/requirements.lock /tmp/requirements.lock
# --no-deps : le lock est complet ; les dependances declarees de mediapipe
# (opencv-contrib, sounddevice) sont volontairement remplacees ou ecartees.
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes --no-deps -r /tmp/requirements.lock

# Stage 2: Image finale (sans build-essential)
FROM base-${BUILD_ARCH}
ARG BUILD_ARCH

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Micro : parecord/pactl (PulseAudio natif) ; RTSP : ffmpeg ; YAMNet :
# mediapipe (libgles2, libegl1). libgl1 et libglib2.0-0 sont tires par ffmpeg
# (cv2 « headless » n'en a pas besoin) ; declares pour ne pas dependre de ce
# detail. Ni PortAudio ni sounddevice (ecarte du lock).
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    ffmpeg \
    libpulse0 \
    libgles2 \
    libegl1 \
    libgl1 \
    libglib2.0-0 \
    pulseaudio-utils \
    && rm -rf /var/lib/apt/lists/*

# Copier le venv compilé depuis le builder
COPY --from=builder /usr/src/app/venv /usr/src/app/venv

WORKDIR /usr/src/app
ENV VIRTUAL_ENV=/usr/src/app/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Version de l'add-on (BUILD_VERSION fourni par l'action de build en CI)
ARG BUILD_VERSION
ENV CLAPTRAP_VERSION=${BUILD_VERSION}
# Cache de polices de matplotlib (importe par mediapipe) cree au build : il
# etait regenere a chaque demarrage du conteneur.
ENV MPLCONFIGDIR=/usr/src/app/.matplotlib
RUN python -c "import matplotlib.pyplot" && chmod -R a+rX /usr/src/app/.matplotlib

# Autotest du vrai YAMNet : classification et decoupage en fenetres (la
# datation des claps en depend ; les tests unitaires utilisent un faux).
# Fichiers montes depuis le contexte (pas de couche) : l'etape ne se relance
# que si l'un d'eux, ou le venv, change.
RUN --mount=type=bind,source=data/selftest_yamnet.py,target=/selftest/selftest_yamnet.py \
    --mount=type=bind,source=data/audio_detector.py,target=/selftest/audio_detector.py \
    --mount=type=bind,source=data/audio_utils.py,target=/selftest/audio_utils.py \
    --mount=type=bind,source=data/clap_logic.py,target=/selftest/clap_logic.py \
    --mount=type=bind,source=data/yamnet.tflite,target=/selftest/yamnet.tflite \
    PYTHONDONTWRITEBYTECODE=1 python /selftest/selftest_yamnet.py

# Delai laisse aux services a l'arret avant SIGKILL (3 s par defaut) : arret
# propre de Gunicorn (graceful_timeout 6 s), publications MQTT comprises.
ENV S6_KILL_GRACETIME=9000

# Copier les fichiers de l'application
COPY data/ ./
# .pyc precompiles : Python ne tente plus d'ecrire dans /usr/src/app a chaque
# demarrage (refuse par AppArmor en mode strict).
RUN python -m compileall -q -x '/(venv|tests|\.matplotlib)/' /usr/src/app
# Test de fumee : lock complet (installe en --no-deps) et imports reels. Une
# nouvelle dependance de mediapipe absente du lock echoue ici, pas chez
# l'utilisateur.
RUN out="$(pip check 2>&1 || true)"; \
    bad="$(echo "$out" | grep -vE 'opencv-contrib-python|sounddevice' | grep . || true)"; \
    if [ -n "$bad" ]; then echo "$bad"; exit 1; fi; \
    python -c "from mediapipe.tasks.python import audio; import cv2, matplotlib.pyplot, flask_socketio, paho.mqtt.client, simple_websocket"

# Copier le script de démarrage
COPY run.sh /
RUN chmod a+x /run.sh

# Labels (version, architecture, source, date et revision : poses par
# l'action de build ; licence de l'image de base effacee, le depot n'en a pas)
LABEL \
    io.hass.name="ClapTrap" \
    io.hass.description="Application de détection d'applaudissements en temps réel utilisant YAMNet" \
    io.hass.type="addon" \
    maintainer="powange" \
    org.opencontainers.image.title="ClapTrap" \
    org.opencontainers.image.description="Détection d'applaudissements et de sons (YAMNet) pour Home Assistant" \
    org.opencontainers.image.authors="powange" \
    org.opencontainers.image.vendor="powange" \
    org.opencontainers.image.url="https://github.com/powange/ClapTrap-HA-Addon" \
    org.opencontainers.image.documentation="https://github.com/powange/ClapTrap-HA-Addon/blob/main/DOCS.md" \
    org.opencontainers.image.licenses=""

CMD [ "/run.sh" ]
