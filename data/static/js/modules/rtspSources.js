import { callApi } from './api.js';
import { showError, showSuccess } from './utils.js';

let rtspStreams = [];

export async function initRtspSources() {
    try {
        rtspStreams = await callApi('/api/rtsp/streams', 'GET');
        setupRtspStreams();
    } catch (error) {
        showError('Erreur lors du chargement des flux RTSP');
    }
}

function setupRtspStreams() {
    const container = document.getElementById('rtspStreamsContainer');
    if (!container) return;

    // Vider le conteneur
    container.innerHTML = '';

    // Ajouter le bouton pour ajouter un nouveau flux
    const addButton = document.createElement('button');
    addButton.className = 'btn btn-primary mb-3';
    addButton.textContent = 'Ajouter un flux RTSP';
    addButton.onclick = showAddStreamForm;
    container.appendChild(addButton);

    // Afficher les flux existants
    rtspStreams.forEach((stream, index) => {
        const streamElement = createStreamElement(stream);
        container.appendChild(streamElement);
    });
}

function createStreamElement(stream) {
    const div = document.createElement('div');
    div.className = 'list-group-item';
    div.innerHTML = `
        <div class="d-flex justify-content-between align-items-center">
            <div>
                <input type="text" class="rtsp-name-input"
                       value="${stream.name || 'Flux RTSP'}"
                       data-id="${stream.id}"
                       placeholder="Nom du flux">
                <span class="rtsp-status-dot" data-url="${stream.url}" title="Status inconnu"></span>
            </div>
            <div class="source-controls">
                <label class="switch" title="Activer/Désactiver le flux">
                    <input type="checkbox" class="stream-enabled" data-id="${stream.id}" ${stream.enabled ? 'checked' : ''}>
                    <span class="slider round"></span>
                </label>
                <button type="button" class="btn btn-light btn-sm delete-vban-btn" data-id="${stream.id}">
                    <span class="icon" style="color: #dc3545;">❌</span>
                </button>
            </div>
        </div>
        <div class="webhook-input-group mt-2">
            <label class="form-label">URL RTSP</label>
            <input type="url" class="webhook-input rtsp-url" 
                   value="${stream.url}" 
                   data-id="${stream.id}"
                   placeholder="rtsp://votre-camera:port/flux">
        </div>
        <div class="webhook-input-group mt-2">
            <label class="form-label">URL Webhook</label>
            <div class="webhook-input-with-test">
                <input type="url" class="webhook-input webhook-url"
                       value="${stream.webhook_url || ''}"
                       data-id="${stream.id}"
                       placeholder="https://votre-serveur.com/webhook">
                <button type="button" class="test-webhook" data-source="rtsp-${stream.id}">
                    <span class="icon">🔔</span>
                    Tester
                </button>
            </div>
        </div>
        <div class="mic-volume-section">
            <div class="mic-volume-header">
                <label>Gain audio : <span class="rtsp-gain-value" data-id="${stream.id}">${stream.gain || 10}x</span></label>
            </div>
            <input type="range" class="mic-volume-slider rtsp-gain" min="1" max="50" step="1"
                   value="${stream.gain || 10}" data-id="${stream.id}">
        </div>
        <div class="mic-test-section">
            <button type="button" class="btn-mic-test rtsp-test-btn" data-url="${stream.url}" data-id="${stream.id}">
                🔊 Tester le flux
            </button>
            <div class="vu-meter rtsp-test-vu" data-id="${stream.id}" style="display: none;">
                <div class="vu-meter-track">
                    <div class="vu-meter-bar rtsp-test-bar" data-id="${stream.id}"></div>
                </div>
                <span class="vu-meter-label rtsp-test-db" data-id="${stream.id}">-60 dB</span>
            </div>
        </div>
    `;

    // Ajouter les écouteurs d'événements
    setupStreamEventListeners(div, stream);

    return div;
}

