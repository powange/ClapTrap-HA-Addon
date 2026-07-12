import { startDetection, stopDetection } from './detection.js';
import { saveSettings } from './settings.js';

export function setupEventListeners() {
    setupDetectionButtons();
    setupThresholdControl();
    setupParameterChangeListeners();
}

function setupDetectionButtons() {
    const startButton = document.getElementById('startButton');
    const stopButton = document.getElementById('stopButton');

    if (startButton) {
        startButton.addEventListener('click', () => {
            startDetection();
        });
    }
    
    if (stopButton) {
        stopButton.addEventListener('click', stopDetection);
    }
}

function setupThresholdControl() {
    const threshold = document.getElementById('threshold');
    const thresholdValue = document.getElementById('threshold-value');
    
    if (threshold && thresholdValue) {
        threshold.addEventListener('input', function() {
            thresholdValue.textContent = this.value;
        });
    }
}

function setupParameterChangeListeners() {
    // Ajouter ici les écouteurs pour les changements de paramètres si nécessaire
} 