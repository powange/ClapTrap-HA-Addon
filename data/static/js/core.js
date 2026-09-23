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
        rtspStatus: {},     // id -> connecting|connected|reconnecting|error
        live: {},           // sourceId -> {scores: {slug: [[t, score]...]}}
        testing: null,      // {key, stopUrl, domId}
        openPanels: {},     // ids des <details> ouverts (conserves entre rendus)
        entityIds: {}       // entity_id calcules par le serveur ("mic", "rtsp:<id>", "vban:<id>")
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
    CT.dbToPct = function (db) { return Math.max(0, Math.min(100, ((db + 60) / 60) * 100)); };
    CT.setMeterFill = function (fill, pct) {
        fill.style.clipPath = 'inset(0 ' + (100 - pct) + '% 0 0)';
        fill.dataset.pct = String(Math.round(pct));
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
        el.textContent = message;
        box.appendChild(el);
        setTimeout(function () { el.classList.add('toast-out'); }, kind === 'error' ? 6000 : 2500);
        setTimeout(function () { el.remove(); }, kind === 'error' ? 6400 : 2900);
    };
    CT.error = function (message) { CT.toast(message, 'error'); };
    CT.success = function (message) { CT.toast(message, 'success'); };

    // ---- Dialogues dans la page (focus piege, Echap) --------------------------
    CT.trapFocus = function (container, onClose) {
        var previous = document.activeElement;
        function focusables() {
            return CT.$$('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])', container)
                .filter(function (el) { return !el.disabled && el.offsetParent !== null; });
        }
        function onKey(e) {
            if (e.key === 'Escape') { e.preventDefault(); onClose(); return; }
            if (e.key !== 'Tab') return;
            var f = focusables();
            if (!f.length) return;
            var first = f[0], last = f[f.length - 1];
            if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
            else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
        }
        container.addEventListener('keydown', onKey);
        return function release() {
            container.removeEventListener('keydown', onKey);
            if (previous && previous.focus) previous.focus();
        };
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
    // key  : cle de l'API unifiee (/api/sources/<kind>/<key>)
    // apiKey : cle des routes de groupes (device_index | id)
    // sourceId : identifiant emis par le serveur pendant la detection
    CT.sourceList = function () {
        var s = CT.state.settings || {};
        var out = [];
        var mic = s.microphone;
        if (mic && mic.configured !== false) {
            out.push({kind: 'mic', key: 'mic', apiKey: String(mic.device_index || 0),
                      sourceId: 'mic', domId: 'src-mic',
                      name: mic.audio_source && mic.audio_source !== 'default' ? mic.audio_source : 'Micro par défaut',
                      enabled: !!mic.enabled, data: mic});
        }
        (s.rtsp_sources || []).forEach(function (r) {
            out.push({kind: 'rtsp', key: r.id, apiKey: r.id, sourceId: 'rtsp_' + r.id,
                      domId: 'src-rtsp-' + CT.slug(r.id), name: r.name || 'Caméra', enabled: !!r.enabled, data: r});
        });
        (s.saved_vban_sources || []).forEach(function (v) {
            // Id propre a chaque flux (deux flux d'une meme IP etaient confondus).
            var vid = v.id || v.ip;
            out.push({kind: 'vban', key: vid, apiKey: vid, sourceId: 'vban_' + vid,
                      domId: 'src-vban-' + CT.slug(vid), name: v.name || v.ip,
                      enabled: !!v.enabled, data: v});
        });
        return out;
    };
    CT.findSource = function (predicate) {
        return CT.sourceList().filter(predicate)[0] || null;
    };
    CT.kindLabel = {mic: 'Micro', rtsp: 'Caméra', vban: 'VBAN'};

    CT.patchSource = function (src, body) {
        return CT.api('PATCH', '/api/sources/' + src.kind + '/' + src.key.split('/').map(encodeURIComponent).join('/'), body);
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
    CT.reloadStatus = function () {
        return fetch(basePath + '/status').then(function (r) { return r.json(); }).then(function (st) {
            CT.state.status = st;
            return st;
        });
    };
    // Ne pas reconstruire la page pendant une saisie : on perdrait le texte.
    CT.isEditing = function () {
        var a = document.activeElement;
        return !!(a && /INPUT|SELECT|TEXTAREA/.test(a.tagName) && a.type !== 'range' && a.type !== 'checkbox' &&
                  !a.closest('.modal'));
    };
    CT.render = function () {
        if (CT.renderStatus) CT.renderStatus();
        if (CT.renderSources) CT.renderSources();
    };
    CT.resync = function () {
        return Promise.all([CT.reloadSettings(), CT.reloadStatus()]).then(function () {
            if (!CT.isEditing()) CT.render(); else if (CT.renderStatus) CT.renderStatus();
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
        return {key: 'vban_' + src.data.ip, domId: src.domId, startUrl: '/api/vban/test/start', stopUrl: '/api/vban/test/stop',
                body: {ip: src.data.ip, id: src.data.id || null}};
    };
    function onTestLevel(key, data) {
        var t = CT.state.testing;
        if (!t || t.key !== key) return;
        if (t.token && data.token && data.token !== t.token) return;  // ancien test
        if (data.error) { CT.stopTest('Test interrompu : ' + data.error); return; }
        if (CT.onTestLevel) CT.onTestLevel(t, data);
    }
    CT.on('mic_level', function (d) { onTestLevel('mic', d || {}); });
    CT.on('rtsp_level', function (d) { d = d || {}; onTestLevel('rtsp_' + (d.id || ''), d); });
    CT.on('vban_level', function (d) { d = d || {}; onTestLevel('vban_' + (d.ip || ''), d); });
})();
