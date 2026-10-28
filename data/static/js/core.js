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
        sourceErrors: {},   // sourceId -> cause de la derniere erreur (affichee sur la carte)
        audioFormats: {},   // sourceId -> dernier format audio recu {codec, sample_rate, channels}
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
    CT.pct = function (n) { return Math.round((n || 0) * 100) + ' %'; };
    // Reduction du bruit de fond : nom de chaque niveau (denoise.LEVELS, 0 a 3).
    CT.NR_NAMES = ['Désactivée', 'Légère', 'Moyenne', 'Forte'];
    // Nom francais d'un son YAMNet (sounds_fr.js), le nom anglais a defaut.
    CT.soundLabel = function (label) { return (CT.SOUNDS_FR || {})[label] || label; };
    // Filtre de pastilles par recherche (nom YAMNet ou francais) ; `labelOf`
    // donne le son d'une pastille.
    CT.filterChips = function (chips, query, labelOf) {
        var q = (query || '').trim().toLowerCase();
        chips.forEach(function (chip) {
            var label = labelOf(chip);
            chip.hidden = !!q && (label + ' ' + CT.soundLabel(label)).toLowerCase().indexOf(q) === -1;
        });
    };
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
        // Le conteneur #toasts est deja une region live : un role « status »
        // en plus faisait lire deux fois. Les erreurs restent des alertes.
        if (kind === 'error') el.setAttribute('role', 'alert');
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
        // Sauf l'ancetre de la fenetre (confirmation ouverte dans les reglages
        // d'une source, eux-memes dans <main>).
        var outside = CT.$$('body > header, body > main, body > .skip-link')
            .filter(function (el) { return !el.contains(container); });
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
            // Fenetre native ouverte (reglages d'une source) : le reste de la
            // page est inerte, la confirmation doit s'ouvrir DEDANS.
            (document.querySelector('dialog[open]') || document.body).appendChild(back);
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
            // Action destructive : focus sur « Annuler » (Entree supprimait aussitot).
            (input || back.querySelector(opts.danger ? '[data-r="cancel"]' : '[data-r="ok"]')).focus();
        });
    };
    CT.confirm = function (message, okLabel) {
        return CT.dialog(message, {danger: true, okLabel: okLabel || 'Supprimer'});
    };
    CT.prompt = function (message, value, okLabel) {
        return CT.dialog(message, {prompt: true, value: value, okLabel: okLabel || 'Valider'});
    };

    CT.copy = function (text) {
        // Texte long (configuration) : « Copié » seul, pas tout le texte dans l'annonce.
        var done = function () { CT.success(text.length > 80 ? 'Copié' : 'Copié : ' + text); };
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
    CT.sourceByDomId = function (domId) {
        return CT.findSource(function (s) { return s.domId === domId; });
    };
    CT.findSource = function (predicate) {
        return CT.sourceList().filter(predicate)[0] || null;
    };
    CT.kindLabel = {mic: 'Micro', rtsp: 'Caméra', vban: 'VBAN'};
    // Reference d'une source pour l'API (« mic », « rtsp:<id> », « vban:<id> »).
    CT.sourceRef = function (src) { return src.kind === 'mic' ? 'mic' : src.kind + ':' + src.key; };
    CT.LEARNED_PREFIX = 'Appris : ';   // sons appris (learned.PREFIX)

    CT.patchSource = function (src, body) {
        return CT.api('PATCH', '/api/sources/' + src.kind + '/' + encodeURIComponent(src.key), body);
    };

    // ---- Theme : celui de Home Assistant ------------------------------------------
    // Applique des le <head> (script integre a index.html, sans flash au
    // chargement) puis resynchronise ici toutes les 5 s.
    CT.syncTheme = window.ctSyncTheme || function () {};
    // Page cachee (application mobile en arriere-plan) : rien ; au retour,
    // resynchronisation immediate.
    setInterval(function () { if (!document.hidden) CT.syncTheme(); }, 5000);
    document.addEventListener('visibilitychange', function () { if (!document.hidden) CT.syncTheme(); });

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
            if (r === 'ok') CT.success('Détection mise à jour avec la nouvelle configuration');
            else if (r === 'failed') CT.error('La mise à jour de la détection a échoué (voir le journal)');
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
        // Annonce prioritaire : l'anti-rafale de 2 s bloquait sinon l'annonce
        // d'un clap juste apres l'enregistrement.
        if (CT.announce) CT.announce(text || 'Enregistré', true);
    };

    // ---- Resynchronisation ------------------------------------------------------
    CT.reloadEntityIds = function () {
        return CT.api('GET', '/api/ha/entity-ids').then(function (ids) { CT.state.entityIds = ids || {}; })
            .catch(function () {});
    };
    // Reglages recharges fusionnes DANS les objets existants : une source (id)
    // ou un groupe (slug) garde le meme objet tant qu'il existe. Les
    // gestionnaires qui en gardent une reference (fenetre de reglages,
    // groupes, requete en cours) voient ainsi les nouvelles valeurs ; avant,
    // tout etait remplace et ils modifiaient des objets que l'ecran
    // n'affichait plus.
    function keyOf(item) {
        return item && typeof item === 'object' && !Array.isArray(item) ? (item.id != null ? 'id:' + item.id : item.slug != null ? 'slug:' + item.slug : null) : null;
    }
    function isPlain(v) { return v && typeof v === 'object' && !Array.isArray(v); }
    CT.mergeInto = function (target, fresh) {
        if (Array.isArray(target) && Array.isArray(fresh)) {
            var byKey = {}, used = {};
            target.forEach(function (item) { var k = keyOf(item); if (k && !byKey[k]) byKey[k] = item; });
            var merged = fresh.map(function (item) {
                var k = keyOf(item), old = k && !used[k] ? byKey[k] : null;
                if (k) used[k] = true;   // identifiant en double : un seul objet repris
                return old && isPlain(item) ? CT.mergeInto(old, item) : item;
            });
            target.length = 0;
            Array.prototype.push.apply(target, merged);
            return target;
        }
        if (isPlain(target) && isPlain(fresh)) {
            var merged = {};
            Object.keys(fresh).forEach(function (k) {
                var a = target[k], b = fresh[k];
                merged[k] = (Array.isArray(a) && Array.isArray(b)) || (isPlain(a) && isPlain(b)) ? CT.mergeInto(a, b) : b;
            });
            // Cles dans l'ordre du serveur (comparaisons par JSON inchangees).
            Object.keys(target).forEach(function (k) { delete target[k]; });
            Object.keys(merged).forEach(function (k) { target[k] = merged[k]; });
            return target;
        }
        return fresh;
    };
    CT.reloadSettings = function () {
        return Promise.all([CT.api('GET', '/api/settings'), CT.reloadEntityIds()])
            .then(function (r) {
                var changed = JSON.stringify(CT.state.settings) !== JSON.stringify(r[0]);
                CT.state.settings = isPlain(CT.state.settings) ? CT.mergeInto(CT.state.settings, r[0]) : r[0];
                // Fenetre de reglages ouverte : la grille n'est pas reconstruite
                // (elle attend la fermeture), ses groupes et son titre si.
                if (changed && CT.onSettingsReloaded) CT.onSettingsReloaded();
                return CT.state.settings;
            });
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
        // Valeur = nom PulseAudio du micro (unique ; nom affiche a defaut),
        // pas sa position : la liste peut changer entre l'affichage et le
        // choix (micro branche), et la position designait un autre micro.
        var picked = false;
        devices.forEach(function (d) {
            var sel = !picked && d.name === current;   // deux micros identiques : un seul selectionne
            if (sel) picked = true;
            html += '<option value="dev:' + CT.esc(d.pulse_name || d.name) + '"' + (sel ? ' selected' : '') + '>' + CT.esc(d.name) + '</option>';
        });
        return html;
    };
    CT.deviceFromOption = function (value, mic) {
        if (value === 'default') return {index: 0, name: 'default', pulse_name: ''};
        if (value === 'current' && mic) return {index: mic.device_index || 0, name: mic.audio_source, pulse_name: mic.pulse_name || ''};
        var id = String(value).replace(/^dev:/, '');
        var devices = CT.state.devices || [];
        var dev = devices.filter(function (d) { return d.pulse_name && d.pulse_name === id; })[0] ||
                  devices.filter(function (d) { return d.name === id; })[0];
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
    // Un groupe sans son coche ne se declenche jamais.
    CT.groupHasSounds = function (g) {
        var wl = (g && g.sound_whitelist) || {};
        return Object.keys(wl).some(function (k) { return wl[k]; });
    };
    // Ne pas reconstruire la page pendant une saisie : on perdrait le texte.
    // Saisie en cours dans un champ (hors assistant) : pas de rendu qui
    // l'effacerait.
    CT.isTyping = function () {
        var a = document.activeElement;
        return !!(a && /INPUT|SELECT|TEXTAREA/.test(a.tagName) && a.type !== 'range' && a.type !== 'checkbox' &&
                  a.type !== 'file' && !a.closest('.modal:not(.source-settings)'));
    };
    // Rendu complet de la grille a reporter : saisie en cours, ou reglages
    // d'une source ouverts (fenetre dans la carte : elle serait detruite,
    // avec son defilement et ses confirmations) ; fait a la fermeture.
    CT.isEditing = function () {
        return !!document.querySelector('dialog.source-settings[open]') || CT.isTyping();
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
        // Reperes stables uniquement (data-*, id) : la premiere classe CSS
        // (« .input ») designait parfois un autre champ du groupe.
        ['data-role', 'data-field', 'data-action', 'data-copy', 'data-label', 'data-clap', 'data-group-action'].some(function (attr) {
            if (a.hasAttribute(attr)) { sel = '[' + attr + '="' + CT.cssEscape(a.getAttribute(attr)) + '"]'; return true; }
            return false;
        });
        if (!sel && a.id) return {id: a.id, caret: typeof a.selectionStart === 'number' ? a.selectionStart : null};
        if (!sel) return null;
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
            var scope = d.group ? card.querySelector('.' + d.group.cls + '[data-slug="' + CT.cssEscape(d.group.slug) + '"]') : card;
            // Groupe disparu (supprime) : focus sur « Nouveau groupe », et non
            // sur le meme bouton d'un AUTRE groupe (Entree le supprimait).
            el = scope ? scope.querySelector(d.sel) : card.querySelector('[data-action="add-group"]');
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
    // Appui en cours (doigt ou bouton pose). Remis a faux au relachement ;
    // un relachement hors de la page (cadre de HA) n'est jamais recu : apres
    // PRESS_MAX ms, l'appui est considere comme termine.
    var pointerDown = false, pointerDownAt = 0, PRESS_MAX = 3000;
    document.addEventListener('pointerdown', function () { pointerDown = true; pointerDownAt = Date.now(); }, true);
    document.addEventListener('pointerup', function () { pointerDown = false; }, true);
    document.addEventListener('pointercancel', function () { pointerDown = false; }, true);
    function pressed() { return pointerDown && Date.now() - pointerDownAt < PRESS_MAX; }
    // `fn` tout de suite si `busy()` est faux et qu'aucun appui n'est en cours,
    // sinon a la fin de l'occupation (focus qui quitte le champ, clic qui suit
    // le relachement), une seule fois par `key` (rendu de la grille, groupes
    // d'une source). Une liste reconstruite entre l'appui et le clic
    // remplacait la pastille visee et le toucher etait perdu.
    var idlePending = {};
    CT.whenIdle = function (key, busy, fn) {
        if (!busy() && !pressed()) { delete idlePending[key]; fn(); return; }
        var queued = key in idlePending;
        idlePending[key] = fn;
        if (queued) return;
        var waiting = false;
        function attempt() {
            if (!(key in idlePending)) return;   // deja rendu (appel immediat depuis)
            if (busy()) return;              // focusout relancera
            if (pressed()) return waitClick();
            document.removeEventListener('focusout', retry);
            var run = idlePending[key];
            delete idlePending[key];
            run();
        }
        function waitClick() {
            if (waiting) return;
            waiting = true;
            // Le clic suit le relachement (au toucher, dans une tache plus
            // tardive) ; sans clic (defilement, relachement hors de la page),
            // limite de temps.
            var timer;
            function go() {
                document.removeEventListener('click', go, true);
                clearTimeout(timer);
                waiting = false;
                setTimeout(attempt, 0);
            }
            document.addEventListener('click', go, true);
            timer = setTimeout(go, PRESS_MAX);
        }
        function retry() { setTimeout(attempt, 0); }
        document.addEventListener('focusout', retry);
        if (!busy()) waitClick();
    };
    CT.setText = function (el, text) { if (el && el.textContent !== text) el.textContent = text; };
    // Reglages en attente d'enregistrement (gain, volume : 600 ms) : envoyes
    // tout de suite a la fermeture d'une fenetre ou quand la page est cachee.
    CT.pendingSaves = new Set();
    CT.flushPendingSaves = function () { Array.from(CT.pendingSaves).forEach(function (f) { f(); }); };
    document.addEventListener('visibilitychange', function () { if (document.hidden) CT.flushPendingSaves(); });
    CT.renderWhenIdle = function () {
        if (CT.isEditing() && CT.renderStatus) CT.renderStatus();
        CT.whenIdle('grid', CT.isEditing, function () { CT.render(); });
    };
    // Reconstruire la grille seulement si les reglages ont change : au retour
    // sur l'onglet, un rendu complet remasquait l'URL, fermait les menus et
    // effacait la ligne en direct pour rien.
    CT.resync = function () {
        var before = JSON.stringify([CT.state.settings, CT.state.entityIds]);
        return Promise.all([CT.reloadSettings(), CT.reloadStatus()]).then(function () {
            if (JSON.stringify([CT.state.settings, CT.state.entityIds]) !== before) CT.renderWhenIdle();
            else if (CT.renderStatus) CT.renderStatus();
        }).catch(function () {});
    };

    // ---- Temps reel : une seule connexion, reconnexion illimitee ---------------
    CT.socket = typeof io === 'function' ? io({path: basePath + '/socket.io'}) : null;
    CT.on = function (event, handler) { if (CT.socket) CT.socket.on(event, handler); };

    // ---- Calibrages : prises enchainees automatiquement ----------------------
    // Choix memorise par navigateur (commun aux deux calibrages).
    var AUTO_KEY = 'claptrap-auto-takes';
    CT.autoTakes = function (on) {
        try {
            if (on !== undefined) localStorage.setItem(AUTO_KEY, on ? '1' : '0');
            return localStorage.getItem(AUTO_KEY) === '1';
        } catch (e) { return !!on; }
    };
    CT.AUTO_DELAY = 3;   // s pour se preparer avant une prise enchainee
    // Compte a rebours dans `out`, puis done() ; retourne la fonction d'arret.
    CT.countdown = function (out, seconds, label, done) {
        var left = seconds;
        out.textContent = label + left + ' s';
        var timer = setInterval(function () {
            left -= 1;
            if (left > 0) { out.textContent = label + left + ' s'; return; }
            clearInterval(timer);
            done();
        }, 1000);
        return function () { clearInterval(timer); };
    };
    // Etat d'une prise, en couleur : 'wait' (orange, compte a rebours avant une
    // prise enchainee), 'listen' (vert, ecoute), 'analyze' (bleu), '' (neutre).
    CT.calState = function (out, state, text) {
        if (state) out.setAttribute('data-state', state); else out.removeAttribute('data-state');
        if (text !== undefined) out.textContent = text;
    };

    // Prise en cours : ecoute (vert) pendant `seconds` s, puis analyse (bleu)
    // jusqu'a la reponse. Retourne la fonction d'arret.
    CT.recordCountdown = function (out, seconds, label) {
        var left = seconds;
        CT.calState(out, 'listen', label + left + ' s');
        var timer = setInterval(function () {
            left -= 1;
            if (left > 0) { CT.calState(out, 'listen', label + left + ' s'); return; }
            clearInterval(timer);
            CT.calState(out, 'analyze', 'Analyse de la prise…');
        }, 1000);
        return function () { clearInterval(timer); };
    };

    // Ecran d'une prise en mode enchaine : compte a rebours dans .cal-countdown,
    // bouton Pause (data-c="pause"), puis start(). Retourne la fonction d'arret.
    CT.autoTake = function (body, rec, start) {
        var pause = body.querySelector('[data-c="pause"]');
        var out = body.querySelector('.cal-countdown');
        CT.calState(out, 'wait');
        var stop = CT.countdown(out, CT.AUTO_DELAY, 'Début de la prise dans ', function () {
            if (pause) pause.remove();
            start();
        });
        pause.addEventListener('click', function () {
            // Prise lancee a la main ; les suivantes s'enchainent de nouveau.
            stop();
            pause.remove();
            CT.calState(out, '', 'En pause : cliquez sur « Enregistrer » quand vous êtes prêt.');
            rec.focus();
        });
        pause.focus();
        return function () { stop(); if (pause.parentNode) pause.remove(); };
    };
    // Icone d'information : le texte s'affiche au survol, au focus clavier
    // et au toucher (bouton), et se ferme avec Echap ou en touchant ailleurs.
    // `text` : texte simple (echappe ici).
    CT.infoTip = function (id, text, label) {
        return '<span class="info-tip"><button type="button" class="info-tip-btn" aria-label="' + CT.esc(label || 'Explication') +
            '" aria-describedby="' + id + '" aria-expanded="false">i</button>' +
            '<span class="info-tip-text" role="tooltip" id="' + id + '">' + CT.esc(text) + '</span></span>';
    };
    function closeTips(except) {
        CT.$$('.info-tip.is-open').forEach(function (t) {
            if (t === except) return;
            t.classList.remove('is-open');
            t.querySelector('.info-tip-btn').setAttribute('aria-expanded', 'false');
        });
    }
    // Echap masque aussi une bulle affichee au survol ou au focus, jusqu'a
    // ce que la souris ou le focus en sorte.
    function shownTip() {
        var a = document.activeElement;
        return (a && a.classList && a.classList.contains('info-tip-btn') ? a.parentNode : null) ||
            CT.$$('.info-tip').filter(function (t) { return t.matches(':hover'); })[0] || null;
    }
    ['mouseleave', 'focusout'].forEach(function (ev) {
        document.addEventListener(ev, function (e) {
            var t = e.target.closest && e.target.closest('.info-tip');
            if (t && !t.contains(e.relatedTarget)) t.classList.remove('is-dismissed');
        }, true);
    });
    document.addEventListener('click', function (e) {
        var btn = e.target.closest && e.target.closest('.info-tip-btn');
        var tip = btn && btn.parentNode;
        closeTips(tip);
        if (!tip) return;
        tip.classList.remove('is-dismissed');
        var open = tip.classList.toggle('is-open');
        btn.setAttribute('aria-expanded', String(open));
    });
    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Escape') return;
        var shown = shownTip();
        if (!CT.$('.info-tip.is-open') && (!shown || shown.classList.contains('is-dismissed'))) return;   // Echap ferme la fenetre
        closeTips(null);
        if (shown) shown.classList.add('is-dismissed');
        e.stopPropagation();
    }, true);

    CT.autoTakesHtml = function (id) {
        return '<label class="check"><input type="checkbox" id="' + id + '"' + (CT.autoTakes() ? ' checked' : '') + '> ' +
            'Enchaîner les prises automatiquement (' + CT.AUTO_DELAY + ' s pour se préparer, sans cliquer entre deux prises)</label>';
    };
    (function () {
        var connectedOnce = false;
        // Reglage modifie depuis Home Assistant (entite, commande MQTT).
        CT.on('settings_changed', function () { CT.resync(); });
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
                    body: {id: src.key, url: url || src.data.url, gain: gain != null ? gain : (src.data.gain != null ? src.data.gain : 10)}};
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
