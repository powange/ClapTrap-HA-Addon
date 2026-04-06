ARG BUILD_FROM
FROM ${BUILD_FROM}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Installation des dépendances système
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    libasound-dev \
    libportaudio2 \
    libportaudiocpp0 \
    portaudio19-dev \
    python3-opencv \
    ffmpeg \
    libasound2-plugins \
    && rm -rf /var/lib/apt/lists/*

# Créer et activer un environnement virtuel Python
WORKDIR /usr/src/app
ENV VIRTUAL_ENV=/usr/src/app/venv
RUN python3 -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Installer les dépendances Python
COPY data/requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt

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
