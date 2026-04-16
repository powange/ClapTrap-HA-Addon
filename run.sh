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

cd /usr/src/app || exit 1
source /usr/src/app/venv/bin/activate
exec python app.py
