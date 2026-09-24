/* Assistant d'ajout de source en 3 etapes : type -> parametres -> verification. */
(function () {
    'use strict';
    var CT = window.CT;
    var esc = CT.esc;
    var modal, body, release, created;
    // Numero de session : une reponse arrivee apres la fermeture (ou une
    // reouverture) est ignoree. Fermer pendant l'ajout lancait sinon un test
    // dans la fenetre cachee, sans rafraichir la grille.
    var session = 0;

    function open() {
        var back = document.getElementById('wizard');
        modal = back.querySelector('.modal');
        body = back.querySelector('[data-role="wizard-body"]');
        back.hidden = false;
        session++;
        created = null;
        release = CT.trapFocus(modal, close, function () {
            var add = document.getElementById('add-source');
            return add && !add.hidden ? add : document.getElementById('empty-add');
        });
        stepType();
    }
    function close() {
        CT.stopTest();
        var back = document.getElementById('wizard');
        if (back.hidden) return;
        back.hidden = true;
        session++;
        var rel = release;
        release = null;
        // Toujours recharger : un ajout termine apres la fermeture (ou un micro
        // ajoute dont le reglage a echoue) doit apparaitre dans la grille.
        var done = CT.refresh().catch(function () {});
        // Rendre le focus au bouton "Ajouter" une fois la grille reconstruite.
        done.then(function () { if (rel) rel(); });
    }
    function setStep(n, title) {
        CT.$('#wizard-title').textContent = title;
        CT.$$('.steps li', modal).forEach(function (li, i) {
            li.classList.toggle('is-current', i === n - 1);
            li.classList.toggle('is-done', i < n - 1);
            if (i === n - 1) li.setAttribute('aria-current', 'step'); else li.removeAttribute('aria-current');
        });
    }

    // ---- Etape 1 : type -------------------------------------------------------
    function stepType() {
        setStep(1, 'Quel type de source ?');
        var mic = CT.state.settings.microphone;
        var micTaken = !!(mic && mic.configured !== false);
        body.innerHTML =
            '<div class="choice-grid">' +
            choice('mic', 'Micro', micTaken ? 'Déjà ajouté (un seul micro possible)' : 'Un micro branché sur la machine Home Assistant', micTaken) +
            choice('rtsp', 'Caméra RTSP', 'Le son d\'une caméra ou d\'un flux RTSP', false) +
            choice('vban', 'Flux VBAN', 'Un PC qui diffuse du son avec Voicemeeter', false) +
            '</div>';
        body.querySelector('.choice:not([disabled])').focus();
        CT.$$('.choice', body).forEach(function (b) {
            b.addEventListener('click', function () {
                var kind = b.getAttribute('data-kind');
                if (kind === 'mic') stepMic(); else if (kind === 'rtsp') stepRtsp(); else stepVban();
            });
        });
    }
    function choice(kind, title, text, disabled) {
        return '<button type="button" class="choice" data-kind="' + kind + '"' + (disabled ? ' disabled' : '') + '>' +
            '<span class="tag tag-' + kind + '">' + CT.kindLabel[kind] + '</span><strong>' + title + '</strong>' +
            '<span class="muted">' + text + '</span></button>';
    }
    function actions(okLabel) {
        return '<div class="modal-actions"><button type="button" class="btn btn-ghost" data-w="back">Retour</button>' +
            '<button type="button" class="btn btn-primary" data-w="ok">' + okLabel + '</button></div>';
    }
    function wire(backFn, okFn) {
        body.querySelector('[data-w="back"]').addEventListener('click', backFn);
        var ok = body.querySelector('[data-w="ok"]');
        var label = ok.textContent;
        ok.addEventListener('click', function () {
            var mine = session;
            ok.disabled = true;
            ok.textContent = 'Ajout en cours…';
            Promise.resolve(okFn(mine)).catch(function (err) { if (mine === session) CT.error(err.message); })
                .finally(function () { ok.disabled = false; ok.textContent = label; });
        });
    }
    function current(mine) { return mine === session; }

    // ---- Etape 2 : parametres -----------------------------------------------------
    function stepMic() {
        setStep(2, 'Choisir le micro');
        body.innerHTML = '<div class="field"><label class="field-label" for="wz-device">Périphérique</label>' +
            '<select class="input" id="wz-device">' + CT.deviceOptions('default') + '</select>' +
            '<p class="hint" id="wz-nodev"' + ((CT.state.devices || []).length ? ' hidden' : '') + '>Aucun micro détecté par Home Assistant : le micro par défaut sera utilisé.</p>' +
            '</div>' + actions('Ajouter le micro');
        CT.focusFirst(body);
        // Un micro branche depuis l'ouverture de la page apparait.
        CT.reloadDevices().then(function () {
            var sel = CT.$('#wz-device');
            if (!sel) return;
            var v = sel.value;
            sel.innerHTML = CT.deviceOptions('default');
            sel.value = v;
            CT.$('#wz-nodev').hidden = (CT.state.devices || []).length > 0;
        });
        wire(stepType, function (mine) {
            var dev = CT.deviceFromOption(CT.$('#wz-device').value) || CT.deviceFromOption('default');
            return CT.api('POST', '/api/microphone')
                .then(function () { return CT.patchSource({kind: 'mic', key: 'mic'}, {device: dev}); })
                .then(function () { return CT.patchSource({kind: 'mic', key: 'mic'}, {enabled: true}); })
                .then(function () { return CT.reloadSettings(); })
                .then(function () {
                    if (!current(mine)) return;
                    created = CT.findSource(function (s) { return s.kind === 'mic'; });
                    stepCheck();
                });
        });
    }

    function stepRtsp() {
        setStep(2, 'Adresse de la caméra');
        body.innerHTML =
            '<div class="field"><label class="field-label" for="wz-name">Nom</label><input class="input" id="wz-name" value="Caméra" autocomplete="off"></div>' +
            '<div class="field"><label class="field-label" for="wz-url">Adresse du flux</label>' +
            '<input class="input" id="wz-url" type="url" placeholder="rtsp://utilisateur:motdepasse@192.168.1.20:554/stream" spellcheck="false">' +
            '<p class="hint">Vous la trouverez dans l\'application ou la documentation de la caméra.</p></div>' +
            actions('Ajouter la caméra');
        CT.$('#wz-url').focus();
        wire(stepType, function (mine) {
            var url = CT.$('#wz-url').value.trim();
            if (!url) { CT.$('#wz-url').focus(); throw new Error("Saisissez l'adresse du flux."); }
            return CT.api('POST', '/api/rtsp/stream', {name: CT.$('#wz-name').value.trim() || 'Caméra', url: url, enabled: true})
                .then(function (d) { return CT.reloadSettings().then(function () { return d; }); })
                .then(function (d) {
                    if (!current(mine)) return;
                    created = CT.findSource(function (s) { return s.kind === 'rtsp' && s.key === d.stream.id; });
                    stepCheck();
                });
        });
    }

    function stepVban() {
        setStep(2, 'Choisir le flux VBAN');
        body.innerHTML =
            '<div class="field"><div class="field-row"><span class="field-label">Flux détectés sur le réseau</span>' +
            '<button type="button" class="btn-link" id="wz-refresh">Rafraîchir</button></div>' +
            '<div class="vban-list" id="wz-vban-list" aria-live="polite"><p class="hint">Recherche…</p></div></div>' +
            '<details class="field"><summary>Ajouter à la main (multicast, source qui ne diffuse pas encore…)</summary>' +
            '<div class="form-grid">' +
            '<label>Nom affiché<input class="input" id="wz-v-name" placeholder="Bureau"></label>' +
            '<label>Adresse IP<input class="input" id="wz-v-ip" placeholder="192.168.1.10 ou 239.255.0.1"></label>' +
            '<label>Port<input class="input" id="wz-v-port" type="number" value="6980"></label>' +
            '<label>Nom du flux<input class="input" id="wz-v-stream" placeholder="Stream1"></label>' +
            '</div><button type="button" class="btn btn-ghost" id="wz-v-add">Ajouter ce flux</button></details>' +
            '<div class="modal-actions"><button type="button" class="btn btn-ghost" data-w="back">Retour</button></div>';
        body.querySelector('[data-w="back"]').addEventListener('click', stepType);
        CT.focusFirst(body);
        CT.$('#wz-refresh').addEventListener('click', refreshVban);
        CT.$('#wz-v-add').addEventListener('click', function () {
            var name = CT.$('#wz-v-name').value.trim(), ip = CT.$('#wz-v-ip').value.trim();
            if (!name || !ip) { CT.error('Nom et adresse IP sont obligatoires.'); return; }
            saveVban({name: name, ip: ip, port: parseInt(CT.$('#wz-v-port').value, 10) || 6980,
                      stream_name: CT.$('#wz-v-stream').value.trim() || name, enabled: true});
        });
        refreshVban();
    }
    function refreshVban() {
        var list = CT.$('#wz-vban-list');
        list.innerHTML = '<p class="hint">Recherche…</p>';
        CT.api('GET', '/refresh_vban_sources').then(function (d) {
            var sources = d.sources || [];
            if (!sources.length) {
                list.innerHTML = '<p class="hint">Aucun flux VBAN détecté. Vérifiez que l\'émetteur diffuse vers l\'adresse de Home Assistant (port 6980), ou ajoutez-le à la main.</p>';
                return;
            }
            var saved = CT.state.settings.saved_vban_sources || [];
            list.innerHTML = sources.map(function (s, i) {
                var added = saved.some(function (v) { return v.ip === s.ip && (v.stream_name || v.name) === s.name; });
                return '<button type="button" class="vban-item" data-i="' + i + '"' + (added ? ' disabled data-added="1"' : '') + '><strong>' +
                    esc(s.name || '(sans nom)') + (added ? ' <span class="pill">déjà ajouté</span>' : '') + '</strong>' +
                    '<span class="muted">' + esc(s.ip) + (s.sample_rate ? ' · ' + esc(s.sample_rate) + ' Hz' : '') +
                    (s.channels ? ' · ' + esc(s.channels) + ' canal' + (s.channels > 1 ? 'x' : '') : '') + '</span></button>';
            }).join('');
            CT.$$('.vban-item', list).forEach(function (b) {
                b.addEventListener('click', function () {
                    var s = sources[parseInt(b.getAttribute('data-i'), 10)];
                    saveVban({name: s.name, ip: s.ip, port: s.port || 6980, stream_name: s.name, enabled: true});
                });
            });
        }).catch(function () { list.innerHTML = '<p class="hint">Recherche impossible.</p>'; });
    }
    var savingVban = false;
    function saveVban(src) {
        if (savingVban) return;  // double clic : le 2e envoi repondait « existe deja »
        savingVban = true;
        var mine = session;
        CT.$$('.vban-item, #wz-v-add', body).forEach(function (b) { b.disabled = true; });
        CT.api('POST', '/api/vban/save', src)
            .then(function (d) { return CT.reloadSettings().then(function () { return d.source; }); })
            .then(function (saved) {
                if (!current(mine)) return;
                created = CT.findSource(function (s) { return s.kind === 'vban' && s.data.id === saved.id; });
                stepCheck();
            })
            .catch(function (err) {
                if (!current(mine)) return;
                CT.error('Ajout impossible : ' + err.message);
                CT.$$('.vban-item:not([data-added]), #wz-v-add', body).forEach(function (b) { b.disabled = false; });
            })
            .finally(function () { savingVban = false; });
    }

    // ---- Etape 3 : verification ---------------------------------------------------
    // Repere « bon niveau » du VU-metre : un clap doit depasser -20 dB.
    var GOOD_DB = -20;
    function stepCheck() {
        setStep(3, 'Vérifier le son');
        if (!created) { close(); return; }
        var running = CT.state.status.running;
        body.innerHTML =
            '<p>Tapez dans vos mains près de <strong>' + esc(created.name) + '</strong> : le niveau doit dépasser le repère « bon niveau ».</p>' +
            '<div class="meter meter-large" id="wz-meter"><div class="meter-fill"></div>' +
            '<span class="meter-target" style="left:' + CT.dbToPct(GOOD_DB) + '%" aria-hidden="true"><span>bon niveau</span></span>' +
            '<span class="meter-label">En attente du son…</span></div>' +
            '<p class="hint" id="wz-hint">' + (running
                ? 'La détection tourne : les claps reconnus s\'afficheront sur la carte de la source.'
                : 'Ce test mesure seulement le niveau. Démarrez la détection pour que vos claps soient reconnus.') + '</p>' +
            '<div class="modal-actions">' +
            (running ? '' : '<button type="button" class="btn btn-ghost" data-w="start">Démarrer la détection</button>') +
            '<button type="button" class="btn btn-primary" data-w="done">Terminer</button></div>';
        body.querySelector('[data-w="done"]').addEventListener('click', close);
        var start = body.querySelector('[data-w="start"]');
        if (start) start.addEventListener('click', function () {
            start.disabled = true;
            CT.api('POST', '/api/detection/start', {})
                .then(function () { return CT.reloadStatus(); })
                .then(function () {
                    if (CT.renderStatus) CT.renderStatus();
                    start.remove();
                    CT.$('#wz-hint').textContent = 'Détection démarrée : tapez dans vos mains, les claps reconnus s\'affichent sur la carte de la source.';
                })
                .catch(function (err) { start.disabled = false; CT.error('Démarrage impossible : ' + err.message); });
        });
        body.querySelector('[data-w="done"]').focus();
        var test = CT.testFor(created);
        test.domId = 'wizard';
        test.onLevel = function (data) {
            var m = CT.$('#wz-meter');
            if (!m) return;
            CT.setMeterFill(m.querySelector('.meter-fill'), CT.dbToPct(data.db));
            m.querySelector('.meter-label').textContent = Math.round(data.db) + ' dB';
        };
        CT.startTest(test).catch(function (err) {
            CT.$('#wz-hint').textContent = 'Test du son impossible : ' + err.message;
        });
    }

    CT.openWizard = open;
    CT.initWizard = function () {
        var back = document.getElementById('wizard');
        back.addEventListener('click', function (e) { if (e.target === back) close(); });
        back.querySelector('[data-w="close"]').addEventListener('click', close);
        var emptyAdd = document.getElementById('empty-add');
        if (emptyAdd) emptyAdd.addEventListener('click', open);
    };
})();
