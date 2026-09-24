/* Parcours complet de l'interface dans jsdom : chaque fonctionnalite appelle
 * la bonne route avec le bon corps et affiche le bon resultat.
 * Usage : python3 render_page.py page.html && node ui_test.js page.html */
const { JSDOM } = require('jsdom');
const fs = require('fs');
const html = fs.readFileSync(process.argv[2] || __dirname + '/page.html', 'utf8');
const base = JSON.parse(fs.readFileSync(__dirname + '/settings_ui.json', 'utf8'));
let settings = JSON.parse(JSON.stringify(base));
const calls = [], handlers = {}, errors = [], results = [];
let status = { running: false, source: null, sources: [] };
const ok = (name, cond) => results.push([cond ? 'OK ' : 'KO ', name]);
const dom = new JSDOM(html, {
  runScripts: 'dangerously', url: 'http://localhost/', pretendToBeVisual: true,
  beforeParse(w) {
    w.io = () => ({ on: (ev, fn) => { (handlers[ev] = handlers[ev] || []).push(fn); }, emit() {} });
    w.CSS = { escape: s => String(s).replace(/["\\]/g, '\\$&') };
    w.fetch = (url, opts = {}) => {
      const method = opts.method || 'GET';
      const body = opts.body && typeof opts.body === 'string' ? JSON.parse(opts.body) : opts.body;
      calls.push([method, url, body]);
      let res = { success: true };
      if (url === '/status') res = status;
      else if (url === '/api/settings') res = JSON.parse(JSON.stringify(settings));
      else if (url === '/api/detections/history' && method === 'GET') res = [{ source_id: 'mic', timestamp: 1e9, score: 0.8, clap_count: 2, group_name: 'Clap', labels: [] }];
      else if (url === '/api/sound_exclusions' && method === 'GET') res = { excluded: ['Speech'], available: ['Knock', 'Dog'] };
      else if (url === '/refresh_vban_sources') res = { sources: [{ name: 'PC2', ip: '10.0.0.9', sample_rate: 48000, channels: 2 }] };
      else if (url === '/api/rtsp/stream' && method === 'POST') { const s = { id: 'new-1', name: body.name, url: body.url, enabled: true, sound_groups: [{ slug: 'clap', name: 'Clap', ha_entities: [1], sound_whitelist: {} }] }; settings.rtsp_sources.push(s); res = { success: true, stream: s }; }
      else if (url === '/api/vban/save') { const s = Object.assign({ sound_groups: [] }, body); settings.saved_vban_sources.push(s); res = { success: true, source: s }; }
      else if (url === '/api/detection/start') status = { running: true, source: 'x', since: Date.now() / 1000 - 125, sources: ['mic', 'rtsp_abc-123'] };
      else if (url === '/api/detection/stop') status = { running: false, sources: [] };
      else if (url.startsWith('/api/sources/') && method === 'PATCH') res = { success: true, source: {}, ...(w.__restart ? { restart: w.__restart } : {}) };
      else if (url === '/api/microphone' && method === 'DELETE') { settings.microphone.configured = false; }
      else if (url.startsWith('/api/rtsp/stream/') && method === 'DELETE') { settings.rtsp_sources = settings.rtsp_sources.filter(s => !url.endsWith(s.id)); }
      else if (url === '/api/source/sound_groups' && method === 'POST') { settings.microphone.sound_groups.push({ slug: 'snap', name: body.name, ha_entities: [1], sound_whitelist: {} }); }
      else if (url === '/api/ha/entity-ids') res = { mic: { clap: ['binary_sensor.claptrap_mic_clap_1clap', 'binary_sensor.claptrap_mic_clap_2claps'] } };
      else if (url === '/api/ha/cleanup') res = { success: true, message: '2 entité(s) orpheline(s) supprimée(s).' };
      if (url.includes('FAIL')) return Promise.resolve({ ok: false, status: 400, json: () => Promise.resolve({ error: 'refusé' }) });
      const reply = { ok: true, status: 200, json: () => Promise.resolve(res) };
      if (w.__hold && w.__hold.url === url) return new Promise(r => { w.__hold.release = () => r(reply); });
      return Promise.resolve(reply);
    };
    w.confirm = () => true;
    w.addEventListener('error', e => errors.push(e.message));
    w.HTMLCanvasElement.prototype.getContext = () => null;
  }
});
const w = dom.window, d = w.document, $ = s => d.querySelector(s), $$ = s => [...d.querySelectorAll(s)];
const tick = (n = 1) => new Promise(r => setTimeout(r, 25 * n));
const emit = (ev, data) => (handlers[ev] || []).forEach(f => f(data));
const change = (el, v) => { if (v !== undefined) { if (el.type === 'checkbox') el.checked = v; else el.value = v; } el.dispatchEvent(new w.Event('change', { bubbles: true })); };
const lastCall = (m, u) => calls.filter(c => c[0] === m && (typeof u === 'string' ? c[1] === u : u.test(c[1]))).pop();
(async () => {
  await tick(4);
  // 8 / 21 : toutes les sources visibles
  ok('8. toutes les sources visibles en cartes', $$('.source-card').length === 3 && !!$('#add-source'));
  // 1 : demarrer / arreter
  $('#toggle-detection').click(); await tick(3);
  ok('1. Démarrer (corps {})', JSON.stringify(lastCall('POST', '/api/detection/start')[2]) === '{}' && $('#status-title').textContent === 'Détection en cours' && /2 sources · depuis 2 min/.test($('#status-detail').textContent));
  // 2 : auto-start
  change($('#auto-start'), true); await tick();
  ok('2. démarrage automatique', lastCall('PUT', '/api/microphone/auto-start')[2].enabled === true);
  // 22 : statut RTSP
  emit('source_status', { source_id: 'rtsp_abc-123', status: 'reconnecting' }); await tick();
  ok('22. statut RTSP sur la carte', /reconnexion/i.test($('#src-rtsp-abc_123 .status-text').textContent));
  ok('    micro « À l\'écoute »', $('#src-mic .status-text').textContent === "À l'écoute");
  // nouveau : VU permanent + score/seuil
  emit('source_level', { source_id: 'mic', db: -20 }); emit('group_scores', { source_id: 'mic', scores: { clap: 0.7, toc: 0.1 } }); await tick();
  ok('+  VU-mètre en détection', $('#src-mic .meter-fill').dataset.pct !== '0' && /-20 dB/.test($('#src-mic [data-role="meter-label"]').textContent));
  ok('+  score du groupe face au seuil', $('#src-mic .group-live[data-slug="clap"] .group-live-score').textContent === '70 %' && $('#src-mic .group-live[data-slug="clap"] .scorebar-fill').classList.contains('is-over'));
  // 3 / 9 : labels + clap
  emit('labels', { source: 'mic', detected: [{ label: 'Clapping', score: 0.8 }] }); await tick();
  ok('3/9. sons en direct sur la carte', /Applaudissement 80 %/.test($('#src-mic [data-role="live"]').textContent));
  emit('clap', { source_id: 'mic', clap_count: 2, score: 0.81, group_slug: 'clap', group_name: 'Clap', ignored: false, timestamp: 2e9 }); await tick(); const clapAnnounce = $('#announcer').textContent;
  ok('3.  clap : badge + ligne live', !$('#src-mic .group-live[data-slug="clap"] .clap-badge').hidden && /2 claps/.test($('#src-mic [data-role="live"]').textContent));
  // 4 : historique
  ok('4. historique chargé + nouveau clap', $$('#history-list li').length === 2 && $('#history-empty').hidden);
  $('#history-clear').click(); await tick(); $('.modal [data-r="ok"]').click(); await tick(3);
  ok('4. effacer l\'historique (serveur)', !!lastCall('DELETE', '/api/detections/history') && $$('#history-list li').length === 0);
  // seuil en direct
  const thr = $('#src-mic .group-live[data-slug="clap"] .threshold'); change(thr, '0.65'); await tick(30);
  ok('15. seuil de confiance depuis la carte', lastCall('PUT', '/api/source/sound_groups')[2].threshold === 0.65);
  // 10 : activer / desactiver
  change($('#src-rtsp-abc_123 [data-action="toggle"]'), false); await tick(3);
  ok('10. activer/désactiver (API unifiée)', lastCall('PATCH', /\/api\/sources\/rtsp\/abc-123/)[2].enabled === false);
  // 11 : micro
  $('#src-mic details').open = true;
  change($('#src-mic [data-field="device"]'), 'dev:1'); await tick(3);
  ok('11. choix du micro (device seul)', JSON.stringify(lastCall('PATCH', '/api/sources/mic/mic')[2]) === JSON.stringify({ device: { index: 5, name: 'A|B', pulse_name: 'x' } }));
  change($('#src-mic [data-field="volume"]'), '90'); await tick();
  ok('11. volume', lastCall('PATCH', '/api/sources/mic/mic')[2].volume === 90);
  change($('#src-mic [data-field="auto_volume"]'), true); await tick();
  ok('11. volume automatique', lastCall('PATCH', '/api/sources/mic/mic')[2].auto_volume === true && $('#src-mic [data-field="volume"]').disabled);
  emit('auto_volume_update', { volume: 120 }); await tick();
  ok('19. mise à jour du volume auto', $('#src-mic [data-out="volume"]').textContent === '120 %');
  // test micro
  $('#src-mic [data-action="test"]').click(); await tick();
  emit('mic_level', { db: -12 }); await tick();
  ok('11. test du micro + VU', !!lastCall('POST', '/api/mic/test/start') && /Test · -12 dB/.test($('#src-mic [data-role="meter-label"]').textContent));
  // 12 : rtsp
  change($('#src-rtsp-abc_123 [data-field="url"]'), 'rtsp://cam/2'); await tick(3);
  ok('12. URL RTSP', lastCall('PATCH', /rtsp\/abc-123/)[2].url === 'rtsp://cam/2');
  change($('#src-rtsp-abc_123 [data-field="gain"]'), '20'); await tick();
  ok('12. gain RTSP', lastCall('PATCH', /rtsp\/abc-123/)[2].gain === 20);
  $('#src-rtsp-abc_123 [data-action="test"]').click(); await tick();
  ok('12. test RTSP (remplace le test micro)', lastCall('POST', '/api/rtsp/test/start')[2].id === 'abc-123');
  change($('#src-rtsp-abc_123 [data-field="name"]'), 'Entrée'); await tick(3);
  ok('10. renommer la caméra', lastCall('PATCH', /rtsp\/abc-123/)[2].name === 'Entrée');
  // 13 : vban
  const vb = $$('.source-card').find(c => c.id.startsWith('src-vban'));
  ok('10. badge multicast VBAN', /multicast/.test(vb.textContent));
  change(vb.querySelector('[data-field="gain"]'), '4'); await tick();
  ok('13. gain VBAN (ip/nom)', lastCall('PATCH', /\/api\/sources\/vban\/vb-1$/)[2].gain === 4);
  vb.querySelector('[data-action="test"]').click(); await tick();
  ok('13. test VBAN (par id)', JSON.stringify(lastCall('POST', '/api/vban/test/start')[2]) === JSON.stringify({ id: 'vb-1' }));
  // 14 : entites + copier
  const ent = $$('#src-mic .entity-list code').map(c => c.textContent);
  ok('14. entités HA par groupe', ent.includes('binary_sensor.claptrap_mic_clap_1clap') && ent.includes('binary_sensor.claptrap_mic_clap_2claps'));
  // 15 : groupes
  const clapCard = $('#src-mic .group-card[data-slug="clap"]');
  change(clapCard.querySelector('[data-clap="3"]'), true); await tick();
  ok('15. nombres de claps', JSON.stringify(lastCall('PUT', '/api/source/sound_groups')[2].ha_entities) === '[1,2,3]');
  const knockChip = $('#src-mic .group-card[data-slug="clap"] .chip input[data-label="Knock"]');
  ok('15. exclusivité (son actif ailleurs grisé)', knockChip.disabled);
  change($('#src-mic .group-card[data-slug="clap"] .chip input[data-label="Speech"]'), true); await tick();
  ok('15. cocher un son', lastCall('PUT', '/api/source/sound_whitelist')[2].label === 'Speech');
  const nameInput = $('#src-mic .group-card[data-slug="toc"] .group-name'); change(nameInput, 'Toc table'); await tick();
  ok('15. renommer un groupe', lastCall('PUT', '/api/source/sound_groups')[2].name === 'Toc table');
  ok('15. groupe principal non supprimable', !$('#src-mic .group-card[data-slug="clap"] [data-group-action="delete"]') && !!$('#src-mic .group-card[data-slug="toc"] [data-group-action="delete"]'));
  $('#src-mic .group-card[data-slug="clap"] [data-group-action="cleanup"]').click(); await tick(3);
  ok('15. retirer les sons non cochés', !!lastCall('POST', '/api/source/sound_whitelist/cleanup'));
  $('#src-mic [data-action="add-group"]').click(); await tick();
  $('.modal #dlg-input').value = 'Snap'; $('.modal [data-r="ok"]').click(); await tick(4);
  ok('15. nouveau groupe (sans rechargement)', lastCall('POST', '/api/source/sound_groups')[2].name === 'Snap' && !!$('#src-mic .group-live[data-slug="snap"]'));
  emit('sound_seen', { source_id: 'mic', label: 'Dog' }); await tick();
  ok('15. auto-découverte d\'un son', !!$('#src-mic .chip input[data-label="Dog"]'));
  $('#src-mic .group-card[data-slug="toc"] [data-group-action="delete"]').click(); await tick();
  $('.modal [data-r="ok"]').click(); await tick(3);
  ok('15. supprimer un groupe', lastCall('DELETE', '/api/source/sound_groups')[2].group_slug === 'toc');
  // 16 : webhook
  const wh = $('#src-mic [data-field="webhook_url"]'); change(wh, 'http://homeassistant:8123/api/webhook/x'); await tick();
  ok('16. webhook enregistré', lastCall('PATCH', '/api/sources/mic/mic')[2].webhook_url === 'http://homeassistant:8123/api/webhook/x');
  $('#src-mic [data-action="test-webhook"]').click(); await tick();
  ok('16. tester le webhook', lastCall('POST', '/api/webhook/test')[2].url === 'http://homeassistant:8123/api/webhook/x');
  // 10 : suppression avec confirmation
  $('#src-rtsp-abc_123 [data-action="delete"]').click(); await tick();
  ok('10. confirmation de suppression', /Supprimer « /.test($('.modal-message').textContent));
  $('.modal [data-r="ok"]').click(); await tick(4);
  ok('10. suppression (serveur)', !!lastCall('DELETE', '/api/rtsp/stream/abc-123') && !$('#src-rtsp-abc_123'));
  // 7 : assistant
  $('#add-source').click(); await tick();
  ok('7. assistant : micro déjà ajouté grisé', $('.choice[data-kind="mic"]').disabled);
  $('.choice[data-kind="rtsp"]').click(); await tick();
  $('#wz-url').value = 'rtsp://cam/new'; $('[data-w="ok"]').click(); await tick(5);
  ok('7. assistant : caméra ajoutée + étape vérification', lastCall('POST', '/api/rtsp/stream')[2].url === 'rtsp://cam/new' && $('#wizard-title').textContent === 'Vérifier le son' && !!lastCall('POST', '/api/rtsp/test/start'));
  emit('rtsp_level', { id: 'new-1', db: -10 }); await tick();
  ok('7. assistant : VU-mètre de vérification', /-10 dB/.test($('#wz-meter').textContent));
  $('[data-w="done"]').click(); await tick(4);
  ok('7. assistant fermé, carte affichée', $('#wizard').hidden && !!$('#src-rtsp-new_1'));
  $('#add-source').click(); await tick(); $('.choice[data-kind="vban"]').click(); await tick(3);
  ok('7. assistant VBAN : flux découverts', /PC2/.test($('#wz-vban-list').textContent));
  $('.vban-item').click(); await tick(5);
  ok('7. assistant VBAN : ajout', lastCall('POST', '/api/vban/save')[2].name === 'PC2');
  $('[data-w="done"]').click(); await tick(3);
  $('#add-source').click(); await tick(); $('.choice[data-kind="vban"]').click(); await tick(2);
  $('#wz-v-name').value = 'Manuel'; $('#wz-v-ip').value = '192.168.1.50'; $('#wz-v-add').click(); await tick(5);
  ok('7. assistant VBAN : ajout manuel', lastCall('POST', '/api/vban/save')[2].ip === '192.168.1.50');
  d.querySelector('.modal').dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); await tick(2);
  ok('+  Échap ferme l\'assistant', $('#wizard').hidden);
  // Onglet reglages : 5, 6, 17
  $('#tab-settings').click(); await tick(3);
  ok('5. réglages avancés affichés', $('#adv-delay').value !== '' && !$('#panel-settings').hidden);
  change($('#adv-ratio'), '4'); await tick();
  ok('5. réglage avancé enregistré', lastCall('PUT', '/api/settings/advanced')[2].peak_ratio === 4);
  $('#adv-defaults').click(); await tick();
  ok('5. valeurs par défaut', lastCall('PUT', '/api/settings/advanced')[2].delay === 1.5);
  ok('6. exclusions chargées', /Parole/.test($('#excluded-list').textContent) && /Chien/.test($('#available-list').textContent));
  $('#available-list [data-exclude="Dog"]').click(); await tick();
  ok('6. exclure un son', lastCall('PUT', '/api/sound_exclusions')[2].excluded === true);
  $('#excluded-list [data-unexclude="Speech"]').click(); await tick();
  ok('6. ne plus exclure', lastCall('PUT', '/api/sound_exclusions')[2].excluded === false);
  change($('#debug-toggle'), true); await tick();
  ok('17. journal détaillé', lastCall('PUT', '/api/settings/debug')[2].enabled === true);
  $('#ha-cleanup').click(); await tick();
  ok('+  nettoyage des entités orphelines', !!lastCall('POST', '/api/ha/cleanup'));
  ok('17. export / import présents', !!$('#export-config') && !!$('#import-config'));
  // 20 : statut + resync
  status = { running: false, sources: [] }; emit('detection_status', { status: 'stopped' }); await tick(3);
  ok('20. arrêt reçu du serveur', $('#status-title').textContent === 'Détection arrêtée');
  emit('connect'); emit('connect'); await tick(3);
  ok('20. resynchronisation à la reconnexion', calls.filter(c => c[1] === '/api/settings').length >= 1);
  // 18 : messages
  ok('18. messages (toasts aria-live)', $$('#toasts .toast').length > 0);
  // micro : suppression + re-ajout via assistant
  $('#tab-listen').click(); await tick();
  $('#src-mic [data-action="delete"]').click(); await tick(); $('.modal [data-r="ok"]').click(); await tick(4);
  ok('10. supprimer le micro (serveur)', !!lastCall('DELETE', '/api/microphone') && !$('#src-mic'));
  // --- lot interface 6.36
  ok('+  annonce des claps (lecteurs d\'écran)', /claps? détectés? sur/.test(clapAnnounce));
  ok('+  plus d\'aria-live sur la ligne en direct', !$$('[data-role="live"][aria-live]').length);
  ok('+  h1 présent', !!$('h1'));
  // --- lot interface 6.42
  ok("+  zone d'état sans aria-live", !$('.status[aria-live]') && !$('.status[role="status"]'));
  ok('+  export sans secrets', !!$('#export-shareable'));
  ok('+  poignée annoncée en clair (h1 gardé sur mobile)', !!$('h1.brand'));
  // assistant ferme pendant l'ajout d'une camera
  w.__hold = { url: '/api/rtsp/stream' };
  $('#add-source').click(); await tick();
  $('.choice[data-kind="rtsp"]').click(); await tick();
  $('#wz-url').value = 'rtsp://cam/late'; $('[data-w="ok"]').click(); await tick();
  const busy = $('[data-w="ok"]').textContent;
  $('[data-w="close"]').click(); await tick(3);
  const testsBefore = calls.filter(c => /test\/start/.test(c[1])).length;
  w.__hold.release(); await tick(6);
  ok('+  assistant : « Ajout en cours… » pendant la requête', /Ajout en cours/.test(busy));
  ok('+  assistant fermé : aucun test lancé après coup', calls.filter(c => /test\/start/.test(c[1])).length === testsBefore && $('#wizard').hidden);
  ok('+  assistant fermé : grille rechargée', !!d.querySelector('[id^="src-rtsp-new"]'));
  // --- lot « simplifier l'usage » 6.44
  ok('+  vocabulaire : « Tester le son » / « Démarrer la détection »', !!d.querySelector('[data-action="test"]') && /Tester le son/.test(d.querySelector('[data-action="test"]').textContent) && /détection/i.test($('#toggle-detection').textContent));
  ok('+  seuil affiché en %', / %$/.test(d.querySelector('.threshold-value').textContent));
  ok('+  mot de passe RTSP masqué dans le champ', !d.querySelector('[data-field="url"][data-secret]') || !/:[^•@\/]+@/.test(d.querySelector('[data-field="url"][data-secret]').value));
  ok('+  bouton dans l\'accueil', !!$('#empty-add'));
  // --- lot interface 6.49
  const cw = dom.window, CT = cw.CT;
  const nameField = d.querySelector('[id$="-name"][data-field="name"]');
  nameField.focus(); CT.render();
  ok('+  focus restauré sur un champ (id)', d.activeElement && d.activeElement.id === nameField.id);
  const chip = d.querySelector('.group-card .chip');
  if (chip) { chip.focus(); CT.render(); }
  ok('+  focus restauré sur une puce de groupe', !chip || (d.activeElement && d.activeElement.classList.contains('chip')));
  const typing = d.querySelector('[data-field="webhook_url"]');
  typing.focus(); typing.value = 'http://en-cours';
  await CT.refresh(); await tick();
  ok('+  pas de rendu pendant une saisie', d.querySelector('[data-field="webhook_url"]') === typing && typing.value === 'http://en-cours');
  typing.blur(); typing.dispatchEvent(new cw.FocusEvent('focusout', { bubbles: true })); await tick(3);
  ok('+  rendu effectué après la saisie', d.querySelector('[data-field="webhook_url"]') !== typing);
  const toastsBefore = $$('#toasts .toast').length;
  status = { running: true, sources: ['mic'], since: Date.now() / 1000 };
  await CT.reloadStatus(); CT.renderStatus();
  w.__restart = null;
  await CT.withRestart(CT.patchSource({ kind: 'mic', key: 'mic' }, { enabled: true }));
  ok('+  pas de « redémarrée » si le serveur n\'a pas redémarré', !$$('#toasts .toast').slice(toastsBefore).some(t => /redémarrée/.test(t.textContent)));
  w.__restart = 'ok';
  await CT.withRestart(CT.patchSource({ kind: 'mic', key: 'mic' }, { enabled: true }));
  ok('+  « redémarrée » quand le serveur l\'indique', $$('#toasts .toast').some(t => /redémarrée/.test(t.textContent)));
  w.__restart = null;
  CT.state.sourceStatus['mic'] = 'error'; CT.renderStatus();
  ok('+  barre d\'état : source en erreur signalée', /en erreur/.test($('#status-detail').textContent) && $('#statusbar').classList.contains('is-degraded'));
  CT.state.sourceStatus = {};
  // Carte RTSP avec identifiants (rtsp://u:p@h/x dans settings_ui.json)
  CT.state.settings.rtsp_sources.push({ id: 'sec-1', name: 'Sec', url: 'rtsp://u:p@h/x', enabled: false, sound_groups: [] });
  CT.render();
  const rtspCard = d.querySelector('#src-rtsp-sec_1 [data-field="url"][data-secret]');
  ok('+  carte avec identifiants présente', !!rtspCard);
  if (rtspCard) {
    const card = rtspCard.closest('.source-card');
    ok('+  URL masquée en lecture seule', rtspCard.readOnly && /••••/.test(rtspCard.value));
    card.querySelector('[data-action="reveal-url"]').click();
    ok('+  « Afficher » révèle l\'adresse', !rtspCard.readOnly && /u:p@/.test(rtspCard.value));
  }
  ok('+  zone des messages audible', $('#toasts').getAttribute('aria-live') === 'polite');
  ok('JS : aucune erreur', errors.length === 0);
  results.forEach(r => console.log(r.join(' ')));
  if (errors.length) console.log(errors);
  const failures = results.filter(r => r[0] === 'KO ').length + (errors.length ? 1 : 0);
  console.log(failures + ' échec(s) sur ' + results.length);
  process.exit(failures ? 1 : 0);
})();
