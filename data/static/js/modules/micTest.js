import { callApi } from './api.js';
import { showError } from './utils.js';

let testing = false;

export function initMicTest(socket) {
    const btn = document.getElementById('mic-test-btn');
    if (!btn) return;

    btn.addEventListener('click', () => {
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
}

async function startTest() {
    try {
        const response = await callApi('/api/mic/test/start', 'POST');
        if (response.success) {
            testing = true;
            const btn = document.getElementById('mic-test-btn');
            btn.innerHTML = '<span class="icon">⏹</span> Arrêter le test';
            btn.classList.add('active');
            document.getElementById('mic-test-vu').style.display = 'flex';
        }
    } catch (error) {
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
    btn.innerHTML = '<span class="icon">🎤</span> Tester le micro';
    btn.classList.remove('active');
    document.getElementById('mic-test-vu').style.display = 'none';
    document.getElementById('mic-test-bar').style.width = '0%';
}

function updateVuMeter(peak, db) {
    const bar = document.getElementById('mic-test-bar');
    const label = document.getElementById('mic-test-db');
    // Mapper -60dB..0dB sur 0%..100%
    const pct = Math.max(0, Math.min(100, ((db + 60) / 60) * 100));
    bar.style.width = pct + '%';
    label.textContent = db + ' dB';
}
