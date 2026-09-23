import { initializeSocketIO } from './modules/socketHandlers.js';

window.showClap = function(sourceId) {
    const detectionDisplay = document.getElementById('detection_display');
    let clapEmoji = document.querySelector(`.clap-emoji[data-source="${sourceId}"]`);

    if (!clapEmoji) {
        clapEmoji = document.createElement('span');
        clapEmoji.className = 'clap-emoji';
        clapEmoji.textContent = '\u{1F44F}';
        clapEmoji.dataset.source = sourceId;
        clapEmoji.style.display = 'none';
        if (detectionDisplay) detectionDisplay.appendChild(clapEmoji);
    }

    if (clapEmoji && detectionDisplay) {
        clapEmoji.classList.add('visible');
        detectionDisplay.classList.add('clap');
        setTimeout(() => {
            clapEmoji.classList.remove('visible');
            detectionDisplay.classList.remove('clap');
        }, 1000);
    }
};

document.addEventListener('DOMContentLoaded', () => {
    // Le test micro est gere par la page (micTest.js envoyait un 2e POST).
    initializeSocketIO();
});
