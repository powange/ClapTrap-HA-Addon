import { callApi } from './api.js';
import { showError } from './utils.js';

let testing = false;

export function initMicTest(socket) {
    // Délégation d'événements sur document — fonctionne même si le bouton est caché au chargement
    document.addEventListener('click', (e) => {
        const btn = e.target.closest('#mic-test-btn');
        if (!btn) return;

        console.log('🎤 Bouton cliqué, testing =', testing);
        if (testing) {
            stopTest();
        } else {
            startTest();
        }
    });

    // Ecouter les niveaux audio du serveur
    socket.on('mic_level', (data) => {
        if (data.error) {
            showError('Erreur test micro: ' + data.error);
            stopTest();
            return;
        }
        updateVuMeter(data.peak, data.db);
    });

    console.log('🎤 initMicTest: délégation d\'événements activée');
}

async function startTest() {
    try {
        console.log('🎤 Appel /api/mic/test/start...');
        const response = await callApi('/api/mic/test/start', 'POST');
        console.log('🎤 Réponse:', response);
        if (response.success) {
            testing = true;
            const btn = document.getElementById('mic-test-btn');
            btn.innerHTML = '<span class="icon">⏹</span> Arrêter le test';
            btn.classList.add('active');
            document.getElementById('mic-test-vu').style.display = 'flex';
        }
    } catch (error) {
        console.error('🎤 Erreur startTest:', error);
        showError('Impossible de démarrer le test micro');
    }
}

async function stopTest() {
    try {
        await callApi('/api/mic/test/stop', 'POST');
    } catch (e) {
        // Ignorer les erreurs à l'arrêt
    }
    testing = false;
    const btn = document.getElementById('mic-test-btn');
    if (btn) {
        btn.innerHTML = '<span class="icon">🎤</span> Tester le micro';
        btn.classList.remove('active');
    }
    const vu = document.getElementById('mic-test-vu');
    if (vu) vu.style.display = 'none';
    const bar = document.getElementById('mic-test-bar');
    if (bar) bar.style.width = '0%';
}

function updateVuMeter(peak, db) {
    const bar = document.getElementById('mic-test-bar');
    const label = document.getElementById('mic-test-db');
    if (!bar || !label) return;
    // Mapper -60dB..0dB sur 0%..100%
    const pct = Math.max(0, Math.min(100, ((db + 60) / 60) * 100));
    bar.style.width = pct + '%';
    label.textContent = db + ' dB';
}
