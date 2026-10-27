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

    var setText = CT.setText;   // n'ecrit que si le texte change
    var wasRunning = null;

    var degradedShown = false;
    CT.renderStatus = function () {
        var st = CT.state.status;
        if (st.settings_degraded && !degradedShown) {
            // Reglages illisibles : valeurs par defaut en memoire, rien n'est
            // supprime ; le dire au lieu d'afficher une configuration vide.
            degradedShown = true;
            CT.error('Réglages illisibles (settings.json et sa sauvegarde) : valeurs par défaut affichées. '
                     + 'Importez une sauvegarde ; les fichiers illisibles sont conservés.');
        }
        var bar = document.getElementById('statusbar');
        if (!bar) return;
        var allSources = CT.sourceList();   // une seule fois par rendu
        var hasSources = allSources.length > 0;
        var enabled = allSources.filter(function (s) { return s.enabled; });
        var running = !!st.running;
        bar.classList.toggle('is-running', running);
        // Demarrage (YAMNet, connexion des cameras : plusieurs secondes) ou
        // arret en cours : dit tel quel, au lieu de « Détection arrêtée ».
        var pending = CT.state.detectionPending;
        var pendingText = pending === 'start' ? 'Démarrage…' : pending === 'stop' ? 'Arrêt…' : '';
        setText(CT.$('#status-title'), CT.state.restarting ? 'Redémarrage…' : pendingText ||
                (running ? 'Détection en cours' : 'Détection arrêtée'));
        var n = running ? (st.sources || []).length : enabled.length;
        // Sources en erreur ou en reconnexion : la barre ne dit plus « tout va
        // bien » en vert quand plus rien n'arrive.
        var bad = running ? (st.sources || []).filter(function (id) {
            var r = CT.state.sourceStatus[id] || (st.source_status || {})[id];
            return r === 'error' || r === 'reconnecting';
        }).length : 0;
        bar.classList.toggle('is-degraded', bad > 0);
        // Groupes actifs sans son coche (ne se declencheront jamais).
        var empty = 0;
        enabled.forEach(function (src) {
            (src.data.sound_groups || []).forEach(function (g) {
                if (g && g.slug && g.enabled !== false && !CT.groupHasSounds(g)) empty += 1;
            });
        });
        var emptyText = empty ? ' · ' + empty + ' groupe' + (empty > 1 ? 's' : '') + ' sans son' : '';
        // Processeur et memoire de l'add-on (tout le conteneur : ffmpeg compris).
        var perf = st.perf || {};
        var perfText = perf.processeur != null ? ' · processeur ' + String(perf.processeur).replace('.', ',') + ' %' +
            (perf.memoire_mo != null ? ', ' + Math.round(perf.memoire_mo) + ' Mo' : '') : '';
        setText(CT.$('#status-detail'), (running
            ? n + ' source' + (n > 1 ? 's' : '') + (bad ? ' · ' + bad + ' en erreur' : '') + ' · ' + sinceText(st.since)
            : (enabled.length ? enabled.length + ' source' + (enabled.length > 1 ? 's' : '') + ' prête' + (enabled.length > 1 ? 's' : '')
                              : hasSources ? 'Activez une source pour pouvoir démarrer'
                                           : 'Ajoutez une source pour pouvoir démarrer')) + emptyText + (running ? perfText : ''));
        if (wasRunning === true && !running && CT.resetLive) CT.resetLive();
        if (wasRunning !== null && wasRunning !== running && CT.announce) {
            CT.announce(running ? 'Détection démarrée' : 'Détection arrêtée', true);
        }
        wasRunning = running;
        var btn = CT.$('#toggle-detection');
        btn.textContent = pendingText || (running ? 'Arrêter la détection' : 'Démarrer la détection');
        btn.classList.toggle('btn-danger', running);
        btn.classList.toggle('btn-primary', !running);
        // Pendant la requete : aria-disabled (et non disabled, qui renvoyait
        // le focus clavier au debut de la page) ; un statut recu entre-temps
        // ne le reactive plus.
        btn.setAttribute('aria-disabled', pending ? 'true' : 'false');
        btn.setAttribute('aria-busy', pending ? 'true' : 'false');
        btn.disabled = !pending && !running && enabled.length === 0;
        btn.title = btn.disabled ? (hasSources ? 'Activez au moins une source pour démarrer'
                                               : 'Ajoutez une source pour démarrer') : '';
        var auto = CT.$('#auto-start');
        if (!auto.dataset.busy) auto.checked = !!((CT.state.settings.microphone || {}).auto_start);
        if (CT.renderSourceStatuses) CT.renderSourceStatuses();
    };
    // Toutes les 30 s pendant la detection : duree affichee et diagnostic des
    // sources (analyses sautees, coupures), relus du serveur ; page cachee
    // (application mobile en arriere-plan) : rien.
    setInterval(function () {
        if (!CT.state.status.running || document.hidden) return;
        CT.reloadStatus().then(CT.renderStatus, CT.renderStatus);
    }, 30000);

    function bindStatusbar() {
        var btn = CT.$('#toggle-detection');
        btn.addEventListener('click', function () {
            if (btn.dataset.busy) return;   // anti double-clic
            btn.dataset.busy = '1';
            var running = CT.state.status.running;
            CT.state.detectionPending = running ? 'stop' : 'start';
            CT.renderStatus();
            // Le serveur demarre avec les reglages ENREGISTRES (corps ignore).
            CT.api('POST', running ? '/api/detection/stop' : '/api/detection/start', {})
                .then(function () { return CT.reloadStatus(); })
                .then(function () { CT.renderStatus(); })
                .catch(function (err) {
                    // Onglet perime (demarree ou arretee ailleurs) : resynchroniser
                    // au lieu d'afficher une erreur si l'etat voulu est atteint.
                    return CT.reloadStatus().then(function (st) {
                        if (!!st.running === !running) { CT.renderStatus(); return; }
                        CT.error((running ? 'Arrêt' : 'Démarrage') + ' impossible : ' + err.message);
                    });
                })
                .finally(function () { delete btn.dataset.busy; CT.state.detectionPending = null; CT.renderStatus(); });
        });
        var auto = CT.$('#auto-start');
        auto.addEventListener('change', function () {
            var value = auto.checked;
            auto.dataset.busy = '1';
            CT.api('PUT', '/api/microphone/auto-start', {enabled: value})
                .then(function () { (CT.state.settings.microphone = CT.state.settings.microphone || {}).auto_start = value; })
                .catch(function (err) { auto.checked = !value; CT.error('Démarrage automatique : ' + err.message); })
                .finally(function () { delete auto.dataset.busy; });
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
            if (tab.id === 'tab-blueprints' && CT.onBlueprintsOpen) CT.onBlueprintsOpen();
            if (tab.id === 'tab-listen' && CT.redrawLive) CT.redrawLive();   // courbes non dessinees pendant l'absence
            if (tab.id !== 'tab-listen') CT.stopTest();
        }
        tabs.forEach(function (t, i) {
            t.addEventListener('click', function () { select(t); });
            t.addEventListener('keydown', function (e) {
                var next = e.key === 'ArrowRight' ? tabs[(i + 1) % tabs.length]
                    : e.key === 'ArrowLeft' ? tabs[(i + tabs.length - 1) % tabs.length]
                    : e.key === 'Home' ? tabs[0] : e.key === 'End' ? tabs[tabs.length - 1] : null;
                if (!next) return;
                e.preventDefault();
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
            '<span class="history-main"><strong>' + (ev.alert ? 'Alerte' : n + ' clap' + (n > 1 ? 's' : '')) + '</strong> · ' +
            CT.esc(sourceNameFor(ev.source_id)) + ' · ' + CT.esc(ev.group_name || ev.group_slug || '') +
            (ev.ignored ? ' <span class="muted">(non retenu : ' + (ev.room ? 'entendu plus fort dans « ' + CT.esc(ev.room) + ' »'
                                                                    : 'autre groupe prioritaire') + ')</span>' : '') + '</span>' +
            '<span class="history-score">' + CT.pct(ev.score) + '</span>';
        var labels = (ev.labels || []).map(function (l) { return CT.soundLabel(l.label) + ' ' + CT.pct(l.score); }).join(' · ');
        if (labels) {
            // Visible (et lu) : en infobulle seulement, invisible au toucher.
            var lab = document.createElement('span');
            lab.className = 'history-labels';
            lab.textContent = labels;
            li.querySelector('.history-main').appendChild(lab);
        }
        return li;
    }
    // « Presque » : raison en clair et piste de reglage.
    function nearText(m) {
        var lab = m.label ? '« ' + CT.esc(CT.soundLabel(m.label)) + ' »' : 'un son du groupe';
        var x = function (v) { return String(Math.round(v * 10) / 10).replace('.', ',') + ' ×'; };
        if (m.kind === 'low_score') {
            return ['Pic de clap sans son reconnu : ' + lab + ' à ' + CT.pct(m.score) + ' pour un seuil de ' + CT.pct(m.threshold) + '.',
                    'Baissez le seuil du groupe, ou cochez le son que YAMNet entend (sons entendus de la source).'];
        }
        if (m.kind === 'weak') {
            return [lab + ' reconnu à ' + CT.pct(m.score) + ', mais aucun pic assez fort pour compter un clap' +
                    (m.strength != null ? ' (' + x(m.strength) + ' le bruit de fond, minimum ' + x(m.required) + ')' : '') + '.',
                    'Baissez « Force minimale d\'un clap » (Réglages › Comptage), ou rapprochez la source.'];
        }
        if (m.kind === 'margin') {
            var rival = '« ' + CT.esc(CT.soundLabel(m.rival)) + ' »';
            if (m.rival_score >= m.score) {
                return [lab + ' à ' + CT.pct(m.score) + ', battu par ' + rival + ' à ' + CT.pct(m.rival_score) + '.',
                        'Aucun écart ne le ferait déclencher : cochez ' + rival + ' dans le groupe s\'il fait partie du geste.'];
            }
            return [lab + ' à ' + CT.pct(m.score) + ', mais ' + rival + ' juste derrière à ' +
                    CT.pct(m.rival_score) + ' (écart demandé : ' + Math.round(m.margin * 100) + ' points).',
                    'Baissez l\'écart avec les autres sons du groupe, ou cochez ' + rival + ' s\'il fait partie du geste.'];
        }
        if (m.kind === 'shape') {
            return ['Pic écarté : il ne ressemble pas à un clap (' + (m.reason === 'tenu'
                        ? 'le son dure : encore ' + CT.pct(m.decay) + ' du niveau 30 ms après'
                        : 'son trop grave : ' + CT.pct(m.high) + ' de son énergie dans les aigus') + ').',
                    'Si c\'était bien un clap, décochez « Vérifier la forme des claps » (Réglages › Comptage).'];
        }
        if (m.kind === 'count') {
            return [m.counted + ' clap' + (m.counted > 1 ? 's' : '') + ' compté' + (m.counted > 1 ? 's' : '') +
                    ', aucune entité pour ce nombre (le groupe attend ' + (m.expected || []).join(' ou ') + ').',
                    'Ajustez la fenêtre multi-clap ou l\'écart minimal (Réglages › Comptage), ou lancez « Calibrer le comptage ».'];
        }
        return ['', ''];
    }
    function nearItem(m) {
        var li = document.createElement('li');
        li.className = 'history-item near-item';
        var when = m.timestamp ? new Date(m.timestamp * 1000) : new Date();
        var t = nearText(m);
        li.innerHTML = '<time>' + when.toLocaleTimeString('fr-FR', {hour: '2-digit', minute: '2-digit', second: '2-digit'}) + '</time>' +
            '<span class="history-main"><strong>' + CT.esc(m.group || '') + '</strong> · ' + CT.esc(m.source_name || sourceNameFor(m.source_id)) +
            '<span class="history-labels">' + t[0] + '</span><span class="history-labels near-hint">' + t[1] + '</span></span>';
        return li;
    }
    function renderNearEmpty() {
        var list = CT.$('#near-list');
        if (!list) return;
        CT.$('#near-empty').hidden = list.children.length > 0;
        CT.$('#near-count').textContent = list.children.length ? '(' + list.children.length + ')' : '';
        renderHistoryEmpty();
    }
    function renderHistoryEmpty() {
        var list = CT.$('#history-list'), near = CT.$('#near-list');
        CT.$('#history-empty').hidden = list.children.length > 0;
        CT.$('#history-clear').disabled = !list.children.length && !(near && near.children.length);
    }
    function bindHistory() {
        var list = CT.$('#history-list');
        var loaded = false;
        var near = CT.$('#near-list');
        function loadHistory() {
            CT.api('GET', '/api/detections/history').then(function (events) {
                loaded = true;
                list.innerHTML = '';
                (Array.isArray(events) ? events : []).slice(0, 20).forEach(function (ev) { list.appendChild(historyItem(ev)); });
                renderHistoryEmpty();
            }).catch(renderHistoryEmpty);
            if (near) CT.api('GET', '/api/detections/near_misses').then(function (items) {
                near.innerHTML = '';
                (Array.isArray(items) ? items : []).slice(0, 20).forEach(function (m) { near.appendChild(nearItem(m)); });
                renderNearEmpty();
            }).catch(renderNearEmpty);
        }
        loadHistory();
        // Apres un redemarrage de l'add-on, l'historique affiche n'existe plus :
        // recharge a chaque reconnexion. La premiere connexion suit le
        // chargement ci-dessus (fait deux fois au demarrage) : rechargement
        // seulement s'il a echoue (add-on encore en demarrage).
        var wasConnected = !!(CT.socket && CT.socket.connected);
        CT.on('connect', function () {
            if (wasConnected || !loaded) loadHistory();
            wasConnected = true;
        });
        function addHistory(ev) {
            list.insertBefore(historyItem(ev || {}), list.firstChild);
            while (list.children.length > 20) list.removeChild(list.lastChild);
            renderHistoryEmpty();
        }
        CT.on('clap', addHistory);
        CT.on('near_miss', function (m) {
            if (!near || !m) return;
            near.insertBefore(nearItem(m), near.firstChild);
            while (near.children.length > 20) near.removeChild(near.lastChild);
            renderNearEmpty();
        });
        // Alerte : au debut seulement (comme l'historique du serveur).
        CT.on('alert', function (ev) { if (ev && ev.alert === 'on') addHistory(ev); });
        CT.$('#history-clear').addEventListener('click', function () {
            var nearList = CT.$('#near-list');
            if (!list.children.length && !(nearList && nearList.children.length)) return;
            // Dialogue de l'interface : window.confirm est peu fiable dans
            // l'application mobile de Home Assistant.
            CT.confirm('Effacer les dernières détections et les « presque » ?', 'Effacer').then(function (ok) {
                if (!ok) return;
                CT.api('DELETE', '/api/detections/history')
                    .then(function () { list.innerHTML = ''; if (near) near.innerHTML = ''; renderHistoryEmpty(); renderNearEmpty(); })
                    .catch(function (err) { CT.error('Historique non effacé : ' + err.message); });
            });
        });
    }

    CT.initStatusbar = function () {
        bindStatusbar();
        bindTabs();
        bindHistory();
    };
})();
