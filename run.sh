#!/usr/bin/with-contenv bashio
# ==============================================================================
# ClapTrap - Detection d'applaudissements en temps reel
# ==============================================================================

bashio::log.info "Demarrage de ClapTrap..."

# Configurer PULSE_SERVER si pas deja defini
if [ -z "${PULSE_SERVER:-}" ]; then
    # Chercher le socket PulseAudio
    for path in /run/audio/pulse.sock /run/pulse/native /run/pulse/pulseaudio.socket /var/run/pulse/native; do
        if [ -e "${path}" ]; then
            export PULSE_SERVER="unix:${path}"
            bashio::log.info "PulseAudio: socket trouve (${path})"
            break
        fi
    done
fi

# Si toujours pas defini, extraire depuis pactl info
if [ -z "${PULSE_SERVER:-}" ] && command -v pactl > /dev/null 2>&1; then
    SERVER_STRING=$(timeout 3 pactl info 2>/dev/null | grep "Server String:" | sed 's/Server String: *//' || true)
    if [ -n "${SERVER_STRING}" ]; then
        export PULSE_SERVER="${SERVER_STRING}"
        bashio::log.info "PulseAudio: server string extrait de pactl (${SERVER_STRING})"
    fi
fi

bashio::log.info "PULSE_SERVER=${PULSE_SERVER:-NON DEFINI}"

# Diagnostic audio
if command -v pactl > /dev/null 2>&1; then
    timeout 3 pactl list sources short 2>&1 || true
fi

# Wyoming STT interceptor: started in-process by app.py as an asyncio thread
# when "wyoming.enabled" is true in settings. Listens on the configured port
# (default 10700) and forwards to the upstream Whisper Wyoming server.
if command -v jq > /dev/null 2>&1 && [ -f /data/settings.json ]; then
    WYOMING_ENABLED=$(jq -r '.wyoming.enabled // false' /data/settings.json 2>/dev/null || echo "false")
    WYOMING_PORT=$(jq -r '.wyoming.port // 10700' /data/settings.json 2>/dev/null || echo "10700")
    if [ "${WYOMING_ENABLED}" = "true" ]; then
        bashio::log.info "Wyoming STT interceptor actif sur le port ${WYOMING_PORT}"
    fi
fi

cd /usr/src/app || exit 1
source /usr/src/app/venv/bin/activate
exec python app.py
