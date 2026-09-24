# Image construite par .github/workflows/ci.yaml (actions Home Assistant, qui
# ne fournissent que BUILD_ARCH) : base unique definie ici.
ARG BUILD_ARCH=amd64
ARG BUILD_FROM=ghcr.io/hassio-addons/debian-base/${BUILD_ARCH}:9.4.0

# Stage 1: Build des dépendances Python
FROM ${BUILD_FROM} AS builder

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
FROM ${BUILD_FROM}
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

# Version de l'add-on (BUILD_VERSION est fourni par le Supervisor au build)
ARG BUILD_VERSION
ENV CLAPTRAP_VERSION=${BUILD_VERSION}
# Cache de polices de matplotlib (importe par mediapipe) cree au build : il
# etait regenere a chaque demarrage du conteneur.
ENV MPLCONFIGDIR=/usr/src/app/.matplotlib
RUN python -c "import matplotlib.pyplot" && chmod -R a+rX /usr/src/app/.matplotlib

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

# Labels Home Assistant
LABEL \
    io.hass.name="ClapTrap" \
    io.hass.description="Application de détection d'applaudissements en temps réel utilisant YAMNet" \
    io.hass.type="addon" \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.arch="${BUILD_ARCH}" \
    org.opencontainers.image.source="https://github.com/powange/ClapTrap-HA-Addon"

CMD [ "/run.sh" ]
