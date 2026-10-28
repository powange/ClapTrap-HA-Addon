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
        var st = CT.$('#shape-toggle');
        if (st) st.checked = g.clap_shape_check !== false;
        var qt = CT.$('#quick-toggle');
        if (qt) qt.checked = g.quick_single === true;
        var rt = CT.$('#rooms-toggle'), rw = CT.$('#rooms-window');
        if (rt) rt.checked = !!g.room_arbitration;
        if (rw && (force || document.activeElement !== rw)) rw.value = g.room_window != null ? g.room_window : 1;
        if (rw) rw.disabled = !g.room_arbitration;
        renderZones();
    }

    // ---- Groupes de sources (arbitrage entre pieces) ----------------------
    var zonesSaving = false, zonesPending = null;
    function zones() { return ((CT.state.settings.global || {}).room_zones || []).map(function (z) {
        return {name: z.name, sources: (z.sources || []).slice()};
    }); }
    function renderZones(force) {
        var box = CT.$('#rooms-zones');
        if (!box) return;
        if (!force && box.contains(document.activeElement) && document.activeElement.type === 'text') return;   // saisie en cours
        var on = !!(CT.state.settings.global || {}).room_arbitration;
        var list = zones(), sources = CT.sourceList();
        var owner = {};
        list.forEach(function (z) { z.sources.forEach(function (sid) { owner[sid] = z.name; }); });
        box.innerHTML = list.map(function (z, i) {
            return '<div class="zone" data-zone="' + i + '"><div class="zone-head">' +
                '<input class="input" type="text" maxlength="40" aria-label="Nom du groupe" data-zone-name value="' + CT.esc(z.name) + '"' + (on ? '' : ' disabled') + '>' +
                '<button type="button" class="btn btn-ghost" data-zone-del' + (on ? '' : ' disabled') + '>Supprimer</button></div>' +
                '<div class="checks">' + (sources.length ? sources.map(function (src) {
                    var mine = z.sources.indexOf(src.sourceId) !== -1;
                    var other = !mine && owner[src.sourceId];
                    return '<label class="check"><input type="checkbox" data-zone-src="' + CT.esc(src.sourceId) + '"' +
                        (mine ? ' checked' : '') + (other || !on ? ' disabled' : '') + '> ' + CT.esc(src.name) +
                        (other ? ' <span class="muted">(dans « ' + CT.esc(other) + ' »)</span>' : '') + '</label>';
                }).join('') : '<span class="hint">Aucune source.</span>') + '</div></div>';
        }).join('');
        var add = CT.$('#rooms-zone-add');
        if (add) add.disabled = !on || list.length >= 10;
        renderWeights();
    }

    // ---- Correction par source (calibrage de l'arbitrage) ---------------------
    function renderWeights(force) {
        var box = CT.$('#rooms-weights');
        if (!box) return;
        if (!force && box.contains(document.activeElement)) return;   // saisie en cours
        var g = CT.state.settings.global || {}, on = !!g.room_arbitration, w = g.room_weights || {};
        var sources = CT.sourceList();
        box.innerHTML = sources.length ? sources.map(function (src) {
            var id = 'rw-' + CT.esc(src.sourceId);
            return '<div class="weight"><label for="' + id + '">' + CT.esc(src.name) + '</label>' +
                '<span class="weight-input">× <input class="input" type="number" id="' + id + '" data-weight-src="' + CT.esc(src.sourceId) +
                '" min="0.05" max="20" step="0.01" value="' + (w[src.sourceId] != null ? w[src.sourceId] : 1) + '"' + (on ? '' : ' disabled') + '></span></div>';
        }).join('') : '<span class="hint">Aucune source.</span>';
        var cal = CT.$('#room-calibrate');
        if (cal) cal.disabled = !on;
    }
    // Une modification faite pendant un envoi part juste apres (la derniere,
    // calculee sur l'etat enregistre) : elle n'est pas perdue.
    function saveZones(build, msg) {
        if (zonesSaving) { zonesPending = {build: build, msg: msg}; return; }
        zonesSaving = true;
        CT.api('PUT', '/api/settings/rooms', {zones: build(zones())}).then(function (d) {
            (CT.state.settings.global = CT.state.settings.global || {}).room_zones = d.room_zones;
            CT.success(msg);
        }).catch(function (err) { CT.error('Groupes refusés : ' + err.message); })
          .finally(function () {
              zonesSaving = false;
              renderZones(true);   // etat enregistre, meme apres un renommage refuse
              var next = zonesPending;
              zonesPending = null;
              if (next) saveZones(next.build, next.msg);
          });
    }

    function bindRooms() {
        var rt = CT.$('#rooms-toggle'), rw = CT.$('#rooms-window');
        if (!rt) return;
        function save(body, el, msg) {
            return CT.api('PUT', '/api/settings/rooms', body)
                .then(function (d) {
                    var g = CT.state.settings.global = CT.state.settings.global || {};
                    if ('room_arbitration' in d) g.room_arbitration = d.room_arbitration;
                    if ('room_window' in d) g.room_window = d.room_window;
                    rw.disabled = !g.room_arbitration;
                    renderZones();
                    CT.markSaved(el, msg);
                })
                .catch(function (err) { fillAdvanced(true); CT.error('Réglage refusé : ' + err.message); });
        }
        rt.addEventListener('change', function () {
            save({enabled: rt.checked}, rt, rt.checked ? 'Seule la source la plus proche déclenche' : 'Chaque source déclenche');
        });
        rw.addEventListener('change', function () {
            if (!rw.checkValidity()) { var why = rw.validationMessage; fillAdvanced(true); CT.error('Attente : ' + why); return; }
            save({window: parseFloat(rw.value)}, rw, 'Attente enregistrée');
        });
        var weights = CT.$('#rooms-weights');
        if (weights) weights.addEventListener('change', function (e) {
            var el = e.target, sid = el.getAttribute('data-weight-src');
            if (!sid) return;
            if (!el.checkValidity()) { var why = el.validationMessage; renderWeights(true); CT.error('Correction : ' + why); return; }
            var body = {};
            body[sid] = parseFloat(el.value);
            CT.api('PUT', '/api/settings/rooms', {weights: body})
                .then(function (d) {
                    (CT.state.settings.global = CT.state.settings.global || {}).room_weights = d.room_weights;
                    CT.markSaved(el, 'Correction enregistrée');
                })
                .catch(function (err) { renderWeights(true); CT.error('Correction refusée : ' + err.message); });
        });
        var box = CT.$('#rooms-zones'), add = CT.$('#rooms-zone-add');
        if (add) add.addEventListener('click', function () {
            saveZones(function (list) {
                var n = list.length + 1;
                var taken = list.map(function (z) { return z.name.toLowerCase(); });
                while (taken.indexOf(('Groupe ' + n).toLowerCase()) !== -1) n++;
                list.push({name: 'Groupe ' + n, sources: []});
                return list;
            }, 'Groupe ajouté : cochez ses sources');
        });
        if (box) {
            box.addEventListener('change', function (e) {
                var zoneEl = e.target.closest('[data-zone]');
                if (!zoneEl) return;
                var i = +zoneEl.getAttribute('data-zone');
                if (e.target.hasAttribute('data-zone-src')) {
                    var sid = e.target.getAttribute('data-zone-src'), on = e.target.checked;
                    saveZones(function (list) {
                        var z = list[i];
                        if (z) {
                            z.sources = z.sources.filter(function (s) { return s !== sid; });
                            if (on) z.sources.push(sid);
                        }
                        return list;
                    }, 'Groupe enregistré');
                } else if (e.target.hasAttribute('data-zone-name')) {
                    var name = e.target.value.trim();
                    if (!name) { renderZones(true); CT.error('Donnez un nom au groupe.'); return; }
                    saveZones(function (list) { if (list[i]) list[i].name = name; return list; }, 'Groupe renommé');
                }
            });
            box.addEventListener('click', function (e) {
                var del = e.target.closest('[data-zone-del]');
                if (!del) return;
                var i = +del.closest('[data-zone]').getAttribute('data-zone'), shown = zones(), name = (shown[i] || {}).name;
                if (!name) return;
                CT.confirm('Supprimer le groupe « ' + name + ' » ? Ses sources ne se départageront plus entre elles' +
                           (shown.length === 1 ? ' (sans groupe, toutes les sources se départagent).' : '.')).then(function (ok) {
                    if (!ok) return;
                    // Liste a jour au moment de l'envoi, groupe retrouve par son nom.
                    saveZones(function (list) {
                        return list.filter(function (z) { return z.name !== name; });
                    }, 'Groupe supprimé');
                });
            });
        }
    }

    // Interrupteur d'un reglage avance booleen (PUT /api/settings/advanced).
    function bindToggle(id, key, onMsg, offMsg) {
        var el = CT.$('#' + id);
        if (!el) return;
        el.addEventListener('change', function () {
            var on = el.checked, body = {};
            body[key] = on;
            CT.api('PUT', '/api/settings/advanced', body).then(function () {
                (CT.state.settings.global = CT.state.settings.global || {})[key] = on;
                CT.markSaved(el, on ? onMsg : offMsg);
            }).catch(function (err) { el.checked = !on; CT.error('Réglage refusé : ' + err.message); });
        });
    }

    function bindAdvanced() {
        bindToggle('shape-toggle', 'clap_shape_check', 'Forme des claps vérifiée', 'Forme des claps non vérifiée');
        bindToggle('quick-toggle', 'quick_single', 'Décision rapide activée', 'Décision rapide désactivée');
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
            // Confirmation : un toucher ecrasait les valeurs d'un calibrage du
            // comptage de deux minutes, sans retour possible.
            var g = CT.state.settings.global || {};
            var line = function (v) {
                return 'fenêtre ' + String(v.delay).replace('.', ',') + ' s, écart ' + String(v.peak_cooldown).replace('.', ',') +
                    ' s, force ×' + String(v.peak_ratio).replace('.', ',');
            };
            var current = {delay: g.delay != null ? g.delay : DEFAULTS.delay,
                           peak_cooldown: g.peak_cooldown != null ? g.peak_cooldown : DEFAULTS.peak_cooldown,
                           peak_ratio: g.peak_ratio != null ? g.peak_ratio : DEFAULTS.peak_ratio};
            CT.confirm('Rétablir les valeurs par défaut du comptage ? Actuelles : ' + line(current) +
                       '. Par défaut : ' + line(DEFAULTS) + '.', 'Rétablir').then(function (ok) {
                if (ok) resetDefaults();
            });
        });
        function resetDefaults() {
            CT.api('PUT', '/api/settings/advanced', DEFAULTS)
                .then(function () { Object.assign(CT.state.settings.global = CT.state.settings.global || {}, DEFAULTS); fillAdvanced(); CT.success('Valeurs par défaut rétablies'); })
                .catch(function (err) { CT.error('Réinitialisation impossible : ' + err.message); });
        }
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
            if (seq === exclusionsSeq) CT.error('Sons ignorés indisponibles : ' + err.message);
        });
    }
    function renderExclusions(data) {
        var excluded = data.excluded || [], available = data.available || [];
        var ex = CT.$('#excluded-list'), av = CT.$('#available-list');
        ex.innerHTML = excluded.length ? excluded.map(function (l) {
            return '<span class="chip is-on"><span title="' + CT.esc(l) + '">' + CT.esc(CT.soundLabel(l)) + '</span>' +
                '<button type="button" class="chip-remove" data-unexclude="' + CT.esc(l) + '" aria-label="Ne plus ignorer ' + CT.esc(CT.soundLabel(l)) + '">×</button></span>';
        }).join('') : '<p class="hint">Aucun son ignoré.</p>';
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
                    .catch(function (err) { CT.error('Réglage des sons ignorés non enregistré : ' + err.message); });
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
        // Par defaut sans secrets (copie a partager pour demander de l'aide) :
        // les mots de passe des cameras s'affichaient en clair alors que
        // l'interface les masque partout ailleurs.
        CT.$('#show-config').addEventListener('click', function () {
            CT.api('GET', '/api/settings/export?secrets=0').then(function (s) {
                showText('Configuration ClapTrap', JSON.stringify(s, null, 2), {
                    note: 'Copie sans secrets (identifiants des caméras et adresses des webhooks masqués), à partager pour demander de l\'aide.',
                    label: 'Afficher la sauvegarde complète',
                    warn: 'Sauvegarde complète : elle contient les mots de passe des caméras et les adresses des webhooks. Ne la partagez pas.',
                    load: function () {
                        return CT.api('GET', '/api/settings/export').then(function (full) { return JSON.stringify(full, null, 2); });
                    }
                });
            }).catch(function (err) { CT.error('Configuration indisponible : ' + err.message); });
        });
        var input = CT.$('#import-config');
        input.addEventListener('change', function () {
            var file = input.files[0];
            if (!file) return;
            // Remplace toutes les sources et tous les reglages : confirmer
            // (un mauvais fichier choisi d'un doigt effacait tout).
            CT.confirm('Remplacer toute la configuration actuelle (sources, groupes, réglages) par « ' + file.name +
                       ' » ? Exportez d\'abord la configuration actuelle si vous voulez pouvoir y revenir.', 'Importer')
                .then(function (ok) {
                    if (ok) importFile(file);
                    else input.value = '';
                });
        });
        function importFile(file) {
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
        }
        var dbg = CT.$('#debug-toggle');
        dbg.addEventListener('change', function () {
            var v = dbg.checked;
            CT.api('PUT', '/api/settings/debug', {enabled: v})
                .then(function () { (CT.state.settings.global = CT.state.settings.global || {}).debug = v; })
                .catch(function (err) { dbg.checked = !v; CT.error('Journal détaillé : ' + err.message); });
        });
    }

    // `alt` : {note, label, warn, load()} — texte par defaut accompagne d'une
    // note, et bouton qui charge une autre version (avec un avertissement).
    function showText(title, text, alt) {
        var back = document.createElement('div');
        back.className = 'modal-backdrop';
        back.innerHTML = '<div class="modal" role="dialog" aria-modal="true" aria-labelledby="txt-title">' +
            '<div class="modal-head"><h2 id="txt-title"></h2></div>' +
            (alt ? '<p class="hint" data-r="note"></p>' : '') +
            '<textarea class="input textarea" readonly aria-labelledby="txt-title"></textarea>' +
            '<div class="modal-actions">' + (alt ? '<button type="button" class="btn btn-ghost" data-r="alt"></button>' : '') +
            '<button type="button" class="btn btn-ghost" data-r="copy">Copier</button>' +
            '<button type="button" class="btn btn-primary" data-r="close">Fermer</button></div></div>';
        back.querySelector('#txt-title').textContent = title;
        var ta = back.querySelector('textarea');
        ta.value = text;
        if (alt) {
            var note = back.querySelector('[data-r="note"]');
            var altBtn = back.querySelector('[data-r="alt"]');
            note.textContent = alt.note;
            altBtn.textContent = alt.label;
            altBtn.addEventListener('click', function () {
                altBtn.disabled = true;
                alt.load().then(function (t) {
                    text = ta.value = t;
                    note.textContent = alt.warn;
                    note.classList.add('text-warn');
                    altBtn.remove();
                }).catch(function (err) { altBtn.disabled = false; CT.error(err.message); });
            });
        }
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
    CT.onRoomsCalibrated = function () { renderWeights(true); };
    CT.onSettingsOpen = function () {
        fillAdvanced();
        loadExclusions();
    };
    CT.initSettings = function () {
        fillAdvanced();
        bindAdvanced();
        bindRooms();
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
