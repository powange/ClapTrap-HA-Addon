# Image construite par .github/workflows/ci.yaml (actions Home Assistant, qui
# fournissent BUILD_ARCH et BUILD_VERSION) : base definie ici, epinglee par
# digest pour chaque architecture (build reproductible ; Dependabot propose
# les nouvelles versions).
ARG BUILD_ARCH=amd64
FROM ghcr.io/hassio-addons/debian-base/amd64:9.4.0@sha256:d76281c74089ccebd7a91a2872b902365015f8a5e831a925d1db33cac677d14b AS base-amd64
FROM ghcr.io/hassio-addons/debian-base/aarch64:9.4.0@sha256:4fa84082956a06c4df61d825bc4b4769a86657e14126e412a2d64bf6f563f8bc AS base-aarch64

# Stage 1: ffmpeg minimal, audio seulement. L'add-on ne s'en sert que pour
# lire l'audio d'un flux rtsp:// ou rtsps:// (audio_sources.rtsp_source) :
# celui de Debian tirait Mesa, LLVM, SDL, x265... (~300 Mo installes).
# Decodeurs : codecs audio des cameras IP (AAC, G.711, G.726, G.722, Opus,
# MP3, AMR, PCM...). Un codec absent donne « no decoder found for: <codec> »
# au journal ; l'ajouter a FFMPEG_DECODERS.
FROM base-${BUILD_ARCH} AS ffmpeg
ARG FFMPEG_VERSION=9.0.2
ARG FFMPEG_SHA256=8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e
ARG FFMPEG_DECODERS=aac,aac_fixed,aac_latm,pcm_mulaw,pcm_alaw,pcm_s16be,pcm_s16le,pcm_s24be,pcm_u8,adpcm_g726,adpcm_g726le,adpcm_g722,opus,mp3,mp3float,mp2,amrnb,amrwb,gsm,gsm_ms,ac3,vorbis,speex
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    nasm \
    pkg-config \
    libssl-dev \
    xz-utils \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /tmp/ffmpeg
RUN curl -fsSL "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz" -o ffmpeg.tar.xz \
    && echo "${FFMPEG_SHA256}  ffmpeg.tar.xz" | sha256sum -c - \
    && tar -xJf ffmpeg.tar.xz --strip-components=1 \
    && ./configure \
        --prefix=/opt/ffmpeg \
        --enable-version3 \
        --disable-everything \
        --disable-autodetect \
        --disable-doc \
        --disable-debug \
        --disable-ffplay \
        --disable-ffprobe \
        --disable-avdevice \
        --disable-swscale \
        --enable-openssl \
        --enable-protocol=rtp,udp,tcp,tls,pipe \
        --enable-demuxer=rtsp,rtp,sdp \
        --enable-decoder="${FFMPEG_DECODERS}" \
        --enable-parser=aac,aac_latm,mpegaudio,opus,ac3,vorbis \
        --enable-encoder=pcm_f32le \
        --enable-muxer=pcm_f32le \
        --enable-filter=aresample,aformat,anull,abuffer,abuffersink \
    && make -j"$(nproc)" \
    && make install \
    && strip /opt/ffmpeg/bin/ffmpeg

# Stage 2: Build des dépendances Python
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
# Lock complet (installe en --no-deps) : une nouvelle dependance de mediapipe
# absente du lock echoue ici, pas chez l'utilisateur. Puis pip retire du venv
# (12 Mo, inutile a l'execution).
RUN out="$(pip check 2>&1 || true)"; \
    bad="$(echo "$out" | grep -vE 'opencv-contrib-python|sounddevice' | grep . || true)"; \
    if [ -n "$bad" ]; then echo "$bad"; exit 1; fi; \
    pip uninstall -y -q pip

# Stage 3: Image finale (sans build-essential)
FROM base-${BUILD_ARCH}
ARG BUILD_ARCH

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Micro : parecord/pactl (PulseAudio natif) ; RTSP : ffmpeg minimal (stage
# 1, OpenSSL pour rtsps://) ; YAMNet : mediapipe lie libEGL et libGLESv2
# (glvnd) sans s'en servir pour l'audio. libegl1 est extrait sans ses
# dependances : il tire Mesa et LLVM (libegl-mesa0) ; meme depot, meme
# version que libglvnd0 installe a cote. cv2 « headless » n'a besoin
# d'aucune bibliotheque graphique. Ni PortAudio ni sounddevice.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    libssl3t64 \
    libpulse0 \
    libgles2 \
    pulseaudio-utils \
    && cd /tmp && apt-get download libegl1 \
    && dpkg-deb -x libegl1_*.deb / && rm libegl1_*.deb \
    && ldconfig \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ffmpeg /opt/ffmpeg/bin/ffmpeg /usr/local/bin/ffmpeg
# Ce que l'add-on demande a ffmpeg (audio_sources.rtsp_source) : demuxeur
# rtsp, sortie f32le, decodeurs audio courants. Sorties lues en entier (un
# grep -q qui coupe le tube ferait echouer ffmpeg sous pipefail).
RUN version="$(ffmpeg -hide_banner -version)"; echo "${version%%$'\n'*}"; \
    formats="$(ffmpeg -hide_banner -formats 2>/dev/null)"; \
    decoders="$(ffmpeg -hide_banner -decoders 2>/dev/null)"; \
    grep -qE '^ +D.* rtsp ' <<< "$formats" || { echo "demuxeur rtsp absent"; exit 1; }; \
    grep -qE '^ +.E.* f32le ' <<< "$formats" || { echo "sortie f32le absente"; exit 1; }; \
    for d in aac pcm_mulaw pcm_alaw opus g726 g722 mp3; do \
        grep -qE " $d " <<< "$decoders" || { echo "decodeur $d absent"; exit 1; }; \
    done

# Copier le venv compilé depuis le builder
COPY --from=builder /usr/src/app/venv /usr/src/app/venv
# Aucune bibliotheque systeme manquante (paquets apt reduits au strict
# necessaire), avant tout usage du venv : message clair plutot qu'un
# ImportError de l'autotest.
RUN missing="$(find /usr/src/app/venv -name '*.so*' -type f -print0 \
        | xargs -0 -r ldd /usr/local/bin/ffmpeg 2>/dev/null | grep 'not found' | sort -u || true)"; \
    if [ -n "$missing" ]; then echo "$missing"; exit 1; fi

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
# Test de fumee : imports reels.
RUN python -c "from mediapipe.tasks.python import audio; import cv2, matplotlib.pyplot, flask_socketio, paho.mqtt.client, simple_websocket"

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
