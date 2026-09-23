import { initializeSocketIO } from './modules/socketHandlers.js';

// Historique des detections + feedback live par carte (la connexion Socket.IO
// est partagee avec le reste de la page, cf. window.claptrapSocket).
document.addEventListener('DOMContentLoaded', () => {
    initializeSocketIO();
});
