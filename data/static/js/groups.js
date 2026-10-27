/* Gestion des groupes de sons d'une source (dans "Reglages de la source"). */
(function () {
    'use strict';
    var CT = window.CT;
    var esc = CT.esc;

    // entity_id calcules par le serveur (memes regles que la publication MQTT).
    function entitiesOf(src, g) {
        var key = src.kind === 'mic' ? 'mic' : src.kind + ':' + src.key;
        return (CT.state.entityIds[key] || {})[g.slug] || [];
    }

    // Ordre des pastilles fige apres le premier affichage (cochees d'abord,
    // puis alphabetique) : cocher ou recevoir un son ne deplace plus les
    // pastilles sous le doigt ; les nouveaux sons s'ajoutent a la fin.
    var chipOrder = {};   // domId|slug -> [labels]
    CT.resetChipOrder = function (domId) {
        Object.keys(chipOrder).forEach(function (k) { if (k.indexOf(domId + '|') === 0) delete chipOrder[k]; });
    };
    function orderedLabels(key, wl) {
        var byName = function (a, b) { return CT.soundLabel(a).localeCompare(CT.soundLabel(b), 'fr'); };
        var known = chipOrder[key];
        if (!known) {
            known = chipOrder[key] = Object.keys(wl).sort(function (a, b) {
                if (!!wl[a] !== !!wl[b]) return wl[a] ? -1 : 1;
                return byName(a, b);
            });
            return known.slice();
        }
        var kept = known.filter(function (l) { return l in wl; });
        var added = Object.keys(wl).filter(function (l) { return kept.indexOf(l) === -1; }).sort(byName);
        chipOrder[key] = kept.concat(added);
        return chipOrder[key].slice();
    }

    function groupHtml(src, g, isDefault, activeElsewhere) {
        var wl = g.sound_whitelist || {};
        var labels = orderedLabels(src.domId + '|' + g.slug, wl);
        var gname = esc(g.name || g.slug);
        var excluded = (CT.state.settings.global || {}).sound_exclusions || [];
        var chips = labels.map(function (label) {
            var on = !!wl[label];
            var other = activeElsewhere[label];
            var disabled = !on && other;
            var isExcluded = excluded.indexOf(label) !== -1;
            return '<label class="chip' + (on ? ' is-on' : '') + (disabled ? ' is-disabled' : '') + (isExcluded ? ' is-excluded' : '') + '" title="' + esc(label) + '">' +
                '<input type="checkbox" data-label="' + esc(label) + '"' + (on ? ' checked' : '') + (disabled ? ' disabled' : '') + '>' +
                '<span>' + esc(CT.soundLabel(label)) + '</span>' +
                // Conflit et exclusion lisibles (l'infobulle seule est invisible
                // au toucher et aux lecteurs d'ecran).
                (disabled ? '<span class="chip-note">· dans « ' + esc(other) + ' »</span>' : '') +
                (isExcluded ? '<span class="chip-note">· ignoré partout</span>' : '') + '</label>';
        }).join('');
        var claps = [1, 2, 3, 4].map(function (n) {
            return '<label class="check"><input type="checkbox" data-clap="' + n + '"' +
                ((Array.isArray(g.ha_entities) ? g.ha_entities : [1, 2]).indexOf(n) !== -1 ? ' checked' : '') + '> ' + n + ' clap' + (n > 1 ? 's' : '') + '</label>';
        }).join('');
        var entities = entitiesOf(src, g).map(function (e) {
            return '<li><code>' + esc(e) + '</code><button type="button" class="btn-link" data-copy="' + esc(e) + '" aria-label="Copier ' + esc(e) + '">Copier</button></li>';
        }).join('');
        return '<section class="group-card" data-slug="' + esc(g.slug) + '">' +
            '<div class="group-head">' +
                '<input type="text" class="input group-name" data-role="group-name" value="' + gname + '" aria-label="Nom du groupe ' + gname + '">' +
                '<label class="switch-inline"><input type="checkbox" role="switch" data-role="group-enabled"' + (g.enabled !== false ? ' checked' : '') +
                    ' aria-label="Groupe ' + gname + ' actif"> Actif</label>' +
                (isDefault ? '<span class="pill">Groupe principal</span>'
                           : '<button type="button" class="btn-link danger" data-group-action="delete">Supprimer le groupe</button>') +
            '</div>' +
            (g.count_mode === 'alerte'
                // Mode alerte : un capteur allume tant que le son dure, pas de
                // nombres de claps.
                ? '<fieldset class="field"><legend class="field-label">Capteur Home Assistant</legend>' +
                  '<p class="hint">Allumé tant que le son est reconnu, éteint après quelques secondes de silence.</p>' +
                  (entities ? '<ul class="entity-list">' + entities + '</ul>' : '') +
                  '<label class="field-row-inline">Allumer après <select class="input" data-role="alert-confirm" aria-label="Confirmation de l\'alerte du groupe ' + gname + '">' +
                  [1, 2, 3, 4].map(function (n) {
                      return '<option value="' + n + '"' + ((g.alert_confirm || 2) === n ? ' selected' : '') + '>' + n + ' reconnaissance' + (n > 1 ? 's' : '') +
                          ' sur 4 analyses' + (n === 1 ? ' (son bref)' : n >= 3 ? ' (son qui dure)' : '') + '</option>';
                  }).join('') + '</select></label>' +
                  '<label class="field-row-inline">Type <select class="input" data-role="device-class" aria-label="Type du capteur du groupe ' + gname + '">' +
                  [['sound', 'Son'], ['smoke', 'Fumée'], ['safety', 'Sécurité'], ['problem', 'Problème'], ['occupancy', 'Présence']].map(function (o) {
                      return '<option value="' + o[0] + '"' + ((g.device_class || 'sound') === o[0] ? ' selected' : '') + '>' + o[1] + '</option>';
                  }).join('') + '</select></label></fieldset>'
                : '<fieldset class="field"><legend class="field-label">Entités Home Assistant à créer</legend><div class="checks">' + claps + '</div>' +
                  (entities ? '<ul class="entity-list">' + entities + '</ul>' : '<p class="hint">Aucune entité : cochez au moins un nombre de claps.</p>') +
                  '</fieldset>') +
            '<div class="field" role="group" aria-labelledby="' + src.domId + '-' + esc(g.slug) + '-sounds">' +
                '<div class="field-row"><span class="field-label" id="' + src.domId + '-' + esc(g.slug) + '-sounds">Sons qui déclenchent le groupe ' + gname + '</span>' +
                '<button type="button" class="btn-link" data-group-action="cleanup">Vider la liste des sons non cochés</button></div>' +
                '<label class="switch-inline"><input type="checkbox" role="switch" data-role="auto-add"' + (g.auto_add_sounds ? ' checked' : '') +
                    ' aria-label="Ajouter les sons entendus au groupe ' + gname + '"> Ajouter les sons entendus</label>' +
                '<p class="hint">Seuls les sons reconnus au-dessus du seuil du groupe sont ajoutés.</p>' +
                // Libelles courts : coupes sur mobile (« claps, coups (attaques ne »).
                '<label class="field-row-inline">Comptage <select class="input" data-role="count-mode" aria-label="Comptage du groupe ' + gname + '">' +
                    '<option value="attaques"' + (g.count_mode !== 'tenus' && g.count_mode !== 'alerte' ? ' selected' : '') + '>Attaques (claps, coups)</option>' +
                    '<option value="tenus"' + (g.count_mode === 'tenus' ? ' selected' : '') + '>Sons tenus (sifflement)</option>' +
                    '<option value="alerte"' + (g.count_mode === 'alerte' ? ' selected' : '') + '>Alerte (alarme, pleurs)</option></select></label>' +
                (g.count_mode === 'alerte' ? '' :
                    '<label class="field-row-inline">Écart avec les autres sons <select class="input" data-role="margin" aria-label="Écart minimal avec les autres sons du groupe ' + gname + '">' +
                    marginChoices(g.margin).map(function (o) {
                        return '<option value="' + o[0] + '"' + (Math.abs((g.margin || 0) - o[0]) < 1e-6 ? ' selected' : '') + '>' + o[1] + '</option>';
                    }).join('') + '</select></label>' +
                    '<p class="hint">Le son du groupe doit battre de cet écart le meilleur autre son entendu (voix, musique…) : moins de ' +
                    'fausses détections dans une pièce bruyante. « Presque déclenchés » montre les gestes écartés ainsi.</p>') +
                // Recherche dans la liste, et parmi tous les sons connus : un son
                // s'ajoute sans avoir ete entendu (« Applaudissement »...).
                '<input type="search" class="input sound-search" data-role="sound-search" placeholder="Rechercher ou ajouter un son…" aria-label="Rechercher ou ajouter un son au groupe ' + gname + '" value="' +
                    esc(CT.state.search[src.domId + '|' + g.slug] || '') + '">' +
                '<div class="chips sound-suggest" data-role="suggest" aria-live="polite"></div>' +
                '<div class="chips">' + (chips || '<p class="hint">' + (g.auto_add_sounds
                    ? 'Les sons entendus pendant la détection apparaîtront ici.'
                    : 'Activez « Ajouter les sons entendus » puis lancez la détection : les sons entendus apparaîtront ici.') + '</p>') + '</div>' +
            '</div></section>';
    }

    CT.renderGroupsManage = function (card, src) {
        CT.withFocus(function () { renderGroupsManage(card, src); });
    };
    function renderGroupsManage(card, src) {
        var box = card.querySelector('[data-role="groups"]');
        if (!box) return;
        var groups = (src.data.sound_groups || []).filter(function (g) { return g && g.slug; });
        box.innerHTML = groups.map(function (g, idx) {
            var elsewhere = {};
            groups.forEach(function (o) {
                if (o === g) return;
                Object.keys(o.sound_whitelist || {}).forEach(function (l) { if (o.sound_whitelist[l]) elsewhere[l] = o.name || o.slug; });
            });
            return groupHtml(src, g, idx === 0, elsewhere);
        }).join('') || '<p class="hint">Aucun groupe.</p>';
        bindGroups(box, src, groups);
        // Reappliquer la recherche en cours (cocher un son l'effacait).
        CT.$$('.sound-search', box).forEach(function (input) { if (input.value) filterChips(input); });
    }

    // Sons connus (les 521 classes de YAMNet, chargees une fois) correspondant
    // a la recherche : un toucher les ajoute coches. Sauf ceux deja dans le
    // groupe, actifs dans un autre groupe de la source (refuses par le
    // serveur) ou ignores partout (sans effet).
    var allSounds = null;
    function knownSounds() {
        if (!allSounds) {
            allSounds = Object.keys(CT.SOUNDS_FR || {});   // en attendant la liste complete
            CT.api('GET', '/api/sounds/known').then(function (d) {
                if (d && d.labels && d.labels.length) allSounds = d.labels;
            }).catch(function () {});
        }
        return allSounds;
    }
    // Son appris enregistre ou supprime : liste relue a la recherche suivante.
    CT.invalidateKnownSounds = function () { allSounds = null; };
    // Ecarts proposes (en points), et la valeur enregistree si elle n'en fait
    // pas partie (reglee par l'API ou une commande MQTT).
    function marginChoices(current) {
        var list = [[0, 'Aucun'], [0.05, '5 points'], [0.1, '10 points'], [0.2, '20 points'], [0.3, '30 points']];
        if (current && !list.some(function (o) { return Math.abs(o[0] - current) < 1e-6; })) {
            list.push([current, Math.round(current * 100) + ' points']);
            list.sort(function (a, b) { return a[0] - b[0]; });
        }
        return list;
    }
    function renderSuggestions(search, wl, src) {
        var box = search.closest('.group-card').querySelector('[data-role="suggest"]');
        var q = search.value.trim().toLowerCase();
        if (!box) return;
        if (q.length < 2) { box.innerHTML = ''; return; }
        var slug = search.closest('.group-card').getAttribute('data-slug');
        var elsewhere = {};
        ((src && src.data.sound_groups) || []).forEach(function (g) {
            if (g && g.slug !== slug) Object.keys(g.sound_whitelist || {}).forEach(function (l) { if (g.sound_whitelist[l]) elsewhere[l] = true; });
        });
        var excluded = (CT.state.settings.global || {}).sound_exclusions || [];
        var found = knownSounds().filter(function (l) {
            return !(l in wl) && !elsewhere[l] && excluded.indexOf(l) === -1 &&
                (l + ' ' + CT.soundLabel(l)).toLowerCase().indexOf(q) !== -1;
        }).sort(function (a, b) { return CT.soundLabel(a).localeCompare(CT.soundLabel(b), 'fr'); }).slice(0, 8);
        box.innerHTML = found.length ? '<span class="hint">Ajouter :</span>' + found.map(function (l) {
            return '<button type="button" class="chip chip-add" data-group-action="add-sound" data-label="' + esc(l) + '" title="' + esc(l) + '">+ ' +
                esc(CT.soundLabel(l)) + '</button>';
        }).join('') : '';
    }

    function filterChips(search) {
        // Pastilles du groupe seulement (pas les boutons « + son » proposes).
        CT.filterChips(CT.$$('label.chip', search.closest('.group-card')), search.value,
                       function (chip) { return chip.querySelector('input').getAttribute('data-label'); });
    }

    function putGroup(src, slug, body) {
        return CT.api('PUT', '/api/source/sound_groups', Object.assign({kind: src.kind, source_key: src.key, group_slug: slug}, body));
    }
    function bindGroups(box, src, groups) {
        // Delegation posee une seule fois : la boite est re-rendue sur place.
        box._ctx = {src: src, groups: groups};
        if (!box._bound) {
            box._bound = true;
            box.addEventListener('click', function (e) { onBoxClick(e, box._ctx.src); });
            // Pastilles de sons : un ecouteur pour toutes (il y en a des
            // centaines, recreees a chaque coche).
            box.addEventListener('change', function (e) {
                if (e.target.matches('.chip input')) onChipChange(e.target, box);
            });
        }
        bindGroupInputs(box, src, groups);
    }

    function onChipChange(cb, box) {
        var src = box._ctx.src;
        var slug = cb.closest('.group-card').getAttribute('data-slug');
        var group = box._ctx.groups.filter(function (g) { return g.slug === slug; })[0];
        if (!group) return;
        var label = cb.getAttribute('data-label');
        var on = cb.checked;
        CT.api('PUT', '/api/source/sound_whitelist', {kind: src.kind, source_key: src.key, group_slug: slug,
                                                      label: label, enabled: on})
            .then(function () {
                (group.sound_whitelist = group.sound_whitelist || {})[label] = on;
                CT.renderGroupsManage(box.closest('.source-card'), src);
                // Groupe vide ou non : barre d'etat tout de suite,
                // carte a la fermeture des reglages.
                if (CT.renderStatus) CT.renderStatus();
                CT.renderWhenIdle();
            })
            .catch(function (err) { cb.checked = !on; CT.error(err.message); });
    }

    function onBoxClick(e, src) {
        var copy = e.target.closest('[data-copy]');
        if (copy) { CT.copy(copy.getAttribute('data-copy')); return; }
        var btn = e.target.closest('[data-group-action]');
        if (!btn) return;
        var slug = btn.closest('.group-card').getAttribute('data-slug');
        var action = btn.getAttribute('data-group-action');
        if (action === 'delete') {
            // Nommer le groupe : la fenetre en contient souvent plusieurs.
            var box0 = btn.closest('[data-role="groups"]');
            var g0 = box0 && box0._ctx && (box0._ctx.groups || []).filter(function (g) { return g.slug === slug; })[0];
            var gname = (g0 && (g0.name || g0.slug)) || slug;
            CT.confirm('Supprimer le groupe « ' + gname + ' » et ses entités Home Assistant ?', 'Supprimer le groupe').then(function (ok) {
                if (!ok) return;
                CT.api('DELETE', '/api/source/sound_groups', {kind: src.kind, source_key: src.key, group_slug: slug})
                    .then(function () { return refreshGroups(src); })
                    .then(function () { CT.success('Groupe supprimé'); })
                    .catch(function (err) { CT.error('Suppression impossible : ' + err.message); });
            });
        } else if (action === 'add-sound') {
            var label = btn.getAttribute('data-label');
            var box = btn.closest('[data-role="groups"]');
            var group = box && (box._ctx.groups || []).filter(function (g) { return g.slug === slug; })[0];
            if (!group) return;
            btn.disabled = true;
            CT.api('PUT', '/api/source/sound_whitelist', {kind: src.kind, source_key: src.key, group_slug: slug,
                                                          label: label, enabled: true})
                .then(function () {
                    (group.sound_whitelist = group.sound_whitelist || {})[label] = true;
                    CT.state.search[src.domId + '|' + slug] = '';
                    CT.renderGroupsManage(box.closest('.source-card'), src);
                    if (CT.renderStatus) CT.renderStatus();
                    CT.renderWhenIdle();
                    CT.success('« ' + CT.soundLabel(label) + ' » ajouté au groupe');
                })
                .catch(function (err) { btn.disabled = false; CT.error('Son non ajouté : ' + err.message); });
        } else if (action === 'cleanup') {
            CT.api('POST', '/api/source/sound_whitelist/cleanup', {kind: src.kind, source_key: src.key, group_slug: slug})
                .then(function (d) { return refreshGroups(src).then(function () { CT.success((d.removed || 0) + ' son(s) retiré(s)'); }); })
                .catch(function (err) { CT.error('Nettoyage impossible : ' + err.message); });
        }
    }

    function bindGroupInputs(box, src, groups) {
        CT.$$('.group-card', box).forEach(function (cardEl) {
            var slug = cardEl.getAttribute('data-slug');
            var group = groups.filter(function (g) { return g.slug === slug; })[0];
            var nameInput = cardEl.querySelector('.group-name');
            nameInput.addEventListener('keydown', function (e) {
                if (e.key === 'Enter') { e.preventDefault(); nameInput.blur(); }  // valider avec Entree
            });
            nameInput.addEventListener('change', function () {
                var name = nameInput.value.trim();
                if (!name || name === group.name) { nameInput.value = group.name; return; }
                if (nameInput._renaming) return;   // deuxieme « change » pendant la recherche
                nameInput._renaming = true;
                nameInput.readOnly = true;
                // Les entity_id suivent le nom : prevenir avant, avec ce que HA
                // ne mettra pas a jour (automatisations, scripts, scenes).
                var q = '?kind=' + encodeURIComponent(src.kind) + '&source_key=' + encodeURIComponent(src.key) +
                        '&group_slug=' + encodeURIComponent(slug);
                CT.api('GET', '/api/source/sound_groups/usage' + q).catch(function () { return {available: false}; })
                    .then(function (u) {
                        var msg = 'Renommer « ' + (group.name || slug) + ' » en « ' + name + ' » ? Les entités Home Assistant du groupe ' +
                            'seront renommées d\'après le nouveau nom (historique conservé). Home Assistant ne met pas à jour ' +
                            'les automatisations, scripts et scènes qui les utilisent.';
                        if (u && u.items && u.items.length) {
                            msg += '\n\nÀ adapter après le renommage :\n' + u.items.map(function (i) {
                                return '• ' + i.type + ' « ' + i.nom + ' »';
                            }).join('\n');
                        } else if (u && u.available) {
                            msg += '\n\nAucune automatisation, aucun script ni aucune scène ne les utilise.';
                        } else {
                            msg += '\n\nPensez à adapter vos automatisations (liste dans le journal de l\'add-on).';
                        }
                        return CT.dialog(msg, {okLabel: 'Renommer'});
                    })
                    .then(function (ok) {
                        nameInput._renaming = false;
                        nameInput.readOnly = false;
                        if (!ok) { nameInput.value = group.name; return; }
                        var before = group.entity_slug || slug;
                        return putGroup(src, slug, {name: name}).then(function (d) {
                            group.name = name;
                            var after = (d && d.group && d.group.entity_slug) || before;
                            group.entity_slug = after;
                            CT.success(after !== before ? 'Groupe renommé ; ses entités sont renommées dans Home Assistant'
                                                        : 'Groupe renommé');
                            // Apres le renommage enregistre : un echec ici n'est pas un echec du renommage.
                            CT.reloadEntityIds().then(function () {
                                CT.renderGroupsManage(box.closest('.source-card'), src);
                                CT.renderWhenIdle();
                            }).catch(function () {});
                        }, function (err) { nameInput.value = group.name; CT.error('Renommage impossible : ' + err.message); });
                    });
            });
            CT.$$('[data-clap]', cardEl).forEach(function (cb) {
                cb.addEventListener('change', function () {
                    var counts = CT.$$('[data-clap]', cardEl).filter(function (c) { return c.checked; })
                        .map(function (c) { return parseInt(c.getAttribute('data-clap'), 10); });
                    putGroup(src, slug, {ha_entities: counts})
                        .then(function () {
                            group.ha_entities = counts;
                            return CT.reloadEntityIds().then(function () { CT.renderGroupsManage(box.closest('.source-card'), src); });
                        })
                        .catch(function (err) { cb.checked = !cb.checked; CT.error('Entités non enregistrées : ' + err.message); });
                });
            });
            var enabledSw = cardEl.querySelector('[data-role="group-enabled"]');
            enabledSw.addEventListener('change', function () {
                var on = enabledSw.checked;
                putGroup(src, slug, {enabled: on})
                    .then(function () {
                        group.enabled = on;
                        CT.success(on ? 'Groupe activé' : 'Groupe désactivé : il ne se déclenche plus');
                        CT.renderWhenIdle();
                    })
                    .catch(function (err) { enabledSw.checked = !on; CT.error('Réglage non enregistré : ' + err.message); });
            });
            var mode = cardEl.querySelector('[data-role="count-mode"]');
            mode.addEventListener('change', function () {
                var value = mode.value, before = group.count_mode || 'attaques';
                putGroup(src, slug, {count_mode: value})
                    .then(function () {
                        group.count_mode = value;
                        if ((value === 'alerte') === (before === 'alerte')) { CT.markSaved(mode, 'Comptage enregistré'); return; }
                        // Alerte <-> claps : autres entites dans HA, autre panneau
                        // (un echec ici n'est pas un echec de l'enregistrement).
                        CT.reloadEntityIds().then(function () {
                            var card = box.closest('.source-card');
                            CT.renderGroupsManage(card, src);
                            CT.markSaved(card.querySelector('.group-card[data-slug="' + CT.cssEscape(slug) + '"] [data-role="count-mode"]'),
                                         value === 'alerte' ? 'Mode alerte : un capteur tant que le son dure' : 'Comptage enregistré');
                        }).catch(function () {});
                    }, function (err) { mode.value = before; CT.error('Réglage non enregistré : ' + err.message); });
            });
            var marginSel = cardEl.querySelector('[data-role="margin"]');
            if (marginSel) marginSel.addEventListener('change', function () {
                var value = parseFloat(marginSel.value), before = group.margin || 0;
                putGroup(src, slug, {margin: value})
                    .then(function () { group.margin = value; CT.markSaved(marginSel, value ? 'Écart enregistré' : 'Sans écart minimal'); })
                    .catch(function (err) { marginSel.value = String(before); CT.error('Réglage non enregistré : ' + err.message); });
            });
            var confirmSel = cardEl.querySelector('[data-role="alert-confirm"]');
            if (confirmSel) confirmSel.addEventListener('change', function () {
                var value = parseInt(confirmSel.value, 10), before = group.alert_confirm || 2;
                putGroup(src, slug, {alert_confirm: value})
                    .then(function () { group.alert_confirm = value; CT.markSaved(confirmSel, 'Confirmation enregistrée'); })
                    .catch(function (err) { confirmSel.value = String(before); CT.error('Réglage non enregistré : ' + err.message); });
            });
            var dcSel = cardEl.querySelector('[data-role="device-class"]');
            if (dcSel) dcSel.addEventListener('change', function () {
                var value = dcSel.value, before = group.device_class || 'sound';
                putGroup(src, slug, {device_class: value})
                    .then(function () { group.device_class = value; CT.markSaved(dcSel, 'Type enregistré'); })
                    .catch(function (err) { dcSel.value = before; CT.error('Réglage non enregistré : ' + err.message); });
            });
            var auto = cardEl.querySelector('[data-role="auto-add"]');
            auto.addEventListener('change', function () {
                var on = auto.checked;
                putGroup(src, slug, {auto_add_sounds: on})
                    .then(function () {
                        group.auto_add_sounds = on;
                        var card = box.closest('.source-card');
                        CT.renderGroupsManage(card, src);
                        // Apres le rendu : la coche placee avant etait aussitot effacee.
                        CT.markSaved(card.querySelector('.group-card[data-slug="' + CT.cssEscape(slug) + '"] [data-role="auto-add"]'),
                                     on ? 'Sons entendus ajoutés' : 'Ajout automatique arrêté');
                    })
                    .catch(function (err) { auto.checked = !on; CT.error('Réglage non enregistré : ' + err.message); });
            });
            var search = cardEl.querySelector('.sound-search');
            if (search) {
                search.addEventListener('input', function () {
                    CT.state.search[src.domId + '|' + slug] = search.value;
                    filterChips(search);
                    renderSuggestions(search, group.sound_whitelist || {}, src);
                });
                if (search.value) renderSuggestions(search, group.sound_whitelist || {}, src);
            }
        });
    }

    // Recharger puis, si les reglages de la source sont ouverts (la grille
    // attend alors leur fermeture), mettre a jour les groupes sur place.
    function refreshGroups(src) {
        return CT.refresh().then(function () {
            var card = document.getElementById(src.domId);
            var fresh = CT.sourceByDomId(src.domId);
            if (card && fresh && card.querySelector('dialog.source-settings[open]')) CT.renderGroupsManage(card, fresh);
        });
    }

    // Ajout d'un groupe : modele pret a l'emploi (sons deja coches, comptage
    // ou alerte) ou groupe vide a nommer.
    var presetsCache = null;
    function loadPresets() {
        if (presetsCache) return Promise.resolve(presetsCache);
        return CT.api('GET', '/api/sound_groups/presets').then(function (d) { presetsCache = d.presets || []; return presetsCache; });
    }
    function createGroup(src, body, label) {
        return CT.api('POST', '/api/source/sound_groups', Object.assign({kind: src.kind, source_key: src.key}, body))
            .then(function (d) { return refreshGroups(src).then(function () { return d; }); })
            .then(function (d) {
                var skipped = (d && d.skipped_sounds) || [];
                CT.success('Groupe « ' + ((d && d.group && d.group.name) || label) + ' » créé' +
                           (skipped.length ? ' (déjà dans un autre groupe, non cochés : ' + skipped.map(CT.soundLabel).join(', ') + ')' : ''));
            })
            .catch(function (err) { CT.error('Création impossible : ' + err.message); });
    }
    function askName(src) {
        CT.prompt('Nom du nouveau groupe (ex. « Toc sur la table ») :', '', 'Créer').then(function (name) {
            name = (name || '').trim();
            if (name) createGroup(src, {name: name}, name);
        });
    }
    CT.addGroup = function (src) {
        loadPresets().then(function (presets) {
            var back = document.createElement('div');
            back.className = 'modal-backdrop';
            var item = function (p) {
                var kind = p.count_mode === 'alerte' ? 'Alerte : capteur tant que le son dure'
                    : 'Gestes : ' + (p.ha_entities || []).join(', ') + ' clap(s)';
                return '<button type="button" class="choice choice-preset" data-preset="' + esc(p.id) + '"><strong>' + esc(p.name) + '</strong>' +
                    '<span class="muted">' + esc(kind) + ' · ' + esc(p.sounds.map(CT.soundLabel).join(', ')) + '</span></button>';
            };
            back.innerHTML = '<div class="modal" role="dialog" aria-modal="true" aria-labelledby="gp-title">' +
                '<div class="modal-head"><h2 id="gp-title">Nouveau groupe</h2></div>' +
                '<p class="hint">Un modèle coche déjà les bons sons et règle le comptage. Tout reste modifiable ensuite.</p>' +
                '<div class="choice-grid">' + presets.map(item).join('') +
                '<button type="button" class="choice choice-preset" data-preset=""><strong>Groupe vide</strong><span class="muted">Nom libre, sons à choisir</span></button></div>' +
                '<div class="modal-actions"><button type="button" class="btn btn-ghost" data-r="close">Annuler</button></div></div>';
            // Dans la fenetre native ouverte (reglages de la source) : ajoutee au
            // document, elle restait cachee dessous.
            (document.querySelector('dialog[open]') || document.body).appendChild(back);
            var release = CT.trapFocus(back.querySelector('.modal'), close);
            function close() { release(); back.remove(); }
            back.addEventListener('click', function (e) {
                var choice = e.target.closest('[data-preset]');
                if (e.target === back || (e.target.getAttribute && e.target.getAttribute('data-r') === 'close')) return close();
                if (!choice) return;
                var id = choice.getAttribute('data-preset');
                close();
                if (!id) return askName(src);
                var p = presets.filter(function (x) { return x.id === id; })[0];
                createGroup(src, {preset: id}, p ? p.name : id);
            });
            CT.focusFirst(back.querySelector('.modal'));
        }).catch(function () { askName(src); });   // liste indisponible : groupe vide
    };

    // Auto-decouverte : un nouveau son entendu apparait dans les groupes qui
    // ajoutent les sons entendus.
    CT.on('sound_seen', function (d) {
        if (!d || !d.label) return;
        var src = CT.findSource(function (s) { return s.sourceId === d.source_id; });
        if (!src) return;
        var changed = false;
        (src.data.sound_groups || []).forEach(function (g) {
            if (!g.auto_add_sounds) return;
            g.sound_whitelist = g.sound_whitelist || {};
            if (!(d.label in g.sound_whitelist)) { g.sound_whitelist[d.label] = false; changed = true; }
        });
        var card = document.getElementById(src.domId);
        if (!changed || !card) return;
        // Mise a jour sur place, y compris fenetre de reglages ouverte
        // (c'est la qu'on regarde les sons arriver). Pendant une saisie :
        // a la fin de celle-ci (le son n'apparaissait sinon jamais).
        CT.whenIdle('groups:' + src.domId, CT.isTyping, function () {
            var fresh = CT.sourceByDomId(src.domId);
            var el = document.getElementById(src.domId);
            if (fresh && el) CT.renderGroupsManage(el, fresh);
        });
    });
})();
