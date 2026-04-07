#!/usr/bin/with-contenv bashio
# ==============================================================================
# ClapTrap - Detection d'applaudissements en temps reel
# ==============================================================================

bashio::log.info "Demarrage de ClapTrap..."

# Chercher le socket PulseAudio (HA peut le monter à différents endroits)
PULSE_SOCKET=""
for path in /run/pulse/native /run/pulse/pulseaudio.socket /var/run/pulse/native; do
    if [ -e "${path}" ]; then
        PULSE_SOCKET="${path}"
        break
    fi
done

if [ -n "${PULSE_SOCKET}" ]; then
    export PULSE_SERVER="unix:${PULSE_SOCKET}"
    bashio::log.info "PulseAudio: socket trouve (${PULSE_SOCKET})"
elif [ -n "${PULSE_SERVER:-}" ]; then
    bashio::log.info "PulseAudio: PULSE_SERVER deja defini (${PULSE_SERVER})"
else
    # Pas de socket, essayer TCP vers le host audio HA
    export PULSE_SERVER="tcp:172.30.32.1"
    bashio::log.warning "PulseAudio: aucun socket trouve, tentative TCP (${PULSE_SERVER})"
fi

bashio::log.info "PULSE_SERVER=${PULSE_SERVER}"

# Diagnostic audio (avec timeout pour ne pas bloquer)
if command -v pactl > /dev/null 2>&1; then
    timeout 3 pactl list sources short 2>/dev/null && bashio::log.info "Sources PulseAudio listees" || bashio::log.warning "pactl: sources non disponibles au demarrage (normal, PulseAudio sera disponible plus tard)"
fi

cd /usr/src/app || exit 1

# Activer l'environnement virtuel Python
source /usr/src/app/venv/bin/activate

# Lancer l'application
exec python app.py
