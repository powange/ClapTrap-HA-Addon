import { callApi } from './api.js';
import { showError, showSuccess } from './utils.js';
import { saveSettings, updateSettings, getCurrentSettings } from './settings.js';

let audioSources = [];

export async function initAudioSources() {
    try {
        const sources = await callApi('/api/audio-sources', 'GET');
        audioSources = sources.filter(source => source.type === 'microphone');
        setupMicrophoneWebhook();
    } catch (error) {
        showError('Erreur lors du chargement des sources audio');
    }
}

function setupMicrophoneWebhook() {
    const webhookInput = document.getElementById('webhook-mic-url');
    const enabledSwitch = document.getElementById('webhook-mic-enabled');

    if (webhookInput) {
        // Stocker les changements en mémoire sans sauvegarder
        let timeout;
        webhookInput.addEventListener('input', (e) => {
            clearTimeout(timeout);
            timeout = setTimeout(() => {
                const newWebhookUrl = e.target.value.trim();
                updateSettings({
                    microphone: {
                        ...getCurrentSettings().microphone,
                        webhook_url: newWebhookUrl
                    }
                });
            }, 500);
        });
    }

    if (enabledSwitch) {
        enabledSwitch.addEventListener('change', (e) => {
            const enabled = e.target.checked;
            updateSettings({
                microphone: {
                    ...getCurrentSettings().microphone,
                    enabled: enabled
                }
            });
        });
    }

    const volumeSlider = document.getElementById('mic-volume');
    const volumeLabel = document.getElementById('mic-volume-value');
    if (volumeSlider) {
        volumeSlider.addEventListener('input', (e) => {
            volumeLabel.textContent = e.target.value + '%';
        });
        let volumeTimeout;
        volumeSlider.addEventListener('change', (e) => {
            const volume = parseInt(e.target.value);
            clearTimeout(volumeTimeout);
            volumeTimeout = setTimeout(() => {
                updateSettings({
                    microphone: {
                        ...getCurrentSettings().microphone,
                        volume: volume
                    }
                });
                callApi('/api/microphone/volume', 'PUT', { volume: volume })
                    .then(() => showSuccess('Volume du micro mis a jour'))
                    .catch(() => showError('Erreur lors du reglage du volume'));
            }, 300);
        });
    }
}

async function updateMicrophoneWebhook(webhookUrl) {
    try {
        const response = await callApi('/api/microphone/webhook', 'PUT', {
            webhook_url: webhookUrl
        });
        if (response.success) {
            showSuccess('Webhook du microphone mis à jour');
        } else {
            throw new Error(response.error || 'Erreur lors de la mise à jour');
        }
    } catch (error) {
        showError('Erreur lors de la mise à jour du webhook du microphone');
    }
}

async function updateMicrophoneEnabled(enabled) {
    try {
        const response = await callApi('/api/microphone/enabled', 'PUT', {
            enabled: enabled
        });
        if (response.success) {
            showSuccess(enabled ? 'Microphone activé' : 'Microphone désactivé');
        } else {
            throw new Error(response.error || 'Erreur lors de la mise à jour');
        }
    } catch (error) {
        showError('Erreur lors de la mise à jour du statut du microphone');
    }
}

function renderAudioSources() {
    const container = document.getElementById('audioSourcesContainer');
    // Code de rendu des sources audio...
}

// Autres fonctions spécifiques aux sources audio... 