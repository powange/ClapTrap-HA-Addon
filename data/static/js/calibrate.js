/* Calibrage d'une source : prises repetees de chaque bruit, puis reglages
 * proposes (gain, sons, seuil, comptage) et appliques. Voir calibration.py. */
(function () {
    'use strict';
    var CT = window.CT;
    var esc = CT.esc;
    var back, body, release, src, plan, step, active = false, busy = false, starting = false;
    var auto = false, stopAuto = null;   // prises enchainees ; arret du compte a rebours en cours
    // Ouverture en cours : la reponse d'une ouverture precedente (analyse
    // abandonnee en fermant la fenetre) ne remplace pas un autre calibrage.
    var run = 0;

    function cancelAuto() { if (stopAuto) stopAuto(); stopAuto = null; }

    function setTitle(text) { CT.$('#calibration-title').textContent = text; }

    function open(source) {
        src = source;
        back = document.getElementById('calibration');
        body = back.querySelector('[data-role="calibration-body"]');
        back.hidden = false;
        run += 1;
        release = CT.trapFocus(back.querySelector('.modal'), close);
        setup();
    }

    function close() {
        if (!back || back.hidden) return;
        // × et Echap ne faisaient rien pendant une prise, sans retour.
        if (busy) { CT.error('Patientez quelques secondes : enregistrement ou application en cours.'); return; }
        // Fermee pendant le demarrage : le calibrage sera annule a la reponse.
        cancelAuto();
        if (active || starting) CT.api('POST', '/api/calibration/cancel', {}).catch(function () {});
        active = false;
        run += 1;
        back.hidden = true;
        if (release) release();
        release = null;
    }

    // ---- 1. Bruits a calibrer ---------------------------------------------------
    function setup() {
        setTitle('Calibrer « ' + src.name + ' »');
        var groups = (src.data.sound_groups || []).filter(function (g) { return g && g.slug; });
        body.innerHTML =
            '<p>Vous allez jouer plusieurs fois chaque bruit à reconnaître, puis le bruit de fond normal de la pièce. ' +
            'ClapTrap en déduit le gain de la source et, pour chaque groupe, les sons à cocher, le seuil et le mode de comptage. ' +
            'Pendant le calibrage, la source ne déclenche rien.</p>' +
            '<fieldset class="field"><legend class="field-label">Bruits à calibrer</legend>' +
            groups.map(function (g) {
                var n = (g.ha_entities && g.ha_entities.length) ? Math.min.apply(null, g.ha_entities) : 1;
                return '<div class="cal-row"><label class="check"><input type="checkbox" data-cal="' + esc(g.slug) + '"' +
                    (g.enabled !== false ? ' checked' : '') + '> ' + esc(g.name || g.slug) + '</label>' +
                    '<label class="cal-expected">pics par essai <input type="number" class="input" min="1" max="10" value="' + n +
                    '" data-cal-expected="' + esc(g.slug) + '" aria-label="Pics attendus par essai pour ' + esc(g.name || g.slug) + '"></label></div>';
            }).join('') +
            '<div class="cal-row"><input type="text" class="input" id="cal-new" placeholder="Nouveau bruit (ex. Siffler)" aria-label="Nom d\'un nouveau bruit à reconnaître">' +
            '<label class="cal-expected">pics par essai <input type="number" class="input" min="1" max="10" value="1" id="cal-new-expected" aria-label="Pics attendus par essai pour le nouveau bruit"></label></div>' +
            '<p class="hint">« Pics par essai » : nombre de claps (ou de coups) faits à chaque essai. Pour un sifflement, indiquez le nombre de claps de l\'entité visée.</p></fieldset>' +
            '<div class="field"><label class="field-label" for="cal-reps">Essais par bruit</label>' +
            '<input type="number" class="input" id="cal-reps" min="2" max="10" value="5"></div>' +
            '<label class="check"><input type="checkbox" id="cal-bg" checked> Enregistrer aussi le bruit de fond (recommandé)</label>' +
            CT.autoTakesHtml('cal-auto') +
            '<div class="modal-actions"><button type="button" class="btn btn-ghost" data-c="cancel">Annuler</button>' +
            '<button type="button" class="btn btn-primary" data-c="start">Commencer</button></div>';
        body.querySelector('[data-c="cancel"]').addEventListener('click', close);
        body.querySelector('[data-c="start"]').addEventListener('click', function () {
            var reps = Math.max(2, Math.min(10, parseInt(CT.$('#cal-reps').value, 10) || 5));
            var targets = groups.filter(function (g) { return body.querySelector('[data-cal="' + CT.cssEscape(g.slug) + '"]').checked; })
                .map(function (g) {
                    return {target: g.slug, name: g.name || g.slug,
                            expected: parseInt(body.querySelector('[data-cal-expected="' + CT.cssEscape(g.slug) + '"]').value, 10) || 1};
                });
            var extra = CT.$('#cal-new').value.trim();
            if (extra) targets.push({target: extra, name: extra, expected: parseInt(CT.$('#cal-new-expected').value, 10) || 1, isNew: true});
            if (!targets.length) { CT.error('Choisissez au moins un bruit à calibrer.'); return; }
            auto = CT.autoTakes(CT.$('#cal-auto').checked);
            plan = [];
            if (CT.$('#cal-bg').checked) plan.push({target: 'fond', name: 'bruit de fond', duration: 5});
            targets.forEach(function (t) {
                for (var i = 1; i <= reps; i++) plan.push({target: t.target, name: t.name, expected: t.expected, n: i, of: reps});
            });
            var btn = this;
            btn.disabled = true;
            btn.textContent = 'Préparation…';
            starting = true;
            var mine = run;
            CT.api('POST', '/api/calibration/start', {source: src.kind === 'mic' ? 'mic' : src.kind + ':' + src.key})
                .then(function () {
                    if (mine !== run) {
                        // Fenetre fermee entre-temps : ne pas laisser la source en
                        // sourdine, sauf si un autre calibrage a ete ouvert
                        // depuis (l'annulation le tuerait).
                        if (back.hidden) CT.api('POST', '/api/calibration/cancel', {}).catch(function () {});
                        return;
                    }
                    starting = false;
                    if (back.hidden) {
                        CT.api('POST', '/api/calibration/cancel', {}).catch(function () {});
                        return;
                    }
                    active = true; step = 0; takeStep();
                })
                .catch(function (err) {
                    if (mine !== run) return;
                    starting = false;
                    btn.disabled = false; btn.textContent = 'Commencer';
                    CT.error('Calibrage impossible : ' + err.message);
                });
        });
        CT.focusFirst(body);
    }

    // ---- 2. Prises ------------------------------------------------------------------
    function instruction(p) {
        if (p.target === 'fond') return 'Laissez la pièce dans son état habituel (télé, conversation…) sans faire le bruit à reconnaître.';
        return 'Jouez « ' + esc(p.name) + ' »' + (p.expected > 1 ? ' : ' + p.expected + ' fois, à votre rythme habituel' : ' une fois') +
            ', comme pour déclencher le groupe.';
    }

    function takeStep(lastResult) {
        cancelAuto();
        var p = plan[step];
        setTitle('Calibrer « ' + src.name + ' » · prise ' + (step + 1) + '/' + plan.length);
        body.innerHTML =
            (lastResult ? '<p class="hint cal-last">' + lastResult + '</p>' : '') +
            '<h3 class="cal-target">' + (p.target === 'fond' ? 'Bruit de fond' : esc(p.name) + ' · essai ' + p.n + '/' + p.of) + '</h3>' +
            '<p>' + instruction(p) + '</p>' +
            '<p class="cal-countdown" aria-live="polite"></p>' +
            '<div class="modal-actions"><button type="button" class="btn btn-ghost" data-c="cancel">Annuler</button>' +
            (step > 0 && plan.slice(step).every(function (q) { return q.target !== 'fond'; }) && enoughTakes() ?
                '<button type="button" class="btn btn-ghost" data-c="analyze">Analyser maintenant</button>' : '') +
            (auto ? '<button type="button" class="btn btn-ghost" data-c="pause">Pause</button>' : '') +
            '<button type="button" class="btn btn-primary" data-c="record">Enregistrer (' + (p.duration || 4) + ' s)</button></div>';
        body.querySelector('[data-c="cancel"]').addEventListener('click', close);
        var an = body.querySelector('[data-c="analyze"]');
        if (an) an.addEventListener('click', function () { cancelAuto(); analyze(); });
        var rec = body.querySelector('[data-c="record"]');
        rec.addEventListener('click', function () { record(p, rec); });
        if (auto) stopAuto = CT.autoTake(body, rec, function () { record(p, rec); });
        else rec.focus();
    }

    // Analyse possible si chaque bruit deja enregistre a au moins 2 prises.
    function enoughTakes() {
        var done = {};
        plan.slice(0, step).forEach(function (q) { done[q.target] = (done[q.target] || 0) + 1; });
        var keys = Object.keys(done).filter(function (k) { return k !== 'fond'; });
        return keys.length > 0 && keys.every(function (k) { return done[k] >= 2; });
    }

    function record(p, btn) {
        cancelAuto();
        busy = true;
        btn.disabled = true;
        body.querySelector('[data-c="cancel"]').disabled = true;
        var out = body.querySelector('.cal-countdown');
        var stopTimer = CT.recordCountdown(out, p.duration || 4, p.target === 'fond' ? 'Écoute… ' : 'Allez-y ! ');
        CT.api('POST', '/api/calibration/record', {target: p.target, expected: p.expected || null, duration: p.duration || 4})
            .then(function (t) {
                stopTimer();
                busy = false;
                var top = (t.top || []).map(function (c) { return CT.soundLabel(c.classe) + ' ' + CT.pct(c.score); }).join(', ');
                var summary = esc(p.name) + ' : ' + (top ? 'reconnu ' + esc(top) : 'aucun son reconnu') +
                    (p.target !== 'fond' ? ' · pics : ' + t.attacks + ' (attaques), ' + t.sustains + ' (sons tenus)' : '') +
                    (t.clipped ? ' · <strong>saturé</strong>' : t.limited ? ' · atténué (limiteur)' : '');
                step += 1;
                if (step >= plan.length) analyze(summary); else takeStep(summary);
            })
            .catch(function (err) {
                stopTimer();
                busy = false;
                btn.disabled = false;
                body.querySelector('[data-c="cancel"]').disabled = false;
                CT.calState(out, '', '');
                CT.error('Prise impossible : ' + err.message);
            });
    }

    // ---- 3. Propositions ----------------------------------------------------------
    function pctOrDash(v) { return v == null ? '—' : CT.pct(v); }
    function sounds(list) { return list.map(function (l) { return esc(CT.soundLabel(l)); }).join(', '); }

    function groupHtml(g, i) {
        if (g.error) {
            return '<div class="cal-result"><h4>' + esc(g.target) + '</h4><p class="hint">' + esc(g.error) + '</p></div>';
        }
        var mode = function (m) { return m === 'tenus' ? 'sons tenus' : 'attaques'; };
        return '<div class="cal-result"><label class="check"><input type="checkbox" data-apply="' + i + '" checked> <strong>' +
            esc(g.target) + '</strong>' + (g.new ? ' <span class="pill">nouveau groupe</span>' : '') + '</label><ul>' +
            '<li>Sons : ' + sounds(g.sounds) + (g.add.length ? ' (ajout : ' + sounds(g.add) + ')' : '') +
            (g.remove.length ? ' · retirés : ' + sounds(g.remove) : '') + '</li>' +
            '<li>Seuil : ' + (g.threshold.current != null ? CT.pct(g.threshold.current) + ' → ' : '') + CT.pct(g.threshold.proposed) + '</li>' +
            '<li>Comptage : ' + (g.count_mode.current ? mode(g.count_mode.current) + ' → ' : '') + mode(g.count_mode.proposed) + '</li>' +
            '<li>Reconnu : ' + (g.before ? pctOrDash(g.before.recall) + ' → ' : '') + pctOrDash(g.after.recall) + ' des essais · ' +
            'fausses détections : ' + (g.before ? pctOrDash(g.before.false_positive) + ' → ' : '') + pctOrDash(g.after.false_positive) + '</li>' +
            (g.warnings || []).map(function (w) { return '<li class="text-warn">' + esc(w) + '</li>'; }).join('') + '</ul></div>';
    }

    function analyze(lastResult) {
        setTitle('Calibrer « ' + src.name + ' » · propositions');
        body.innerHTML = (typeof lastResult === 'string' ? '<p class="hint cal-last">' + lastResult + '</p>' : '') + '<p class="cal-countdown" data-state="analyze" aria-live="polite">Analyse des prises…</p>';
        var mine = run;
        CT.api('POST', '/api/calibration/analyze', {})
            .then(function (p) {
                if (mine !== run) return;   // fenetre fermee (ou autre source) entre-temps
                var gain = p.gain || {};
                body.innerHTML =
                    (gain.field ? '<div class="cal-result"><label class="check"><input type="checkbox" id="cal-apply-gain"' + (gain.change ? ' checked' : ' disabled') + '> <strong>' +
                        (gain.field === 'volume' ? 'Volume du micro' : 'Gain') + '</strong> : ' +
                        (gain.change ? gain.current + ' → ' + gain.proposed : gain.current + ' (inchangé, déjà adapté)') + '</label></div>'
                        : '<p class="hint">' + esc(gain.note || '') + '</p>') +
                    p.groups.map(groupHtml).join('') +
                    (p.warnings || []).map(function (w) { return '<p class="hint text-warn">' + esc(w) + '</p>'; }).join('') +
                    '<div class="modal-actions"><button type="button" class="btn btn-ghost" data-c="cancel">Annuler</button>' +
                    '<button type="button" class="btn btn-primary" data-c="apply">Appliquer</button></div>';
                body.querySelector('[data-c="cancel"]').addEventListener('click', close);
                var apply = body.querySelector('[data-c="apply"]');
                apply.addEventListener('click', function () {
                    var chosen = p.groups.filter(function (g, i) {
                        var cb = body.querySelector('[data-apply="' + i + '"]');
                        return cb && cb.checked;
                    }).map(function (g) { return g.target; });
                    var gainCb = CT.$('#cal-apply-gain');
                    apply.disabled = true;
                    busy = true;
                    CT.api('POST', '/api/calibration/apply', {gain: !!(gainCb && gainCb.checked), groups: chosen})
                        .then(function (r) {
                            busy = false;
                            active = false;
                            if (r.errors && r.errors.length) CT.error('Calibrage appliqué en partie : ' + r.errors.join(' ; '));
                            else CT.success('Calibrage appliqué (' + r.applied.length + ' réglage(s))');
                            close();
                            return CT.refresh();
                        })
                        .catch(function (err) { busy = false; apply.disabled = false; CT.error('Application impossible : ' + err.message); });
                });
                apply.focus();
            })
            .catch(function (err) {
                if (mine !== run) return;
                // Les prises restent enregistrees : reprendre plutot que tout perdre.
                var more = step < plan.length;
                body.innerHTML = '<p>' + esc(err.message) + '</p><div class="modal-actions">' +
                    '<button type="button" class="btn btn-ghost" data-c="cancel">Abandonner</button>' +
                    (more ? '<button type="button" class="btn btn-primary" data-c="resume">Continuer les prises</button>' : '') + '</div>';
                body.querySelector('[data-c="cancel"]').addEventListener('click', close);
                var resume = body.querySelector('[data-c="resume"]');
                if (resume) { resume.addEventListener('click', function () { takeStep(); }); resume.focus(); }
            });
    }

    CT.openCalibration = open;
    CT.initCalibration = function () {
        var b = document.getElementById('calibration');
        if (!b) return;
        b.addEventListener('click', function (e) { if (e.target === b) close(); });
        b.querySelector('[data-c="close"]').addEventListener('click', close);
    };
})();
