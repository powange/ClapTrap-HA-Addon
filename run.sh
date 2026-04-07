#!/usr/bin/with-contenv bashio
# ==============================================================================
# ClapTrap - Detection d'applaudissements en temps reel
# ==============================================================================

bashio::log.info "Demarrage de ClapTrap..."

# Configurer PulseAudio pour utiliser le daemon HA
if [ -e /run/pulse/native ]; then
    export PULSE_SERVER=unix:/run/pulse/native
    bashio::log.info "PulseAudio: utilisation du socket HA (/run/pulse/native)"
elif [ -n "${PULSE_SERVER:-}" ]; then
    bashio::log.info "PulseAudio: PULSE_SERVER deja defini (${PULSE_SERVER})"
else
    export PULSE_SERVER=unix:/run/pulse/native
    bashio::log.warning "PulseAudio: socket /run/pulse/native absent, tentative quand meme"
fi

bashio::log.info "PULSE_SERVER=${PULSE_SERVER}"

# Diagnostic audio (non bloquant)
if command -v pactl > /dev/null 2>&1; then
    pactl list sources short 2>/dev/null && bashio::log.info "Sources PulseAudio listees ci-dessus" || bashio::log.warning "pactl: impossible de lister les sources (PulseAudio pas encore pret ?)"
fi

cd /usr/src/app || exit 1

# Activer l'environnement virtuel Python
source /usr/src/app/venv/bin/activate

# Lancer l'application
exec python app.py
