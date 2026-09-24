# BUILD_FROM est fourni par le Supervisor (build.yaml) ; les actions GitHub
# de Home Assistant (BuildKit) ne fournissent que BUILD_ARCH, d'ou le defaut.
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
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes -r /tmp/requirements.lock

# Stage 2: Image finale (sans build-essential)
FROM ${BUILD_FROM}
ARG BUILD_ARCH

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Micro : parecord/pactl (PulseAudio natif) ; RTSP : ffmpeg ; YAMNet :
# mediapipe (libgles2, libegl1), dont l'import charge cv2 (libgl1,
# libglib2.0-0 : declares ici, ils n'etaient presents que parce que ffmpeg les
# tirait). Pas de PortAudio : sounddevice, installe par mediapipe, n'est jamais
# importe.
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
# Delai laisse aux services a l'arret avant SIGKILL (3 s par defaut) : arret
# propre de Gunicorn (graceful_timeout 6 s), publications MQTT comprises.
ENV S6_KILL_GRACETIME=9000

# Copier les fichiers de l'application
COPY data/ ./

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
