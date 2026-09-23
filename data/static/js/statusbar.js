/* Barre d'etat (demarrer / arreter, demarrage automatique), onglets, historique. */
(function () {
    'use strict';
    var CT = window.CT;

    function sinceText(since) {
        if (!since) return '';
        var min = Math.max(0, Math.round((Date.now() / 1000 - since) / 60));
        if (min < 1) return "depuis moins d'une minute";
        if (min < 60) return 'depuis ' + min + ' min';
        var h = Math.floor(min / 60);
        return 'depuis ' + h + ' h ' + String(min % 60).padStart(2, '0');
    }

    CT.renderStatus = function () {
        var st = CT.state.status;
        var bar = document.getElementById('statusbar');
        if (!bar) return;
        var enabled = CT.sourceList().filter(function (s) { return s.enabled; });
        var running = !!st.running;
        bar.classList.toggle('is-running', running);
        CT.$('#status-title').textContent = running ? "À l'écoute" : 'Arrêté';
        var n = running ? (st.sources || []).length : enabled.length;
        CT.$('#status-detail').textContent = running
            ? n + ' source' + (n > 1 ? 's' : '') + ' · ' + sinceText(st.since)
            : (enabled.length ? enabled.length + ' source' + (enabled.length > 1 ? 's' : '') + ' prête' + (enabled.length > 1 ? 's' : '')
                              : 'Aucune source activée');
        var btn = CT.$('#toggle-detection');
        btn.textContent = running ? 'Arrêter' : "Démarrer l'écoute";
        btn.classList.toggle('btn-danger', running);
        btn.classList.toggle('btn-primary', !running);
        btn.disabled = !running && enabled.length === 0;
        btn.title = btn.disabled ? 'Activez au moins une source pour démarrer' : '';
        var auto = CT.$('#auto-start');
        auto.checked = !!((CT.state.settings.microphone || {}).auto_start);
        if (CT.renderSourceStatuses) CT.renderSourceStatuses();
    };
    setInterval(function () { if (CT.state.status.running) CT.renderStatus(); }, 30000);

    function bindStatusbar() {
        var btn = CT.$('#toggle-detection');
        btn.addEventListener('click', function () {
            if (btn.dataset.busy) return;   // anti double-clic
            btn.dataset.busy = '1';
            btn.disabled = true;
            var running = CT.state.status.running;
            // Le serveur demarre avec les reglages ENREGISTRES (corps ignore).
            CT.api('POST', running ? '/api/detection/stop' : '/api/detection/start', {})
                .then(function () { return CT.reloadStatus(); })
                .then(function () { CT.renderStatus(); })
                .catch(function (err) { CT.error((running ? 'Arrêt' : 'Démarrage') + ' impossible : ' + err.message); })
                .finally(function () { delete btn.dataset.busy; CT.renderStatus(); });
        });
        var auto = CT.$('#auto-start');
        auto.addEventListener('change', function () {
            var value = auto.checked;
            CT.api('PUT', '/api/microphone/auto-start', {enabled: value})
                .then(function () { (CT.state.settings.microphone = CT.state.settings.microphone || {}).auto_start = value; })
                .catch(function (err) { auto.checked = !value; CT.error('Démarrage automatique : ' + err.message); });
        });
        CT.on('detection_status', function () {
            CT.reloadStatus().then(CT.renderStatus).catch(function () {});
        });
    }

    // ---- Onglets --------------------------------------------------------------
    function bindTabs() {
        var tabs = CT.$$('[role="tab"]');
        function select(tab) {
            tabs.forEach(function (t) {
                var on = t === tab;
                t.setAttribute('aria-selected', on ? 'true' : 'false');
                t.tabIndex = on ? 0 : -1;
                document.getElementById(t.getAttribute('aria-controls')).hidden = !on;
            });
            if (tab.id === 'tab-settings' && CT.onSettingsOpen) CT.onSettingsOpen();
            if (tab.id !== 'tab-listen') CT.stopTest();
        }
        tabs.forEach(function (t, i) {
            t.addEventListener('click', function () { select(t); });
            t.addEventListener('keydown', function (e) {
                if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
                var next = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
                select(next);
                next.focus();
            });
        });
    }

    // ---- Historique ----------------------------------------------------------
    function sourceNameFor(sourceId) {
        var src = CT.findSource(function (s) { return s.sourceId === sourceId; });
        return src ? src.name : sourceId;
    }
    function historyItem(ev) {
        var li = document.createElement('li');
        li.className = 'history-item' + (ev.ignored ? ' is-ignored' : '');
        var when = ev.timestamp ? new Date(ev.timestamp * 1000) : new Date();
        var n = ev.clap_count || 1;
        li.innerHTML =
            '<time>' + when.toLocaleTimeString('fr-FR', {hour: '2-digit', minute: '2-digit', second: '2-digit'}) + '</time>' +
            '<span class="history-main"><strong>' + n + ' clap' + (n > 1 ? 's' : '') + '</strong> · ' +
            CT.esc(sourceNameFor(ev.source_id)) + ' · ' + CT.esc(ev.group_name || ev.group_slug || '') +
            (ev.ignored ? ' <span class="muted">(ignoré : un autre groupe a gagné)</span>' : '') + '</span>' +
            '<span class="history-score">' + CT.pct(ev.score) + '</span>';
        var labels = (ev.labels || []).map(function (l) { return l.label + ' ' + CT.pct(l.score); }).join(' · ');
        if (labels) li.title = labels;
        return li;
    }
    function renderHistoryEmpty() {
        var list = CT.$('#history-list');
        CT.$('#history-empty').hidden = list.children.length > 0;
    }
    function bindHistory() {
        var list = CT.$('#history-list');
        fetch(CT.basePath + '/api/detections/history').then(function (r) { return r.ok ? r.json() : []; })
            .then(function (events) {
                list.innerHTML = '';
                (events || []).slice(0, 20).forEach(function (ev) { list.appendChild(historyItem(ev)); });
                renderHistoryEmpty();
            }).catch(renderHistoryEmpty);
        CT.on('clap', function (ev) {
            list.insertBefore(historyItem(ev || {}), list.firstChild);
            while (list.children.length > 20) list.removeChild(list.lastChild);
            renderHistoryEmpty();
        });
        CT.$('#history-clear').addEventListener('click', function () {
            CT.api('DELETE', '/api/detections/history')
                .then(function () { list.innerHTML = ''; renderHistoryEmpty(); })
                .catch(function (err) { CT.error('Historique non effacé : ' + err.message); });
        });
    }

    CT.initStatusbar = function () {
        bindStatusbar();
        bindTabs();
        bindHistory();
    };
})();
