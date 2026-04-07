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
    socket.on('clap', handleClapEvent);
}

function removeSocketListeners() {
    socket.off('clap', handleClapEvent);
}

function handleClapEvent(data) {
    console.log('Clap event received:', data);
    const display = document.getElementById('detection_display');

    if (display) {
        display.classList.add('clap');
        setTimeout(() => display.classList.remove('clap'), 1500);
    }
} 