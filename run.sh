#!/usr/bin/with-contenv bashio
# ==============================================================================
# ClapTrap - Détection d'applaudissements en temps réel
# ==============================================================================

bashio::log.info "Démarrage de ClapTrap..."

cd /usr/src/app || exit 1

# Activer l'environnement virtuel Python
source /usr/src/app/venv/bin/activate

# Lancer l'application
exec python app.py
