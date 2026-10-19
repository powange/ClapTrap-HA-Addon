/* Onglet "Reglages" : detection (pics), exclusions, Home Assistant, configuration. */
(function () {
    'use strict';
    var CT = window.CT;
    var DEFAULTS = window.advancedDefaults || {};  // DEFAULT_SETTINGS du serveur
    var FIELDS = {delay: 'adv-delay', peak_cooldown: 'adv-cooldown', peak_ratio: 'adv-ratio'};

    function fillAdvanced(force) {
        var g = CT.state.settings.global || {};
        Object.keys(FIELDS).forEach(function (k) {
            var el = document.getElementById(FIELDS[k]);
            if (el && (force || document.activeElement !== el)) el.value = g[k] != null ? g[k] : DEFAULTS[k];
        });
        CT.$('#debug-toggle').checked = !!g.debug;
    }

    function bindAdvanced() {
        Object.keys(FIELDS).forEach(function (k) {
            var el = document.getElementById(FIELDS[k]);
            el.addEventListener('change', function () {
                if (!el.checkValidity()) {
                    // Bornes identiques a celles du serveur : valeur refusee,
                    // l'ancienne est remise et la raison reste affichee.
                    var why = el.validationMessage;
                    fillAdvanced(true);
                    CT.error(el.labels[0].textContent + ' : ' + why);
                    return;
                }
                var body = {};
                body[k] = parseFloat(el.value);
                CT.api('PUT', '/api/settings/advanced', body)
                    .then(function () { (CT.state.settings.global = CT.state.settings.global || {})[k] = body[k]; CT.markSaved(el); })
                    .catch(function (err) { fillAdvanced(); CT.error('Réglage refusé : ' + err.message); });
            });
        });
        CT.$('#adv-defaults').addEventListener('click', function () {
            CT.api('PUT', '/api/settings/advanced', DEFAULTS)
                .then(function () { Object.assign(CT.state.settings.global = CT.state.settings.global || {}, DEFAULTS); fillAdvanced(); CT.success('Valeurs par défaut rétablies'); })
                .catch(function (err) { CT.error('Réinitialisation impossible : ' + err.message); });
        });
    }

    // ---- Exclusions -----------------------------------------------------------
    // Plusieurs chargements peuvent se croiser (rechargement differe, action
    // sur une pastille, ouverture de l'onglet) : seule la reponse la plus
    // recente est appliquee, une plus ancienne arrivee apres est ignoree.
    var exclusionsSeq = 0;
    function loadExclusions() {
        var seq = ++exclusionsSeq;
        return CT.api('GET', '/api/sound_exclusions').then(function (data) {
            if (seq !== exclusionsSeq) return;
            // L'etat local sert aux listes de sons des groupes : sans cette
            // copie, un son exclu y restait propose jusqu'au rechargement.
            var g = CT.state.settings && (CT.state.settings.global = CT.state.settings.global || {});
            var excluded = data.excluded || [];
            if (g && JSON.stringify(g.sound_exclusions || []) !== JSON.stringify(excluded)) {
                g.sound_exclusions = excluded;
                CT.renderWhenIdle();
            }
            renderExclusions(data);
        }).catch(function (err) {
            if (seq === exclusionsSeq) CT.error('Exclusions indisponibles : ' + err.message);
        });
    }
    function renderExclusions(data) {
        var excluded = data.excluded || [], available = data.available || [];
        var ex = CT.$('#excluded-list'), av = CT.$('#available-list');
        ex.innerHTML = excluded.length ? excluded.map(function (l) {
            return '<span class="chip is-on"><span title="' + CT.esc(l) + '">' + CT.esc(CT.soundLabel(l)) + '</span>' +
                '<button type="button" class="chip-remove" data-unexclude="' + CT.esc(l) + '" aria-label="Ne plus exclure ' + CT.esc(CT.soundLabel(l)) + '">×</button></span>';
        }).join('') : '<p class="hint">Aucun son exclu.</p>';
        // Ordre du nom affiche (francais) ; un son coche dans un groupe le dit.
        available = available.slice().sort(function (a, b) { return CT.soundLabel(a).localeCompare(CT.soundLabel(b), 'fr'); });
        av.innerHTML = available.length ? available.map(function (l) {
            var used = usedBy(l);
            return '<button type="button" class="chip' + (used.length ? ' chip-used' : '') + '" data-exclude="' + CT.esc(l) + '" title="' + CT.esc(l) + '"' +
                (used.length ? ' aria-label="' + CT.esc(CT.soundLabel(l) + ', utilisé par ' + used.join(', ')) + '"' : '') + '>' +
                '<span>' + CT.esc(CT.soundLabel(l)) + '</span>' +
                (used.length ? '<small class="chip-note">' + CT.esc(used.join(', ')) + '</small>' : '') + '</button>';
        }).join('') : '<p class="hint">Aucun autre son entendu pour l\'instant.</p>';
        filterAvailable();
    }
    // Groupes (« Clap (Micro) ») qui utilisent ce son.
    function usedBy(label) {
        var out = [];
        (CT.sourceList ? CT.sourceList() : []).forEach(function (src) {
            ((src.data && src.data.sound_groups) || []).forEach(function (g) {
                if (g && g.sound_whitelist && g.sound_whitelist[label]) out.push((g.name || g.slug) + ' (' + src.name + ')');
            });
        });
        return out;
    }
    function filterAvailable() {
        CT.filterChips(CT.$$('#available-list .chip'), CT.$('#available-search').value,
                       function (c) { return c.getAttribute('data-exclude'); });
    }
    function bindExclusions() {
        CT.$('#exclusions').addEventListener('click', function (e) {
            var add = e.target.closest('[data-exclude]'), del = e.target.closest('[data-unexclude]');
            if (!add && !del) return;
            var label = (add || del).getAttribute(add ? 'data-exclude' : 'data-unexclude');
            var used = add ? usedBy(label) : [];
            // Ignorer partout un son coche dans un groupe le coupe : demander.
            (used.length ? CT.confirm('« ' + CT.soundLabel(label) + ' » est utilisé par ' + used.join(', ') +
                                      '. L\'ignorer partout empêchera ces groupes de se déclencher sur ce son. Continuer ?',
                                      'Ignorer partout')
                         : Promise.resolve(true)).then(function (ok) {
                if (!ok) return;
                CT.api('PUT', '/api/sound_exclusions', {label: label, excluded: !!add})
                    .then(loadExclusions)
                    .catch(function (err) { CT.error('Exclusion non enregistrée : ' + err.message); });
            });
        });
        CT.$('#available-search').addEventListener('input', filterAvailable);
    }

    // ---- Home Assistant et configuration ----------------------------------------
    function bindConfig() {
        CT.$('#ha-cleanup').addEventListener('click', function () {
            CT.api('POST', '/api/ha/cleanup').then(function (d) { CT.success(d.message || 'Nettoyage terminé'); })
                .catch(function (err) { CT.error('Nettoyage impossible : ' + err.message); });
        });
        CT.$('#export-config').addEventListener('click', function () {
            window.location.href = CT.basePath + '/api/settings/export';
        });
        CT.$('#export-shareable').addEventListener('click', function () {
            window.location.href = CT.basePath + '/api/settings/export?secrets=0';
        });
        // Le telechargement peut etre bloque dans l'app Companion : afficher
        // la configuration pour la copier.
        CT.$('#show-config').addEventListener('click', function () {
            CT.api('GET', '/api/settings').then(function (s) {
                showText('Configuration ClapTrap', JSON.stringify(s, null, 2));
            }).catch(function (err) { CT.error('Configuration indisponible : ' + err.message); });
        });
        var input = CT.$('#import-config');
        input.addEventListener('change', function () {
            var file = input.files[0];
            if (!file) return;
            var fd = new FormData();
            fd.append('file', file);
            CT.apiForm('/api/settings/import', fd)
                .then(function () {
                    CT.success('Configuration importée');
                    input.blur();
                    // Tout recharger : l'onglet Reglages (reglages avances,
                    // exclusions, journal detaille) gardait les anciennes valeurs.
                    return Promise.all([CT.reloadSettings(), CT.reloadStatus()]).then(function () {
                        CT.render();
                        CT.onSettingsOpen();
                    });
                })
                .catch(function (err) { CT.error('Import refusé : ' + err.message); })
                .finally(function () { input.value = ''; });
        });
        var dbg = CT.$('#debug-toggle');
        dbg.addEventListener('change', function () {
            var v = dbg.checked;
            CT.api('PUT', '/api/settings/debug', {enabled: v})
                .then(function () { (CT.state.settings.global = CT.state.settings.global || {}).debug = v; })
                .catch(function (err) { dbg.checked = !v; CT.error('Journal détaillé : ' + err.message); });
        });
    }

    function showText(title, text) {
        var back = document.createElement('div');
        back.className = 'modal-backdrop';
        back.innerHTML = '<div class="modal" role="dialog" aria-modal="true" aria-labelledby="txt-title">' +
            '<div class="modal-head"><h2 id="txt-title"></h2></div>' +
            '<textarea class="input textarea" readonly aria-labelledby="txt-title"></textarea>' +
            '<div class="modal-actions"><button type="button" class="btn btn-ghost" data-r="copy">Copier</button>' +
            '<button type="button" class="btn btn-primary" data-r="close">Fermer</button></div></div>';
        back.querySelector('#txt-title').textContent = title;
        var ta = back.querySelector('textarea');
        ta.value = text;
        document.body.appendChild(back);
        var release = CT.trapFocus(back.querySelector('.modal'), close);
        function close() { release(); back.remove(); }
        back.addEventListener('click', function (e) {
            var r = e.target.getAttribute && e.target.getAttribute('data-r');
            if (e.target === back || r === 'close') close();
            if (r === 'copy') { ta.select(); CT.copy(text); }
        });
        back.querySelector('[data-r="copy"]').focus();
    }

    // Calibrage du comptage applique : champs remplis avec les nouvelles valeurs.
    CT.onCountCalibrated = function () { fillAdvanced(true); };
    CT.onSettingsOpen = function () {
        fillAdvanced();
        loadExclusions();
    };
    CT.initSettings = function () {
        fillAdvanced();
        bindAdvanced();
        bindExclusions();
        bindConfig();
        // Un rechargement pour une rafale de sons nouveaux (une requete par
        // son entendu a la decouverte), au plus un en cours.
        var pending = null, inflight = false, again = false;
        function reloadSoon() {
            if (pending) return;
            pending = setTimeout(function () {
                pending = null;
                if (CT.$('#panel-settings').hidden) return;
                if (inflight) { again = true; return; }
                inflight = true;
                loadExclusions().then(function () {
                    inflight = false;
                    if (again) { again = false; reloadSoon(); }
                });
            }, 1000);
        }
        CT.on('sound_seen', function () {
            if (!CT.$('#panel-settings').hidden) reloadSoon();
        });
    };
})();
