/* Demarrage de l'interface. */
(function () {
    'use strict';
    var CT = window.CT;
    function start() {
        CT.initStatusbar();
        CT.initWizard();
        CT.initSettings();
        CT.render();
        CT.reloadStatus().then(CT.renderStatus).catch(function () {});
        if (!CT.socket) CT.error('Connexion temps réel indisponible : rechargez la page.');
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
})();
