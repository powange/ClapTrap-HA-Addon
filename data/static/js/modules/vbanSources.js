import { callApi } from './api.js';
import { showNotification, showSuccess, showError } from './notifications.js';

let vbanSources = [];
let savedVbanSources = [];

export function initVbanSources() {
    const refreshBtn = document.getElementById('refreshVBANBtn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', () => {
            refreshVbanSources();
        });
    }

    // Charger les sources initiales
    refreshVbanSources();
    refreshSavedVbanSources();  // Charger aussi les sources sauvegardées au démarrage
}

export function refreshVbanSources() {
    const detectedSourcesContainer = document.getElementById('detectedVBANSources');
    if (!detectedSourcesContainer) {
        console.error('Container detectedVBANSources non trouvé');
        return;
    }

    const refreshBtn = document.getElementById('refreshVBANBtn');
    if (refreshBtn) {
        refreshBtn.classList.add('rotating');
    }

    fetch((window.basePath || '') + '/api/vban/sources')
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            return response.json();
        })
        .then(sources => {
            console.log('Sources VBAN reçues:', sources);
            detectedSourcesContainer.innerHTML = '';

            if (!Array.isArray(sources) || sources.length === 0) {
                detectedSourcesContainer.innerHTML = `
                    <div class="list-group-item text-muted">Aucune source VBAN détectée</div>
                `;
                return;
            }

            const template = document.getElementById('vbanDetectedSourceTemplate');
            if (!template) {
                throw new Error('Template vbanDetectedSourceTemplate non trouvé');
            }

            sources.forEach(source => {
                console.log('Traitement de la source:', source);  // Debug log
                const clone = template.content.cloneNode(true);
                
                clone.querySelector('.source-name').textContent = source.name || source.stream_name;
                clone.querySelector('.source-ip').textContent = source.ip;
                clone.querySelector('.source-port').textContent = source.port;
                
                const addButton = clone.querySelector('.add-vban-btn');
                if (addButton) {
                    addButton.setAttribute('data-source-id', source.id);
                    addButton.addEventListener('click', () => {
                        console.log('Ajout de la source:', source);  // Debug log
                        saveVBANSource(source);
                    });
                }

                detectedSourcesContainer.appendChild(clone);
            });
        })
        .catch(error => {
            console.error('Erreur lors de la récupération des sources VBAN:', error);
            detectedSourcesContainer.innerHTML = `
                <div class="list-group-item text-danger">
                    Erreur lors de la récupération des sources VBAN: ${error.message}
                </div>
            `;
        })
        .finally(() => {
            if (refreshBtn) {
                refreshBtn.classList.remove('rotating');
            }
        });
}

