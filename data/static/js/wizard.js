/* Assistant d'ajout de source en 3 etapes : type -> parametres -> verification. */
(function () {
    'use strict';
    var CT = window.CT;
    var esc = CT.esc;
    var modal, body, release, created;
    var VBAN_PORT = window.vbanPort || 6980;   // port d'ecoute de l'add-on (option vban_port)
    // Numero de session : une reponse arrivee apres la fermeture (ou une
    // reouverture) est ignoree. Fermer pendant l'ajout lancait sinon un test
    // dans la fenetre cachee, sans rafraichir la grille.
    var session = 0;
    // Detection en cours : le VU-metre de l'etape 3 suit les niveaux de la
    // detection (source_level) au lieu d'un test, qui ouvrait une 2e session
    // RTSP (limitee sur beaucoup de cameras) et se figeait au demarrage.
    var levelSource = null;

    function showLevel(db) {
        var m = CT.$('#wz-meter');
        if (!m) return;
        CT.setMeterFill(m.querySelector('.meter-fill'), CT.dbToPct(db));
        m.querySelector('.meter-label').textContent = Math.round(db) + ' dB';
    }

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
    function close(show) {
        CT.stopTest();
        levelSource = null;
        var back = document.getElementById('wizard');
        if (back.hidden) return Promise.resolve();
        back.hidden = true;
        session++;
        var rel = release;
        release = null;
        // Toujours recharger : un ajout termine apres la fermeture (ou un micro
        // ajoute dont le reglage a echoue) doit apparaitre dans la grille.
        var done = CT.refresh().catch(function () {});
        // Focus rendu une fois la grille reconstruite : a la nouvelle carte
        // (defilee et mise en evidence) apres un ajout, sinon au bouton "Ajouter".
        return done.then(function () {
            if (rel) rel();   // leve d'abord l'inertie de la page
            var card = show && show.domId && document.getElementById(show.domId);
            if (!card) return;
            if (!card.hasAttribute('tabindex')) card.setAttribute('tabindex', '-1');
            card.focus({preventScroll: true});
            if (card.scrollIntoView) card.scrollIntoView({block: 'center'});
            card.classList.add('is-highlight');
            setTimeout(function () { card.classList.remove('is-highlight'); }, 2000);
            // Groupe encore vide : ouvrir les reglages sur « Ajouter les sons
            // entendus » (le texte de l'etape 3 y renvoyait, loin dans la carte).
            var src = CT.sourceByDomId(show.domId);
            var empty = src && (src.data.sound_groups || []).filter(function (g) {
                return g && g.slug && g.enabled !== false && !CT.groupHasSounds(g);
            })[0];
            if (empty && CT.openSourceSettings) CT.openSourceSettings(show.domId, empty.slug);
        });
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
        var back = body.querySelector('[data-w="back"]');
        ok.addEventListener('click', function () {
            var mine = session;
            ok.disabled = true;
            back.disabled = true;   // « Retour » pendant l'ajout menait a un doublon
            ok.textContent = 'Ajout en cours…';
            // .then : une erreur levee tout de suite (adresse vide) passe aussi
            // par le catch/finally, sinon l'etape restait « Ajout en cours… ».
            Promise.resolve().then(function () { return okFn(mine); }).catch(function (err) { if (mine === session) CT.error(err.message); })
                .finally(function () { ok.disabled = false; back.disabled = false; ok.textContent = label; });
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
            return CT.withRestart(CT.api('POST', '/api/microphone', {device: dev, enabled: true}))
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
            CT.withTip('<label class="switch-inline"><input type="checkbox" role="switch" id="wz-tcp" aria-describedby="wz-tcp-tip"> Transport TCP</label>',
                       CT.infoTip('wz-tcp-tip', 'Plus fiable en Wi-Fi ou si le son se coupe (paquets perdus) ; certaines caméras ne le gèrent pas.',
                                  'Explication du transport TCP')) +
            '<div class="field"><button type="button" class="btn btn-ghost" id="wz-probe">Vérifier l\'adresse</button>' +
            '<div id="wz-probe-result" role="status" aria-live="polite"></div></div>' +
            actions('Ajouter la caméra');
        CT.$('#wz-url').focus();
        var probed = null;   // {url, tcp, format} : format reporte sur la carte creee
        var probeBtn = CT.$('#wz-probe'), probeOut = CT.$('#wz-probe-result');
        var probeSeq = 0;   // reponse d'une verification remplacee : ignoree
        function clearProbe() { probeOut.innerHTML = ''; probed = null; probeSeq++; }
        CT.$('#wz-url').addEventListener('input', clearProbe);
        CT.$('#wz-tcp').addEventListener('change', clearProbe);
        probeBtn.addEventListener('click', function () {
            var mine = session;
            var url = CT.$('#wz-url').value.trim(), tcp = CT.$('#wz-tcp').checked;
            if (!url) { CT.$('#wz-url').focus(); CT.error("Saisissez l'adresse du flux."); return; }
            clearProbe();
            var seq = probeSeq;
            var okBtn = body.querySelector('[data-w="ok"]');
            probeBtn.disabled = true;
            okBtn.disabled = true;   // une seule connexion a la camera (beaucoup n'en servent qu'une avec le son)
            probeBtn.textContent = 'Vérification… (jusqu\'à 15 s)';
            CT.api('POST', '/api/rtsp/probe', {url: url, rtsp_tcp: tcp}).then(function (d) {
                if (!current(mine) || seq !== probeSeq) return;   // adresse modifiee depuis
                var html;
                if (d.ok) {
                    var info = d.format ? CT.audioFormatInfo(d.format) : null;
                    probed = {url: url, tcp: tcp, format: d.format};
                    html = '<p class="' + (info && info.low ? 'text-warn' : 'text-ok') + '"><strong>Son reçu</strong>' +
                        (info ? ' · ' + esc(info.desc) : '') + '</p>' +
                        (info ? '<p class="hint' + (info.low ? ' text-warn' : '') + '">' + info.advice + '</p>' : '');
                } else {
                    html = '<p class="text-warn"><strong>Pas de son</strong> : ' + esc(d.error || 'connexion impossible.') + '</p>' +
                        (d.format ? '<p class="hint">Format annoncé : ' + esc(CT.audioFormatInfo(d.format).desc) + '</p>' : '');
                }
                probeOut.innerHTML = html;
            }).catch(function (err) {
                if (current(mine) && seq === probeSeq) probeOut.innerHTML = '<p class="text-warn">' + esc(err.message) + '</p>';
            }).finally(function () {
                probeBtn.disabled = false;
                okBtn.disabled = false;
                probeBtn.textContent = 'Vérifier l\'adresse';
            });
        });
        wire(stepType, function (mine) {
            var url = CT.$('#wz-url').value.trim();
            if (!url) { CT.$('#wz-url').focus(); throw new Error("Saisissez l'adresse du flux."); }
            return CT.withRestart(CT.api('POST', '/api/rtsp/stream', {name: CT.$('#wz-name').value.trim() || 'Caméra', url: url, enabled: true,
                                                                      rtsp_tcp: CT.$('#wz-tcp').checked}))
                .then(function (d) { return CT.reloadSettings().then(function () { return d; }); })
                .then(function (d) {
                    if (!current(mine)) return;
                    created = CT.findSource(function (s) { return s.kind === 'rtsp' && s.key === d.stream.id; });
                    // Format deja connu (verification de cette adresse) : affiche
                    // sur la carte sans attendre la premiere ecoute.
                    if (created && probed && probed.format && probed.url === url && probed.tcp === CT.$('#wz-tcp').checked &&
                        !CT.state.audioFormats[created.sourceId]) {
                        CT.state.audioFormats[created.sourceId] = Object.assign({probed: true}, probed.format);
                    }
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
            // Un seul port d'ecoute pour tous les flux : option de l'add-on, pas par source.
            '<label>Port d\'écoute<input class="input" id="wz-v-port" type="number" value="' + VBAN_PORT + '" readonly ' +
            'aria-describedby="wz-v-port-hint"></label>' +
            '<label>Nom du flux<input class="input" id="wz-v-stream" placeholder="Stream1"></label>' +
            '</div><p class="hint" id="wz-v-port-hint">Réglez l\'émetteur sur ce port. Pour en changer : option « Port d\'écoute VBAN » ' +
            'de l\'add-on (onglet Configuration), puis redémarrage.</p>' +
            '<button type="button" class="btn btn-ghost" id="wz-v-add">Ajouter ce flux</button></details>' +
            '<div class="modal-actions"><button type="button" class="btn btn-ghost" data-w="back">Retour</button></div>';
        body.querySelector('[data-w="back"]').addEventListener('click', stepType);
        CT.focusFirst(body);
        CT.$('#wz-refresh').addEventListener('click', refreshVban);
        CT.$('#wz-v-add').addEventListener('click', function () {
            var name = CT.$('#wz-v-name').value.trim(), ip = CT.$('#wz-v-ip').value.trim();
            if (!name || !ip) { CT.error('Nom et adresse IP sont obligatoires.'); return; }
            saveVban({name: name, ip: ip, port: VBAN_PORT,
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
                list.innerHTML = '<p class="hint">Aucun flux VBAN détecté. Vérifiez que l\'émetteur diffuse vers l\'adresse de Home Assistant (port ' + VBAN_PORT + '), ou ajoutez-le à la main.</p>';
                return;
            }
            var saved = CT.state.settings.saved_vban_sources || [];
            list.innerHTML = sources.map(function (s, i) {
                var added = saved.some(function (v) { return v.ip === s.ip && (v.stream_name || v.name) === s.name; });
                return '<button type="button" class="vban-item" data-i="' + i + '"' + (added ? ' disabled data-added="1"' : '') + '><strong>' +
                    esc(s.name || '(sans nom)') + (added ? ' <span class="pill">déjà ajouté</span>' : '') + '</strong>' +
                    '<span class="muted">' + esc(s.ip) + (s.sample_rate ? ' · ' + esc(s.sample_rate) + ' Hz' : '') +
                    (s.channels ? ' · ' + esc(s.channels) + (s.channels > 1 ? ' canaux' : ' canal') : '') + '</span></button>';
            }).join('');
            CT.$$('.vban-item', list).forEach(function (b) {
                b.addEventListener('click', function () {
                    var s = sources[parseInt(b.getAttribute('data-i'), 10)];
                    saveVban({name: s.name, ip: s.ip, port: s.port || VBAN_PORT, stream_name: s.name, enabled: true});
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
        CT.withRestart(CT.api('POST', '/api/vban/save', src))
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
    // Consigne de la derniere etape. Le groupe « Clap » d'une nouvelle source
    // reconnait deja les applaudissements ; un groupe encore vide (source
    // reprise d'une sauvegarde) garde la consigne de choix des sons.
    function nextSteps(intro, running) {
        var groups = ((created && created.data && created.data.sound_groups) || []).filter(function (g) { return g && g.enabled !== false; });
        var clapGroup = groups.filter(function (g) {
            var wl = g.sound_whitelist || {};
            return wl.Clapping || wl.Hands || wl.Applause;
        })[0];
        if (clapGroup) {
            return intro + ' Le groupe « ' + (clapGroup.name || clapGroup.slug) + ' » reconnaît déjà les applaudissements : ' +
                (running ? '' : 'démarrez la détection, puis ') + 'tapez deux fois dans vos mains pour l\'essayer. ' +
                'D\'autres sons se choisissent dans « Réglages de la source ».';
        }
        if (groups.some(function (g) { return CT.groupHasSounds(g); })) {
            return intro + ' Les groupes de cette source ont déjà leurs sons : ' +
                (running ? '' : 'démarrez la détection, puis ') + 'faites-les pour vérifier qu\'ils sont reconnus.';
        }
        return intro + ' Ensuite, dans « Réglages de la source », activez « Ajouter les sons entendus » sur le groupe, ' +
            (running ? '' : 'démarrez la détection, ') + 'tapez dans vos mains et cochez les sons qui doivent le déclencher.';
    }
    function stepCheck() {
        setStep(3, 'Vérifier le son');
        if (!created) { close(); return; }
        var running = CT.state.status.running;
        body.innerHTML =
            '<p>Tapez dans vos mains près de <strong>' + esc(created.name) + '</strong> : le niveau doit dépasser le repère « bon niveau ».</p>' +
            '<div class="meter meter-large" id="wz-meter"><div class="meter-fill"></div>' +
            '<span class="meter-target" style="left:' + CT.dbToPct(GOOD_DB) + '%" aria-hidden="true"><span>bon niveau</span></span>' +
            '<span class="meter-label">En attente du son…</span></div>' +
            '<p class="hint" id="wz-hint">' + (running ? nextSteps('La détection tourne.', true)
                                                       : nextSteps('Ce test mesure seulement le niveau.', false)) + '</p>' +
            '<div class="modal-actions">' +
            (running ? '' : '<button type="button" class="btn btn-ghost" data-w="start">Démarrer la détection</button>') +
            '<button type="button" class="btn btn-primary" data-w="done">Terminer</button></div>';
        var shown = created;
        body.querySelector('[data-w="done"]').addEventListener('click', function () { close(shown); });
        var start = body.querySelector('[data-w="start"]');
        if (start) start.addEventListener('click', function () {
            var mine = session;   // assistant ferme pendant la requete : ne plus rien faire
            start.disabled = true;
            // Le test du son ouvrait une 2e session RTSP en parallele de la
            // detection (beaucoup de cameras en limitent le nombre).
            CT.stopTest();
            CT.api('POST', '/api/detection/start', {})
                .catch(function (err) {
                    // Demarree ailleurs entre-temps (409) : ce n'est pas une erreur.
                    return CT.reloadStatus().then(function (st) { if (!st.running) throw err; });
                })
                .then(function () { return CT.reloadStatus(); })
                .then(function () {
                    if (!current(mine)) return;
                    levelSource = shown.sourceId;   // le VU-metre suit maintenant la detection
                    if (CT.renderStatus) CT.renderStatus();
                    start.remove();
                    CT.$('#wz-hint').textContent = nextSteps('Détection démarrée.', true);
                    var done = body.querySelector('[data-w="done"]');
                    done.textContent = 'Terminer et voir la carte';
                })
                .catch(function (err) { start.disabled = false; CT.error('Démarrage impossible : ' + err.message); });
        });
        body.querySelector('[data-w="done"]').focus();
        if (running) { levelSource = created.sourceId; return; }
        var test = CT.testFor(created);
        test.domId = 'wizard';
        test.onLevel = function (data) { showLevel(data.db); };
        CT.startTest(test).catch(function (err) {
            CT.$('#wz-hint').textContent = 'Test du son impossible : ' + err.message;
        });
    }

    CT.openWizard = open;
    CT.on('source_level', function (d) {
        if (levelSource && d && d.source_id === levelSource) showLevel(d.db);
    });
    CT.initWizard = function () {
        var back = document.getElementById('wizard');
        back.querySelector('[data-w="close"]').addEventListener('click', close);
        // Entree (ou « OK » du clavier mobile) dans un champ : action principale
        // de l'etape (« Ajouter ce flux » dans l'ajout VBAN a la main).
        back.addEventListener('keydown', function (e) {
            var t = e.target;
            if (e.key !== 'Enter' || e.isComposing || t.tagName !== 'INPUT' || t.type === 'checkbox') return;
            var manual = t.closest('details');
            var target = manual ? manual.querySelector('#wz-v-add') : back.querySelector('[data-w="ok"]');
            if (!target || target.disabled) return;
            e.preventDefault();
            target.click();
        });
        var emptyAdd = document.getElementById('empty-add');
        if (emptyAdd) emptyAdd.addEventListener('click', open);
    };
})();
