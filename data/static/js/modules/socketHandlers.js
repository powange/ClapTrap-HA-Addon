import { showNotification, showSuccess, showError } from './notifications.js';

function formatSourceId(sourceId) {
    if (!sourceId) return '';
    if (sourceId.startsWith('mic_')) return 'Micro';
    if (sourceId.startsWith('rtsp_')) return 'RTSP';
    if (sourceId.startsWith('vban_')) return 'VBAN';
    return sourceId;
}

export function initializeSocketIO() {
    console.log('🔌 Initializing Socket.IO...');
    const basePath = window.basePath || '';
    const socket = io({
        path: basePath + '/socket.io',
        reconnection: true,
        reconnectionDelay: 1000,
        reconnectionDelayMax: 5000,
        reconnectionAttempts: 5
    });
    
    socket.on('connect', () => {
        console.log('🟢 Socket.IO Connected with ID:', socket.id);
    });
    
    // Gestionnaire pour les claps
    socket.on('clap', (data) => {
        console.log('Clap event received:', data);
        if (typeof window.showClap === 'function') {
            window.showClap(data.source_id);
        }
        // Afficher la source et le nombre de claps
        const container = document.getElementById('detected_labels');
        if (container) {
            const sourceLabel = formatSourceId(data.source_id);
            const claps = data.clap_count || 1;
            const clapText = claps > 1 ? `${claps} claps` : '1 clap';
            const el = document.createElement('div');
            el.className = 'clap-event-label';
            el.innerHTML = `<strong>${clapText}</strong> <span class="source-tag">${sourceLabel}</span> <span class="label-score">${Math.round((data.score || 0) * 100)}%</span>`;
            container.prepend(el);
            // Garder max 10 events
            while (container.children.length > 10) {
                container.removeChild(container.lastChild);
            }
        }
    });

    // Gestionnaire pour les labels
    socket.on('labels', (data) => {
        console.log('🏷️ Labels received:', data);
        const container = document.getElementById('detected_labels');
        const waitingEmoji = document.getElementById('waiting-emoji');
        
        if (!container) {
            console.error('❌ Labels container not found');
            return;
        }

        // Vérifier si on a détecté une éructation
        if (data.detected && Array.isArray(data.detected)) {
            const burpingDetected = data.detected.some(label => label.label === 'Burping, eructation');
            if (burpingDetected && waitingEmoji) {
                console.log('🤢 Burping detected, changing emoji...');
                waitingEmoji.textContent = '😱';
                setTimeout(() => {
                    waitingEmoji.textContent = '👂';
                }, 2000);
            }
        }

        // Ne pas vider les events clap, juste mettre a jour les labels en bas
        // Supprimer les anciens labels (pas les clap-event-label)
        container.querySelectorAll('.label').forEach(el => el.remove());

        // Ajouter les nouveaux labels
        if (data.detected && Array.isArray(data.detected)) {
            const sourceTag = data.source ? formatSourceId(data.source) : '';
            data.detected.forEach(label => {
                const labelElement = document.createElement('span');
                labelElement.className = 'label';
                labelElement.innerHTML = `
                    ${label.label}
                    <span class="label-score">${Math.round(label.score * 100)}%</span>
                    ${sourceTag ? `<span class="source-tag">${sourceTag}</span>` : ''}
                `;
                container.appendChild(labelElement);
            });
        }

        // Afficher le score max à côté du seuil
        if (data.detected && data.detected.length > 0) {
            var maxScore = Math.max(...data.detected.map(function(l) { return l.score; }));
            var thresholdLabel = document.getElementById('threshold-value');
            if (thresholdLabel) {
                var threshold = parseFloat(document.getElementById('threshold')?.value || 0.5);
                thresholdLabel.textContent = threshold.toFixed(1) + ' (score: ' + maxScore.toFixed(2) + ')';
            }
        }
    });

    return socket;
} 