export function refreshSavedVbanSources() {
    const savedSourcesContainer = document.getElementById('savedVBANSources');
    if (!savedSourcesContainer) {
        console.error('Container savedVBANSources non trouvé');
        return;
    }

    fetch((window.basePath || '') + '/api/vban/saved-sources')
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            return response.json();
        })
        .then(sources => {
            console.log('Sources VBAN sauvegardées:', sources);
            savedVbanSources = sources;

            if (!Array.isArray(sources) || sources.length === 0) {
                savedSourcesContainer.innerHTML = `
                    <div class="list-group-item text-muted">Aucune source VBAN configurée</div>
                `;
                return;
            }

            const template = document.getElementById('vbanSavedSourceTemplate');
            if (!template) {
                throw new Error('Template vbanSavedSourceTemplate non trouvé');
            }

            savedSourcesContainer.innerHTML = '';
            sources.forEach(source => {
                const clone = template.content.cloneNode(true);
                
                clone.querySelector('.source-name').textContent = source.name;
                clone.querySelector('.source-ip').textContent = source.ip;
                clone.querySelector('.source-port').textContent = source.port;
                clone.querySelector('.source-enabled').checked = source.enabled;
                clone.querySelector('.webhook-url').value = source.webhook_url || '';
                
                // Configurer le bouton de test
                const testButton = clone.querySelector('.test-webhook');
                if (testButton) {
                    testButton.setAttribute('data-source', `vban-${source.name}`);
                }

                // Configurer le bouton de suppression
                const deleteButton = clone.querySelector('.delete-vban-btn');
                if (deleteButton) {
                    deleteButton.addEventListener('click', () => {
                        if (confirm(`Voulez-vous vraiment supprimer la source VBAN "${source.name}" ?`)) {
                            removeVBANSource(source);
                        }
                    });
                }

                // Configurer le switch d'activation
                const enabledSwitch = clone.querySelector('.source-enabled');
                if (enabledSwitch) {
                    enabledSwitch.addEventListener('change', (event) => {
                        source.enabled = event.target.checked;
                        updateVBANSourceEnabled(source);
                    });
                }

                // Configurer l'input webhook
                const webhookInput = clone.querySelector('.webhook-url');
                if (webhookInput) {
                    webhookInput.addEventListener('change', (event) => {
                        updateVBANSourceWebhook(source, event.target.value);
                    });
                }

                savedSourcesContainer.appendChild(clone);
            });
        })
        .catch(error => {
            console.error('Erreur lors de la récupération des sources VBAN sauvegardées:', error);
            savedSourcesContainer.innerHTML = `
                <div class="list-group-item text-danger">
                    Erreur lors de la récupération des sources VBAN configurées: ${error.message}
                </div>
            `;
        });
}

function removeVBANSource(source) {
    fetch((window.basePath || '') + '/api/vban/remove', {
        method: 'DELETE',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({
            ip: source.ip,
            stream_name: source.stream_name || source.name
        })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showSuccess('Source VBAN supprimée avec succès');
            refreshSavedVbanSources();
        } else {
            throw new Error(data.error || 'Erreur lors de la suppression de la source');
        }
    })
    .catch(error => {
        console.error('Erreur lors de la suppression de la source VBAN:', error);
        showError(error.message);
    });
}

function saveVBANSource(source) {
    fetch((window.basePath || '') + '/api/vban/save', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(source)
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showSuccess('Source VBAN ajoutée avec succès');
            refreshVbanSources();
            refreshSavedVbanSources();  // Rafraîchir aussi la liste des sources sauvegardées
        } else {
            throw new Error(data.error || 'Erreur lors de l\'ajout de la source');
        }
    })
    .catch(error => {
        console.error('Erreur lors de la sauvegarde de la source VBAN:', error);
        showError(error.message);
    });
}

function updateVBANSourceWebhook(source, webhookUrl) {
    fetch((window.basePath || '') + '/api/vban/update', {
        method: 'PUT',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({
            ip: source.ip,
            name: source.name,
            webhook_url: webhookUrl
        })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showSuccess('Webhook mis à jour');
        } else {
            throw new Error(data.error || 'Erreur lors de la mise à jour du webhook');
        }
    })
    .catch(error => {
        console.error('Erreur lors de la mise à jour du webhook:', error);
        showError(error.message);
    });
}

function updateVBANSourceEnabled(source) {
    // Le champ `enabled` DOIT etre envoye explicitement : sans lui le backend
    // ne persiste pas l'etat (la source reste dans la liste des sources
    // activees) et ne redemarre pas la detection pour (re)ajouter/retirer la
    // source. On rafraichit ensuite la liste pour refleter l'etat persiste.
    fetch((window.basePath || '') + '/api/vban/update', {
        method: 'PUT',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({
            ip: source.ip,
            name: source.name,
            enabled: source.enabled
        })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showSuccess(source.enabled ? 'Source VBAN activée' : 'Source VBAN désactivée');
            refreshSavedVbanSources();
        } else {
            throw new Error(data.error || 'Erreur lors de la mise à jour de la source');
        }
    })
    .catch(error => {
        console.error('Erreur lors de la mise à jour de la source VBAN:', error);
        showError(error.message);
    });
}

// Autres fonctions spécifiques aux sources VBAN...