/* Calibrage de l'arbitrage entre pieces : correction (poids) par source.
 * Quelques claps pres de chaque source, entendus par toutes en meme temps,
 * puis poids proposes pour que la source de la piece gagne. Voir
 * room_calibration.py. */
(function () {
    'use strict';
    var CT = window.CT;
    var esc = CT.esc;
    var back, body, release, plan, step, group, active = false, busy = false, starting = false;
    var run = 0;   // ouverture en cours : une reponse d'une ouverture precedente est ignoree
    var REPEAT = 2;

    function setTitle(text) { CT.$('#room-calibration-title').textContent = text; }
    function num(v) { return String(v).replace('.', ','); }

    function open() {
        back = document.getElementById('room-calibration');
        body = back.querySelector('[data-role="room-calibration-body"]');
        back.hidden = false;
        run += 1;
        release = CT.trapFocus(back.querySelector('.modal'), close);
        choose();
    }

    function close() {
        if (!back || back.hidden) return;
        if (busy) { CT.error('Patientez quelques secondes : enregistrement ou application en cours.'); return; }
        CT.closeGuard(active ? step : 0, closeNow, ['prise', 'prises']);
    }

    function closeNow() {
        if (active || starting) CT.api('POST', '/api/calibration/rooms/cancel', {}).catch(function () {});
        active = false;
        run += 1;
        back.hidden = true;
        if (release) release();
        release = null;
    }

    function actions(html) {
        return '<div class="modal-actions"><button type="button" class="btn btn-ghost" data-c="cancel">Annuler</button>' + (html || '') + '</div>';
    }

    // ---- 1. Groupe de sources ----------------------------------------------------
    function choose() {
        setTitle('Calibrer l\'arbitrage');
        body.innerHTML = '<p class="cal-countdown" data-state="analyze" aria-live="polite">Chargement…</p>';
        var mine = run;
        CT.api('GET', '/api/calibration/rooms').then(function (d) {
            if (mine !== run) return;
            var groups = d.groups || [];
            var label = function (g) {
                return (g.zone ? g.zone + ' : ' : '') + g.sources.map(function (s) { return s.name; }).join(', ');
            };
            body.innerHTML =
                '<p>Vous allez faire ' + REPEAT + ' fois 3 claps près de chaque source, dans sa pièce. Toutes les sources ' +
                'écoutent en même temps : ClapTrap mesure la force que chacune donne à vos claps et propose une ' +
                'correction pour que la source de la pièce gagne toujours. Pendant le calibrage, ces sources ne déclenchent rien.</p>' +
                (groups.length
                    ? '<div class="field"><label class="field-label" for="rc-group">Sources à départager</label>' +
                      '<select class="input" id="rc-group">' + groups.map(function (g, i) {
                          return '<option value="' + i + '">' + esc(label(g)) + '</option>';
                      }).join('') + '</select></div>'
                    : '<p class="hint text-warn">Il faut au moins deux sources actives' +
                      (((CT.state.settings.global || {}).room_zones || []).length ? ' dans un même groupe.</p>' : '.</p>')) +
                actions(groups.length ? '<button type="button" class="btn btn-primary" data-c="start">Commencer</button>' : '');
            body.querySelector('[data-c="cancel"]').addEventListener('click', close);
            var startBtn = body.querySelector('[data-c="start"]');
            if (!startBtn) return;
            startBtn.addEventListener('click', function () {
                group = groups[+CT.$('#rc-group').value];
                plan = [];
                group.sources.forEach(function (s) {
                    for (var i = 1; i <= REPEAT; i++) plan.push({source: s, n: i});
                });
                startBtn.disabled = true;
                starting = true;
                var mineStart = run;
                CT.api('POST', '/api/calibration/rooms/start', {zone: group.zone})
                    .then(function () {
                        starting = false;
                        // Fenetre fermee (ou rouverte) entre-temps : session abandonnee.
                        if (mineStart !== run) { CT.api('POST', '/api/calibration/rooms/cancel', {}).catch(function () {}); return; }
                        active = true;
                        step = 0;
                        takeStep();
                    })
                    .catch(function (err) { starting = false; if (mineStart !== run) return; startBtn.disabled = false; CT.error('Calibrage impossible : ' + err.message); });
            });
            startBtn.focus();
        }).catch(function (err) {
            if (mine !== run) return;
            body.innerHTML = '<p class="hint text-warn">' + esc(err.message) + '</p>' + actions();
            body.querySelector('[data-c="cancel"]').addEventListener('click', close);
        });
    }

    // ---- 2. Prises -------------------------------------------------------------
    function takeStep(lastResult) {
        var p = plan[step];
        setTitle('Calibrer l\'arbitrage · prise ' + (step + 1) + '/' + plan.length);
        body.innerHTML =
            (lastResult ? '<p class="hint cal-last">' + lastResult + '</p>' : '') +
            '<h3 class="cal-target">Près de « ' + esc(p.source.name) + ' » · essai ' + p.n + '/' + REPEAT + '</h3>' +
            '<p>Placez-vous dans la pièce de cette source, à l\'endroit où vous tapez d\'habitude. Cliquez, puis faites ' +
            '<strong>3 claps</strong> espacés d\'environ une seconde.</p>' +
            '<p class="cal-countdown" aria-live="polite"></p>' +
            actions('<button type="button" class="btn btn-primary" data-c="record">Enregistrer (5 s)</button>');
        body.querySelector('[data-c="cancel"]').addEventListener('click', close);
        var rec = body.querySelector('[data-c="record"]');
        rec.addEventListener('click', function () { record(p, rec); });
        rec.focus();
    }

    function record(p, btn) {
        busy = true;
        btn.disabled = true;
        var out = body.querySelector('.cal-countdown');
        var stopTimer = CT.recordCountdown(out, 5, 'Enregistrement… ');
        CT.api('POST', '/api/calibration/rooms/record', {source: p.source.source_id})
            .then(function (d) {
                stopTimer();
                busy = false;
                var forces = Object.keys(d.forces).map(function (n) {
                    return esc(n) + ' × ' + (d.forces[n] == null ? '—' : num(d.forces[n]));
                }).join(', ');
                step += 1;
                var msg = 'Force des claps : ' + forces + ' (plus fort, sans correction : ' + esc(d.loudest) + ').';
                if (step < plan.length) takeStep(msg); else analyze();
            })
            .catch(function (err) {
                stopTimer();
                busy = false;
                btn.disabled = false;
                CT.calState(out, '', '');
                CT.error('Prise refusée : ' + err.message);
            });
    }

    // ---- 3. Proposition --------------------------------------------------------
    function analyze() {
        setTitle('Calibrer l\'arbitrage · résultat');
        body.innerHTML = '<p class="cal-countdown" data-state="analyze" aria-live="polite">Analyse des prises…</p>';
        var mine = run;
        CT.api('POST', '/api/calibration/rooms/analyze', {})
            .then(function (d) { if (mine === run) showProposal(d); })
            .catch(function (err) {
                if (mine !== run) return;
                body.innerHTML = '<p class="hint text-warn">' + esc(err.message) + '</p>' +
                    '<div class="modal-actions"><button type="button" class="btn btn-ghost" data-c="cancel">Fermer</button></div>';
                body.querySelector('[data-c="cancel"]').addEventListener('click', close);
            });
    }

    function showProposal(d) {
        var mark = function (ok) { return ok ? '<span class="cc-ok" aria-label="correct">✓</span>' : '<span class="cc-ko" aria-label="incorrect">✗</span>'; };
        body.innerHTML =
            '<p>Prises gagnées par la bonne pièce : <strong>' + d.score.current + '/' + d.score.takes + '</strong> avec les ' +
            'corrections actuelles, <strong>' + d.score.proposed + '/' + d.score.takes + '</strong> avec les corrections proposées' +
            (d.margin > 1 ? ' (marge : la bonne pièce l\'emporte au moins × ' + num(d.margin) + ')' : '') + '.</p>' +
            (d.warnings || []).map(function (w) { return '<p class="hint text-warn">' + esc(w.charAt(0).toUpperCase() + w.slice(1)) + '.</p>'; }).join('') +
            '<div class="cc-table-wrap"><table class="cc-table"><thead><tr><th>Source</th><th>Actuelle</th><th>Proposée</th></tr></thead><tbody>' +
            d.sources.map(function (s, i) {
                return '<tr><th scope="row"><label for="rc-w' + i + '">' + esc(s.name) + '</label></th><td>× ' + num(s.current) + '</td>' +
                    '<td><input class="input" type="number" id="rc-w' + i + '" step="0.01" min="0.05" max="20" value="' + s.proposed + '"></td></tr>';
            }).join('') + '</tbody></table></div>' +
            '<details class="cc-details"><summary>Détail des prises</summary><div class="cc-table-wrap"><table class="cc-table"><thead><tr>' +
            '<th>Claps près de</th><th>Force par source</th><th>Gagnant actuel</th><th>Gagnant proposé</th></tr></thead><tbody>' +
            d.rows.map(function (r) {
                return '<tr><td>' + esc(r.home) + '</td><td>' + r.forces.map(function (f) {
                    return esc(f.source) + ' × ' + (f.force == null ? '—' : num(f.force));
                }).join(', ') + '</td><td>' + mark(r.current_ok) + ' ' + esc(r.current) + '</td><td>' + mark(r.proposed_ok) + ' ' + esc(r.proposed) + '</td></tr>';
            }).join('') + '</tbody></table></div></details>' +
            actions('<button type="button" class="btn btn-primary" data-c="apply">Appliquer</button>');
        body.querySelector('[data-c="cancel"]').addEventListener('click', close);
        var apply = body.querySelector('[data-c="apply"]');
        apply.addEventListener('click', function () {
            var weights = {};
            for (var i = 0; i < d.sources.length; i++) {
                var el = CT.$('#rc-w' + i);
                if (!el.checkValidity()) { CT.error(d.sources[i].name + ' : ' + el.validationMessage); return; }
                weights[d.sources[i].source_id] = parseFloat(el.value);
            }
            apply.disabled = true;
            busy = true;
            CT.api('POST', '/api/calibration/rooms/apply', {weights: weights})
                .then(function (r) {
                    busy = false;
                    active = false;
                    var g = CT.state.settings.global = CT.state.settings.global || {};
                    var all = Object.assign({}, g.room_weights || {}, r.applied);
                    Object.keys(all).forEach(function (k) { if (all[k] === 1) delete all[k]; });
                    g.room_weights = all;
                    if (CT.onRoomsCalibrated) CT.onRoomsCalibrated();
                    close();
                    CT.success('Corrections de l\'arbitrage appliquées');
                })
                .catch(function (err) { busy = false; apply.disabled = false; CT.error('Application impossible : ' + err.message); });
        });
        apply.focus();
    }

    var openBtn = document.getElementById('room-calibrate');
    if (openBtn) openBtn.addEventListener('click', open);
    var closeBtn = document.querySelector('#room-calibration [data-c="close"]');
    if (closeBtn) closeBtn.addEventListener('click', close);
})();
