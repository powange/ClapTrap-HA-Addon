/* Cartes de source : en-tete, VU-metre, score de chaque groupe face a son seuil
 * (avec courbe des 10 dernieres secondes), reglages de la source. */
(function () {
    'use strict';
    var CT = window.CT;
    var esc = CT.esc;
    var SPARK_SECONDS = 10;
    var LIVE_IDLE_MS = 2500;   // sans son reconnu : retour au texte d'attente

    function idleText(enabled) {
        if (!enabled) return 'Source désactivée';
        return CT.state.status && CT.state.status.running ? 'En attente de sons…' : 'Détection arrêtée';
    }

    function groupsOf(src) {
        return (src.data.sound_groups || []).filter(function (g) { return g && g.slug; });
    }

    // ---- Rendu -----------------------------------------------------------------
    function statusOf(src) {
        var st = CT.state.status;
        if (!src.enabled) return {text: 'Désactivée', cls: 'off'};
        if (st.running && (st.paused_sources || []).indexOf(src.sourceId) !== -1) return {text: 'En pause (test du son)', cls: 'idle'};
        if (!st.running || (st.sources || []).indexOf(src.sourceId) === -1) {
            if (src.kind === 'rtsp' && !src.data.url) return {text: 'URL à renseigner', cls: 'warn'};
            // Activee mais hors de la session en cours (ajoutee depuis) : pas « Prete ».
            return st.running ? {text: 'Non démarrée', cls: 'warn'} : {text: 'Prête', cls: 'idle'};
        }
        var r = CT.state.sourceStatus[src.sourceId] || (st.source_status || {})[src.sourceId];
        if (r === 'reconnecting') return {text: 'Flux perdu, reconnexion…', cls: 'warn'};
        if (r === 'error') return {text: src.kind === 'vban' ? 'Aucun paquet reçu' : src.kind === 'mic' ? 'Erreur du micro' : 'Erreur de flux', cls: 'err'};
        if (src.kind === 'rtsp') return r === 'connected' ? {text: 'Connectée', cls: 'ok'} : {text: 'Connexion…', cls: 'warn'};
        return {text: "À l'écoute", cls: 'ok'};
    }

    function liveGroupHtml(src, g) {
        var t = g.threshold != null ? g.threshold : 0.5;
        var off = g.enabled === false;   // desactive (interface ou Home Assistant)
        // Groupe sans son coche (etat d'un groupe neuf) : il ne se declenchera
        // jamais, le dire sur la carte plutot que d'afficher « 0 % ».
        var empty = !off && !CT.groupHasSounds(g);
        return '<div class="group-live' + (off ? ' is-off' : '') + (empty ? ' is-empty' : '') + '" data-slug="' + esc(g.slug) + '">' +
            '<div class="group-live-head"><span class="group-live-name">' + esc(g.name || g.slug) +
            (off ? ' <span class="pill">désactivé</span>' : '') + '</span>' +
            '<span class="clap-badge" hidden></span>' +
            '<span class="group-live-score" title="Score actuel">0 %</span></div>' +
            (empty ? '<p class="group-empty">Aucun son choisi : ce groupe ne se déclenchera pas. ' +
                '<button type="button" class="btn-link" data-action="choose-sounds" data-group="' + esc(g.slug) + '">Choisir les sons</button></p>' : '') +
            '<div class="scorebar"><div class="scorebar-fill"></div>' +
            '<input type="range" class="threshold" data-role="threshold" min="0" max="1" step="0.01" value="' + t + '" ' +
            'aria-valuetext="' + CT.pct(t) + '" ' +
            'aria-label="Seuil de confiance du groupe ' + esc(g.name || g.slug) + '"></div>' +
            '<canvas class="spark" width="320" height="40" aria-hidden="true"></canvas>' +
            '<div class="group-live-foot"><span>Seuil de confiance <strong class="threshold-value">' + CT.pct(t) + '</strong></span>' +
            '<span class="muted">' + SPARK_SECONDS + ' dernières secondes</span></div></div>';
    }

    function settingsHtml(src) {
        var d = src.data, html = '';
        if (src.kind === 'mic') {
            var devices = CT.state.devices || [];
            var opts = CT.deviceOptions(d.audio_source);
            var vol = d.volume != null ? d.volume : 100;
            html += field('Périphérique', '<select class="input" data-field="device" id="' + src.domId + '-device">' + opts + '</select>' +
                (devices.length ? '' : '<p class="hint">Aucun micro détecté par Home Assistant (Paramètres › Système › Matériel).</p>'), src.domId + '-device') +
                '<div class="field"><div class="field-row"><label for="' + src.domId + '-volume">Volume du micro <strong data-out="volume">' + vol + ' %</strong></label>' +
                '<label class="switch-inline"><input type="checkbox" role="switch" data-field="auto_volume"' + (d.auto_volume ? ' checked' : '') + '> Automatique</label></div>' +
                '<input type="range" id="' + src.domId + '-volume" data-field="volume" min="0" max="150" step="5" value="' + vol + '"' + (d.auto_volume ? ' disabled' : '') + '>' +
                '<p class="hint">Le volume automatique ajuste le micro pour garder un bon niveau.</p></div>';
        } else if (src.kind === 'rtsp') {
            var gain = d.gain != null ? d.gain : 10;
            var shown = maskUrl(d.url || ''), hidden = shown !== (d.url || '');
            html += field('Nom', '<input type="text" class="input" data-field="name" id="' + src.domId + '-name" value="' + esc(d.name || '') + '">', src.domId + '-name') +
                field('Adresse du flux', '<div class="input-group"><input type="text" class="input" data-field="url" data-secret="1" id="' + src.domId + '-url" value="' + esc(shown) + '"' +
                    (hidden ? ' readonly' : '') + ' placeholder="rtsp://camera:554/stream" spellcheck="false" autocomplete="off">' +
                    (hidden ? '<button type="button" class="btn btn-ghost" data-action="reveal-url" aria-pressed="false">Afficher le mot de passe</button>' : '') + '</div>' +
                    '<p class="hint">Le mot de passe est masqué à l\'écran (« Afficher le mot de passe » pour modifier l\'adresse). Il n\'apparaît jamais dans les journaux ; la sauvegarde « Exporter » le contient.</p>', src.domId + '-url') +
                '<div class="field"><label class="switch-inline"><input type="checkbox" role="switch" data-field="rtsp_tcp"' + (d.rtsp_tcp ? ' checked' : '') +
                    '> Transport TCP</label><p class="hint">Plus fiable en Wi-Fi ou si le son se coupe (paquets perdus) ; certaines caméras ne le gèrent pas.</p></div>' +
                '<div class="field" data-role="audio-format">' + audioFormatHtml(src) + '</div>' +
                gainField(src, gain, 100);
        } else {
            var mcast = /^2(2[4-9]|3\d)\./.test(d.ip || '');
            html += field('Nom', '<input type="text" class="input" data-field="name" id="' + src.domId + '-name" value="' + esc(d.name || '') + '">', src.domId + '-name') +
                '<div class="field"><span class="field-label">Flux reçu</span><p class="mono">' + esc(d.stream_name || d.name) + ' · ' + esc(d.ip) + ':' + esc(String(window.vbanPort || d.port || 6980)) +
                ' <span class="pill">' + (mcast ? 'multicast' : 'unicast') + '</span></p>' +
                '<p class="hint">Nom du flux et adresse de l\'émetteur (Voicemeeter) : pour les changer, supprimez la source et ajoutez-la de nouveau. Le nom ci-dessus ne sert qu\'à l\'affichage.</p></div>' +
                gainField(src, d.gain != null ? d.gain : 1, 100);
        }
        var nr = d.noise_reduction || 0;
        html += field('Réduction du bruit de fond',
            '<select class="input" data-field="noise_reduction" id="' + src.domId + '-nr">' +
            CT.NR_NAMES.map(function (label, level) {
                return '<option value="' + level + '"' + (level === nr ? ' selected' : '') + '>' + label + '</option>';
            }).join('') + '</select>' +
            '<p class="hint">Atténue un bruit continu (souffle, ventilation, réfrigérateur) avant la reconnaissance. ' +
            'Le comptage des claps n\'est pas touché. Comparez avec « Enregistrer ' + RECORD_SECONDS + ' s » : ' +
            'une réduction trop forte peut aussi effacer des sons faibles.</p>', src.domId + '-nr');
        html += field('Force minimale d\'un clap', peakRatioSelect(src, d) +
            '<p class="hint">Combien de fois plus fort que le bruit ambiant un pic doit être pour compter. ' +
            'Plus haut pour une source bruyante ou très sensible (claps comptés en trop), ' +
            'plus bas pour une source lointaine (claps manqués). Avec une valeur propre, « Calibrer le comptage » sur cette source la règle elle (et non la valeur globale).</p>',
            src.domId + '-ratio');
        html += '<div class="field"><span class="field-label">Niveau sonore</span>' +
            '<button type="button" class="btn btn-ghost" data-action="test">Tester le son</button>' +
            '<p class="hint" data-role="test-hint"></p></div>' +   // texte : renderSourceStatuses
            '<div class="field"><span class="field-label">Extrait sonore</span>' +
            '<button type="button" class="btn btn-ghost" data-action="record">Enregistrer ' + RECORD_SECONDS + ' s</button>' +
            '<p class="hint" data-role="record-hint"></p><div class="record-result" data-role="record-result">' +
            recordingHtml(src) + '</div></div>';
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
    // ---- Format audio recu d'une camera ---------------------------------------
    // Annonce par ffmpeg a chaque connexion (detection ou test du son). Une
    // frequence sous 16 kHz coupe les aigus que YAMNet utilise : un claquement
    // de doigts devient meconnaissable (pris pour un animal...).
    var CODEC_NAMES = {pcm_mulaw: 'G.711 µ-law', pcm_alaw: 'G.711 A-law', aac: 'AAC', aac_latm: 'AAC',
                       adpcm_g726: 'G.726', adpcm_g726le: 'G.726', adpcm_g722: 'G.722', g722: 'G.722', g726: 'G.726',
                       opus: 'Opus', mp3: 'MP3', mp3float: 'MP3', mp2: 'MP2', amrnb: 'AMR', amrwb: 'AMR-WB',
                       pcm_s16be: 'PCM 16 bits', pcm_s16le: 'PCM 16 bits', speex: 'Speex', ac3: 'AC-3', vorbis: 'Vorbis'};
    var CHANNEL_NAMES = {mono: 'mono', stereo: 'stéréo'};
    // {desc, low, advice} d'un format audio (cartes et assistant).
    CT.audioFormatInfo = function (fmt) {
        var khz = fmt.sample_rate / 1000;
        var low = fmt.sample_rate < 16000;
        return {
            desc: [CODEC_NAMES[fmt.codec] || fmt.codec, String(khz).replace('.', ',') + ' kHz',
                   CHANNEL_NAMES[fmt.channels] || fmt.channels].filter(Boolean).join(' · '),
            low: low,
            advice: low
                ? 'Fréquence trop basse : tout ce qui dépasse ' + String(khz / 2).replace('.', ',') + ' kHz est perdu, et les sons aigus ' +
                  '(claquement de doigts, sifflement) sont mal reconnus. Si la caméra le permet, choisissez AAC à 16 kHz ou plus ' +
                  'dans ses réglages audio.'
                : 'Fréquence suffisante pour la reconnaissance.'
        };
    };
    function audioFormatHtml(src) {
        var fmt = CT.state.audioFormats[src.sourceId];
        var head = '<span class="field-label">Format audio reçu</span>';
        if (!fmt) return head + '<p class="hint">Inconnu pour l\'instant : il s\'affiche dès que la caméra est écoutée (« Tester le son » ou détection).</p>';
        var info = CT.audioFormatInfo(fmt);
        return head + '<p class="audio-format' + (info.low ? ' text-warn' : '') + '">' + esc(info.desc) + '</p>' +
            '<p class="hint' + (info.low ? ' text-warn' : '') + '">' + info.advice + (info.low ? ' Relancez ensuite la détection.' : '') + '</p>';
    }
    // Ecrire seulement ce qui change : chaque statut de source reecrivait le
    // format audio et les aides de toutes les cartes (280 modifications de la
    // page pour 10 statuts identiques).
    var setText = CT.setText;
    function updateAudioFormat(src) {
        var card = document.getElementById(src.domId);
        var el = card && card.querySelector('[data-role="audio-format"]');
        if (!el) return;
        var html = audioFormatHtml(src);
        if (el._html !== html) { el.innerHTML = html; el._html = html; }
    }
    CT.on('source_format', function (d) {
        if (!d || !d.source_id || !d.format) return;
        CT.state.audioFormats[d.source_id] = d.format;
        var src = CT.findSource(function (s) { return s.sourceId === d.source_id; });
        if (src) updateAudioFormat(src);
    });

    // Mot de passe d'une URL RTSP masque a l'affichage (rtsp://admin:••••@…).
    function maskUrl(url) {
        // Jusqu'au dernier « @ » : un mot de passe contenant « / » ou « @ »
        // restait en partie visible.
        return String(url || '').replace(/^([a-z][a-z0-9+.-]*:\/\/[^:@\/\s]*):\S*@/i, '$1:••••@');
    }
    // Adresse reelle d'un champ d'URL : l'adresse masquee affichee (champ
    // non revele) vaut l'adresse enregistree.
    function realUrl(input, src) {
        var v = input.value.trim();
        return input.dataset.secret && v === maskUrl(src.data.url || '') ? (src.data.url || '') : v;
    }
    // Pourquoi « Tester le son » est grise pendant que la detection ecoute la
    // source : ecrit sous le bouton (une infobulle ne s'affiche ni au toucher
    // ni au clavier).
    function testHint(src, listening) {
        return listening
            ? 'Affiche le niveau sonore dans le VU-mètre. Pendant le test, la détection de cette source est suspendue ' +
              '(elle ne déclenche rien), puis reprend dès l\'arrêt du test.'
            : 'Affiche le niveau sonore dans le VU-mètre (en haut de cette fenêtre et sur la carte), sans lancer la détection.';
    }
    // ---- Extrait enregistre (detection en cours) --------------------------
    // Ce que la reconnaissance recoit (gain compris) : on l'ecoute pour
    // reperer echo, saturation ou reduction de bruit, avec les sons reconnus.
    var RECORD_SECONDS = 5;
    // Force minimale d'un clap propre a la source (vide : reglage global).
    var RATIO_CHOICES = [1.5, 2, 3, 4, 6, 8, 12];
    function ratioText(v) { return '× ' + String(v).replace('.', ','); }
    function peakRatioSelect(src, d) {
        var g = ((CT.state.settings || {}).global || {}).peak_ratio;
        var own = d.peak_ratio != null ? Number(d.peak_ratio) : null;
        var choices = RATIO_CHOICES.slice();
        if (own != null && choices.indexOf(own) === -1) choices.push(own);
        choices.sort(function (a, b) { return a - b; });
        return '<select class="input" data-field="peak_ratio" id="' + src.domId + '-ratio">' +
            '<option value=""' + (own == null ? ' selected' : '') + '>Réglage global (' + ratioText(g != null ? g : 3) + ')</option>' +
            choices.map(function (v) {
                return '<option value="' + v + '"' + (own === v ? ' selected' : '') + '>' + ratioText(v) + ' le bruit ambiant</option>';
            }).join('') + '</select>';
    }
    var NR_NAMES = CT.NR_NAMES;
    function recordHint(listening) {
        return listening
            ? 'Enregistre ' + RECORD_SECONDS + ' s de ce que reçoit la reconnaissance (gain compris) : faites le son pendant ' +
              'l\'enregistrement, puis écoutez-le et voyez les sons reconnus.'
            : 'Disponible pendant la détection, quand la source est écoutée.';
    }
    // Enregistrement en cours et dernier extrait, par source : la carte peut
    // etre reconstruite pendant les ~7 s de la requete (fermeture des
    // reglages). Un nouvel enregistrement efface aussitot les anciens extraits
    // de la source (avant et apres reduction du bruit).
    var recordingNow = {}, lastRecording = {};
    function showRecording(src) {
        var cardNow = document.getElementById(src.domId);   // carte actuelle, peut-etre reconstruite
        var out = cardNow && cardNow.querySelector('[data-role="record-result"]');
        if (out) out.innerHTML = recordingHtml(src);
    }
    function recordClip(card, src) {
        if (recordingNow[src.sourceId]) return;
        recordingNow[src.sourceId] = true;
        delete lastRecording[src.sourceId];
        showRecording(src);
        CT.renderSourceStatuses();
        CT.api('POST', '/api/source/record', {source_id: src.sourceId, seconds: RECORD_SECONDS})
            .then(function (d) {
                lastRecording[src.sourceId] = {d: d, url: wavUrl(d.wav), name: recordingName(src),
                                               urlDenoised: d.wav_denoised ? wavUrl(d.wav_denoised) : ''};
            })
            .catch(function (err) { CT.error('Enregistrement impossible : ' + err.message); })
            .then(function () {
                delete recordingNow[src.sourceId];
                showRecording(src);
                CT.renderSourceStatuses();
            });
    }
    // Son integre a la page (data:) plutot qu'une adresse blob: : une
    // application ou un navigateur ne peut pas resservir un ancien extrait.
    function wavUrl(b64) {
        return b64 ? 'data:audio/wav;base64,' + b64 : '';
    }
    document.addEventListener('click', function (e) {
        var a = e.target.closest && e.target.closest('a[data-dl]');
        if (!a) return;
        var rec = lastRecording[a.getAttribute('data-dl-source')];
        var url = rec && rec[a.getAttribute('data-dl')];
        if (url) a.href = url; else e.preventDefault();
    });
    function recordingName(src) {
        var stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
        return 'claptrap-' + (src.data.name || src.sourceId).replace(/[^\w-]+/g, '_') + '-' + stamp + '.wav';
    }
    function recordingHtml(src) {
        var rec = lastRecording[src.sourceId];
        if (!rec) return '';
        var d = rec.d, pct = function (v) { return Math.round(v * 100) + ' %'; };
        function timeline(list) {
            var rows = (list || []).map(function (w) {
                return '<li><span class="mono">' + w.t.toFixed(1).replace('.', ',') + ' s</span> ' +
                    (w.top || []).map(function (c) { return esc(CT.soundLabel(c.classe)) + ' ' + pct(c.score); }).join(' · ') + '</li>';
            }).join('');
            return rows ? '<ul class="record-timeline">' + rows + '</ul>' : '<p class="hint">Aucun son reconnu.</p>';
        }
        var nrName = (NR_NAMES[d.noise_reduction] || '').toLowerCase();
        // Lien de telechargement rempli au clic (le son n'est pas recopie
        // une seconde fois dans la page).
        var player = function (url, name, label) {
            return url ? '<audio controls src="' + url + '"' + (label ? ' data-role="' + label + '"' : '') + '></audio>' +
                '<p><a href="#" data-dl="' + (label === 'denoised' ? 'urlDenoised' : 'url') + '" data-dl-source="' + esc(src.sourceId) + '"' +
                ' download="' + esc(name) + '">Télécharger l\'extrait (WAV)</a></p>' : '';
        };
        var warn = d.clipped ? '<p class="hint text-warn">Le son arrive déjà saturé du micro ou de la caméra : éloignez la source ' +
                               'du bruit ou baissez le volume du micro dans la caméra (le gain de ClapTrap n\'y peut rien).</p>'
            : d.limited ? '<p class="hint">Des sons forts ont été atténués par le limiteur (gain de la source élevé) : ' +
                          'baissez le gain si cela arrive souvent.</p>' : '';
        var legend = ' (une fenêtre d\'environ 1 s par ligne, depuis le début) :';
        if (!rec.urlDenoised) {
            return player(rec.url, rec.name) + warn + '<p class="hint">Sons reconnus' + legend + '</p>' + timeline(d.timeline);
        }
        // Reduction du bruit active : chaque extrait avec ses propres sons reconnus.
        return warn +
            '<div class="record-part"><h4>Avant la réduction du bruit</h4>' + player(rec.url, rec.name, 'raw') +
            '<p class="hint">Sons reconnus sur cet extrait' + legend + '</p>' + timeline(d.timeline_raw) + '</div>' +
            '<div class="record-part" data-role="denoised-part"><h4>Après la réduction du bruit (' + esc(nrName) + ')</h4>' +
            '<p class="hint">Ce que YAMNet reçoit : c\'est sur ce son que la détection décide.</p>' +
            player(rec.urlDenoised, rec.name.replace(/\.wav$/, '-reduit.wav'), 'denoised') +
            '<p class="hint">Sons reconnus sur cet extrait' + legend + '</p>' + timeline(d.timeline) + '</div>';
    }

    function field(label, control, forId) {
        return '<div class="field"><label class="field-label"' + (forId ? ' for="' + forId + '"' : '') + '>' + label + '</label>' + control + '</div>';
    }
    function gainField(src, gain, max) {
        return '<div class="field"><label for="' + src.domId + '-gain">Gain <strong data-out="gain">' + gain + '×</strong></label>' +
            '<input type="range" id="' + src.domId + '-gain" data-field="gain" min="1" max="' + max + '" step="1" value="' + gain + '">' +
            '<p class="hint">Amplifie un flux trop faible. Surveillez le VU-mètre : il ne doit pas rester dans le rouge.</p></div>';
    }

    // VU-metre (carte et fenetre de reglages) : setMeter les met tous a jour.
    function meterHtml() {
        return '<div class="meter" data-role="meter"><div class="meter-fill"></div><span class="meter-label" data-role="meter-label">—</span></div>';
    }

    function cardHtml(src) {
        var groups = groupsOf(src);
        return '<article class="card source-card' + (src.enabled ? '' : ' is-disabled') + '" id="' + src.domId + '">' +
            '<header class="card-head">' +
                '<span class="tag tag-' + src.kind + '">' + CT.kindLabel[src.kind] + '</span>' +
                '<h2 class="card-title">' + esc(src.name) + '</h2>' +
                '<label class="switch"><input type="checkbox" role="switch" data-action="toggle"' + (src.enabled ? ' checked' : '') +
                ' aria-label="Activer ' + esc(src.name) + '"><span class="switch-track" aria-hidden="true"></span></label>' +
                '<div class="menu"><button type="button" class="btn-icon" data-action="menu" aria-haspopup="true" aria-expanded="false" aria-label="Actions pour ' + esc(src.name) + '">' +
                '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><circle cx="5" cy="12" r="2" fill="currentColor"/><circle cx="12" cy="12" r="2" fill="currentColor"/><circle cx="19" cy="12" r="2" fill="currentColor"/></svg></button>' +
                '<div class="menu-list" role="menu" hidden>' +
                    '<button type="button" role="menuitem" data-action="test">Tester le son</button>' +
                    '<button type="button" role="menuitem" data-action="open-settings">Réglages de la source</button>' +
                    '<button type="button" role="menuitem" data-action="calibrate">Calibrer…</button>' +
                    '<button type="button" role="menuitem" class="danger" data-action="delete">Supprimer…</button>' +
                '</div></div>' +
            '</header>' +
            '<p class="source-status" data-role="status"><span class="dot"></span><span class="status-text"></span></p>' +
            // Cause d'une erreur (« 401 Unauthorized », camera sans audio...) :
            // « Erreur de flux » seul ne disait pas quoi corriger.
            '<p class="hint text-warn source-error" data-role="error" hidden></p>' +
            '<p class="hint source-diag" data-role="diag" hidden></p>' +
            meterHtml() +
            '<p class="live-line" data-role="live">' + idleText(src.enabled) + '</p>' +
            '<div class="groups-live">' + groups.map(function (g) { return liveGroupHtml(src, g); }).join('') + '</div>' +
            '<button type="button" class="btn btn-ghost source-settings-open" data-role="settings-open" data-action="open-settings">Réglages de la source</button>' +
            // Fenetre (<dialog>) DANS la carte : les champs gardent leurs
            // gestionnaires (delegation sur la carte) ; le navigateur gere le
            // premier plan, l'inertie du reste de la page et Echap.
            '<dialog class="modal source-settings" data-role="settings" aria-labelledby="' + src.domId + '-settings-title">' +
                // Titre et VU-metre colles en haut de la fenetre : visibles en
                // faisant defiler (test du son, gain, volume). Sous le titre
                // sans plus, le VU-metre sortait de l'ecran une fois descendu
                // jusqu'a « Tester le son » (celui de la carte est sous le fond).
                '<div class="settings-top"><div class="modal-head"><h2 id="' + src.domId + '-settings-title">Réglages · ' + esc(src.name) + '</h2>' +
                '<button type="button" class="btn-icon" data-action="close-settings" aria-label="Fermer les réglages">' +
                '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></button></div>' +
                meterHtml() + '</div>' +
                '<div class="settings-body">' + settingsHtml(src) + '</div></dialog>' +
            '</article>';
    }

    CT.renderSources = function () {
        var grid = document.getElementById('sources-grid');
        if (!grid) return;
        // Rendu force pendant que des reglages sont ouverts (rare : les rendus
        // differes attendent leur fermeture) : fermer proprement (messages
        // rendus au <body>), puis rouvrir apres le rendu.
        CT.$$('dialog.source-settings', grid).forEach(function (d) {
            var card = d.closest('.source-card');
            if (!card) return;
            CT.state.openPanels[card.id] = d.open;
            if (d.open) { d._silent = true; closeSettings(card); }
        });
        var sources = CT.sourceList();
        grid.innerHTML = sources.map(cardHtml).join('') +
            '<button type="button" class="card add-card" id="add-source">' +
            '<span class="add-plus" aria-hidden="true">+</span><span>Ajouter une source</span>' +
            '<span class="muted">Micro, caméra RTSP ou flux VBAN</span></button>';
        CT.$('#sources-empty').hidden = sources.length > 0;
        // Premiers pas : pas d'historique ni de seconde carte « Ajouter » tant
        // qu'aucune source n'existe (l'accueil a son propre bouton).
        CT.$('#history').hidden = sources.length === 0;
        CT.$('#add-source').hidden = sources.length === 0;
        indexCards(sources);
        sources.forEach(function (src) {
            var card = document.getElementById(src.domId);
            bindCard(card, src);
            if (CT.renderGroupsManage) CT.renderGroupsManage(card, src);
            // Fenetre de reglages ouverte avant le rendu : rouverte.
            if (CT.state.openPanels[src.domId]) openSettings(card, src, true);
        });
        CT.$('#add-source').addEventListener('click', function () { if (CT.openWizard) CT.openWizard(); });
        // Scores et courbes conserves d'un rendu a l'autre (ils repartaient a
        // zero a chaque action), ligne de seuil visible des l'affichage.
        CT.redrawLive();
        CT.renderSourceStatuses();
        if (CT.state.testing) CT.onTestChange(CT.state.testing, true);
    };

    CT.renderSourceStatuses = function () {
        var running = !!(CT.state.status && CT.state.status.running);
        // Test du son pendant la detection : le serveur suspend la detection
        // de cette source le temps du test (pas de seconde capture), puis la
        // reprend ; une detection demarree pendant le test l'attend aussi.
        // Formats connus du serveur (page ouverte apres la connexion des
        // cameras) ; un format recu en direct depuis, plus recent, est garde.
        var known = (CT.state.status && CT.state.status.audio_formats) || {};
        Object.keys(known).forEach(function (sid) {
            var cur = CT.state.audioFormats[sid];
            if (!cur || cur.probed) CT.state.audioFormats[sid] = known[sid];   // verifie dans l'assistant : le serveur est plus recent
        });
        CT.sourceList().forEach(function (src) {
            var card = document.getElementById(src.domId);
            if (!card) return;
            if (src.kind === 'rtsp') updateAudioFormat(src);
            // Source ecoutee par la detection : son VU-metre suit deja le son, et
            // un test ouvrait une seconde capture (2e session RTSP). Une source
            // hors de la session (desactivee, ajoutee depuis) reste testable.
            var listening = running && (CT.state.status.sources || []).indexOf(src.sourceId) !== -1;
            var paused = running && (CT.state.status.paused_sources || []).indexOf(src.sourceId) !== -1;
            setText(card.querySelector('[data-role="test-hint"]'), testHint(src, listening || paused));
            // Extrait : l'inverse du test, seulement quand la detection ecoute la source.
            var rec = card.querySelector('[data-action="record"]');
            if (rec) {
                var busy = !!recordingNow[src.sourceId];
                rec.disabled = busy || !listening;
                setText(rec, busy ? 'Enregistrement… faites le son' : 'Enregistrer ' + RECORD_SECONDS + ' s');
            }
            setText(card.querySelector('[data-role="record-hint"]'), recordHint(listening));
            // Texte d'attente selon l'etat de la detection (il restait
            // « Détection arrêtée » apres le demarrage, en silence).
            var line = card.querySelector('[data-role="live"]');
            if (line && (line.textContent === 'En attente de sons…' || line.textContent === 'Détection arrêtée')) {
                setText(line, idleText(src.enabled));
            }
            var st = statusOf(src);
            var errEl = card.querySelector('[data-role="error"]');
            if (errEl) {
                // Cause recue en direct ; celle du dernier /status seulement si
                // aucun statut en direct n'est encore arrive pour cette source.
                var live = src.sourceId in CT.state.sourceStatus;
                var detail = live ? CT.state.sourceErrors[src.sourceId] || ''
                                  : ((CT.state.status || {}).source_errors || {})[src.sourceId] || '';
                setText(errEl, detail);
                errEl.hidden = !(st.cls === 'err' && detail);
            }
            var diagEl = card.querySelector('[data-role="diag"]');
            if (diagEl) {
                // Diagnostic de la detection : analyses sautees (processeur
                // trop charge) et coupures du flux, seulement s'il y en a.
                var ds = CT.state.status.running ? ((CT.state.status.source_stats || {})[src.sourceId] || {}) : {};
                var parts = [];
                // Duree d'analyse YAMNet (performance) : toujours visible pendant
                // la detection, le reste seulement s'il y a eu un probleme.
                if (ds.analyse_ms != null) parts.push('Analyse : ' + String(ds.analyse_ms).replace('.', ',') + ' ms en moyenne' +
                    (ds.analyse_max_ms != null ? ' (max ' + String(ds.analyse_max_ms).replace('.', ',') + ' ms)' : '') +
                    (ds.analyses_centrees ? ' · ' + ds.analyses_centrees + ' analyse(s) centrée(s) sur un clap' +
                        (ds.analyses ? ' sur ' + ds.analyses + ' depuis le démarrage' : '') : ''));
                var late = (ds.analyses_sautees || 0) + (ds.blocs_perdus || 0);
                if (late) parts.push(late + ' analyse(s) ou bloc(s) perdu(s) : processeur trop chargé');
                if (ds.coupures) parts.push(ds.coupures + ' coupure(s) du flux depuis le démarrage');
                if (ds.reconnexions) parts.push(ds.reconnexions + ' reconnexion(s)');
                var diag = parts.join(' · ');
                setText(diagEl, diag);
                diagEl.hidden = !diag;
            }
            var el = card.querySelector('[data-role="status"]');
            if (el.className !== 'source-status status-' + st.cls) el.className = 'source-status status-' + st.cls;
            setText(el.querySelector('.status-text'), st.text);
            if (!CT.state.status.running && !(CT.state.testing && CT.state.testing.domId === src.domId)) {
                setMeter(card, null);
            }
        });
    };

    // ---- Interactions ------------------------------------------------------------
    function refreshAfter(promise, okMessage) {
        return promise.then(function (d) {
            if (okMessage) CT.success(okMessage);
            return CT.refresh().then(function () { return d; });
        });
    }

    // Ferme les menus « ⋯ » ouverts, sauf celui qui contient `keep` (clic
    // dedans) : un menu ferme ne reste pas « developpe » (aria-expanded).
    function closeMenus(keep) {
        CT.$$('.menu-list').forEach(function (m) {
            if (m.hidden || (keep && m.parentNode.contains(keep))) return;
            m.hidden = true;
            var btn = m.parentNode.querySelector('[data-action="menu"]');
            if (btn) btn.setAttribute('aria-expanded', 'false');
        });
    }

    function bindCard(card, bound) {
        // Source relue a chaque action : apres un rechargement des reglages
        // (retour sur l'onglet, renommage, reconnexion), l'objet de la liaison
        // etait perime et les modifications partaient dans des donnees que
        // l'ecran n'affichait plus.
        function cur() { return CT.sourceByDomId(card.id) || bound; }
        var menuBtn = card.querySelector('[data-action="menu"]');
        var menu = card.querySelector('.menu-list');
        function closeMenu() { menu.hidden = true; menuBtn.setAttribute('aria-expanded', 'false'); }
        menuBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            var open = menu.hidden;
            closeMenus();
            menu.hidden = !open;
            menuBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
            if (open) {
                var first = CT.$$('[role="menuitem"]', menu).filter(function (b) { return !b.hidden && !b.disabled; })[0];
                if (first) first.focus();
            }
        });
        menu.addEventListener('keydown', function (e) {
            var items = CT.$$('[role="menuitem"]', menu).filter(function (b) { return !b.hidden && !b.disabled; });
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
            var src = cur();
            if (action === 'test') { closeMenu(); toggleTest(src, card); }
            else if (action === 'record') { recordClip(card, src); }
            else if (action === 'open-settings') { closeMenu(); openSettings(card, src); }
            else if (action === 'choose-sounds') { openSettings(card, src, false, btn.getAttribute('data-group')); }
            else if (action === 'close-settings') { closeSettings(card); }
            else if (action === 'calibrate') {
                closeMenu();
                if (!src.enabled) { CT.error('Activez la source avant de la calibrer.'); return; }
                if (CT.openCalibration) CT.openCalibration(src);
            } else if (action === 'delete') { closeMenu(); deleteSource(src); }
            else if (action === 'test-webhook') { testWebhook(card, src); }
            else if (action === 'reveal-url') {
                // Bouton explicite : afficher l'adresse complete pour la modifier.
                var urlInput = card.querySelector('[data-field="url"]');
                var reveal = btn.getAttribute('aria-pressed') !== 'true';
                urlInput.value = reveal ? (src.data.url || '') : maskUrl(src.data.url || '');
                urlInput.readOnly = !reveal;
                btn.setAttribute('aria-pressed', String(reveal));
                if (reveal) urlInput.focus();
            }
            else if (action === 'add-group' && CT.addGroup) { CT.addGroup(src); }
        });

        var toggle = card.querySelector('[data-action="toggle"]');
        toggle.addEventListener('change', function () {
            var src = cur();
            var value = toggle.checked;
            if (toggle.dataset.busy) { toggle.checked = !value; return; }   // requete deja en cours
            // aria-disabled et non disabled : un champ desactive perd le focus
            // (Espace sur la 2e carte puis Tab ramenait a la 1re).
            toggle.dataset.busy = '1';
            toggle.setAttribute('aria-disabled', 'true');
            refreshAfter(CT.withRestart(CT.patchSource(src, {enabled: value})))
                .catch(function (err) { toggle.checked = !value; CT.error('Activation impossible : ' + err.message); })
                .finally(function () { delete toggle.dataset.busy; toggle.setAttribute('aria-disabled', 'false'); });
        });

        // Seuil de chaque groupe, directement sur le score en direct
        CT.$$('.group-live', card).forEach(function (row) {
            var slider = row.querySelector('.threshold');
            var prev = slider.value;
            slider.addEventListener('input', function () {
                row.querySelector('.threshold-value').textContent = CT.pct(slider.value);
                slider.setAttribute('aria-valuetext', CT.pct(slider.value));
                var live = (CT.state.live[bound.sourceId] || {scores: {}}).scores[row.getAttribute('data-slug')] || [];
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
                var src = cur();
                var slug = row.getAttribute('data-slug');
                CT.api('PUT', '/api/source/sound_groups', {kind: src.kind, source_key: src.key, group_slug: slug,
                                                           threshold: parseFloat(slider.value)})
                    .then(function () {
                        prev = slider.value;
                        var g = groupsOf(src).filter(function (x) { return x.slug === slug; })[0];
                        if (g) g.threshold = parseFloat(slider.value);
                        CT.markSaved(row.querySelector('.group-live-foot span'), 'Seuil enregistré');
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
            // Gain et volume : chaque flèche du clavier envoyait une requête (et
            // une annonce « Enregistré ») qui pouvaient se croiser. Attente de
            // 600 ms après la dernière, comme le seuil, et une seule requête
            // à la fois, avec la dernière valeur.
            var timer = null, inflight = false, again = false;
            input.addEventListener('change', function () {
                if (input.type !== 'range') return saveField();
                clearTimeout(timer);
                timer = setTimeout(function () { timer = null; saveField(); }, 600);
                CT.pendingSaves.add(flush);
            });
            // Fenetre fermee ou application mise en arriere-plan avant les
            // 600 ms : enregistre tout de suite (la valeur etait perdue).
            function flush() {
                CT.pendingSaves.delete(flush);
                if (timer) { clearTimeout(timer); timer = null; saveField(); }
            }
            function saveField() {
                if (inflight) { again = true; return; }
                var done = saveNow();
                if (!done) return;
                inflight = true;
                done.then(null, function () {}).then(function () {
                    inflight = false;
                    if (again) { again = false; saveField(); }
                });
            }
            function saveNow() {
                var src = cur();
                var body = {};
                if (name === 'device') {
                    var dev = CT.deviceFromOption(input.value, src.data);
                    if (!dev) {
                        input.value = input.dataset.prev;
                        CT.error('Ce micro n\'est plus détecté : choisissez-en un autre.');
                        return null;
                    }
                    body.device = dev;
                } else if (input.type === 'checkbox') {
                    body[name] = input.checked;
                } else if (input.type === 'range' || name === 'noise_reduction') {
                    body[name] = parseInt(input.value, 10);
                } else if (name === 'peak_ratio') {
                    body[name] = input.value === '' ? null : parseFloat(input.value);   // null : reglage global
                } else {
                    body[name] = input.value.trim();
                }
                if (input.dataset.secret && realUrl(input, src) === (src.data.url || '')) return null;  // rien change
                if (input.type === 'range' && input.value === input.dataset.prev) return null;
                // Valeur envoyee : une valeur plus recente (flèches pendant la
                // requete) n'est ni prise pour deja enregistree ni ecrasee.
                var sent = input.type === 'checkbox' ? String(input.checked) : input.value;
                var mayRestart = name === 'auto_volume' || (src.enabled && ['url', 'device', 'rtsp_tcp'].indexOf(name) !== -1);
                var fieldId = input.id;
                return CT.withRestart(CT.patchSource(src, body), mayRestart).then(function (d) {
                    input.dataset.prev = sent;
                    // Tout de suite, avant le rechargement : « Afficher le mot de
                    // passe » remontrait sinon l'ancienne adresse.
                    Object.assign(src.data, d.source || {});
                    if (d.source && !('peak_ratio' in d.source)) delete src.data.peak_ratio;   // revenu au global
                    if (name === 'auto_volume') {
                        var vol = card.querySelector('[data-field="volume"]');
                        if (vol) vol.disabled = input.checked;
                    }
                    if (['name', 'url', 'device'].indexOf(name) !== -1) {
                        return CT.refresh().then(function () { CT.markSaved(document.getElementById(fieldId) || input); });
                    }
                    CT.markSaved(input);
                }).catch(function (err) {
                    var current = input.type === 'checkbox' ? String(input.checked) : input.value;
                    if (current === sent) {   // sinon une valeur plus recente part juste apres
                        if (input.type === 'checkbox') input.checked = input.dataset.prev === 'true';
                        else input.value = input.dataset.prev;
                        if (out) input.dispatchEvent(new Event('input'));
                    }
                    CT.error('Non enregistré : ' + err.message);
                });
            }
        });

        var dlg = card.querySelector('dialog.source-settings');
        dlg.addEventListener('close', function () {
            CT.flushPendingSaves();
            var toasts = document.getElementById('toasts');
            if (toasts && dlg.contains(toasts)) document.body.appendChild(toasts);
            if (dlg._silent) { dlg._silent = false; return; }
            CT.state.openPanels[card.id] = false;
            var opener = card.querySelector('.source-settings-open');
            if (opener) opener.focus();
            // Carte a jour (groupes, « Aucun son choisi ») : les rendus etaient
            // reportes pendant la fenetre.
            CT.renderWhenIdle();
        });
        // Clic sur le fond : hors du rectangle de la fenetre (un clic dans sa
        // marge, ou un curseur relache dehors, vise aussi l'element <dialog>).
        dlg.addEventListener('click', function (e) {
            if (e.target !== dlg || !e.clientX && !e.clientY) return;
            var r = dlg.getBoundingClientRect();
            if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) closeSettings(card);
        });
    }

    // Ouvre les reglages d'une source ; `groupSlug` : aller a ce groupe et
    // donner le focus a « Ajouter les sons entendus » (groupe vide, fin de
    // l'assistant).
    CT.openSourceSettings = function (domId, groupSlug) {
        var card = document.getElementById(domId);
        var src = CT.sourceByDomId(domId);
        if (card && src) openSettings(card, src, false, groupSlug);
    };

    function openSettings(card, src, rerender, groupSlug) {
        var dlg = card.querySelector('dialog.source-settings');
        if (!dlg) return;
        if (!rerender && CT.resetChipOrder && !dlg.open) {
            // Nouvel affichage : nouveau tri, applique tout de suite (sinon il se
            // faisait au premier clic et la pastille cochee se deplacait).
            CT.resetChipOrder(src.domId);
            if (CT.renderGroupsManage) CT.renderGroupsManage(card, src);
        }
        if (dlg.open) return focusGroup(dlg, groupSlug);
        if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
        CT.state.openPanels[card.id] = true;
        // Messages au premier plan, lisibles et annonces (le reste de la page
        // est inerte pendant la fenetre).
        var toasts = document.getElementById('toasts');
        if (toasts) dlg.appendChild(toasts);
        // Focus sur « Fermer » (et non sur un champ : le rendu de la grille
        // attendrait la fin de cette « saisie »).
        if (!rerender) dlg.querySelector('[data-action="close-settings"]').focus();
        if (!rerender && groupSlug) focusGroup(dlg, groupSlug);
        if (!rerender && src.kind === 'mic') {
            // Un micro branche apres l'ouverture de la page apparait : liste
            // mise a jour dans le menu (la grille, elle, attend la fermeture).
            CT.reloadDevices().then(function () {
                var sel = card.querySelector('[data-field="device"]');
                var fresh = CT.sourceByDomId(card.id) || src;
                if (!sel || CT.isTyping()) return;
                var v = sel.value;
                sel.innerHTML = CT.deviceOptions(fresh.data.audio_source);
                if (CT.$$('option', sel).some(function (o) { return o.value === v; })) sel.value = v;
                sel.dataset.prev = sel.value;   // valeur de retour en cas d'echec
            });
        }
    }

    // Reglages recharges pendant qu'une fenetre est ouverte : titre et groupes
    // refaits sur place depuis les nouveaux reglages (apres la saisie en cours).
    CT.onSettingsReloaded = function () {
        CT.$$('dialog.source-settings[open]').forEach(function (dlg) {
            var card = dlg.closest('.source-card');
            var fresh = card && CT.sourceByDomId(card.id);
            if (!fresh) return;
            var title = dlg.querySelector('.modal-head h2');
            if (title) title.textContent = 'Réglages · ' + fresh.name;
            // Meme cle que les rendus de groups.js : un seul rendu apres la saisie.
            CT.whenIdle('groups:' + card.id, CT.isTyping, function () {
                var now = CT.sourceByDomId(card.id);
                if (now && dlg.open && CT.renderGroupsManage) CT.renderGroupsManage(card, now);
            });
        });
    };

    function focusGroup(dlg, groupSlug) {
        var group = groupSlug && dlg.querySelector('.group-card[data-slug="' + CT.cssEscape(groupSlug) + '"]');
        if (!group) return;
        if (group.scrollIntoView) group.scrollIntoView({block: 'start'});
        var target = group.querySelector('[data-role="auto-add"]');
        if (target) target.focus();
    }

    function closeSettings(card) {
        var dlg = card.querySelector('dialog.source-settings');
        if (!dlg || !dlg.open) return;
        if (typeof dlg.close === 'function') dlg.close();
        else { dlg.removeAttribute('open'); dlg.dispatchEvent(new Event('close')); }
    }

    function deleteSource(src) {
        CT.confirm('Supprimer « ' + src.name + ' » ? Ses entités Home Assistant seront retirées.').then(function (ok) {
            if (!ok) return;
            var req = src.kind === 'mic' ? CT.api('DELETE', '/api/microphone')
                : src.kind === 'rtsp' ? CT.api('DELETE', '/api/rtsp/stream/' + encodeURIComponent(src.key))
                : CT.api('DELETE', '/api/vban/' + encodeURIComponent(src.key));
            if (CT.state.testing && CT.state.testing.domId === src.domId) CT.stopTest();
            refreshAfter(CT.withRestart(req, src.enabled), 'Source supprimée').then(function () {
                // Focus sur l'ajout (la carte n'existe plus) plutot que perdu.
                var add = document.getElementById('add-source');
                (add && !add.hidden ? add : document.getElementById('empty-add')).focus();
            }).catch(function (err) { CT.error('Suppression impossible : ' + err.message); });
        });
    }

    function testWebhook(card, src) {
        var input = card.querySelector('[data-field="webhook_url"]');
        var url = input.value.trim();
        if (!url) { CT.error("Saisissez d'abord l'adresse du webhook."); input.focus(); return; }
        CT.api('POST', '/api/webhook/test', {url: url, source: src.sourceId})
            .then(function () { CT.success('Webhook de test envoyé et accepté'); })
            .catch(function (err) { CT.error('Test du webhook échoué : ' + err.message); });
    }

    // ---- Tests et VU-metre -----------------------------------------------------
    function toggleTest(src, card) {
        if (CT.state.testing && CT.state.testing.domId === src.domId) { CT.stopTest(); return; }
        var url = card.querySelector('[data-field="url"]');
        var gain = card.querySelector('[data-field="gain"]');
        var u = url && realUrl(url, src);
        CT.startTest(CT.testFor(src, u, gain && parseInt(gain.value, 10)))
            .catch(function (err) { CT.error('Test impossible : ' + err.message); });
    }
    CT.onTestChange = function (test, on) {
        CT.$$('.source-card').forEach(function (card) {
            var mine = on && card.id === test.domId;
            card.classList.toggle('is-testing', mine);
            CT.$$('[data-action="test"]', card).forEach(function (b) {
                b.textContent = mine ? 'Arrêter le test' : 'Tester le son';
            });
            if (!mine && card.id === test.domId) setMeter(card, null);
        });
    };
    CT.onTestLevel = function (test, data) {
        var card = document.getElementById(test.domId);
        if (card) setMeter(card, data.db, 'Test');
    };
    function setMeter(card, db, prefix) {
        // Carte et fenetre de reglages (gain regle en regardant le niveau).
        CT.$$('[data-role="meter"]', card).forEach(function (m) {
            var fill = m.querySelector('.meter-fill'), label = m.querySelector('[data-role="meter-label"]');
            if (db == null) { CT.setMeterFill(fill, 0); label.textContent = '—'; return; }
            CT.setMeterFill(fill, CT.dbToPct(db));
            label.textContent = (prefix ? prefix + ' · ' : '') + Math.round(db) + ' dB';
        });
    }

    // Un seul ecouteur global ferme les menus ouverts (pas un par rendu de carte).
    document.addEventListener('click', function (e) { closeMenus(e.target); });

    // ---- Temps reel pendant la detection ----------------------------------------
    // Index sourceId -> {src, card, rows} construit a chaque rendu : les
    // evenements en direct (~5/s par source) reconstruisaient toute la liste
    // des sources et relancaient des querySelector.
    var cardIndex = {};
    // Taille des courbes mise en cache (lire clientWidth a chaque dessin, 5 fois
    // par seconde et par ligne, forcait une mise en page) et cartes hors ecran
    // non dessinees, redessinees quand elles reapparaissent.
    var sparkSize = new WeakMap();
    var resized = [];
    var resizeObs = window.ResizeObserver && new ResizeObserver(function (entries) {
        entries.forEach(function (e) {
            var before = sparkSize.get(e.target), w = e.contentRect.width, h = e.contentRect.height;
            sparkSize.set(e.target, {w: w, h: h});
            // 1re mesure (observe() a chaque rendu, qui vient de dessiner) :
            // rien a refaire. Ensuite, seule la carte redimensionnee.
            if (before && (before.w !== w || before.h !== h)) {
                var card = e.target.closest('.source-card');
                if (card && resized.indexOf(card) === -1) resized.push(card);
            }
        });
        if (resized.length === 1) requestAnimationFrame(function () {
            var cards = resized; resized = [];
            if (!liveVisible()) return;
            var now = Date.now() / 1000;
            cards.forEach(function (card) {
                var c = cardIndex[card.getAttribute('data-source-id')];
                if (c && c.card === card && c.onScreen) redrawCard(c, now);
            });
        });
    });
    var onScreenObs = window.IntersectionObserver && new IntersectionObserver(function (entries) {
        entries.forEach(function (e) {
            var c = cardIndex[e.target.getAttribute('data-source-id')];
            if (!c || c.card !== e.target) return;
            c.onScreen = e.isIntersecting;
            if (c.onScreen && liveVisible()) redrawCard(c, Date.now() / 1000);
        });
    });
    function indexCards(sources) {
        cardIndex = {};
        if (resizeObs) resizeObs.disconnect();
        if (onScreenObs) onScreenObs.disconnect();
        sources.forEach(function (src) {
            var card = document.getElementById(src.domId);
            if (!card) return;
            var rows = {};
            CT.$$('.group-live', card).forEach(function (row) {
                rows[row.getAttribute('data-slug')] = row;
                var canvas = row.querySelector('canvas');
                if (canvas && resizeObs) resizeObs.observe(canvas);
            });
            card.setAttribute('data-source-id', src.sourceId);
            cardIndex[src.sourceId] = {src: src, card: card, rows: rows, onScreen: true};
            if (onScreenObs) onScreenObs.observe(card);
        });
    }
    // Rien a dessiner si la page est cachee ou l'onglet Reglages affiche ; les
    // courbes sont redessinees depuis l'etat au retour.
    function liveVisible() {
        var panel = document.getElementById('panel-listen');
        return !document.hidden && !(panel && panel.hidden);
    }
    function redrawCard(c, now) {
        var live = (CT.state.live[c.src.sourceId] || {scores: {}}).scores;
        Object.keys(c.rows).forEach(function (slug) {
            var row = c.rows[slug], series = live[slug] || [];
            var threshold = parseFloat(row.querySelector('.threshold').value);
            setScore(row, series.length ? series[series.length - 1][1] : 0, threshold);
            drawSpark(row.querySelector('canvas'), series, threshold, now);
        });
    }
    CT.redrawLive = function () {
        var now = Date.now() / 1000;
        Object.keys(cardIndex).forEach(function (sid) {
            if (cardIndex[sid].onScreen) redrawCard(cardIndex[sid], now);
        });
    };
    document.addEventListener('visibilitychange', function () { if (!document.hidden) CT.redrawLive(); });

    CT.on('source_level', function (d) {
        var c = d && cardIndex[d.source_id];
        if (!c || !c.card) return;
        if (CT.state.testing && CT.state.testing.domId === c.src.domId) return;
        if (liveVisible()) setMeter(c.card, d.db);
    });

    CT.on('group_scores', function (d) {
        var c = d && cardIndex[d.source_id];
        if (!c || !c.card) return;
        var live = CT.state.live[d.source_id] = CT.state.live[d.source_id] || {scores: {}};
        var now = Date.now() / 1000, visible = liveVisible();
        Object.keys(d.scores || {}).forEach(function (slug) {
            var score = d.scores[slug];
            var series = live.scores[slug] = (live.scores[slug] || []).filter(function (p) { return now - p[0] <= SPARK_SECONDS; });
            series.push([now, score]);
            var row = c.rows[slug];
            if (!row || !visible || !c.onScreen) return;
            var threshold = parseFloat(row.querySelector('.threshold').value);
            setScore(row, score, threshold);
            drawSpark(row.querySelector('canvas'), series, threshold, now);
        });
    });

    function setScore(row, score, threshold) {
        row.querySelector('.group-live-score').textContent = CT.pct(score);
        var fill = row.querySelector('.scorebar-fill');
        fill.style.width = Math.min(100, score * 100) + '%';
        fill.classList.toggle('is-over', score >= threshold);
    }

    // Detection arretee : les dernieres valeurs ne sont plus d'actualite.
    CT.resetLive = function () {
        CT.state.live = {};
        CT.state.sourceStatus = {};
        CT.state.sourceErrors = {};
        CT.redrawLive();
        Object.keys(cardIndex).forEach(function (sid) {
            var card = cardIndex[sid].card;
            var line = card.querySelector('[data-role="live"]');
            if (line) line.textContent = idleText(!card.classList.contains('is-disabled'));
        });
    };

    // Couleurs lues une fois (getComputedStyle a chaque dessin forcait un
    // recalcul de style), relues quand le theme change.
    var sparkColors = null;
    function colors() {
        if (!sparkColors) {
            var css = getComputedStyle(document.documentElement);
            sparkColors = {accent: css.getPropertyValue('--accent').trim() || '#0d7c6b',
                           line: css.getPropertyValue('--ink-3').trim() || '#888'};
        }
        return sparkColors;
    }
    // data-theme suit aussi le theme du systeme (script de index.html).
    if (window.MutationObserver) {
        new MutationObserver(function () { sparkColors = null; CT.redrawLive(); })
            .observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme']});
    }
    function drawSpark(canvas, series, threshold, now) {
        var ctx = canvas && canvas.getContext && canvas.getContext('2d');
        if (!ctx) return;
        // Taille reelle x densite de l'ecran : un canevas fixe de 320x40 etire
        // en largeur etait flou et deforme.
        var dpr = window.devicePixelRatio || 1;
        var size = sparkSize.get(canvas);
        if (!size || !size.w) size = {w: canvas.clientWidth, h: canvas.clientHeight};   // avant la 1re mesure
        var cw = Math.round((size.w || 320) * dpr), ch = Math.round((size.h || 40) * dpr);
        if (canvas.width !== cw || canvas.height !== ch) { canvas.width = cw; canvas.height = ch; }
        var w = canvas.width, h = canvas.height, c = colors();
        var accent = c.accent, line = c.line;
        ctx.lineWidth = dpr;
        ctx.clearRect(0, 0, w, h);
        var ty = h - threshold * (h - 4) - 2;
        ctx.strokeStyle = line;
        ctx.setLineDash([4 * dpr, 4 * dpr]);
        ctx.beginPath(); ctx.moveTo(0, ty); ctx.lineTo(w, ty); ctx.stroke();
        ctx.setLineDash([]);
        if (!series.length) return;
        var x = function (t) { return w - ((now - t) / SPARK_SECONDS) * w; };
        var y = function (s) { return h - s * (h - 4) - 2; };
        ctx.beginPath();
        series.forEach(function (p, i) { i ? ctx.lineTo(x(p[0]), y(p[1])) : ctx.moveTo(x(p[0]), y(p[1])); });
        ctx.strokeStyle = accent;
        ctx.lineWidth = 1.5 * dpr;
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
        var c = d && cardIndex[d.source];
        if (!c || !c.card || !Array.isArray(d.detected)) return;
        var line = c.card.querySelector('[data-role="live"]');
        if (line.dataset.clapUntil && Date.now() < +line.dataset.clapUntil) return;
        line.textContent = d.detected.slice(0, 3).map(function (l) { return CT.soundLabel(l.label) + ' ' + CT.pct(l.score); }).join(' · ');
        // Le serveur n'envoie rien en silence : sans nouvelle, revenir a
        // « En attente de sons… » (le dernier son restait affiche des minutes).
        scheduleIdle(line, LIVE_IDLE_MS);
    });

    // Retour au texte d'attente apres `delay` ms sans nouveau son ; repousse
    // apres l'affichage d'un clap (il restait sinon affiche indefiniment).
    function scheduleIdle(line, delay) {
        clearTimeout(line._idle);
        line._idle = setTimeout(function () {
            var clapLeft = line.dataset.clapUntil ? +line.dataset.clapUntil - Date.now() : 0;
            if (clapLeft > 0) return scheduleIdle(line, clapLeft + 50);
            line.textContent = idleText(true);
        }, delay);
    }

    // Mode alerte : mention sur la carte et le groupe tant que le son dure.
    CT.on('alert', function (d) {
        var c = d && cardIndex[d.source_id];
        if (!c || !c.card) return;
        var on = d.alert === 'on';
        var line = c.card.querySelector('[data-role="live"]');
        if (on) {
            line.textContent = '🚨 ' + (d.group_name || d.group_slug) + ' détecté (' + CT.pct(d.score) + ')';
            CT.announce((d.group_name || d.group_slug) + ' détecté sur ' + c.src.name, true);
        } else if (/^🚨/.test(line.textContent)) {
            line.textContent = idleText(true);
        }
        var row = c.card.querySelector('.group-live[data-slug="' + CT.cssEscape(d.group_slug) + '"]');
        var badge = row && row.querySelector('.clap-badge');
        if (badge) {
            clearTimeout(badge._t);
            badge.textContent = 'Alerte';
            badge.hidden = !on;
        }
    });

    CT.on('clap', function (d) {
        var c = d && cardIndex[d.source_id];
        if (!c || !c.card) return;
        var n = d.clap_count || 1;
        var text = n + ' clap' + (n > 1 ? 's' : '');
        if (!d.ignored) {
            var line = c.card.querySelector('[data-role="live"]');
            line.textContent = '👏 ' + text + ' · ' + (d.group_name || d.group_slug) + ' (' + CT.pct(d.score) + ')';
            line.dataset.clapUntil = String(Date.now() + 2500);
            scheduleIdle(line, 2600);
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

    CT.on('source_status', function (d) {
        if (!d || !d.source_id) return;
        CT.state.sourceStatus[d.source_id] = d.status;
        if (d.status === 'error' && d.error) CT.state.sourceErrors[d.source_id] = d.error;
        else if (d.status === 'connected' || d.status === 'connecting') delete CT.state.sourceErrors[d.source_id];
        if (CT.renderStatus) CT.renderStatus(); else CT.renderSourceStatuses();
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
