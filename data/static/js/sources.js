/* Cartes de source : en-tete, VU-metre, score de chaque groupe face a son seuil
 * (avec courbe des 10 dernieres secondes), reglages de la source. */
(function () {
    'use strict';
    var CT = window.CT;
    var esc = CT.esc;
    var SPARK_SECONDS = 10;

    function groupsOf(src) {
        return (src.data.sound_groups || []).filter(function (g) { return g && g.slug; });
    }

    // ---- Rendu -----------------------------------------------------------------
    function statusOf(src) {
        var st = CT.state.status;
        if (!src.enabled) return {text: 'Désactivée', cls: 'off'};
        if (!st.running || (st.sources || []).indexOf(src.sourceId) === -1) {
            if (src.kind === 'rtsp' && !src.data.url) return {text: 'URL à renseigner', cls: 'warn'};
            return {text: 'Prête', cls: 'idle'};
        }
        if (src.kind === 'rtsp') {
            var r = CT.state.rtspStatus[src.key];
            if (r === 'connected') return {text: 'Connectée', cls: 'ok'};
            if (r === 'reconnecting') return {text: 'Flux perdu, reconnexion…', cls: 'warn'};
            if (r === 'error') return {text: 'Erreur de flux', cls: 'err'};
            return {text: 'Connexion…', cls: 'warn'};
        }
        return {text: "À l'écoute", cls: 'ok'};
    }

    function liveGroupHtml(src, g) {
        var t = g.threshold != null ? g.threshold : 0.5;
        return '<div class="group-live" data-slug="' + esc(g.slug) + '">' +
            '<div class="group-live-head"><span class="group-live-name">' + esc(g.name || g.slug) + '</span>' +
            '<span class="clap-badge" hidden></span>' +
            '<span class="group-live-score" aria-label="Score actuel">0,00</span></div>' +
            '<div class="scorebar"><div class="scorebar-fill"></div><div class="scorebar-mark" style="left:' + (t * 100) + '%"></div>' +
            '<input type="range" class="threshold" min="0" max="1" step="0.01" value="' + t + '" ' +
            'aria-label="Seuil de confiance du groupe ' + esc(g.name || g.slug) + '"></div>' +
            '<canvas class="spark" width="320" height="40" aria-hidden="true"></canvas>' +
            '<div class="group-live-foot"><span>Seuil de confiance <strong class="threshold-value">' + CT.fmt(t) + '</strong></span>' +
            '<span class="muted">' + SPARK_SECONDS + ' dernières secondes</span></div></div>';
    }

    function settingsHtml(src) {
        var d = src.data, html = '';
        if (src.kind === 'mic') {
            var devices = CT.state.devices || [];
            var current = d.audio_source || 'default';
            var found = devices.some(function (dev) { return dev.name === current; });
            var opts = '<option value="default"' + (current === 'default' ? ' selected' : '') + '>Micro par défaut du système</option>';
            if (!found && current !== 'default') opts += '<option value="current" selected>' + esc(current) + ' (non détecté)</option>';
            devices.forEach(function (dev, i) {
                opts += '<option value="dev:' + i + '"' + (dev.name === current ? ' selected' : '') + '>' + esc(dev.name) + '</option>';
            });
            var vol = d.volume != null ? d.volume : 100;
            html += field('Périphérique', '<select class="input" data-field="device" id="' + src.domId + '-device">' + opts + '</select>' +
                (devices.length ? '' : '<p class="hint">Aucun micro détecté par Home Assistant (Paramètres › Système › Matériel).</p>'), src.domId + '-device') +
                '<div class="field"><div class="field-row"><label for="' + src.domId + '-volume">Volume du micro <strong data-out="volume">' + vol + ' %</strong></label>' +
                '<label class="switch-inline"><input type="checkbox" role="switch" data-field="auto_volume"' + (d.auto_volume ? ' checked' : '') + '> Automatique</label></div>' +
                '<input type="range" id="' + src.domId + '-volume" data-field="volume" min="0" max="150" step="5" value="' + vol + '"' + (d.auto_volume ? ' disabled' : '') + '>' +
                '<p class="hint">Le volume automatique ajuste le micro pour garder un bon niveau.</p></div>';
        } else if (src.kind === 'rtsp') {
            var gain = d.gain != null ? d.gain : 10;
            html += field('Nom', '<input type="text" class="input" data-field="name" id="' + src.domId + '-name" value="' + esc(d.name || '') + '">', src.domId + '-name') +
                field('Adresse du flux', '<input type="url" class="input" data-field="url" id="' + src.domId + '-url" value="' + esc(d.url || '') + '" placeholder="rtsp://camera:554/stream" spellcheck="false">' +
                    '<p class="hint">Les identifiants éventuels (rtsp://utilisateur:mot-de-passe@…) ne sont jamais affichés dans les journaux.</p>', src.domId + '-url') +
                gainField(src, gain, 50);
        } else {
            var mcast = /^2(2[4-9]|3\d)\./.test(d.ip || '');
            html += '<div class="field"><span class="field-label">Flux</span><p class="mono">' + esc(d.name) + ' · ' + esc(d.ip) + ':' + esc(String(d.port || 6980)) +
                ' <span class="pill">' + (mcast ? 'multicast' : 'unicast') + '</span></p></div>' +
                gainField(src, d.gain != null ? d.gain : 1, 20);
        }
        html += '<div class="field"><span class="field-label">Tester le son</span>' +
            '<button type="button" class="btn btn-ghost" data-action="test">Écouter cette source</button>' +
            '<p class="hint">Affiche le niveau sonore dans le VU-mètre de la carte, sans lancer la détection.</p></div>';
        html += '<div class="field"><span class="field-label">Groupes de sons</span>' +
            '<p class="hint">Chaque groupe a son propre seuil, ses entités Home Assistant et sa liste de sons. Un son ne peut être actif que dans un seul groupe d\'une même source.</p>' +
            '<div class="groups-manage" data-role="groups"></div>' +
            '<button type="button" class="btn btn-ghost" data-action="add-group">Nouveau groupe</button></div>';
        html += field('Webhook (facultatif)',
            '<div class="input-group"><input type="url" class="input" data-field="webhook_url" id="' + src.domId + '-webhook" value="' + esc(d.webhook_url || '') + '" placeholder="http://homeassistant:8123/api/webhook/…" spellcheck="false">' +
            '<button type="button" class="btn btn-ghost" data-action="test-webhook">Tester</button></div>' +
            '<p class="hint">Appelé à chaque détection, en plus des entités Home Assistant.</p>', src.domId + '-webhook');
        return html;
    }
    function field(label, control, forId) {
        return '<div class="field"><label class="field-label"' + (forId ? ' for="' + forId + '"' : '') + '>' + label + '</label>' + control + '</div>';
    }
    function gainField(src, gain, max) {
        return '<div class="field"><label for="' + src.domId + '-gain">Gain <strong data-out="gain">' + gain + '×</strong></label>' +
            '<input type="range" id="' + src.domId + '-gain" data-field="gain" min="1" max="' + max + '" step="1" value="' + gain + '">' +
            '<p class="hint">Amplifie un flux trop faible. Surveillez le VU-mètre : il ne doit pas rester dans le rouge.</p></div>';
    }

    function cardHtml(src) {
        var groups = groupsOf(src);
        var open = CT.state.openPanels[src.domId] ? ' open' : '';
        return '<article class="card source-card' + (src.enabled ? '' : ' is-disabled') + '" id="' + src.domId + '">' +
            '<header class="card-head">' +
                '<span class="tag tag-' + src.kind + '">' + CT.kindLabel[src.kind] + '</span>' +
                '<h3 class="card-title">' + esc(src.name) + '</h3>' +
                '<label class="switch"><input type="checkbox" role="switch" data-action="toggle"' + (src.enabled ? ' checked' : '') +
                ' aria-label="Activer ' + esc(src.name) + '"><span class="switch-track" aria-hidden="true"></span></label>' +
                '<div class="menu"><button type="button" class="btn-icon" data-action="menu" aria-haspopup="true" aria-expanded="false" aria-label="Actions pour ' + esc(src.name) + '">' +
                '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><circle cx="5" cy="12" r="2" fill="currentColor"/><circle cx="12" cy="12" r="2" fill="currentColor"/><circle cx="19" cy="12" r="2" fill="currentColor"/></svg></button>' +
                '<div class="menu-list" role="menu" hidden>' +
                    '<button type="button" role="menuitem" data-action="test">Écouter cette source</button>' +
                    '<button type="button" role="menuitem" data-action="open-settings">Réglages de la source</button>' +
                    '<button type="button" role="menuitem" class="danger" data-action="delete">Supprimer…</button>' +
                '</div></div>' +
            '</header>' +
            '<p class="source-status" data-role="status"><span class="dot"></span><span class="status-text"></span></p>' +
            '<div class="meter" data-role="meter"><div class="meter-fill"></div><span class="meter-label" data-role="meter-label">—</span></div>' +
            '<p class="live-line" data-role="live">' + (src.enabled ? 'En attente de sons…' : 'Source désactivée') + '</p>' +
            '<div class="groups-live">' + groups.map(function (g) { return liveGroupHtml(src, g); }).join('') + '</div>' +
            '<details class="source-settings" data-role="settings"' + open + '><summary>Réglages de la source</summary>' +
            '<div class="settings-body">' + settingsHtml(src) + '</div></details>' +
            '</article>';
    }

    CT.renderSources = function () {
        var grid = document.getElementById('sources-grid');
        if (!grid) return;
        CT.$$('details.source-settings', grid).forEach(function (d) {
            var card = d.closest('.source-card');
            if (card) CT.state.openPanels[card.id] = d.open;
        });
        var sources = CT.sourceList();
        grid.innerHTML = sources.map(cardHtml).join('') +
            '<button type="button" class="card add-card" id="add-source">' +
            '<span class="add-plus" aria-hidden="true">+</span><span>Ajouter une source</span>' +
            '<span class="muted">Micro, caméra RTSP ou flux VBAN</span></button>';
        CT.$('#sources-empty').hidden = sources.length > 0;
        sources.forEach(function (src) {
            var card = document.getElementById(src.domId);
            bindCard(card, src);
            if (CT.renderGroupsManage) CT.renderGroupsManage(card, src);
        });
        CT.$('#add-source').addEventListener('click', function () { if (CT.openWizard) CT.openWizard(); });
        // Scores et courbes conserves d'un rendu a l'autre (ils repartaient a
        // zero a chaque action), ligne de seuil visible des l'affichage.
        var now = Date.now() / 1000;
        sources.forEach(function (src) {
            var live = (CT.state.live[src.sourceId] || {scores: {}}).scores;
            CT.$$('.group-live', document.getElementById(src.domId)).forEach(function (row) {
                var series = live[row.getAttribute('data-slug')] || [];
                var threshold = parseFloat(row.querySelector('.threshold').value);
                if (series.length) setScore(row, series[series.length - 1][1], threshold);
                drawSpark(row.querySelector('canvas'), series, threshold, now);
            });
        });
        CT.renderSourceStatuses();
        if (CT.state.testing) CT.onTestChange(CT.state.testing, true);
    };

    CT.renderSourceStatuses = function () {
        CT.sourceList().forEach(function (src) {
            var card = document.getElementById(src.domId);
            if (!card) return;
            var st = statusOf(src);
            var el = card.querySelector('[data-role="status"]');
            el.className = 'source-status status-' + st.cls;
            el.querySelector('.status-text').textContent = st.text;
            var listening = st.cls === 'ok' || st.cls === 'warn';
            card.classList.toggle('is-listening', listening && CT.state.status.running);
            if (!CT.state.status.running && !(CT.state.testing && CT.state.testing.domId === src.domId)) {
                setMeter(card, null);
            }
        });
    };

    // ---- Interactions ------------------------------------------------------------
    function refreshAfter(promise, okMessage) {
        return promise.then(function (d) {
            if (okMessage) CT.success(okMessage);
            return CT.reloadSettings().then(function () { CT.render(); return d; });
        });
    }

    function bindCard(card, src) {
        var menuBtn = card.querySelector('[data-action="menu"]');
        var menu = card.querySelector('.menu-list');
        function closeMenu() { menu.hidden = true; menuBtn.setAttribute('aria-expanded', 'false'); }
        menuBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            var open = menu.hidden;
            CT.$$('.menu-list').forEach(function (m) { m.hidden = true; });
            menu.hidden = !open;
            menuBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
            if (open) menu.querySelector('button').focus();
        });
        menu.addEventListener('keydown', function (e) {
            var items = CT.$$('[role="menuitem"]', menu);
            var i = items.indexOf(document.activeElement);
            if (e.key === 'Escape') { e.preventDefault(); closeMenu(); menuBtn.focus(); }
            else if (e.key === 'ArrowDown') { e.preventDefault(); items[(i + 1) % items.length].focus(); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
            else if (e.key === 'Home') { e.preventDefault(); items[0].focus(); }
            else if (e.key === 'End') { e.preventDefault(); items[items.length - 1].focus(); }
            else if (e.key === 'Tab') { closeMenu(); }
        });

        card.addEventListener('click', function (e) {
            var btn = e.target.closest('[data-action]');
            if (!btn || !card.contains(btn)) return;
            var action = btn.getAttribute('data-action');
            if (action === 'test') { closeMenu(); toggleTest(src, card); }
            else if (action === 'open-settings') {
                closeMenu();
                var det = card.querySelector('details');
                det.open = true;
                det.querySelector('summary').focus();
            } else if (action === 'delete') { closeMenu(); deleteSource(src); }
            else if (action === 'test-webhook') { testWebhook(card, src); }
            else if (action === 'add-group' && CT.addGroup) { CT.addGroup(src); }
        });

        var toggle = card.querySelector('[data-action="toggle"]');
        toggle.addEventListener('change', function () {
            var value = toggle.checked;
            toggle.disabled = true;
            refreshAfter(CT.patchSource(src, {enabled: value}))
                .catch(function (err) { toggle.checked = !value; CT.error('Activation impossible : ' + err.message); })
                .finally(function () { toggle.disabled = false; });
        });

        // Seuil de chaque groupe, directement sur le score en direct
        CT.$$('.group-live', card).forEach(function (row) {
            var slider = row.querySelector('.threshold');
            var prev = slider.value;
            slider.addEventListener('input', function () {
                row.querySelector('.threshold-value').textContent = CT.fmt(slider.value);
                row.querySelector('.scorebar-mark').style.left = (slider.value * 100) + '%';
                var live = (CT.state.live[src.sourceId] || {scores: {}}).scores[row.getAttribute('data-slug')] || [];
                drawSpark(row.querySelector('canvas'), live, parseFloat(slider.value), Date.now() / 1000);
            });
            var saveTimer = null;
            slider.addEventListener('change', function () {
                // Enregistrement apres une courte pause : au clavier, chaque
                // fleche declenchait une requete.
                clearTimeout(saveTimer);
                saveTimer = setTimeout(save, 600);
            });
            function save() {
                var slug = row.getAttribute('data-slug');
                CT.api('PUT', '/api/source/sound_groups', {kind: src.kind, source_key: src.apiKey, group_slug: slug,
                                                           threshold: parseFloat(slider.value)})
                    .then(function () {
                        prev = slider.value;
                        var g = groupsOf(src).filter(function (x) { return x.slug === slug; })[0];
                        if (g) g.threshold = parseFloat(slider.value);
                    })
                    .catch(function (err) {
                        slider.value = prev;
                        slider.dispatchEvent(new Event('input'));
                        CT.error('Seuil non enregistré : ' + err.message);
                    });
            }
        });

        // Reglages de la source : enregistres a la validation du champ
        CT.$$('[data-field]', card).forEach(function (input) {
            var name = input.getAttribute('data-field');
            var out = card.querySelector('[data-out="' + name + '"]');
            if (input.type === 'range' && out) {
                input.addEventListener('input', function () {
                    out.textContent = name === 'volume' ? input.value + ' %' : input.value + '×';
                });
            }
            input.dataset.prev = input.type === 'checkbox' ? String(input.checked) : input.value;
            input.addEventListener('change', function () {
                var body = {};
                if (name === 'device') {
                    var dev = selectedDevice(input, src);
                    if (!dev) return;
                    body.device = dev;
                } else if (input.type === 'checkbox') {
                    body[name] = input.checked;
                } else if (input.type === 'range') {
                    body[name] = parseInt(input.value, 10);
                } else {
                    body[name] = input.value.trim();
                }
                CT.patchSource(src, body).then(function (d) {
                    input.dataset.prev = input.type === 'checkbox' ? String(input.checked) : input.value;
                    if (name === 'auto_volume') {
                        var vol = card.querySelector('[data-field="volume"]');
                        if (vol) vol.disabled = input.checked;
                    }
                    if (name === 'gain' && src.kind === 'rtsp' && CT.state.testing && CT.state.testing.domId === src.domId) {
                        return; // le test lit le gain en direct
                    }
                    if (['name', 'url', 'device'].indexOf(name) !== -1) {
                        return CT.reloadSettings().then(function () { CT.render(); CT.success('Enregistré'); });
                    }
                    Object.assign(src.data, d.source || {});
                }).catch(function (err) {
                    if (input.type === 'checkbox') input.checked = input.dataset.prev === 'true';
                    else input.value = input.dataset.prev;
                    if (out) input.dispatchEvent(new Event('input'));
                    CT.error('Non enregistré : ' + err.message);
                });
            });
        });

        card.querySelector('details').addEventListener('toggle', function (e) {
            CT.state.openPanels[card.id] = e.target.open;
            if (e.target.open && src.kind === 'mic') {
                // Un micro branche apres l'ouverture de la page apparait.
                var before = JSON.stringify(CT.state.devices);
                CT.reloadDevices().then(function () {
                    if (JSON.stringify(CT.state.devices) !== before && !CT.isEditing()) CT.render();
                });
            }
        });
    }

    function selectedDevice(select, src) {
        if (select.value === 'default') return {index: 0, name: 'default', pulse_name: ''};
        if (select.value === 'current') {
            return {index: src.data.device_index || 0, name: src.data.audio_source, pulse_name: src.data.pulse_name || ''};
        }
        var dev = (CT.state.devices || [])[parseInt(select.value.replace('dev:', ''), 10)];
        return dev ? {index: dev.index || 0, name: dev.name, pulse_name: dev.pulse_name || ''} : null;
    }

    function deleteSource(src) {
        CT.confirm('Supprimer « ' + src.name + ' » ? Ses entités Home Assistant seront retirées.').then(function (ok) {
            if (!ok) return;
            var req = src.kind === 'mic' ? CT.api('DELETE', '/api/microphone')
                : src.kind === 'rtsp' ? CT.api('DELETE', '/api/rtsp/stream/' + encodeURIComponent(src.key))
                : CT.api('DELETE', '/api/vban/remove', {ip: src.data.ip, name: src.data.name,
                                                         stream_name: src.data.stream_name || src.data.name});
            if (CT.state.testing && CT.state.testing.domId === src.domId) CT.stopTest();
            refreshAfter(req, 'Source supprimée').catch(function (err) { CT.error('Suppression impossible : ' + err.message); });
        });
    }

    function testWebhook(card, src) {
        var input = card.querySelector('[data-field="webhook_url"]');
        var url = input.value.trim();
        if (!url) { CT.error("Saisissez d'abord l'adresse du webhook."); input.focus(); return; }
        CT.api('POST', '/api/webhook/test', {url: url, source: src.sourceId})
            .then(function () { CT.success('Webhook reçu par ' + url); })
            .catch(function (err) { CT.error('Test du webhook échoué : ' + err.message); });
    }

    // ---- Tests et VU-metre -----------------------------------------------------
    function toggleTest(src, card) {
        if (CT.state.testing && CT.state.testing.domId === src.domId) { CT.stopTest(); return; }
        var url = card.querySelector('[data-field="url"]');
        var gain = card.querySelector('[data-field="gain"]');
        CT.startTest(CT.testFor(src, url && url.value, gain && parseInt(gain.value, 10)))
            .catch(function (err) { CT.error('Test impossible : ' + err.message); });
    }
    CT.onTestChange = function (test, on) {
        CT.$$('.source-card').forEach(function (card) {
            var mine = on && card.id === test.domId;
            card.classList.toggle('is-testing', mine);
            CT.$$('[data-action="test"]', card).forEach(function (b) {
                b.textContent = mine ? 'Arrêter le test' : 'Écouter cette source';
            });
            if (!mine && card.id === test.domId) setMeter(card, null);
        });
    };
    CT.onTestLevel = function (test, data) {
        var card = document.getElementById(test.domId);
        if (card) setMeter(card, data.db, 'Test');
    };
    function setMeter(card, db, prefix) {
        var fill = card.querySelector('.meter-fill');
        var label = card.querySelector('[data-role="meter-label"]');
        if (!fill) return;
        if (db == null) { CT.setMeterFill(fill, 0); label.textContent = '—'; return; }
        CT.setMeterFill(fill, CT.dbToPct(db));
        label.textContent = (prefix ? prefix + ' · ' : '') + Math.round(db) + ' dB';
    }

    // Un seul ecouteur global ferme les menus ouverts (pas un par rendu de carte).
    document.addEventListener('click', function (e) {
        CT.$$('.menu-list').forEach(function (m) {
            if (!m.hidden && !m.parentNode.contains(e.target)) {
                m.hidden = true;
                var btn = m.parentNode.querySelector('[data-action="menu"]');
                if (btn) btn.setAttribute('aria-expanded', 'false');
            }
        });
    });

    // ---- Temps reel pendant la detection ----------------------------------------
    function cardFor(sourceId) {
        var src = CT.findSource(function (s) { return s.sourceId === sourceId; });
        return src ? {src: src, card: document.getElementById(src.domId)} : null;
    }

    CT.on('source_level', function (d) {
        var c = d && cardFor(d.source_id);
        if (!c || !c.card) return;
        if (CT.state.testing && CT.state.testing.domId === c.src.domId) return;
        setMeter(c.card, d.db);
    });

    CT.on('group_scores', function (d) {
        var c = d && cardFor(d.source_id);
        if (!c || !c.card) return;
        var live = CT.state.live[d.source_id] = CT.state.live[d.source_id] || {scores: {}};
        var now = Date.now() / 1000;
        Object.keys(d.scores || {}).forEach(function (slug) {
            var score = d.scores[slug];
            var series = live.scores[slug] = (live.scores[slug] || []).filter(function (p) { return now - p[0] <= SPARK_SECONDS; });
            series.push([now, score]);
            var row = c.card.querySelector('.group-live[data-slug="' + CT.cssEscape(slug) + '"]');
            if (!row) return;
            var threshold = parseFloat(row.querySelector('.threshold').value);
            setScore(row, score, threshold);
            drawSpark(row.querySelector('canvas'), series, threshold, now);
        });
    });

    function setScore(row, score, threshold) {
        row.querySelector('.group-live-score').textContent = CT.fmt(score);
        var fill = row.querySelector('.scorebar-fill');
        fill.style.width = Math.min(100, score * 100) + '%';
        fill.classList.toggle('is-over', score >= threshold);
    }

    // Detection arretee : les dernieres valeurs ne sont plus d'actualite.
    CT.resetLive = function () {
        CT.state.live = {};
        CT.$$('.source-card').forEach(function (card) {
            CT.$$('.group-live', card).forEach(function (row) {
                setScore(row, 0, 1);
                drawSpark(row.querySelector('canvas'), [], parseFloat(row.querySelector('.threshold').value), Date.now() / 1000);
            });
            var line = card.querySelector('[data-role="live"]');
            if (line) line.textContent = card.classList.contains('is-disabled') ? 'Source désactivée' : 'En attente de sons…';
        });
    };

    function drawSpark(canvas, series, threshold, now) {
        var ctx = canvas && canvas.getContext && canvas.getContext('2d');
        if (!ctx) return;
        var w = canvas.width, h = canvas.height, css = getComputedStyle(canvas);
        var accent = css.getPropertyValue('--accent').trim() || '#0d7c6b';
        var line = css.getPropertyValue('--ink-3').trim() || '#888';
        ctx.clearRect(0, 0, w, h);
        var ty = h - threshold * (h - 4) - 2;
        ctx.strokeStyle = line;
        ctx.setLineDash([4, 4]);
        ctx.beginPath(); ctx.moveTo(0, ty); ctx.lineTo(w, ty); ctx.stroke();
        ctx.setLineDash([]);
        if (!series.length) return;
        var x = function (t) { return w - ((now - t) / SPARK_SECONDS) * w; };
        var y = function (s) { return h - s * (h - 4) - 2; };
        ctx.beginPath();
        series.forEach(function (p, i) { i ? ctx.lineTo(x(p[0]), y(p[1])) : ctx.moveTo(x(p[0]), y(p[1])); });
        ctx.strokeStyle = accent;
        ctx.lineWidth = 1.5;
        ctx.stroke();
        ctx.lineTo(x(series[series.length - 1][0]), h);
        ctx.lineTo(x(series[0][0]), h);
        ctx.closePath();
        ctx.globalAlpha = 0.15;
        ctx.fillStyle = accent;
        ctx.fill();
        ctx.globalAlpha = 1;
    }

    CT.on('labels', function (d) {
        var c = d && cardFor(d.source);
        if (!c || !c.card || !Array.isArray(d.detected)) return;
        var line = c.card.querySelector('[data-role="live"]');
        if (line.dataset.clapUntil && Date.now() < +line.dataset.clapUntil) return;
        line.textContent = d.detected.slice(0, 3).map(function (l) { return l.label + ' ' + CT.pct(l.score); }).join(' · ');
    });

    CT.on('clap', function (d) {
        var c = d && cardFor(d.source_id);
        if (!c || !c.card) return;
        var n = d.clap_count || 1;
        var text = n + ' clap' + (n > 1 ? 's' : '');
        if (!d.ignored) {
            var line = c.card.querySelector('[data-role="live"]');
            line.textContent = '👏 ' + text + ' · ' + (d.group_name || d.group_slug) + ' (' + CT.pct(d.score) + ')';
            line.dataset.clapUntil = String(Date.now() + 2500);
            CT.announce(text + ' détecté' + (n > 1 ? 's' : '') + ' sur ' + c.src.name);
            c.card.classList.remove('flash'); void c.card.offsetWidth; c.card.classList.add('flash');
        }
        var row = c.card.querySelector('.group-live[data-slug="' + CT.cssEscape(d.group_slug) + '"]');
        if (row && !d.ignored) {
            var badge = row.querySelector('.clap-badge');
            badge.textContent = text;
            badge.hidden = false;
            clearTimeout(badge._t);
            badge._t = setTimeout(function () { badge.hidden = true; }, 2500);
        }
    });

    CT.on('rtsp_status', function (d) {
        if (!d || !d.id) return;
        CT.state.rtspStatus[d.id] = d.status;
        CT.renderSourceStatuses();
    });

    CT.on('auto_volume_update', function (d) {
        var mic = CT.state.settings.microphone;
        if (!mic || !d) return;
        mic.volume = d.volume;
        var slider = document.getElementById('src-mic-volume');
        var card = document.getElementById('src-mic');
        if (slider) slider.value = d.volume;
        var out = card && card.querySelector('[data-out="volume"]');
        if (out) out.textContent = d.volume + ' %';
    });
})();
