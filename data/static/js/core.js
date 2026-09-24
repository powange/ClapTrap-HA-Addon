/* ClapTrap : socle commun (etat, API, messages, dialogues, temps reel).
 * Scripts classiques (pas de modules ES) charges dans l'ordre par index.html ;
 * tout est expose sous window.CT. */
(function () {
    'use strict';
    var CT = window.CT = window.CT || {};
    var basePath = window.basePath || '';

    // ---- Etat -------------------------------------------------------------
    CT.state = {
        settings: window.initialSettings || {},
        devices: window.initialDevices || [],
        status: {running: false, source: null, since: null, sources: []},
        sourceStatus: {},   // sourceId -> connecting|connected|reconnecting|error
        live: {},           // sourceId -> {scores: {slug: [[t, score]...]}}
        testing: null,      // {key, stopUrl, domId}
        openPanels: {},     // ids des <details> ouverts (conserves entre rendus)
        entityIds: {},      // entity_id calcules par le serveur ("mic", "rtsp:<id>", "vban:<id>")
        search: {}          // recherche en cours par groupe (conservee entre rendus)
    };

    // ---- Outils --------------------------------------------------------------
    CT.esc = function (s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
            return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
        });
    };
    CT.slug = function (s) {
        return String(s || '').toLowerCase().replace(/[^a-z0-9]/g, '_').replace(/_+/g, '_').replace(/^_|_$/g, '');
    };
    CT.fmt = function (n, digits) {
        return Number(n || 0).toFixed(digits == null ? 2 : digits).replace('.', ',');
    };
    CT.pct = function (n) { return Math.round((n || 0) * 100) + ' %'; };
    // Nom francais d'un son YAMNet (sounds_fr.js), le nom anglais a defaut.
    CT.soundLabel = function (label) { return (CT.SOUNDS_FR || {})[label] || label; };
    CT.dbToPct = function (db) { return Math.max(0, Math.min(100, ((db + 60) / 60) * 100)); };
    CT.setMeterFill = function (fill, pct) {
        fill.style.clipPath = 'inset(0 ' + (100 - pct) + '% 0 0)';
        fill.dataset.pct = String(Math.round(pct));
    };
    CT.cssEscape = function (s) {
        return (window.CSS && CSS.escape) ? CSS.escape(String(s)) : String(s).replace(/["\\\]\[]/g, '\\$&');
    };
    CT.$ = function (sel, root) { return (root || document).querySelector(sel); };
    CT.$$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

    // ---- API -------------------------------------------------------------------
    function checkJson(r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
            if (!r.ok || (d && d.success === false)) throw new Error((d && d.error) || ('Erreur HTTP ' + r.status));
            return d;
        });
    }
    CT.api = function (method, url, body) {
        var opts = {method: method, headers: {'Content-Type': 'application/json'}};
        if (body !== undefined) opts.body = JSON.stringify(body);
        return fetch(basePath + url, opts).then(checkJson);
    };
    CT.apiForm = function (url, formData) {
        return fetch(basePath + url, {method: 'POST', body: formData}).then(checkJson);
    };
    CT.basePath = basePath;

    // ---- Messages (aria-live) ------------------------------------------------
    CT.toast = function (message, kind) {
        var box = document.getElementById('toasts');
        if (!box) return;
        var el = document.createElement('div');
        el.className = 'toast toast-' + (kind || 'info');
        el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
        var text = document.createElement('span');
        text.textContent = message;
        el.appendChild(text);
        var remove = function () { el.classList.add('toast-out'); setTimeout(function () { el.remove(); }, 300); };
        if (kind === 'error') {
            // Les erreurs restent jusqu'a ce qu'on les ferme (ou 15 s).
            var close = document.createElement('button');
            close.type = 'button';
            close.className = 'toast-close';
            close.setAttribute('aria-label', 'Fermer ce message');
            close.textContent = '×';
            close.addEventListener('click', remove);
            el.appendChild(close);
            setTimeout(remove, 15000);
        } else {
            setTimeout(remove, 2500);
        }
        box.appendChild(el);
    };
    // Annonces pour lecteurs d'ecran : seulement les evenements importants
    // (claps), au plus une toutes les 2 s.
    var lastAnnounce = 0;
    // `important` (demarrage / arret) : jamais filtre, et ne bloque pas
    // l'annonce d'un clap qui suit.
    CT.announce = function (text, important) {
        var now = Date.now();
        var box = document.getElementById('announcer');
        if (!box || (!important && now - lastAnnounce < 2000)) return;
        if (!important) lastAnnounce = now;
        box.textContent = text;
    };
    CT.error = function (message) { CT.toast(message, 'error'); };
    CT.success = function (message) { CT.toast(message, 'success'); };

    // ---- Dialogues dans la page (focus piege, Echap) --------------------------
    CT.focusables = function (container) {
        return CT.$$('button, [href], input, select, textarea, summary, [tabindex]:not([tabindex="-1"])', container)
            .filter(function (el) { return !el.disabled && !el.closest('[hidden]'); });
    };
    // Piege de focus sur le document entier (et non sur la fenetre) : quand
    // le bouton qui avait le focus disparait, Echap et Tab restent geres. Le
    // reste de la page est rendu inerte.
    CT.trapFocus = function (container, onClose, returnFocus) {
        var previous = document.activeElement;
        var outside = CT.$$('body > header, body > main, body > .skip-link');
        outside.forEach(function (el) { el.inert = true; el.setAttribute('aria-hidden', 'true'); });
        function onKey(e) {
            if (e.key === 'Escape') { e.preventDefault(); onClose(); return; }
            if (e.key !== 'Tab') return;
            var f = CT.focusables(container);
            if (!f.length) { e.preventDefault(); return; }
            var first = f[0], last = f[f.length - 1];
            if (!container.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
            else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
            else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
        }
        document.addEventListener('keydown', onKey, true);
        return function release() {
            document.removeEventListener('keydown', onKey, true);
            outside.forEach(function (el) { el.inert = false; el.removeAttribute('aria-hidden'); });
            var target = typeof returnFocus === 'function' ? returnFocus() : previous;
            if (target && target.isConnected && target.focus) target.focus();
        };
    };
    CT.focusFirst = function (container) {
        var f = CT.focusables(container);
        if (f.length) f[0].focus();
    };
    CT.dialog = function (message, opts) {
        opts = opts || {};
        return new Promise(function (resolve) {
            var back = document.createElement('div');
            back.className = 'modal-backdrop';
            back.innerHTML =
                '<div class="modal modal-small" role="dialog" aria-modal="true" aria-labelledby="dlg-msg">' +
                '<p id="dlg-msg" class="modal-message"></p>' +
                (opts.prompt ? '<input type="text" class="input" id="dlg-input" aria-labelledby="dlg-msg">' : '') +
                '<div class="modal-actions">' +
                '<button type="button" class="btn btn-ghost" data-r="cancel">Annuler</button>' +
                '<button type="button" class="btn ' + (opts.danger ? 'btn-danger' : 'btn-primary') + '" data-r="ok"></button>' +
                '</div></div>';
            back.querySelector('.modal-message').textContent = message;
            back.querySelector('[data-r="ok"]').textContent = opts.okLabel || 'OK';
            var input = back.querySelector('#dlg-input');
            if (input) input.value = opts.value || '';
            document.body.appendChild(back);
            var modal = back.querySelector('.modal');
            var release = CT.trapFocus(modal, function () { close(false); });
            function close(ok) {
                release();
                back.remove();
                resolve(opts.prompt ? (ok ? input.value : null) : ok);
            }
            back.addEventListener('click', function (e) {
                if (e.target === back) close(false);
                var r = e.target.getAttribute && e.target.getAttribute('data-r');
                if (r) close(r === 'ok');
            });
            if (input) input.addEventListener('keydown', function (e) { if (e.key === 'Enter') close(true); });
            (input || back.querySelector('[data-r="ok"]')).focus();
        });
    };
    CT.confirm = function (message, okLabel) {
        return CT.dialog(message, {danger: true, okLabel: okLabel || 'Supprimer'});
    };
    CT.prompt = function (message, value, okLabel) {
        return CT.dialog(message, {prompt: true, value: value, okLabel: okLabel || 'Valider'});
    };

    CT.copy = function (text) {
        var done = function () { CT.success('Copié : ' + text); };
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(done).catch(fallback);
        } else {
            fallback();
        }
        function fallback() {
            var ta = document.createElement('textarea');
            ta.value = text;
            document.body.appendChild(ta);
            ta.select();
            try { document.execCommand('copy'); done(); } catch (e) { CT.error('Copie impossible, sélectionnez le texte.'); }
            ta.remove();
        }
    };

    // ---- Sources (modele commun a toutes les vues) ------------------------------
    // kind : mic | rtsp | vban
    // key  : cle de l'API (/api/sources/<kind>/<key>, routes de groupes) : "mic" ou id
    // sourceId : identifiant emis par le serveur pendant la detection
    CT.sourceList = function () {
        var s = CT.state.settings || {};
        var out = [];
        var mic = s.microphone;
        if (mic && mic.configured !== false) {
            out.push({kind: 'mic', key: 'mic',
                      sourceId: 'mic', domId: 'src-mic',
                      name: mic.audio_source && mic.audio_source !== 'default' ? mic.audio_source : 'Micro par défaut',
                      enabled: !!mic.enabled, data: mic});
        }
        (s.rtsp_sources || []).forEach(function (r) {
            out.push({kind: 'rtsp', key: r.id, sourceId: 'rtsp_' + r.id,
                      domId: 'src-rtsp-' + CT.slug(r.id), name: r.name || 'Caméra', enabled: !!r.enabled, data: r});
        });
        (s.saved_vban_sources || []).forEach(function (v) {
            // Id propre a chaque flux (deux flux d'une meme IP etaient confondus).
            out.push({kind: 'vban', key: v.id, sourceId: 'vban_' + v.id,
                      domId: 'src-vban-' + CT.slug(v.id), name: v.name || v.ip,
                      enabled: !!v.enabled, data: v});
        });
        return out;
    };
    CT.findSource = function (predicate) {
        return CT.sourceList().filter(predicate)[0] || null;
    };
    CT.kindLabel = {mic: 'Micro', rtsp: 'Caméra', vban: 'VBAN'};

    CT.patchSource = function (src, body) {
        return CT.api('PATCH', '/api/sources/' + src.kind + '/' + encodeURIComponent(src.key), body);
    };

    // ---- Theme : celui de Home Assistant ------------------------------------------
    // Applique des le <head> (script integre a index.html, sans flash au
    // chargement) puis resynchronise ici toutes les 5 s.
    CT.syncTheme = window.ctSyncTheme || function () {};
    setInterval(CT.syncTheme, 5000);

    // ---- Redemarrage de la detection -----------------------------------------------
    // Activer, desactiver, ajouter ou supprimer une source (ou changer de micro)
    // peut redemarrer toute la detection. « Redemarrage… » s'affiche pendant la
    // requete si c'est possible ; le message final vient du serveur (champ
    // `restart` : ok | stopped | failed) et non d'une supposition.
    CT.withRestart = function (promise, mayRestart) {
        var show = CT.state.status.running && mayRestart !== false;
        if (show) {
            CT.state.restarting = true;
            if (CT.renderStatus) CT.renderStatus();
        }
        return promise.then(function (d) {
            var r = d && d.restart;
            if (r === 'ok') CT.success('Détection redémarrée avec la nouvelle configuration');
            else if (r === 'failed') CT.error('Détection arrêtée : le redémarrage a échoué (voir le journal)');
            else if (r === 'stopped') CT.toast('Détection arrêtée : plus aucune source active');
            return d;
        }).finally(function () {
            if (!show) return;
            CT.state.restarting = false;
            CT.reloadStatus().then(function () { if (CT.renderStatus) CT.renderStatus(); }).catch(function () {});
        });
    };
    // Enregistrement reussi : coche discrete a cote du champ (plutot qu'un
    // message par reglage), annoncee aux lecteurs d'ecran.
    CT.markSaved = function (el, text) {
        if (!el) return;
        var field = el.closest('.field') || el.parentNode;
        var mark = field.querySelector('.saved-mark');
        if (!mark) {
            mark = document.createElement('span');
            mark.className = 'saved-mark';
            mark.setAttribute('aria-hidden', 'true');
            field.appendChild(mark);
        }
        mark.textContent = '✓ ' + (text || 'Enregistré');
        mark.classList.add('is-on');
        clearTimeout(mark._t);
        mark._t = setTimeout(function () { mark.classList.remove('is-on'); }, 2000);
        if (CT.announce) CT.announce(text || 'Enregistré');
    };

    // ---- Resynchronisation ------------------------------------------------------
    CT.reloadEntityIds = function () {
        return CT.api('GET', '/api/ha/entity-ids').then(function (ids) { CT.state.entityIds = ids || {}; })
            .catch(function () {});
    };
    CT.reloadSettings = function () {
        return Promise.all([CT.api('GET', '/api/settings'), CT.reloadEntityIds()])
            .then(function (r) { CT.state.settings = r[0]; return r[0]; });
    };
    // Recharger les reglages puis reconstruire la page (une seule facon de le
    // faire, il y en avait trois).
    CT.refresh = function () {
        return CT.reloadSettings().then(function () { CT.renderWhenIdle(); });
    };
    // Liste des micros : options du <select> et corps de requete, communs a la
    // carte du micro et a l'assistant (ecrits trois fois auparavant).
    CT.deviceOptions = function (current) {
        var devices = CT.state.devices || [];
        current = current || 'default';
        var html = '<option value="default"' + (current === 'default' ? ' selected' : '') + '>Micro par défaut du système</option>';
        if (current !== 'default' && !devices.some(function (d) { return d.name === current; })) {
            html += '<option value="current" selected>' + CT.esc(current) + ' (non détecté)</option>';
        }
        devices.forEach(function (d, i) {
            html += '<option value="dev:' + i + '"' + (d.name === current ? ' selected' : '') + '>' + CT.esc(d.name) + '</option>';
        });
        return html;
    };
    CT.deviceFromOption = function (value, mic) {
        if (value === 'default') return {index: 0, name: 'default', pulse_name: ''};
        if (value === 'current' && mic) return {index: mic.device_index || 0, name: mic.audio_source, pulse_name: mic.pulse_name || ''};
        var dev = (CT.state.devices || [])[parseInt(String(value).replace('dev:', ''), 10)];
        return dev ? {index: dev.index || 0, name: dev.name, pulse_name: dev.pulse_name || ''} : null;
    };
    CT.reloadDevices = function () {
        return CT.api('GET', '/api/audio-sources').then(function (d) {
            if (Array.isArray(d)) CT.state.devices = d;
        }).catch(function () {});
    };
    CT.reloadStatus = function () {
        // Via CT.api : une erreur 500 affichait « Arrêté » au lieu d'echouer.
        return CT.api('GET', '/status').then(function (st) {
            CT.state.status = st;
            return st;
        });
    };
    // Ne pas reconstruire la page pendant une saisie : on perdrait le texte.
    CT.isEditing = function () {
        var a = document.activeElement;
        return !!(a && /INPUT|SELECT|TEXTAREA/.test(a.tagName) && a.type !== 'range' && a.type !== 'checkbox' &&
                  a.type !== 'file' && !a.closest('.modal'));
    };
    // Rendu avec restauration du focus : chaque action reconstruisait la
    // grille et renvoyait l'utilisateur clavier en haut de la page.
    CT.focusDescriptor = function () {
        var a = document.activeElement;
        if (!a || a === document.body) return null;
        var card = a.closest && a.closest('.source-card');
        if (!card) return a.id ? {id: a.id} : null;
        var group = a.closest('.group-card[data-slug], .group-live[data-slug]');
        var sel = null;
        ['data-field', 'data-action', 'data-copy', 'data-label', 'data-clap', 'data-group-action'].some(function (attr) {
            if (a.hasAttribute(attr)) { sel = '[' + attr + '="' + CT.cssEscape(a.getAttribute(attr)) + '"]'; return true; }
            return false;
        });
        if (!sel && a.id) return {id: a.id, caret: typeof a.selectionStart === 'number' ? a.selectionStart : null};
        if (!sel) sel = a.className ? '.' + String(a.className).trim().split(/\s+/)[0] : a.tagName.toLowerCase();
        return {card: card.id, sel: sel,
                group: group ? {slug: group.getAttribute('data-slug'),
                                cls: group.classList.contains('group-card') ? 'group-card' : 'group-live'} : null,
                caret: typeof a.selectionStart === 'number' ? a.selectionStart : null};
    };
    CT.restoreFocus = function (d) {
        if (!d) return;
        var el = null;
        if (d.id) el = document.getElementById(d.id);
        else {
            var card = document.getElementById(d.card);
            if (!card) return;
            var scope = d.group ? card.querySelector('.' + d.group.cls + '[data-slug="' + CT.cssEscape(d.group.slug) + '"]') || card : card;
            el = scope.querySelector(d.sel);
        }
        if (!el || el.hidden) return;
        el.focus({preventScroll: true});
        if (d.caret != null && typeof el.setSelectionRange === 'function') {
            try { el.setSelectionRange(d.caret, d.caret); } catch (e) { /* type sans selection */ }
        }
    };
    CT.withFocus = function (fn) {
        var d = CT.focusDescriptor();
        fn();
        CT.restoreFocus(d);
    };
    CT.render = function () {
        CT.withFocus(function () {
            if (CT.renderStatus) CT.renderStatus();
            if (CT.renderSources) CT.renderSources();
        });
    };
    // Rendu complet, differe tant qu'un champ est en cours de saisie : un
    // enregistrement qui se terminait pendant la frappe dans un autre champ
    // reconstruisait la carte et effacait le texte tape.
    var renderPending = false;
    CT.renderWhenIdle = function () {
        if (!CT.isEditing()) { CT.render(); return; }
        if (CT.renderStatus) CT.renderStatus();
        if (renderPending) return;
        renderPending = true;
        document.addEventListener('focusout', function retry() {
            setTimeout(function () {
                if (CT.isEditing()) return;
                document.removeEventListener('focusout', retry);
                renderPending = false;
                CT.render();
            }, 0);
        });
    };
    CT.resync = function () {
        return Promise.all([CT.reloadSettings(), CT.reloadStatus()]).then(function () {
            CT.renderWhenIdle();
        }).catch(function () {});
    };

    // ---- Temps reel : une seule connexion, reconnexion illimitee ---------------
    CT.socket = typeof io === 'function' ? io({path: basePath + '/socket.io'}) : null;
    CT.on = function (event, handler) { if (CT.socket) CT.socket.on(event, handler); };
    (function () {
        var connectedOnce = false;
        CT.on('connect', function () {
            if (connectedOnce) CT.resync();
            connectedOnce = true;
        });
        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'visible') CT.resync();
        });
    })();

    // ---- Tests (VU-metre) : un seul a la fois, comme cote serveur -------------
    // Le serveur remplace lui-meme le test en cours : pas de "stop" a envoyer
    // avant un "start" (l'ancien couple stop/start non attendu se croisait).
    CT.stopTest = function (message) {
        var t = CT.state.testing;
        if (!t) return;
        CT.state.testing = null;
        CT.api('POST', t.stopUrl, {token: t.token || null}).catch(function () {});
        if (CT.onTestChange) CT.onTestChange(t, false);
        if (message) CT.error(message);
    };
    CT.startTest = function (test) {
        var previous = CT.state.testing;
        if (previous && CT.onTestChange) CT.onTestChange(previous, false);
        CT.state.testing = test;  // enregistre AVANT la reponse : annulable
        return CT.api('POST', test.startUrl, test.body || {}).then(function (d) {
            test.token = d.token;
            if (CT.state.testing !== test) {
                // Annule pendant la requete (fenetre fermee, autre test) :
                // arreter ce test cote serveur plutot que de le laisser tourner.
                CT.api('POST', test.stopUrl, {token: d.token}).catch(function () {});
                return;
            }
            if (CT.onTestChange) CT.onTestChange(test, true);
        }, function (err) {
            if (CT.state.testing === test) CT.state.testing = null;
            throw err;
        });
    };
    CT.testFor = function (src, url, gain) {
        if (src.kind === 'mic') {
            var mic = src.data;
            return {key: 'mic', domId: src.domId, startUrl: '/api/mic/test/start', stopUrl: '/api/mic/test/stop',
                    body: {pulse_name: mic.pulse_name || '', audio_source: mic.audio_source || ''}};
        }
        if (src.kind === 'rtsp') {
            return {key: 'rtsp_' + src.key, domId: src.domId, startUrl: '/api/rtsp/test/start', stopUrl: '/api/rtsp/test/stop',
                    body: {id: src.key, url: url || src.data.url, gain: gain || src.data.gain || 10}};
        }
        return {key: 'vban_' + src.key, domId: src.domId, startUrl: '/api/vban/test/start', stopUrl: '/api/vban/test/stop',
                body: {id: src.key}};
    };
    function onTestLevel(key, data) {
        var t = CT.state.testing;
        if (!t || t.key !== key) return;
        if (t.token && data.token && data.token !== t.token) return;  // ancien test
        if (data.error) { CT.stopTest('Test interrompu : ' + data.error); return; }
        // Un test peut avoir son propre affichage (assistant), sinon la carte.
        if (t.onLevel) t.onLevel(data); else if (CT.onTestLevel) CT.onTestLevel(t, data);
    }
    CT.on('mic_level', function (d) { onTestLevel('mic', d || {}); });
    CT.on('rtsp_level', function (d) { d = d || {}; onTestLevel('rtsp_' + (d.id || ''), d); });
    CT.on('vban_level', function (d) { d = d || {}; onTestLevel('vban_' + (d.id || ''), d); });
})();
