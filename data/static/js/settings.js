/* Onglet "Reglages" : detection (pics), exclusions, Home Assistant, configuration. */
(function () {
    'use strict';
    var CT = window.CT;
    var DEFAULTS = {delay: 1.5, peak_cooldown: 0.08, peak_ratio: 3.0, peak_reset: 0.3};
    var FIELDS = {delay: 'adv-delay', peak_cooldown: 'adv-cooldown', peak_ratio: 'adv-ratio', peak_reset: 'adv-reset'};

    function fillAdvanced() {
        var g = CT.state.settings.global || {};
        Object.keys(FIELDS).forEach(function (k) {
            var el = document.getElementById(FIELDS[k]);
            if (el && document.activeElement !== el) el.value = g[k] != null ? g[k] : DEFAULTS[k];
        });
        CT.$('#debug-toggle').checked = !!g.debug;
    }

    function bindAdvanced() {
        Object.keys(FIELDS).forEach(function (k) {
            var el = document.getElementById(FIELDS[k]);
            el.addEventListener('change', function () {
                var body = {};
                body[k] = parseFloat(el.value);
                CT.api('PUT', '/api/settings/advanced', body)
                    .then(function () { (CT.state.settings.global = CT.state.settings.global || {})[k] = body[k]; CT.success('Enregistré'); })
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
    function loadExclusions() {
        return CT.api('GET', '/api/sound_exclusions').then(renderExclusions).catch(function (err) {
            CT.error('Exclusions indisponibles : ' + err.message);
        });
    }
    function renderExclusions(data) {
        var excluded = data.excluded || [], available = data.available || [];
        var ex = CT.$('#excluded-list'), av = CT.$('#available-list');
        ex.innerHTML = excluded.length ? excluded.map(function (l) {
            return '<span class="chip is-on"><span title="' + CT.esc(l) + '">' + CT.esc(CT.soundLabel(l)) + '</span>' +
                '<button type="button" class="chip-remove" data-unexclude="' + CT.esc(l) + '" aria-label="Ne plus exclure ' + CT.esc(l) + '">×</button></span>';
        }).join('') : '<p class="hint">Aucun son exclu.</p>';
        av.innerHTML = available.length ? available.map(function (l) {
            return '<button type="button" class="chip" data-exclude="' + CT.esc(l) + '" title="' + CT.esc(l) + '">' +
                '<span>' + CT.esc(CT.soundLabel(l)) + '</span></button>';
        }).join('') : '<p class="hint">Aucun autre son entendu pour l\'instant.</p>';
        filterAvailable();
    }
    function filterAvailable() {
        var q = (CT.$('#available-search').value || '').trim().toLowerCase();
        CT.$$('#available-list .chip').forEach(function (c) {
            var l = c.getAttribute('data-exclude');
            c.hidden = !!q && (l + ' ' + CT.soundLabel(l)).toLowerCase().indexOf(q) === -1;
        });
    }
    function bindExclusions() {
        CT.$('#exclusions').addEventListener('click', function (e) {
            var add = e.target.closest('[data-exclude]'), del = e.target.closest('[data-unexclude]');
            if (!add && !del) return;
            var label = (add || del).getAttribute(add ? 'data-exclude' : 'data-unexclude');
            CT.api('PUT', '/api/sound_exclusions', {label: label, excluded: !!add})
                .then(loadExclusions)
                .catch(function (err) { CT.error('Exclusion non enregistrée : ' + err.message); });
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
        var input = CT.$('#import-config');
        input.addEventListener('change', function () {
            var file = input.files[0];
            if (!file) return;
            var fd = new FormData();
            fd.append('file', file);
            CT.apiForm('/api/settings/import', fd)
                .then(function () { CT.success('Configuration importée'); return CT.resync(); })
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

    CT.onSettingsOpen = function () {
        fillAdvanced();
        loadExclusions();
    };
    CT.initSettings = function () {
        fillAdvanced();
        bindAdvanced();
        bindExclusions();
        bindConfig();
        CT.on('sound_seen', function () {
            if (!CT.$('#panel-settings').hidden) loadExclusions();
        });
    };
})();
