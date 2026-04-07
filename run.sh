#!/usr/bin/with-contenv bashio
# ==============================================================================
# ClapTrap - Detection d'applaudissements en temps reel
# ==============================================================================

bashio::log.info "Demarrage de ClapTrap..."

# HA definit PULSE_SERVER automatiquement quand audio: true dans config.yaml
# Ne pas surcharger si deja defini par le systeme
if [ -z "${PULSE_SERVER:-}" ]; then
    # Chercher le socket PulseAudio manuellement
    for path in /run/pulse/native /run/pulse/pulseaudio.socket /var/run/pulse/native; do
        if [ -e "${path}" ]; then
            export PULSE_SERVER="unix:${path}"
            break
        fi
    done
fi

bashio::log.info "PULSE_SERVER=${PULSE_SERVER:-NON DEFINI}"

# Diagnostic : lister tout ce qui est dans /run/pulse/
bashio::log.info "Contenu de /run/pulse/:"
ls -la /run/pulse/ 2>/dev/null || bashio::log.warning "/run/pulse/ n'existe pas"

# Diagnostic audio (avec timeout)
if command -v pactl > /dev/null 2>&1; then
    timeout 3 pactl info 2>&1 | head -3 || true
    timeout 3 pactl list sources short 2>&1 || true
fi

cd /usr/src/app || exit 1
source /usr/src/app/venv/bin/activate
exec python app.py
