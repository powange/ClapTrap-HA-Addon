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

    function groupHtml(src, g, isDefault, activeElsewhere) {
        var wl = g.sound_whitelist || {};
        var labels = Object.keys(wl).sort(function (a, b) {
            if (!!wl[a] !== !!wl[b]) return wl[a] ? -1 : 1;
            return CT.soundLabel(a).localeCompare(CT.soundLabel(b), 'fr');
        });
        var chips = labels.map(function (label) {
            var on = !!wl[label];
            var other = activeElsewhere[label];
            var disabled = !on && other;
            var excluded = (CT.state.settings.global || {}).sound_exclusions || [];
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
                '<input type="text" class="input group-name" value="' + esc(g.name || g.slug) + '" aria-label="Nom du groupe">' +
                (isDefault ? '<span class="pill">Groupe principal</span>'
                           : '<button type="button" class="btn-link danger" data-group-action="delete">Supprimer le groupe</button>') +
            '</div>' +
            '<fieldset class="field"><legend class="field-label">Entités Home Assistant à créer</legend><div class="checks">' + claps + '</div>' +
            (entities ? '<ul class="entity-list">' + entities + '</ul>' : '<p class="hint">Aucune entité : cochez au moins un nombre de claps.</p>') +
            '</fieldset>' +
            '<div class="field"><div class="field-row"><span class="field-label">Sons qui déclenchent ce groupe</span>' +
                '<button type="button" class="btn-link" data-group-action="cleanup">Vider la liste des sons non cochés</button></div>' +
                (labels.length > 8 ? '<input type="search" class="input sound-search" placeholder="Rechercher un son…" aria-label="Rechercher un son" value="' +
                    esc(CT.state.search[src.domId + '|' + g.slug] || '') + '">' : '') +
                '<div class="chips">' + (chips || '<p class="hint">Les sons entendus pendant la détection apparaîtront ici.</p>') + '</div>' +
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

    function filterChips(search) {
        var q = search.value.trim().toLowerCase();
        CT.$$('.chip', search.closest('.group-card')).forEach(function (chip) {
            var label = chip.querySelector('input').getAttribute('data-label');
            chip.hidden = !!q && (label + ' ' + CT.soundLabel(label)).toLowerCase().indexOf(q) === -1;
        });
    }

    function putGroup(src, slug, body) {
        return CT.api('PUT', '/api/source/sound_groups', Object.assign({kind: src.kind, source_key: src.key, group_slug: slug}, body));
    }
    function bindGroups(box, src, groups) {
        // Delegation posee une seule fois : la boite est re-rendue sur place.
        box._ctx = {src: src};
        if (!box._bound) {
            box._bound = true;
            box.addEventListener('click', function (e) { onBoxClick(e, box._ctx.src); });
        }
        bindGroupInputs(box, src, groups);
    }

    function onBoxClick(e, src) {
        {
            var copy = e.target.closest('[data-copy]');
            if (copy) { CT.copy(copy.getAttribute('data-copy')); return; }
            var btn = e.target.closest('[data-group-action]');
            if (!btn) return;
            var slug = btn.closest('.group-card').getAttribute('data-slug');
            var action = btn.getAttribute('data-group-action');
            if (action === 'delete') {
                CT.confirm('Supprimer ce groupe et ses entités Home Assistant ?').then(function (ok) {
                    if (!ok) return;
                    CT.api('DELETE', '/api/source/sound_groups', {kind: src.kind, source_key: src.key, group_slug: slug})
                        .then(function () { return CT.refresh(); })
                        .then(function () { CT.success('Groupe supprimé'); })
                        .catch(function (err) { CT.error('Suppression impossible : ' + err.message); });
                });
            } else if (action === 'cleanup') {
                CT.api('POST', '/api/source/sound_whitelist/cleanup', {kind: src.kind, source_key: src.key, group_slug: slug})
                    .then(function (d) { return CT.refresh().then(function () { CT.success((d.removed || 0) + ' son(s) retiré(s)'); }); })
                    .catch(function (err) { CT.error('Nettoyage impossible : ' + err.message); });
            }
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
                if (!name) { nameInput.value = group.name; return; }
                putGroup(src, slug, {name: name})
                    .then(function () { group.name = name; CT.renderSources(); })
                    .catch(function (err) { nameInput.value = group.name; CT.error('Renommage impossible : ' + err.message); });
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
            CT.$$('.chip input', cardEl).forEach(function (cb) {
                cb.addEventListener('change', function () {
                    var label = cb.getAttribute('data-label');
                    var on = cb.checked;
                    CT.api('PUT', '/api/source/sound_whitelist', {kind: src.kind, source_key: src.key, group_slug: slug,
                                                                  label: label, enabled: on})
                        .then(function () {
                            (group.sound_whitelist = group.sound_whitelist || {})[label] = on;
                            CT.renderGroupsManage(box.closest('.source-card'), src);
                        })
                        .catch(function (err) { cb.checked = !on; CT.error(err.message); });
                });
            });
            var search = cardEl.querySelector('.sound-search');
            if (search) {
                search.addEventListener('input', function () {
                    CT.state.search[src.domId + '|' + slug] = search.value;
                    filterChips(search);
                });
            }
        });
    }

    CT.addGroup = function (src) {
        CT.prompt('Nom du nouveau groupe (ex. « Toc sur la table ») :', '', 'Créer').then(function (name) {
            name = (name || '').trim();
            if (!name) return;
            CT.api('POST', '/api/source/sound_groups', {kind: src.kind, source_key: src.key, name: name})
                .then(function () { CT.state.openPanels[src.domId] = true; return CT.refresh(); })
                .then(function () { CT.success('Groupe « ' + name + ' » créé'); })
                .catch(function (err) { CT.error('Création impossible : ' + err.message); });
        });
    };

    // Auto-decouverte : un nouveau son entendu apparait dans chaque groupe.
    CT.on('sound_seen', function (d) {
        if (!d || !d.label) return;
        var src = CT.findSource(function (s) { return s.sourceId === d.source_id; });
        if (!src) return;
        var changed = false;
        (src.data.sound_groups || []).forEach(function (g) {
            g.sound_whitelist = g.sound_whitelist || {};
            if (!(d.label in g.sound_whitelist)) { g.sound_whitelist[d.label] = false; changed = true; }
        });
        var card = document.getElementById(src.domId);
        if (changed && card && !CT.isEditing()) CT.renderGroupsManage(card, src);
    });
})();
