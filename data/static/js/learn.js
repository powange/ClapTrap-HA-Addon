/* Sons appris : liste (onglet Reglages) et apprentissage guide (bruit de
 * fond, puis 3 a 10 exemples du son). Voir learned.py. */
(function () {
    'use strict';
    var CT = window.CT;
    var esc = CT.esc;
    var back, body, release, active = false, busy = false, starting = false;
    var run = 0;          // ouverture en cours : une reponse d'une ouverture precedente est ignoree
    var limits = {min_examples: 3, max_examples: 10, max_sounds: 20};
    var sounds = [];
    var name = '', examples = 0;
    var relearn = null;   // son reappris (memes nom et id) ; null : nouveau son

    function setTitle(text) { CT.$('#learn-title').textContent = text; }

    // ---- Liste ----------------------------------------------------------------
    function renderList() {
        var ul = CT.$('#learned-list');
        if (!ul) return;
        ul.innerHTML = sounds.length ? sounds.map(function (s) {
            var q = s.quality || {};
            // Source d'apprentissage dont la reduction du bruit a change : les
            // empreintes ne correspondent plus.
            var src = s.source_ref ? CT.sourceList().filter(function (x) {
                return CT.sourceRef(x) === s.source_ref;
            })[0] : null;
            var changed = src && (src.data.noise_reduction || 0) !== (s.noise_reduction || 0);
            return '<li class="learned-item"><div class="learned-main"><strong>' + esc(s.label) + '</strong>' +
                '<span class="hint">' + s.examples + ' exemples' + (s.source ? ' · ' + esc(s.source) : '') +
                (s.negatives ? ' · ' + s.negatives + ' contre-exemple' + (s.negatives > 1 ? 's' : '') : '') +
                (s.noise_reduction ? ' · réduction du bruit ' + esc((CT.NR_NAMES[s.noise_reduction] || '').toLowerCase()) : '') +
                (changed ? ' · <span class="text-warn">la réduction du bruit de cette source a changé depuis : apprenez-le à nouveau</span>' : '') +
                (q.warnings && q.warnings.length ? ' · <span class="text-warn">' + esc(q.warnings[0]) + '</span>' : '') +
                '</span></div><div class="learned-actions">' +
                '<button type="button" class="btn btn-ghost" data-relearn="' + esc(s.id) + '">Réapprendre</button>' +
                '<button type="button" class="btn btn-ghost" data-forget="' + esc(s.id) + '">Supprimer</button></div></li>';
        }).join('') : '<li class="hint">Aucun son appris pour l\'instant.</li>';
        var btn = CT.$('#learn-sound');
        if (btn) btn.disabled = sounds.length >= limits.max_sounds;
    }

    function reload() {
        if (CT.invalidateKnownSounds) CT.invalidateKnownSounds();
        return CT.api('GET', '/api/learned').then(function (d) {
            sounds = d.sounds || [];
            if (d.limits) limits = d.limits;
            renderList();
        }).catch(function () {});
    }
    CT.reloadLearned = reload;

    document.addEventListener('click', function (e) {
        var r = e.target.closest && e.target.closest('[data-relearn]');
        if (r) {
            var target = sounds.filter(function (x) { return x.id === r.getAttribute('data-relearn'); })[0];
            if (target) open(target);
            return;
        }
        var b = e.target.closest && e.target.closest('[data-forget]');
        if (!b) return;
        var s = sounds.filter(function (x) { return x.id === b.getAttribute('data-forget'); })[0];
        if (!s) return;
        CT.confirm('Supprimer « ' + s.label + ' » ? Les groupes qui le cochent ne le reconnaîtront plus.').then(function (ok) {
            if (!ok) return;
            return CT.api('DELETE', '/api/learned/' + encodeURIComponent(s.id)).then(function () {
                CT.success('« ' + s.label + ' » supprimé');
                return reload();
            });
        }).catch(function (err) { CT.error('Suppression impossible : ' + err.message); });
    });

    // ---- Apprentissage ----------------------------------------------------------
    function open(target) {
        relearn = target && target.id ? target : null;
        back = document.getElementById('learn');
        body = back.querySelector('[data-role="learn-body"]');
        back.hidden = false;
        run += 1;
        examples = 0;
        release = CT.trapFocus(back.querySelector('.modal'), close);
        choose();
    }

    function close() {
        if (!back || back.hidden) return;
        if (busy) { CT.error('Patientez quelques secondes : enregistrement en cours.'); return; }
        if (active || starting) CT.api('POST', '/api/calibration/cancel', {}).catch(function () {});
        active = false;
        run += 1;
        back.hidden = true;
        if (release) release();
        release = null;
    }

    function actions(extra) {
        return '<div class="modal-actions"><button type="button" class="btn btn-ghost" data-c="cancel">Annuler</button>' + extra + '</div>';
    }

    function choose() {
        setTitle(relearn ? 'Réapprendre « ' + relearn.name + ' »' : 'Apprendre un son');
        var list = CT.sourceList().filter(function (s) { return s.enabled; });
        body.innerHTML =
            (relearn
                ? '<p>Les nouveaux exemples remplacent les anciens. Le nom ne change pas : les groupes qui cochent « ' +
                  esc(relearn.label) + ' » le gardent' + (relearn.negatives ? ', ses contre-exemples aussi' : '') + '. ' +
                  'Faites entendre le son ' + limits.min_examples + ' à ' + limits.max_examples +
                  ' fois (5 conseillées). Pendant l\'apprentissage, la source ne déclenche rien.</p>'
                : '<p>Choisissez la source qui entend le son, puis faites-le entendre ' + limits.min_examples + ' à ' +
                  limits.max_examples + ' fois (5 conseillées), dans les conditions habituelles. Comptez 2 minutes. ' +
                  'Pendant l\'apprentissage, la source ne déclenche rien.</p>' +
                  '<div class="field"><label class="field-label" for="ln-name">Nom du son</label>' +
                  '<input class="input" id="ln-name" maxlength="40" placeholder="Sonnette" autocomplete="off"></div>') +
            (list.length
                ? '<div class="field"><label class="field-label" for="ln-source">Source</label>' +
                  '<select class="input" id="ln-source">' + list.map(function (s) {
                      var ref = CT.sourceRef(s);
                      return '<option value="' + esc(ref) + '"' + (relearn && relearn.source_ref === ref ? ' selected' : '') + '>' +
                          esc(s.name) + '</option>';
                  }).join('') + '</select></div>'
                : '<p class="hint text-warn">Aucune source active : activez une source pour apprendre un son.</p>') +
            actions(list.length ? '<button type="button" class="btn btn-primary" data-c="start">Commencer</button>' : '');
        body.querySelector('[data-c="cancel"]').addEventListener('click', close);
        var startBtn = body.querySelector('[data-c="start"]');
        (CT.$('#ln-name') || CT.$('#ln-source') || body.querySelector('[data-c="cancel"]')).focus();
        if (!startBtn) return;
        startBtn.addEventListener('click', function () {
            if (relearn) {
                name = relearn.name;
            } else {
                name = CT.$('#ln-name').value.trim().replace(/\s+/g, ' ');
                if (!name) { CT.$('#ln-name').focus(); CT.error('Donnez un nom au son.'); return; }
                if (sounds.some(function (s) { return s.name.toLowerCase() === name.toLowerCase(); })) {
                    CT.$('#ln-name').focus(); CT.error('Un son appris s\'appelle déjà « ' + name + ' ».'); return;
                }
            }
            var mine = run;
            startBtn.disabled = true;
            starting = true;
            var req = {source: CT.$('#ln-source').value};
            if (relearn) req.sound_id = relearn.id;
            CT.api('POST', '/api/learned/start', req)
                .then(function () {
                    starting = false;
                    if (mine !== run) { CT.api('POST', '/api/calibration/cancel', {}).catch(function () {}); return; }
                    active = true;
                    take('fond');
                })
                .catch(function (err) { starting = false; startBtn.disabled = false; CT.error('Apprentissage impossible : ' + err.message); });
        });
    }

    function take(kind, lastResult) {
        var fond = kind === 'fond';
        var seconds = fond ? 5 : 3;
        var canFinish = !fond && examples >= limits.min_examples;
        setTitle((relearn ? 'Réapprendre' : 'Apprendre') + ' « ' + name + ' » · ' + (fond ? 'bruit de fond' : 'exemple ' + (examples + 1)));
        body.innerHTML =
            (lastResult ? '<p class="hint cal-last">' + lastResult + '</p>' : '') +
            '<h3 class="cal-target">' + (fond ? 'Bruit de fond' : 'Exemple ' + (examples + 1) + ' sur ' + limits.min_examples + ' à ' + limits.max_examples) + '</h3>' +
            '<p>' + (fond
                ? 'Restez silencieux : seul le bruit normal de la pièce doit être entendu.'
                : 'Faites entendre <strong>une fois</strong> le son « ' + esc(name) + ' », juste après avoir cliqué.') + '</p>' +
            '<p class="cal-countdown" aria-live="polite"></p>' +
            actions((canFinish ? '<button type="button" class="btn btn-ghost" data-c="finish">Terminer</button>' : '') +
                    '<button type="button" class="btn btn-primary" data-c="record">Enregistrer (' + seconds + ' s)</button>');
        body.querySelector('[data-c="cancel"]').addEventListener('click', close);
        var fin = body.querySelector('[data-c="finish"]');
        if (fin) fin.addEventListener('click', preview);
        var rec = body.querySelector('[data-c="record"]');
        rec.addEventListener('click', function () { record(kind, seconds, rec); });
        rec.focus();
    }

    function record(kind, seconds, btn) {
        var mine = run;
        busy = true;
        btn.disabled = true;
        var fin = body.querySelector('[data-c="finish"]');
        if (fin) fin.disabled = true;
        var out = body.querySelector('.cal-countdown');
        var stopTimer = CT.recordCountdown(out, seconds, 'Enregistrement… ');
        CT.api('POST', '/api/learned/record', {kind: kind})
            .then(function (d) {
                stopTimer();
                busy = false;
                if (mine !== run) return;
                if (kind === 'fond') { take('exemple', 'Bruit de fond enregistré.'); return; }
                examples = d.examples;
                var top = (d.top || []).map(function (c) { return esc(CT.soundLabel(c.classe)) + ' ' + CT.pct(c.score); }).join(', ');
                var msg = 'Exemple ' + examples + ' enregistré' + (top ? ' (YAMNet y entend : ' + top + ')' : '') + '.';
                if (d.distinct !== undefined && d.distinct < 0.1) msg += ' <span class="text-warn">Presque comme le bruit de fond : le son était-il assez fort ?</span>';
                if (examples >= limits.max_examples) preview(); else take('exemple', msg);
            })
            .catch(function (err) {
                stopTimer();
                busy = false;
                if (mine !== run) return;
                btn.disabled = false;
                if (fin) fin.disabled = false;
                CT.calState(out, '', '');
                CT.error('Prise impossible : ' + err.message);
            });
    }

    function preview() {
        var mine = run;
        setTitle((relearn ? 'Réapprendre' : 'Apprendre') + ' « ' + name + ' » · bilan');
        body.innerHTML = '<p class="cal-countdown" data-state="analyze" aria-live="polite">Analyse des exemples…</p>';
        CT.api('POST', '/api/learned/preview', {})
            .then(function (q) {
                if (mine !== run) return;
                body.innerHTML =
                    '<p><strong>' + q.recognized + ' exemples sur ' + q.examples + '</strong> sont reconnus par les autres.</p>' +
                    (q.warnings || []).map(function (w) { return '<p class="hint text-warn">' + esc(w.charAt(0).toUpperCase() + w.slice(1)) + '.</p>'; }).join('') +
                    (relearn
                        ? '<p class="hint">Les groupes qui cochent « ' + esc(relearn.label) + ' » utiliseront les nouveaux exemples.</p>'
                        : '<div class="field"><label class="field-label" for="ln-final">Nom du son</label>' +
                          '<input class="input" id="ln-final" maxlength="40" autocomplete="off" value="' + esc(name) + '"></div>' +
                          '<p class="hint">Une fois enregistré, cochez « Appris : <span data-role="ln-name">' + esc(name) + '</span> » dans les sons d\'un groupe ' +
                          '(seuil conseillé : 50 à 60 %).</p>') +
                    actions((q.examples < limits.max_examples ? '<button type="button" class="btn btn-ghost" data-c="more">Un exemple de plus</button>' : '') +
                            '<button type="button" class="btn btn-primary" data-c="save">Enregistrer le son</button>');
                body.querySelector('[data-c="cancel"]').addEventListener('click', close);
                var more = body.querySelector('[data-c="more"]');
                if (more) more.addEventListener('click', function () { take('exemple'); });
                var save = body.querySelector('[data-c="save"]');
                var finalName = CT.$('#ln-final');
                if (finalName) finalName.addEventListener('input', function () {
                    body.querySelector('[data-role="ln-name"]').textContent = finalName.value.trim();
                });
                save.addEventListener('click', function () {
                    // Nom modifiable ici : un nom refuse a l'enregistrement
                    // (pris entre-temps) ne fait pas perdre les exemples.
                    if (finalName) {
                        name = finalName.value.trim().replace(/\s+/g, ' ');
                        if (!name) { finalName.focus(); CT.error('Donnez un nom au son.'); return; }
                    }
                    save.disabled = true;
                    busy = true;
                    CT.api('POST', '/api/learned/save', relearn ? {} : {name: name}).then(function (s) {
                        busy = false;
                        active = false;
                        close();
                        CT.success(relearn ? '« ' + s.label + ' » réappris' : '« ' + s.label + ' » enregistré : cochez-le dans un groupe');
                        reload();
                    }).catch(function (err) {
                        busy = false;
                        save.disabled = false;
                        CT.error('Enregistrement impossible : ' + err.message);
                        (finalName || save).focus();
                    });
                });
                save.focus();
            })
            .catch(function (err) {
                if (mine !== run) return;
                body.innerHTML = '<p class="hint text-warn">' + esc(err.message) + '</p>' +
                    actions('<button type="button" class="btn btn-ghost" data-c="more">Un exemple de plus</button>');
                body.querySelector('[data-c="cancel"]').addEventListener('click', close);
                body.querySelector('[data-c="more"]').addEventListener('click', function () { take('exemple'); });
            });
    }

    var openBtn = document.getElementById('learn-sound');
    if (openBtn) openBtn.addEventListener('click', function () { open(null); });
    var closeBtn = document.querySelector('#learn [data-c="close"]');
    if (closeBtn) closeBtn.addEventListener('click', close);
    if (CT.$('#learned-list')) reload();
})();
