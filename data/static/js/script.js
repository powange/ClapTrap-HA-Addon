import { initAudioSources } from './modules/audioSources.js';
import { initVbanSources, refreshVbanSources } from './modules/vbanSources.js';
import { initRtspSources } from './modules/rtspSources.js';
import { initWebhooks } from './modules/webhooks.js';
import { setupEventListeners } from './modules/events.js';
import { updateSettings, saveSettings, initSettings } from './modules/settings.js';
import { initializeSocketIO } from './modules/socketHandlers.js';
import { initMicTest } from './modules/micTest.js';
import { showError } from './modules/utils.js';

window.showClap = function(sourceId) {
    console.log('📢 showClap called for sourceId:', sourceId);
    
    const clapEmojis = document.querySelectorAll('.clap-emoji');
    let clapEmoji = null;
    
    // Créer un nouvel emoji s'il n'existe pas pour cette source
    if (!document.querySelector(`.clap-emoji[data-source="${sourceId}"]`)) {
        clapEmoji = document.createElement('span');
        clapEmoji.className = 'clap-emoji';
        clapEmoji.textContent = '👏';
        clapEmoji.dataset.source = sourceId;
        clapEmoji.style.display = 'none';
        document.getElementById('detection_display').appendChild(clapEmoji);
    } else {
        clapEmoji = document.querySelector(`.clap-emoji[data-source="${sourceId}"]`);
    }
    
    const detectionDisplay = document.getElementById('detection_display');
    const waitingEmoji = document.getElementById('waiting-emoji');
    
    if (clapEmoji && detectionDisplay) {
        console.log('🎯 Showing clap emoji for source:', sourceId);
        
        // Forcer la visibilité
        clapEmoji.classList.add('visible');
        detectionDisplay.classList.add('clap');
        
        console.log('Updated styles:', {
            classList: clapEmoji.classList,
            displayClassList: detectionDisplay.classList
        });
        
        setTimeout(() => {
            console.log('🔄 Hiding clap emoji for source:', sourceId);
            clapEmoji.classList.remove('visible');
            detectionDisplay.classList.remove('clap');
        }, 1000);
    } else {
        console.warn('❌ Elements not found:', {
            emoji: clapEmoji,
            display: detectionDisplay
        });
    }
};

// Ajouter un log pour vérifier que la fonction est bien attachée à window
console.log('🔍 showClap function attached to window:', typeof window.showClap);

window.updateDetectionState = function(isDetecting) {
    // S'assurer que les labels et emojis restent visibles
    document.querySelectorAll('.source-label, .clap-emoji').forEach(element => {
        element.style.display = 'inline-block';
    });
    
    // Désactiver les inputs mais garder les labels visibles
    document.querySelectorAll('input, select').forEach(element => {
        element.disabled = isDetecting;
    });
};

let detectionActive = false;

async function toggleDetection() {
    try {
        // Si on démarre la détection, on sauvegarde d'abord les paramètres
        if (!detectionActive) {
            console.log('💾 Tentative de sauvegarde des paramètres...');
            const saved = await saveSettings();
            console.log('💾 Résultat de la sauvegarde:', saved);
            if (!saved) {
                throw new Error('Failed to save settings');
            }
        }

        const response = await fetch((window.basePath || '') + '/api/detection/toggle', {
            method: 'POST'
        });
        
        if (!response.ok) {
            throw new Error('Detection toggle failed');
        }
        
        detectionActive = !detectionActive;
        updateUIState(detectionActive);
    } catch (error) {
        console.error('Error:', error);
        // Remettre l'UI dans un état cohérent
        detectionActive = false;
        updateUIState(false);
        showError('La détection a échoué. Veuillez réessayer.');
    }
}

function updateUIState(active) {
    const button = document.getElementById('detectionButton');
    button.textContent = active ? 'Arrêter la détection' : 'Démarrer la détection';
    // Désactiver/activer les champs de configuration
    document.querySelectorAll('.config-field').forEach(field => {
        field.disabled = active;
    });
}

document.addEventListener('DOMContentLoaded', () => {
    console.log('🚀 DOM fully loaded');
    
    // Initialiser les paramètres
    if (window.settings) {
        console.log('📝 Paramètres chargés depuis le serveur:', window.settings);
        initSettings(window.settings);
        // Les sources sont gérées par buildTabs() dans le script inline
    } else {
        console.error('⚠️ Aucun paramètre trouvé');
    }

    // Socket.IO et test micro sont toujours initialisés
    const socket = initializeSocketIO();
    initMicTest(socket);
    console.log('✅ Socket.IO initialized');
    
    // Ajouter le gestionnaire d'événements pour le bouton de sauvegarde
    const saveButton = document.getElementById('saveConfigButton');
    if (saveButton) {
        saveButton.addEventListener('click', async () => {
            saveButton.disabled = true;
            try {
                await saveSettings();
            } finally {
                saveButton.disabled = false;
            }
        });
    }
    
    // Vérifier les éléments DOM
    setTimeout(checkDOMElements, 1000);
    setTimeout(checkDOMElements, 5000);
});

function debugSocketIO() {
    console.log('Debugging Socket.IO connection...');
    const socket = io({ path: (window.basePath || '') + '/socket.io' });
    
    // Événements de connexion
    socket.on('connect', () => {
        console.log('Socket.IO: Connected successfully');
        console.log('Socket ID:', socket.id);
    });
    
    // Test d'émission
    setInterval(() => {
        socket.emit('ping');
        console.log('Ping sent');
    }, 5000);
    
    // Réception des événements
    socket.on('pong', () => {
        console.log('Pong received');
    });
}

// Appeler la fonction de debug au chargement
document.addEventListener('DOMContentLoaded', debugSocketIO);

function checkDOMElements() {
    const elements = {
        'clap-microphone': document.getElementById('clap-microphone'),
        'detection_display': document.getElementById('detection_display'),
        'detected_labels': document.getElementById('detected_labels'),
        'all-clap-emojis': Array.from(document.querySelectorAll('.clap-emoji')).map(el => ({
            id: el.id,
            display: el.style.display,
            className: el.className
        }))
    };
    
    console.log('🔍 DOM Elements Check:', elements);
    return elements;
}
