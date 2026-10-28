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
            '<span class="history-main"><strong>' + (ev.alert ? 'Alerte' : n + ' clap' + (n > 1 ? 's' : '') +
                // Rythme avec pause(s) : « 3 claps (2+1) »
                (ev.rhythm && ev.rhythm.indexOf('+') !== -1 ? ' (' + CT.esc(ev.rhythm) + ')' : '')) + '</strong> · ' +
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
        var forces = roomForcesText(ev);
        if (forces) {
            var rf = document.createElement('span');
            rf.className = 'history-labels room-forces';
            rf.textContent = forces;
            li.querySelector('.history-main').appendChild(rf);
        }
        if (ev.id && !ev.ignored) {
            var toggle = document.createElement('button');
            toggle.type = 'button';
            toggle.className = 'btn-link history-wrong';
            toggle.textContent = 'Ce n\'était pas ça';
            toggle.setAttribute('aria-expanded', 'false');
            li.querySelector('.history-main').appendChild(toggle);
            toggle.addEventListener('click', function () {
                var panel = li.querySelector('.history-feedback');
                if (panel) {
                    panel.hidden = !panel.hidden;
                } else {
                    panel = feedbackPanel(ev);
                    li.appendChild(panel);
                }
                toggle.setAttribute('aria-expanded', String(!panel.hidden));
                var first = panel.querySelector('button');
                if (!panel.hidden && first) first.focus();
            });
        }
        return li;
    }
    // Arbitrage entre pieces : force vue par chaque source, corrigee, la
    // gagnante en premier.
    function x(v) { return '× ' + String(Math.round(v * 10) / 10).replace('.', ','); }
    function xw(v) { return '× ' + String(Math.round(v * 100) / 100).replace('.', ','); }   // correction : 2 decimales
    function roomForcesText(ev) {
        var f = ev.room_forces;
        if (!f || f.length < 2) return '';
        var rows = f.map(function (s) { return {s: s, v: (s.strength || 0) * (s.weight == null ? 1 : s.weight)}; });
        rows.sort(function (a, b) { return (b.s.useful !== false) - (a.s.useful !== false) || b.v - a.v; });
        return 'Arbitrage : ' + rows.map(function (r) {
            var w = r.s.weight == null ? 1 : r.s.weight;
            return r.s.name + ' ' + x(r.s.strength || 0) + (w !== 1 ? ' → ' + x(r.v) + ' (correction ' + xw(w) + ')' : '') +
                (r.s.useful === false ? ' (nombre de claps sans entité)' : '');
        }).join(' · ');
    }
    // « Ce n'etait pas ca » : corrections proposees pour une detection.
    // Meme calcul que le serveur : score de l'historique (3 decimales) + 1 point.
    function raisedThreshold(score) { return Math.round(Math.min(0.99, Math.round((score || 0) * 1000) / 1000 + 0.01) * 100) / 100; }
    function feedbackActions(ev) {
        var acts = [];
        (ev.labels || []).forEach(function (l) {
            var name = '« ' + CT.soundLabel(l.label) + ' »';
            if (l.label.indexOf(CT.LEARNED_PREFIX) === 0 && (ev.negatives || []).indexOf(l.label) >= 0) {
                acts.push({action: 'negative', label: l.label, text: 'Retenir ce son comme différent de ' + name,
                           done: 'Contre-exemple ajouté à ' + name});
            } else {
                acts.push({action: 'ignore', label: l.label, text: 'Ne plus réagir à ' + name + ' dans ce groupe',
                           done: name + ' décoché dans le groupe « ' + (ev.group_name || '') + ' »'});
            }
        });
        // Mauvaise piece : la correction de l'arbitrage est recalculee pour
        // que la bonne source gagne ce clap.
        var rf = ev.room_forces && ev.room_forces.length > 1 ? ev.room_forces : [];
        var names = {}, mine = rf.filter(function (s) { return s.source_id === ev.source_id; })[0] || {};
        rf.forEach(function (s) { names[s.source_id] = s.name; });
        rf.forEach(function (s) {
            // Comme le serveur : sans force mesuree, ou nombre de claps sans
            // entite face a une gagnante qui en a une, aucune correction n'y fait.
            if (s.source_id === ev.source_id || !(s.strength > 0) || (s.useful === false && mine.useful !== false)) return;
            acts.push({action: 'room', label: s.source_id, text: 'Le clap venait de « ' + s.name + ' » (pas la bonne pièce)',
                       done: function (d) {
                           var w = d.weights || {};
                           return 'Corrections de l\'arbitrage : ' + Object.keys(w).map(function (sid) { return (names[sid] || sid) + ' ' + xw(w[sid]); }).join(', ') +
                                  ' (Réglages › Plusieurs pièces)';
                       }});
        });
        if ((ev.score || 0) < 0.99) {
            var t = raisedThreshold(ev.score);
            acts.push({action: 'threshold', label: null, text: 'Relever le seuil du groupe à ' + CT.pct(t),
                       done: function (d) {
                           return 'Seuil du groupe « ' + (ev.group_name || '') + ' » relevé à ' + CT.pct(d.threshold) +
                                  ' (modifiable dans les réglages de la source)';
                       }});
        }
        return acts;
    }
    function feedbackPanel(ev) {
        var panel = document.createElement('div');
        panel.className = 'history-feedback';
        var acts = feedbackActions(ev);
        panel.innerHTML = '<span class="hint">' + (acts.length ? 'Pour que ça ne se reproduise pas :' : 'Aucune correction possible.') + '</span>';
        var done = ev.feedback || [];
        acts.forEach(function (a) {
            var b = document.createElement('button');
            b.type = 'button';
            b.className = 'btn btn-ghost';
            b.textContent = a.text;
            var already = done.some(function (f) { return f.action === a.action && (f.label || null) === a.label; });
            if (already) { b.disabled = true; b.textContent = '✓ ' + a.text; }
            b.addEventListener('click', function () {
                b.disabled = true;
                CT.api('POST', '/api/detections/' + encodeURIComponent(ev.id) + '/feedback', {action: a.action, label: a.label})
                    .then(function (d) {
                        ev.feedback = d.feedback || [];
                        b.textContent = '✓ ' + a.text;
                        CT.success(typeof a.done === 'function' ? a.done(d) : a.done);
                        if (d.warning) CT.toast(d.warning.charAt(0).toUpperCase() + d.warning.slice(1) + '.');
                        if (a.action === 'negative') { if (CT.reloadLearned) CT.reloadLearned(); }
                        else CT.reloadSettings();
                    })
                    .catch(function (err) { b.disabled = false; CT.error('Correction impossible : ' + err.message); });
            });
            panel.appendChild(b);
        });
        return panel;
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
                    'Baissez « Force minimale d\'un clap » (réglages de la source, ou Réglages › Comptage pour toutes), ou rapprochez la source.'];
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
            '<span class="history-main"><span class="pill log-kind">Presque</span><strong>' + CT.esc(m.group || '') + '</strong> · ' +
            CT.esc(m.source_name || sourceNameFor(m.source_id)) +
            '<span class="history-labels">' + t[0] + '</span><span class="history-labels near-hint">' + t[1] + '</span></span>';
        return li;
    }

    // ---- Journal : detections et « presque », filtres par type et source -------
    // Plafond par type : des « presque » frequents (portes, chocs) ne
    // chassent pas les vraies detections et leur « Ce n'etait pas ça ».
    var LOG_MAX = {detection: 30, near: 30};
    var logFilter = 'all';
    function logEntry(li, kind, item, ts) {
        li.setAttribute('data-kind', kind);
        li.setAttribute('data-source', item.source_id || '');
        li.setAttribute('data-source-name', item.source_name || '');
        li.setAttribute('data-ts', String(ts || Date.now() / 1000));
        return li;
    }
    // Detections : instant du 1er clap (timestamp : envoi, 1,5 a 2,5 s plus
    // tard), comme les « presque » : un seul ordre, celui des gestes.
    function clapEntry(ev) { return logEntry(historyItem(ev), ev.ignored ? 'ignored' : 'clap', ev, ev.clap_time || ev.timestamp); }
    function nearEntry(m) { return logEntry(nearItem(m), 'near', m, m.timestamp); }
    function family(li) { return li.getAttribute('data-kind') === 'near' ? 'near' : 'detection'; }
    // Le plus recent en haut, a sa place : un « presque » est note quelques
    // secondes apres son geste.
    function insertLog(list, li) {
        var ts = +li.getAttribute('data-ts');
        var before = CT.$$('li', list).find(function (x) { return +x.getAttribute('data-ts') <= ts; });
        list.insertBefore(li, before || null);
        var fam = family(li);
        var same = CT.$$('li', list).filter(function (x) { return family(x) === fam; });
        same.slice(LOG_MAX[fam]).forEach(function (x) { list.removeChild(x); });
    }
    function fillSources() {
        var sel = CT.$('#log-source');
        if (!sel) return;
        var cur = sel.value, seen = {};
        var opts = CT.sourceList().map(function (src) { seen[src.sourceId] = true; return [src.sourceId, src.name]; });
        // Sources supprimees encore presentes dans le journal
        CT.$$('#history-list > li').forEach(function (li) {
            var sid = li.getAttribute('data-source');
            if (sid && !seen[sid]) { seen[sid] = true; opts.push([sid, li.getAttribute('data-source-name') || sid]); }
        });
        sel.innerHTML = '<option value="">Toutes les sources</option>' + opts.map(function (o) {
            return '<option value="' + CT.esc(o[0]) + '">' + CT.esc(o[1]) + '</option>';
        }).join('');
        sel.value = seen[cur] ? cur : '';
        if (sel.value !== cur) applyLogFilter();   // source disparue : filtre retire
    }
    function applyLogFilter() {
        var list = CT.$('#history-list'), sel = CT.$('#log-source');
        var source = sel ? sel.value : '';
        var counts = {all: 0, clap: 0, ignored: 0, near: 0}, shown = 0;
        CT.$$('li', list).forEach(function (li) {
            var kind = li.getAttribute('data-kind');
            var mine = !source || li.getAttribute('data-source') === source;
            if (mine) { counts.all++; counts[kind]++; }
            li.hidden = !mine || (logFilter !== 'all' && kind !== logFilter);
            if (!li.hidden) shown++;
        });
        logChips.forEach(function (b) {
            var f = b.getAttribute('data-log-filter'), n = counts[f];
            b.setAttribute('aria-pressed', String(f === logFilter));
            b.querySelector('[data-log-count]').textContent = n ? '(' + n + ')' : '';
        });
        var empty = CT.$('#history-empty');
        empty.hidden = shown > 0;
        empty.textContent = list.children.length ? 'Rien pour ce filtre.'
            : 'Aucun clap pour l\'instant : démarrez la détection et tapez dans vos mains.';
        var help = CT.$('#near-help');
        if (help) help.hidden = logFilter !== 'near';
        CT.$('#history-clear').disabled = !list.children.length;
    }
    var logChips = [];
    function bindHistory() {
        var list = CT.$('#history-list');
        var loaded = false;
        logChips = CT.$$('[data-log-filter]');
        function loadHistory() {
            // Chargements independants : un echec n'efface pas l'autre liste.
            var none = function () { return null; };
            Promise.all([
                CT.api('GET', '/api/detections/history').then(function (events) { loaded = true; return events; }, none),
                CT.api('GET', '/api/detections/near_misses').catch(none)
            ]).then(function (r) {
                if (r[0] === null && r[1] === null) { applyLogFilter(); return; }
                var keep = function (items, fam) {
                    return (Array.isArray(items) ? items : []).slice(0, LOG_MAX[fam]).map(fam === 'near' ? nearEntry : clapEntry);
                };
                var old = CT.$$('li', list);
                var entries = (r[0] === null ? old.filter(function (li) { return family(li) === 'detection'; }) : keep(r[0], 'detection'))
                    .concat(r[1] === null ? old.filter(function (li) { return family(li) === 'near'; }) : keep(r[1], 'near'));
                entries.sort(function (x, y) { return +y.getAttribute('data-ts') - +x.getAttribute('data-ts'); });
                list.innerHTML = '';
                entries.forEach(function (li) { list.appendChild(li); });
                fillSources();
                applyLogFilter();
            });
        }
        loadHistory();
        // Apres un redemarrage de l'add-on, le journal a pu changer (claps
        // pendant l'arret de l'onglet) : recharge a chaque reconnexion. La premiere connexion suit le
        // chargement ci-dessus (fait deux fois au demarrage) : rechargement
        // seulement s'il a echoue (add-on encore en demarrage).
        var wasConnected = !!(CT.socket && CT.socket.connected);
        CT.on('connect', function () {
            if (wasConnected || !loaded) loadHistory();
            wasConnected = true;
        });
        function add(li) {
            insertLog(list, li);
            applyLogFilter();
        }
        CT.on('clap', function (ev) { add(clapEntry(ev || {})); });
        CT.on('near_miss', function (m) { if (m) add(nearEntry(m)); });
        // Alerte : au debut seulement (comme l'historique du serveur).
        CT.on('alert', function (ev) { if (ev && ev.alert === 'on') add(clapEntry(ev)); });
        logChips.forEach(function (b) {
            b.addEventListener('click', function () { logFilter = b.getAttribute('data-log-filter'); applyLogFilter(); });
        });
        var sel = CT.$('#log-source');
        if (sel) {
            // Sources ajoutees ou renommees depuis : avant l'ouverture de la
            // liste (pas au focus, qui l'ouvre deja sur mobile).
            sel.addEventListener('pointerdown', fillSources);
            sel.addEventListener('keydown', function (e) { if (e.key !== 'Tab') fillSources(); });
            sel.addEventListener('change', applyLogFilter);
        }
        CT.$('#history-clear').addEventListener('click', function () {
            if (!list.children.length) return;
            // Dialogue de l'interface : window.confirm est peu fiable dans
            // l'application mobile de Home Assistant.
            CT.confirm('Effacer tout le journal (détections et « presque ») ?', 'Effacer').then(function (ok) {
                if (!ok) return;
                CT.api('DELETE', '/api/detections/history')
                    .then(function () { list.innerHTML = ''; applyLogFilter(); })
                    .catch(function (err) { CT.error('Journal non effacé : ' + err.message); });
            });
        });
    }

    CT.initStatusbar = function () {
        bindStatusbar();
        bindTabs();
        bindHistory();
    };
})();
