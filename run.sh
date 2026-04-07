#!/usr/bin/with-contenv bashio
# ==============================================================================
# ClapTrap - Detection d'applaudissements en temps reel
# ==============================================================================

bashio::log.info "Demarrage de ClapTrap..."

# Configurer PulseAudio pour utiliser le daemon HA
# HA expose le socket PulseAudio via /run/pulse/native
if [ -e /run/pulse/native ]; then
    export PULSE_SERVER=unix:/run/pulse/native
    bashio::log.info "PulseAudio: utilisation du socket HA (/run/pulse/native)"
elif [ -n "${PULSE_SERVER:-}" ]; then
    bashio::log.info "PulseAudio: PULSE_SERVER deja defini (${PULSE_SERVER})"
else
    export PULSE_SERVER=unix:/run/pulse/native
    bashio::log.warning "PulseAudio: socket /run/pulse/native absent, tentative quand meme"
fi

# Diagnostic audio au demarrage
bashio::log.info "PULSE_SERVER=${PULSE_SERVER}"
if command -v pactl &> /dev/null; then
    pactl info 2>&1 | head -5 | while read -r line; do
        bashio::log.info "PulseAudio info: ${line}"
    done
    bashio::log.info "Sources PulseAudio disponibles:"
    pactl list sources short 2>&1 | while read -r line; do
        bashio::log.info "  ${line}"
    done
fi

cd /usr/src/app || exit 1

# Activer l'environnement virtuel Python
source /usr/src/app/venv/bin/activate

# Lancer l'application
exec python app.py