function setupStreamEventListeners(element, stream) {
    // Nom du flux
    const nameInput = element.querySelector('.rtsp-name-input');
    let nameTimeout;
    nameInput.addEventListener('input', (e) => {
        clearTimeout(nameTimeout);
        nameTimeout = setTimeout(() => {
            updateStream(stream.id, { ...stream, name: e.target.value.trim() });
        }, 500);
    });

    // URL RTSP
    const urlInput = element.querySelector('.rtsp-url');
    let urlTimeout;
    urlInput.addEventListener('input', (e) => {
        clearTimeout(urlTimeout);
        urlTimeout = setTimeout(() => {
            const updatedStream = { ...stream, url: e.target.value.trim() };
            updateStream(stream.id, updatedStream);
        }, 500);
    });

    // Webhook URL
    const webhookInput = element.querySelector('.webhook-url');
    let webhookTimeout;
    webhookInput.addEventListener('input', (e) => {
        clearTimeout(webhookTimeout);
        webhookTimeout = setTimeout(() => {
            const updatedStream = { ...stream, webhook_url: e.target.value.trim() };
            updateStream(stream.id, updatedStream);
        }, 500);
    });

    // Gain slider
    const gainSlider = element.querySelector('.rtsp-gain');
    const gainLabel = element.querySelector('.rtsp-gain-value');
    if (gainSlider) {
        gainSlider.addEventListener('input', (e) => {
            gainLabel.textContent = e.target.value + 'x';
        });
        let gainTimeout;
        gainSlider.addEventListener('change', (e) => {
            clearTimeout(gainTimeout);
            gainTimeout = setTimeout(() => {
                updateStream(stream.id, { ...stream, gain: parseInt(e.target.value) });
            }, 300);
        });
    }

    // Enabled switch
    const enabledSwitch = element.querySelector('.stream-enabled');
    enabledSwitch.addEventListener('change', (e) => {
        const updatedStream = { ...stream, enabled: e.target.checked };
        updateStream(stream.id, updatedStream);
    });

    // Delete button
    const deleteButton = element.querySelector('.delete-vban-btn');
    deleteButton.addEventListener('click', () => {
        if (confirm('Voulez-vous vraiment supprimer ce flux RTSP ?')) {
            deleteStream(stream.id);
        }
    });

    // RTSP test button
    const testBtn = element.querySelector('.rtsp-test-btn');
    if (testBtn) {
        let testing = false;
        testBtn.addEventListener('click', () => {
            const basePath = window.basePath || '';
            const url = element.querySelector('.rtsp-url')?.value || stream.url;
            if (testing) {
                fetch(basePath + '/api/rtsp/test/stop', {method: 'POST'});
                testing = false;
                testBtn.textContent = '🔊 Tester le flux';
                testBtn.classList.remove('active');
                const vu = element.querySelector('.rtsp-test-vu');
                if (vu) vu.style.display = 'none';
            } else {
                const currentGain = parseInt(element.querySelector('.rtsp-gain')?.value || 10);
                fetch(basePath + '/api/rtsp/test/start', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({url: url, gain: currentGain})
                }).then(r => r.json()).then(data => {
                    if (data.success) {
                        testing = true;
                        testBtn.textContent = '⏹ Arreter le test';
                        testBtn.classList.add('active');
                        const vu = element.querySelector('.rtsp-test-vu');
                        if (vu) vu.style.display = 'flex';
                    } else {
                        showError(data.error || 'Erreur test RTSP');
                    }
                }).catch(err => showError('Erreur test RTSP'));
            }
        });
    }
}

function showAddStreamForm() {
    const div = document.createElement('div');
    div.className = 'list-group-item';
    div.innerHTML = `
        <div class="d-flex justify-content-between align-items-center">
            <div>
                <input type="text" class="rtsp-name-input" id="new-stream-name"
                       placeholder="Nom du flux">
            </div>
            <div class="source-controls">
                <label class="switch" title="Activer/Desactiver le flux">
                    <input type="checkbox" id="new-stream-enabled" checked>
                    <span class="slider round"></span>
                </label>
            </div>
        </div>
        <div class="webhook-input-group mt-2">
            <label class="form-label">URL RTSP</label>
            <input type="url" class="webhook-input" id="new-stream-url"
                   placeholder="rtsp://votre-camera:port/flux">
        </div>
        <div class="webhook-input-group mt-2">
            <label class="form-label">URL Webhook</label>
            <div class="webhook-input-with-test">
                <input type="url" class="webhook-input" id="new-stream-webhook"
                       placeholder="https://votre-serveur.com/webhook">
            </div>
        </div>
        <div class="webhook-actions">
            <button class="btn btn-primary" id="save-new-stream">Ajouter</button>
            <button class="btn btn-secondary" id="cancel-new-stream">Annuler</button>
        </div>
    `;

    const container = document.getElementById('rtspStreamsContainer');
    container.insertBefore(div, container.firstChild);

    document.getElementById('save-new-stream').onclick = addNewStream;
    document.getElementById('cancel-new-stream').onclick = () => div.remove();
}

async function addNewStream() {
    const name = document.getElementById('new-stream-name').value.trim();
    const url = document.getElementById('new-stream-url').value.trim();
    const webhook_url = document.getElementById('new-stream-webhook').value.trim();
    const enabled = document.getElementById('new-stream-enabled').checked;

    if (!url) {
        showError('L\'URL RTSP est requise');
        return;
    }

    try {
        const response = await callApi('/api/rtsp/stream', 'POST', {
            name,
            url,
            webhook_url,
            enabled
        });

        if (response.success) {
            rtspStreams.push(response.stream);
            setupRtspStreams();
            showSuccess('Flux RTSP ajouté avec succès');
        } else {
            throw new Error(response.error || 'Erreur lors de l\'ajout du flux');
        }
    } catch (error) {
        showError('Erreur lors de l\'ajout du flux RTSP');
    }
}

async function updateStream(streamId, data) {
    try {
        const response = await callApi(`/api/rtsp/stream/${streamId}`, 'PUT', data);
        if (response.success) {
            // Mettre à jour le stream dans la liste locale
            const index = rtspStreams.findIndex(s => s.id === streamId);
            if (index !== -1) {
                rtspStreams[index] = { ...rtspStreams[index], ...data };
            }
            showSuccess('Flux RTSP mis à jour');
        } else {
            throw new Error(response.error || 'Erreur lors de la mise à jour');
        }
    } catch (error) {
        showError('Erreur lors de la mise à jour du flux RTSP');
    }
}

async function deleteStream(streamId) {
    try {
        const response = await callApi(`/api/rtsp/stream/${streamId}`, 'DELETE');
        if (response.success) {
            rtspStreams = rtspStreams.filter(s => s.id !== streamId);
            setupRtspStreams();
            showSuccess('Flux RTSP supprimé');
        } else {
            throw new Error(response.error || 'Erreur lors de la suppression');
        }
    } catch (error) {
        showError('Erreur lors de la suppression du flux RTSP');
    }
}