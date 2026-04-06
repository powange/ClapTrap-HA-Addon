import { callApi } from './api.js';
import { showError, showSuccess } from './utils.js';
import { getCurrentSettings, saveSettings } from './settings.js';

let socket = io({ path: (window.basePath || '') + '/socket.io' });
let isDetecting = false;

export async function startDetection() {
    try {
        // Sauvegarder les paramètres avant de démarrer la détection
        console.log('💾 Sauvegarde des paramètres avant démarrage...');
        const saved = await saveSettings();
        if (!saved) {
            throw new Error('Failed to save settings');
        }
        console.log('💾 Paramètres sauvegardés avec succès');

        const settings = getCurrentSettings();
        
        // Créer une copie des paramètres pour la détection uniquement
        const detectionSettings = {
            global: settings.global,
            microphone: settings.microphone.enabled ? settings.microphone : null,
            saved_vban_sources: settings.saved_vban_sources.filter(source => source.enabled),
            rtsp_sources: settings.rtsp_sources.filter(source => source.enabled)
        };

        console.log('🎯 Démarrage de la détection avec les sources actives:', detectionSettings);
        const response = await callApi('/api/detection/start', 'POST', detectionSettings);
        
        if (response.success) {
            isDetecting = true;
            updateDetectionUI(true);
            setupSocketListeners();
            showSuccess('Détection démarrée');
        }
    } catch (error) {
        showError('Erreur lors du démarrage de la détection');
        console.error('Start detection error:', error);
    }
}

export async function stopDetection() {
    try {
        const response = await callApi('/api/detection/stop', 'POST');
        if (response.success) {
            isDetecting = false;
            updateDetectionUI(false);
            removeSocketListeners();
            showSuccess('Détection arrêtée');
        }
    } catch (error) {
        showError('Erreur lors de l\'arrêt de la détection');
        console.error('Stop detection error:', error);
    }
}

function updateDetectionUI(isActive) {
    const startButton = document.getElementById('startButton');
    const stopButton = document.getElementById('stopButton');
    
    if (startButton && stopButton) {
        startButton.style.display = isActive ? 'none' : 'block';
        stopButton.style.display = isActive ? 'block' : 'none';
    }
}

function setupSocketListeners() {
    socket.on('detection_event', handleDetectionEvent);
}

function removeSocketListeners() {
    socket.off('detection_event', handleDetectionEvent);
}

function handleDetectionEvent(data) {
    console.log('Detection event received:', data);
    const display = document.getElementById('detection_display');
    const waitingEmoji = document.getElementById('waiting-emoji');
    console.log('Found elements:', { display, waitingEmoji });
    const detectedLabel = document.createElement('div');
    detectedLabel.textContent = `Détection: ${data.label}`;
    
    if (display) {
        // Remplacer le texte de détection s'il existe déjà
        const existingLabel = display.querySelector('.detected-label');
        if (existingLabel) {
            existingLabel.remove();
        }
        detectedLabel.classList.add('detected-label');
        display.appendChild(detectedLabel);

        if (data.label === 'Burping, eructation') {
            console.log('Burping detected, changing emoji...');
            if (waitingEmoji) {
                console.log('Current emoji:', waitingEmoji.textContent);
                waitingEmoji.textContent = '😱';
                console.log('New emoji set:', waitingEmoji.textContent);
                // Remettre l'emoji d'écoute après 2 secondes
                setTimeout(() => {
                    console.log('Resetting emoji...');
                    waitingEmoji.textContent = '👂';
                    console.log('Emoji reset to:', waitingEmoji.textContent);
                }, 2000);
            }
        } else if (data.label.toLowerCase().includes('clap')) {
            display.classList.add('clap');
            setTimeout(() => display.classList.remove('clap'), 1000);
        }
    }
} 