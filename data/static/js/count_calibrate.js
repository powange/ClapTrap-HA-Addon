/* Calibrage du comptage des claps (reglages globaux : fenetre, ecart minimal,
 * force minimale). Prises guidees sur une source, puis valeurs proposees,
 * verifiees en rejouant les prises. Voir count_calibration.py. */
(function () {
    'use strict';
    var CT = window.CT;
    var esc = CT.esc;
    var back, body, release, plan, step, srcName, active = false, busy = false, starting = false;
    var run = 0;   // ouverture en cours : une reponse d'une ouverture precedente est ignoree

    var STEPS = [
        {kind: 'fond', title: 'Bruit de fond', duration: 5,
         text: 'Restez silencieux : seul le bruit normal de la pièce doit être entendu.'},
        {kind: 'simple', title: 'Clap simple', repeat: 3, duration: 3,
         text: 'Faites <strong>un</strong> clap, normalement, comme pour une commande.'},
        {kind: 'double', title: '2 claps', repeat: 3, duration: 4,
         text: 'Faites <strong>2 claps</strong> comme pour la commande « 2 claps ».'},
        {kind: 'triple', title: '3 claps', repeat: 2, duration: 4,
         text: 'Faites <strong>3 claps</strong> comme pour la commande « 3 claps ».'},
        {kind: 'separe', title: 'Deux commandes', optional: true, duration: 7,
         text: 'Faites <strong>2 claps</strong>, attendez comme entre deux commandes, puis <strong>1 clap</strong>. ' +
               'Vérifie que la fenêtre ne fusionne pas deux commandes.'}
    ];
    var LABELS = {delay: 'Fenêtre multi-clap', peak_cooldown: 'Écart minimal entre deux claps', peak_ratio: 'Force minimale d\'un clap'};
    var UNITS = {delay: ' s', peak_cooldown: ' s', peak_ratio: ' × le bruit'};

    function setTitle(text) { CT.$('#count-calibration-title').textContent = text; }
    function fmt(key, v) { return String(v).replace('.', ',') + UNITS[key]; }

    function open() {
        back = document.getElementById('count-calibration');
        body = back.querySelector('[data-role="count-calibration-body"]');
        back.hidden = false;
        run += 1;
        release = CT.trapFocus(back.querySelector('.modal'), close);
        choose();
    }

    function close() {
        if (!back || back.hidden) return;
        if (busy) { CT.error('Prise en cours : patientez quelques secondes.'); return; }
        if (active || starting) CT.api('POST', '/api/calibration/cancel', {}).catch(function () {});
        active = false;
        run += 1;
        back.hidden = true;
        if (release) release();
        release = null;
    }

    // ---- 1. Source de mesure ----------------------------------------------------
    function choose() {
        setTitle('Calibrer le comptage');
        var sources = CT.sourceList().filter(function (s) { return s.enabled; });
        body.innerHTML =
            '<p>Ces réglages valent pour toutes les sources. Ils sont mesurés sur celle que vous utilisez le plus : ' +
            'bruit de fond, force de vos claps, rebonds et durée de vos gestes. Comptez environ 2 minutes. ' +
            'Pendant le calibrage, la source ne déclenche rien.</p>' +
            (sources.length
                ? '<div class="field"><label class="field-label" for="cc-source">Source de mesure</label>' +
                  '<select class="input" id="cc-source">' + sources.map(function (s) {
                      return '<option value="' + esc(s.kind === 'mic' ? 'mic' : s.kind + ':' + s.key) + '">' + esc(s.name) + '</option>';
                  }).join('') + '</select></div>'
                : '<p class="hint text-warn">Aucune source active : activez une source pour calibrer le comptage.</p>') +
            '<label class="check"><input type="checkbox" id="cc-separe" checked> Vérifier aussi deux commandes séparées (recommandé)</label>' +
            '<div class="modal-actions"><button type="button" class="btn btn-ghost" data-c="cancel">Annuler</button>' +
            (sources.length ? '<button type="button" class="btn btn-primary" data-c="start">Commencer</button>' : '') + '</div>';
        body.querySelector('[data-c="cancel"]').addEventListener('click', close);
        var startBtn = body.querySelector('[data-c="start"]');
        if (!startBtn) return;
        startBtn.addEventListener('click', function () {
            var sel = CT.$('#cc-source');
            srcName = sel.options[sel.selectedIndex].textContent;
            var withSepare = CT.$('#cc-separe').checked;
            plan = [];
            STEPS.forEach(function (s) {
                if (s.optional && !withSepare) return;
                for (var i = 1; i <= (s.repeat || 1); i++) plan.push({kind: s.kind, title: s.title, text: s.text, duration: s.duration, n: i, of: s.repeat || 1});
            });
            startBtn.disabled = true;
            starting = true;
            CT.api('POST', '/api/calibration/count/start', {source: sel.value})
                .then(function () {
                    starting = false;
                    if (back.hidden) { CT.api('POST', '/api/calibration/cancel', {}).catch(function () {}); return; }
                    active = true;
                    step = 0;
                    takeStep();
                })
                .catch(function (err) { starting = false; startBtn.disabled = false; CT.error('Calibrage impossible : ' + err.message); });
        });
        if (startBtn) startBtn.focus();
    }

    // ---- 2. Prises -------------------------------------------------------------
    function takeStep(lastResult) {
        var p = plan[step];
        setTitle('Calibrer le comptage · prise ' + (step + 1) + '/' + plan.length);
        body.innerHTML =
            (lastResult ? '<p class="hint cal-last">' + lastResult + '</p>' : '') +
            '<h3 class="cal-target">' + esc(p.title) + (p.of > 1 ? ' · essai ' + p.n + '/' + p.of : '') + '</h3>' +
            '<p>' + p.text + ' Commencez juste après avoir cliqué.</p>' +
            '<p class="cal-countdown" aria-live="polite"></p>' +
            '<div class="modal-actions"><button type="button" class="btn btn-ghost" data-c="cancel">Annuler</button>' +
            (p.kind === 'separe' ? '<button type="button" class="btn btn-ghost" data-c="skip">Passer</button>' : '') +
            '<button type="button" class="btn btn-primary" data-c="record">Enregistrer (' + p.duration + ' s)</button></div>';
        body.querySelector('[data-c="cancel"]').addEventListener('click', close);
        var skip = body.querySelector('[data-c="skip"]');
        if (skip) skip.addEventListener('click', next);
        var rec = body.querySelector('[data-c="record"]');
        rec.addEventListener('click', function () { record(p, rec); });
        rec.focus();
    }

    function record(p, btn) {
        busy = true;
        btn.disabled = true;
        var out = body.querySelector('.cal-countdown');
        var left = p.duration;
        out.textContent = 'Enregistrement… ' + left + ' s';
        var timer = setInterval(function () {
            left -= 1;
            out.textContent = left > 0 ? 'Enregistrement… ' + left + ' s' : 'Analyse de la prise…';
        }, 1000);
        CT.api('POST', '/api/calibration/count/record', {kind: p.kind, duration: p.duration})
            .then(function (d) {
                clearInterval(timer);
                busy = false;
                var found = d.claps_found == null ? '' : p.kind === 'fond'
                    ? (d.claps_found ? d.claps_found + ' pic(s) parasite(s) avec les réglages actuels' : 'aucun pic parasite')
                    : (d.gestures || []).join(' + ') + ' clap(s) compté(s) avec les réglages actuels';
                next(esc(p.title) + ' enregistré' + (found ? ' : ' + found + '.' : '.'));
            })
            .catch(function (err) {
                clearInterval(timer);
                busy = false;
                btn.disabled = false;
                out.textContent = '';
                CT.error('Prise impossible : ' + err.message);
            });
    }

    function next(lastResult) {
        step += 1;
        if (step < plan.length) takeStep(typeof lastResult === 'string' ? lastResult : '');
        else analyze();
    }

    // ---- 3. Proposition --------------------------------------------------------
    function analyze() {
        setTitle('Calibrer le comptage · résultat');
        body.innerHTML = '<p class="cal-countdown" aria-live="polite">Analyse des prises…</p>';
        var mine = run;
        CT.api('POST', '/api/calibration/count/analyze', {})
            .then(function (d) { if (mine === run) showProposal(d); })
            .catch(function (err) {
                if (mine !== run) return;
                body.innerHTML = '<p class="hint text-warn">' + esc(err.message) + '</p>' +
                    '<div class="modal-actions"><button type="button" class="btn btn-ghost" data-c="cancel">Fermer</button></div>';
                body.querySelector('[data-c="cancel"]').addEventListener('click', close);
            });
    }

    function showProposal(d) {
        var keys = ['delay', 'peak_cooldown', 'peak_ratio'];
        var steps = {delay: 0.1, peak_cooldown: 0.01, peak_ratio: 0.1};
        var mark = function (ok) { return ok ? '<span class="cc-ok" aria-label="correct">✓</span>' : '<span class="cc-ko" aria-label="incorrect">✗</span>'; };
        var shown = function (g) { return g.length ? g.join(' + ') : 'aucun'; };
        body.innerHTML =
            '<p>Mesuré sur <strong>' + esc(srcName) + '</strong>. Prises correctement comptées : <strong>' +
            d.score.current + '/' + d.score.takes + '</strong> avec les réglages actuels, <strong>' +
            d.score.proposed + '/' + d.score.takes + '</strong> avec les réglages proposés.</p>' +
            (d.warnings || []).map(function (w) { return '<p class="hint text-warn">' + esc(w.charAt(0).toUpperCase() + w.slice(1)) + '.</p>'; }).join('') +
            '<div class="cc-table-wrap"><table class="cc-table"><thead><tr><th>Réglage</th><th>Actuel</th><th>Proposé</th></tr></thead><tbody>' +
            keys.map(function (k) {
                var lim = (d.limits || {})[k] || [];
                return '<tr><th scope="row"><label for="cc-' + k + '">' + LABELS[k] + '</label></th><td>' + fmt(k, d.current[k]) + '</td>' +
                    '<td><input class="input" type="number" id="cc-' + k + '" step="' + steps[k] + '" value="' + d.proposed[k] + '"' +
                    (lim.length ? ' min="' + lim[0] + '" max="' + lim[1] + '"' : '') + '></td></tr>';
            }).join('') + '</tbody></table></div>' +
            '<details class="cc-details"><summary>Détail des prises</summary><div class="cc-table-wrap"><table class="cc-table"><thead><tr>' +
            '<th>Prise</th><th>Attendu</th><th>Actuel</th><th>Proposé</th></tr></thead><tbody>' +
            d.rows.map(function (r) {
                return '<tr><td>' + esc(r.label) + '</td><td>' + shown(r.expected) + '</td><td>' + mark(r.current_ok) + ' ' + shown(r.current) +
                    '</td><td>' + mark(r.proposed_ok) + ' ' + shown(r.proposed) + '</td></tr>';
            }).join('') + '</tbody></table></div></details>' +
            '<div class="modal-actions"><button type="button" class="btn btn-ghost" data-c="cancel">Annuler</button>' +
            '<button type="button" class="btn btn-primary" data-c="apply">Appliquer</button></div>';
        body.querySelector('[data-c="cancel"]').addEventListener('click', close);
        var apply = body.querySelector('[data-c="apply"]');
        apply.addEventListener('click', function () {
            var values = {};
            keys.forEach(function (k) { values[k] = parseFloat(CT.$('#cc-' + k).value); });
            apply.disabled = true;
            busy = true;
            CT.api('POST', '/api/calibration/count/apply', {values: values})
                .then(function (r) {
                    busy = false;
                    active = false;
                    Object.assign(CT.state.settings.global = CT.state.settings.global || {}, r.applied);
                    if (CT.onCountCalibrated) CT.onCountCalibrated();
                    close();
                    CT.success('Réglages du comptage appliqués');
                })
                .catch(function (err) { busy = false; apply.disabled = false; CT.error('Application impossible : ' + err.message); });
        });
        apply.focus();
    }

    var openBtn = document.getElementById('count-calibrate');
    if (openBtn) openBtn.addEventListener('click', open);
    var closeBtn = document.querySelector('#count-calibration [data-c="close"]');
    if (closeBtn) closeBtn.addEventListener('click', close);
})();
