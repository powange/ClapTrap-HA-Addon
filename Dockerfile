ARG BUILD_FROM

# Stage 1: Build des dépendances Python
FROM ${BUILD_FROM} AS builder

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    build-essential \
    libasound-dev \
    libportaudio2 \
    libportaudiocpp0 \
    portaudio19-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src/app
ENV VIRTUAL_ENV=/usr/src/app/venv
RUN python3 -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

COPY data/requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --prefer-binary -r /tmp/requirements.txt

# Stage 2: Image finale (sans build-essential)
FROM ${BUILD_FROM}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-venv \
    libportaudio2 \
    libportaudiocpp0 \
    ffmpeg \
    libasound2-plugins \
    libgles2 \
    libegl1 \
    pulseaudio-utils \
    && rm -rf /var/lib/apt/lists/*

# Copier le venv compilé depuis le builder
COPY --from=builder /usr/src/app/venv /usr/src/app/venv

WORKDIR /usr/src/app
ENV VIRTUAL_ENV=/usr/src/app/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

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
    io.hass.version="DEV"

CMD [ "/run.sh" ]
