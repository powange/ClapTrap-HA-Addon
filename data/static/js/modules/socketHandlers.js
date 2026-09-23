import { showNotification, showSuccess, showError } from './notifications.js';

// Echappe le HTML : les noms de source / groupe sont saisis par l'utilisateur
// et les noms de flux VBAN peuvent venir d'un tiers sur le reseau -> injectes
// dans innerHTML ci-dessous, ils permettraient un XSS.
function escHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
}

// Met a jour la zone de feedback live de la carte source dont data-detid ==
// detid (le source_id emis par le backend). No-op si la carte n'existe pas.
function _setSourceLive(detid, text, flash) {
    if (!detid) return;
    let panel;
    try {
        panel = document.querySelector('.tab-panel[data-detid="' + String(detid).replace(/["\\]/g, '\\$&') + '"]');
    } catch (e) { return; }
    if (!panel) return;
    const live = panel.querySelector('.source-live');
    if (!live) return;
    const txt = live.querySelector('.source-live-text');
    if (txt && text != null) txt.textContent = text;   // textContent => pas de XSS
    if (flash) {
        live.classList.remove('flash');
        void live.offsetWidth;   // force un reflow pour rejouer l'animation
        live.classList.add('flash');
    }
}

function formatSourceId(sourceId) {
    if (!sourceId) return '';
    const settings = window.settings || {};

    if (sourceId.startsWith('mic_')) {
        const name = settings.microphone?.audio_source;
        return name && name !== 'default' ? name : 'Micro';
    }
    if (sourceId.startsWith('rtsp_')) {
        // source_id = rtsp_<id de la source> depuis la 6.25.0
        const id = sourceId.replace('rtsp_', '');
        const s = (settings.rtsp_sources || []).find(x => String(x.id) === id);
        return (s && s.name) || 'RTSP';
    }
    if (sourceId.startsWith('vban_')) {
        const vbanSources = settings.saved_vban_sources || [];
        for (const s of vbanSources) {
            if (sourceId.includes(s.ip)) {
                return s.name || 'VBAN';
            }
        }
        return 'VBAN';
    }
    return sourceId;
}

export function initializeSocketIO() {
    console.log('🔌 Initializing Socket.IO...');
    const basePath = window.basePath || '';
    // Connexion partagee creee par la page (une seule par onglet, reconnexion
    // illimitee : avec reconnectionAttempts: 5 l'historique ne se mettait plus
    // a jour apres un redemarrage de l'add-on de plus de ~20 s).
    const socket = window.claptrapSocket || io({ path: basePath + '/socket.io' });
    window.claptrapSocket = socket;
    
    socket.on('connect', () => {
        console.log('🟢 Socket.IO Connected with ID:', socket.id);
    });
    
    // Historique : recharge depuis le serveur (il etait perdu a chaque
    // rechargement de page alors que le serveur le conserve).
    fetch(basePath + '/api/detections/history')
        .then(r => r.ok ? r.json() : [])
        .then(events => {
            const container = document.getElementById('detected_labels');
            if (!container || !Array.isArray(events)) return;
            container.innerHTML = '';
            // Le serveur renvoie du plus recent au plus ancien.
            events.slice(0, 10).reverse().forEach(renderHistoryEvent);
        })
        .catch(() => {});

    // Gestionnaire pour les claps
    socket.on('clap', (data) => {
        if (!data.ignored && typeof window.showClap === 'function') {
            window.showClap(data.source_id);
        }
        renderHistoryEvent(data);
        // Feedback live sur la carte de la source : flash + dernier clap.
        _setSourceLive(
            data.source_id,
            (data.clap_count > 1 ? `${data.clap_count} claps` : '1 clap')
                + (data.group_name ? ` · ${data.group_name}` : '')
                + ` (${Math.round((data.score || 0) * 100)}%)`,
            !data.ignored
        );
    });

    function renderHistoryEvent(data) {
        // Afficher la source et le nombre de claps
        const container = document.getElementById('detected_labels');
        if (container) {
            const sourceLabel = formatSourceId(data.source_id);
            const claps = data.clap_count || 1;
            const clapText = claps > 1 ? `${claps} claps` : '1 clap';
            const el = document.createElement('div');
            el.className = 'clap-event-label' + (data.ignored ? ' ignored' : '');
            const labels = Array.isArray(data.labels) ? data.labels : [];
            const labelsHtml = labels.length > 0
                ? `<div class="clap-event-sounds">${labels.map(l =>
                    `<span class="label">${escHtml(l.label)} <span class="label-score">${Math.round((l.score || 0) * 100)}%</span></span>`
                  ).join('')}</div>`
                : '';
            const groupName = data.group_name || (data.group_slug ? data.group_slug : '');
            const groupHtml = groupName
                ? ` <span class="group-tag">${escHtml(groupName)}</span>`
                : '';
            el.innerHTML = `<div class="clap-event-header"><strong>${clapText}</strong> <span class="source-tag">${escHtml(sourceLabel)}</span>${groupHtml} <span class="label-score">${Math.round((data.score || 0) * 100)}%</span></div>${labelsHtml}`;
            container.prepend(el);
            // Garder max 10 events
            while (container.children.length > 10) {
                container.removeChild(container.lastChild);
            }
        }
    }

    // Gestionnaire pour les labels (detection en cours → barre de controle)
    socket.on('labels', (data) => {
        // Feedback live par carte (independant de #current_detection, qui peut
        // ne pas exister dans le layout actuel).
        if (data && Array.isArray(data.detected) && data.detected.length && data.source) {
            const top = data.detected.slice(0, 3)
                .map(l => `${l.label} ${Math.round((l.score || 0) * 100)}%`).join(' · ');
            _setSourceLive(data.source, top, false);
        }

        const container = document.getElementById('current_detection');
        if (!container) return;

        container.innerHTML = '';
        if (data.detected && Array.isArray(data.detected)) {
            const sourceTag = data.source ? formatSourceId(data.source) : '';
            data.detected.forEach(label => {
                const labelElement = document.createElement('span');
                labelElement.className = 'label';
                labelElement.innerHTML = `
                    ${escHtml(label.label)}
                    <span class="label-score">${Math.round(label.score * 100)}%</span>
                    ${sourceTag ? `<span class="source-tag">${escHtml(sourceTag)}</span>` : ''}
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