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
    w.URL.createObjectURL = () => 'blob:extrait'; w.URL.revokeObjectURL = () => {};
    w.fetch = (url, opts = {}) => {
      const method = opts.method || 'GET';
      const body = opts.body && typeof opts.body === 'string' ? JSON.parse(opts.body) : opts.body;
      calls.push([method, url, body]);
      let res = { success: true };
      if (url === '/status') res = status;
      else if (url === '/api/settings') res = JSON.parse(JSON.stringify(settings));
      else if (url === '/api/settings/export?secrets=0') res = { rtsp_sources: [{ url: 'rtsp://***@cam/1' }], _shareable: true };
      else if (url === '/api/settings/export') res = { rtsp_sources: [{ url: 'rtsp://admin:secret@cam/1' }] };
      else if (url === '/api/detections/history' && method === 'GET') res = [{ source_id: 'mic', timestamp: 1e9, score: 0.8, clap_count: 2, group_name: 'Clap', labels: [] }];
      else if (url === '/api/sound_exclusions' && method === 'GET') res = { excluded: ['Speech'], available: ['Knock', 'Dog'] };
      else if (url === '/api/calibration/count/record') res = { success: true, kind: body.kind, claps_found: body.kind === 'fond' ? 0 : 1, gestures: body.kind === 'fond' ? [] : [1] };
      else if (url === '/api/calibration/count/analyze') res = { success: true, current: { delay: 1.5, peak_cooldown: 0.08, peak_ratio: 3 },
        proposed: { delay: 1.4, peak_cooldown: 0.13, peak_ratio: 6.2 }, warnings: ['entre deux commandes, laissez au moins 1,7 s'],
        score: { current: 4, proposed: 9, takes: 9 }, rows: [{ kind: 'simple', label: 'Clap simple', expected: [1], current: [2], proposed: [1], current_ok: false, proposed_ok: true }] };
      else if (url === '/api/calibration/count/apply') res = { success: true, applied: body.values, active: false };
      else if (url === '/api/source/record') res = { success: true, seconds: 5, wav: 'UklGRg==', clipped: true,
        timeline: [{ t: 0, top: [{ classe: 'Clapping', score: 0.72 }, { classe: 'Hands', score: 0.4 }] }, { t: 0.98, top: [{ classe: 'Animal', score: 0.5 }] }] };
      else if (url === '/refresh_vban_sources') res = { sources: [{ name: 'PC2', ip: '10.0.0.9', sample_rate: 48000, channels: 2 }] };
      else if (url === '/api/rtsp/stream' && method === 'POST') { const s = { id: 'new-1', name: body.name, url: body.url, enabled: true, sound_groups: [{ slug: 'clap', name: 'Clap', ha_entities: [1], sound_whitelist: {} }] }; settings.rtsp_sources.push(s); res = { success: true, stream: s }; }
      else if (url === '/api/vban/save') { const s = Object.assign({ sound_groups: [] }, body); settings.saved_vban_sources.push(s); res = { success: true, source: s }; }
      else if (url === '/api/detection/start') status = { running: true, source: 'x', since: Date.now() / 1000 - 125, sources: ['mic', 'rtsp_abc-123'] };
      else if (url === '/api/detection/stop') status = { running: false, sources: [] };
      else if (url.startsWith('/api/sources/') && method === 'PATCH') res = { success: true, source: {}, ...(w.__restart ? { restart: w.__restart } : {}) };
      else if (url === '/api/microphone' && method === 'DELETE') { settings.microphone.configured = false; }
      else if (url.startsWith('/api/source/sound_groups/usage')) res = { success: true, available: true, items: [{ id: 'automation.lumiere', nom: 'Lumière salon', type: 'automatisation', entites: [] }] };
      else if (url === '/api/calibration/start') res = { success: true, active: true };
      else if (url === '/api/calibration/record') res = { success: true, attacks: 2, sustains: 2, clipped: false, top: [{ classe: 'Clapping', score: 0.85 }] };
      else if (url === '/api/calibration/analyze') res = { success: true, gain: { field: 'volume', current: 80, proposed: 100, change: true }, warnings: [],
        groups: [{ target: 'Clap', group: 'clap', new: false, sounds: ['Clapping'], add: [], remove: ['Speech'], threshold: { current: 0.5, proposed: 0.7 },
                   count_mode: { current: 'attaques', proposed: 'attaques' }, before: { recall: 1, false_positive: 0.5 }, after: { recall: 1, false_positive: 0 }, warnings: [] }] };
      else if (url === '/api/calibration/apply') res = { success: true, applied: ['a', 'b'], errors: [] };
      else if (url === '/api/microphone' && method === 'POST') { Object.assign(settings.microphone, { configured: true, enabled: true }); }
      else if (url.startsWith('/api/rtsp/stream/') && method === 'DELETE') { settings.rtsp_sources = settings.rtsp_sources.filter(s => !url.endsWith(s.id)); }
      else if (url === '/api/source/sound_groups' && method === 'POST') {
        const g = body.preset === 'fumee'
          ? { slug: 'fumee', name: 'Détecteur de fumée', ha_entities: [], count_mode: 'alerte', alert_confirm: 3, device_class: 'smoke',
              sound_whitelist: { 'Smoke detector, smoke alarm': true, 'Fire alarm': true } }
          : { slug: 'snap', name: body.name, ha_entities: [1], sound_whitelist: {} };
        settings.microphone.sound_groups.push(g);
        res = { success: true, group: g, skipped_sounds: body.preset === 'fumee' ? ['Fire alarm'] : [] };
      }
      else if (url === '/api/settings/rooms') { res = { success: true, room_arbitration: body.enabled }; settings.global = settings.global || {}; settings.global.room_arbitration = body.enabled; }
      else if (url === '/api/sound_groups/presets') res = { presets: [
        { id: 'claps', name: 'Applaudissements', sounds: ['Clapping', 'Hands'], ha_entities: [1, 2], count_mode: 'attaques' },
        { id: 'fumee', name: 'Détecteur de fumée', sounds: ['Smoke detector, smoke alarm', 'Fire alarm'], count_mode: 'alerte', alert_confirm: 3 }] };
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
const wait = ms => new Promise(r => setTimeout(r, ms));
const emit = (ev, data) => (handlers[ev] || []).forEach(f => f(data));
const change = (el, v) => { if (v !== undefined) { if (el.type === 'checkbox') el.checked = v; else el.value = v; } el.dispatchEvent(new w.Event('change', { bubbles: true })); };
const lastCall = (m, u) => calls.filter(c => c[0] === m && (typeof u === 'string' ? c[1] === u : u.test(c[1]))).pop();
(async () => {
  await tick(4);
  // --- lot 6.68 : simplifications
  const histGets = () => calls.filter(c => c[0] === 'GET' && c[1] === '/api/detections/history').length;
  ok('+  historique chargé une seule fois au démarrage', histGets() === 1);
  ok('+  thème toujours posé (Home Assistant ou système)', ['light', 'dark'].includes(d.documentElement.getAttribute('data-theme')));
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
  // --- 6.72 : cause d'une erreur ecrite sur la carte
  emit('source_status', { source_id: 'rtsp_abc-123', status: 'error', error: "la caméra annonce une piste audio (aac, 16000 Hz) mais n'envoie aucun son" }); await tick();
  ok('+  cause de l’erreur affichée sur la carte', !$('#src-rtsp-abc_123 [data-role="error"]').hidden && /n'envoie aucun son/.test($('#src-rtsp-abc_123 [data-role="error"]').textContent));
  emit('source_status', { source_id: 'rtsp_abc-123', status: 'connected' }); await tick();
  ok('+  cause effacée une fois connectée', $('#src-rtsp-abc_123 [data-role="error"]').hidden);
  emit('source_status', { source_id: 'rtsp_abc-123', status: 'reconnecting' }); await tick();
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
  $('#src-mic [data-action="open-settings"]').click(); await tick();
  ok('+  réglages de la source dans une fenêtre', $('#src-mic dialog.source-settings').open === true);
  change($('#src-mic [data-field="device"]'), 'dev:x'); await tick(3);
  ok('11. choix du micro (device seul)', JSON.stringify(lastCall('PATCH', '/api/sources/mic/mic')[2]) === JSON.stringify({ device: { index: 5, name: 'A|B', pulse_name: 'x' } }));
  // --- 6.71 : micro designe par son nom (la liste peut changer entre affichage et choix)
  dom.window.CT.state.devices = [{ index: 9, name: 'Nouveau micro', pulse_name: 'n' }, { index: 5, name: 'A|B', pulse_name: 'x' }, { index: 2, name: 'USB', pulse_name: 'u' }];
  change($('#src-mic [data-field="device"]'), 'dev:u'); await tick(3);
  ok('+  micro choisi par son nom, même si la liste a changé', lastCall('PATCH', '/api/sources/mic/mic')[2].device.name === 'USB');
  change($('#src-mic [data-field="volume"]'), '90'); await wait(650);   // enregistre 600 ms apres le dernier changement
  ok('11. volume', lastCall('PATCH', '/api/sources/mic/mic')[2].volume === 90);
  change($('#src-mic [data-field="auto_volume"]'), true); await tick();
  ok('11. volume automatique', lastCall('PATCH', '/api/sources/mic/mic')[2].auto_volume === true && $('#src-mic [data-field="volume"]').disabled);
  emit('auto_volume_update', { volume: 120 }); await tick();
  ok('19. mise à jour du volume auto', $('#src-mic [data-out="volume"]').textContent === '120 %');
  // test micro : desactive pendant la detection (le niveau de la carte suit deja le son)
  ok('+  « Tester le son » désactivé pendant la détection',
     $('#src-mic dialog [data-action="test"]').disabled && $('#src-mic .menu-list [data-action="test"]').hidden);
  ok('+  raison écrite sous le bouton grisé', /La détection écoute cette source.*Arrêtez la détection/.test($('#src-mic [data-role="test-hint"]').textContent) &&
     !/format audio/.test($('#src-mic [data-role="test-hint"]').textContent));   // micro : pas de format
  status = { running: false, sources: [] }; await dom.window.CT.reloadStatus(); dom.window.CT.renderStatus(); await tick();
  ok('+  texte normal détection arrêtée', /sans lancer la détection/.test($('#src-mic [data-role="test-hint"]').textContent));
  $('#src-mic [data-action="test"]').click(); await tick();
  emit('mic_level', { db: -12 }); await tick();
  ok('11. test du micro + VU', !!lastCall('POST', '/api/mic/test/start') && /Test · -12 dB/.test($('#src-mic [data-role="meter-label"]').textContent));
  ok('+  VU-mètre aussi dans la fenêtre de réglages', /Test · -12 dB/.test($('#src-mic dialog.source-settings [data-role="meter-label"]').textContent));
  ok('+  VU-mètre de la fenêtre collé en haut avec le titre', $$('#src-mic dialog [data-role="meter"]').length === 1 &&
     !!$('#src-mic dialog .settings-top [data-role="meter"]') && !!$('#src-mic dialog .settings-top [data-action="close-settings"]'));
  ok('+  VU-mètre de la carte conservé (hors de la fenêtre)', $$('#src-mic [data-role="meter"]').filter(m => !m.closest('dialog')).length === 1 &&
     /Test · -12 dB/.test($$('#src-mic [data-role="meter"]').find(m => !m.closest('dialog')).textContent));
  $('#src-mic [data-action="close-settings"]').click(); await tick(2);
  // 12 : rtsp
  change($('#src-rtsp-abc_123 [data-field="url"]'), 'rtsp://cam/2'); await tick(3);
  ok('12. URL RTSP', lastCall('PATCH', /rtsp\/abc-123/)[2].url === 'rtsp://cam/2');
  change($('#src-rtsp-abc_123 [data-field="gain"]'), '20'); await wait(650);   // enregistre 600 ms apres le dernier changement
  ok('12. gain RTSP', lastCall('PATCH', /rtsp\/abc-123/)[2].gain === 20);
  change($('#src-rtsp-abc_123 [data-field="rtsp_tcp"]'), true); await tick(2);
  ok('+  transport TCP de la caméra', lastCall('PATCH', /rtsp\/abc-123/)[2].rtsp_tcp === true);
  $('#src-rtsp-abc_123 [data-action="test"]').click(); await tick();
  ok('12. test RTSP (remplace le test micro)', lastCall('POST', '/api/rtsp/test/start')[2].id === 'abc-123');
  change($('#src-rtsp-abc_123 [data-field="name"]'), 'Entrée'); await tick(3);
  ok('10. renommer la caméra', lastCall('PATCH', /rtsp\/abc-123/)[2].name === 'Entrée');
  // 13 : vban
  const vb = $$('.source-card').find(c => c.id.startsWith('src-vban'));
  ok('10. badge multicast VBAN', /multicast/.test(vb.textContent));
  change(vb.querySelector('[data-field="gain"]'), '4'); await wait(650);   // enregistre 600 ms apres le dernier changement
  ok('13. gain VBAN (ip/nom)', lastCall('PATCH', /\/api\/sources\/vban\/vb-1$/)[2].gain === 4);
  vb.querySelector('[data-action="test"]').click(); await tick();
  ok('13. test VBAN (par id)', JSON.stringify(lastCall('POST', '/api/vban/test/start')[2]) === JSON.stringify({ id: 'vb-1' }));
  // 14 : entites + copier
  $('#src-mic [data-action="open-settings"]').click(); await tick();
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
  await tick(2);
  ok('+  renommer : confirmation (entités renommées dans HA)', /entités Home Assistant du groupe seront renommées/.test($('.modal-message').textContent));
  ok('+  renommer : automatisations à adapter listées', /automatisation « Lumière salon »/.test($('.modal-message').textContent));
  $('.modal-backdrop [data-r="ok"]').click(); await tick(3);
  ok('15. renommer un groupe', lastCall('PUT', '/api/source/sound_groups')[2].name === 'Toc table');
  ok('15. groupe principal non supprimable', !$('#src-mic .group-card[data-slug="clap"] [data-group-action="delete"]') && !!$('#src-mic .group-card[data-slug="toc"] [data-group-action="delete"]'));
  $('#src-mic .group-card[data-slug="clap"] [data-group-action="cleanup"]').click(); await tick(3);
  ok('15. retirer les sons non cochés', !!lastCall('POST', '/api/source/sound_whitelist/cleanup'));
  $('#src-mic [data-action="add-group"]').click(); await tick(3);
  ok('+  nouveau groupe : modèles proposés', $$('.modal [data-preset]').length === 3 && /Alerte : capteur tant que le son dure/.test($$('[data-preset]').map(b => b.textContent).join(' ')));
  $('.modal [data-preset=""]').click(); await tick(2);
  $('.modal #dlg-input').value = 'Snap'; $('.modal [data-r="ok"]').click(); await tick(4);
  ok('15. nouveau groupe (sans rechargement)', lastCall('POST', '/api/source/sound_groups')[2].name === 'Snap' && !!$('#src-mic dialog .group-card[data-slug="snap"]'));
  $('#src-mic [data-action="add-group"]').click(); await tick(3);
  $('.modal [data-preset="fumee"]').click(); await tick(5);
  ok('+  modèle « Détecteur de fumée » : groupe créé, sons déjà pris signalés', lastCall('POST', '/api/source/sound_groups')[2].preset === 'fumee' &&
     /Détecteur de fumée/.test($('#toasts').textContent) && /non cochés/.test($('#toasts').textContent));
  const fumee = () => $('#src-mic dialog .group-card[data-slug="fumee"]');
  ok('+  mode alerte : capteur, confirmation et type au lieu des nombres de claps', !!fumee() && !fumee().querySelector('[data-clap]') &&
     fumee().querySelector('[data-role="alert-confirm"]').value === '3' && fumee().querySelector('[data-role="device-class"]').value === 'smoke' &&
     fumee().querySelector('[data-role="count-mode"]').value === 'alerte');
  change(fumee().querySelector('[data-role="alert-confirm"]'), '1'); await tick(3);
  ok('+  mode alerte : confirmation enregistrée', lastCall('PUT', '/api/source/sound_groups')[2].alert_confirm === 1);
  emit('alert', { source_id: 'mic', group_slug: 'fumee', group_name: 'Détecteur de fumée', alert: 'on', score: 0.8 }); await tick();
  ok('+  alerte : signalée sur la carte et dans l\'historique', /🚨 Détecteur de fumée détecté/.test($('#src-mic [data-role="live"]').textContent) &&
     /Alerte/.test($('#history-list').textContent));
  emit('alert', { source_id: 'mic', group_slug: 'fumee', group_name: 'Détecteur de fumée', alert: 'off', score: 0 }); await tick();
  ok('+  fin d\'alerte : mention retirée', !/🚨/.test($('#src-mic [data-role="live"]').textContent));
  emit('sound_seen', { source_id: 'mic', label: 'Dog' }); await tick();
  ok('15. auto-découverte d\'un son (groupe avec « Ajouter les sons entendus »)', !!$('#src-mic .chip input[data-label="Dog"]'));
  $('#src-mic .group-card[data-slug="toc"] [data-group-action="delete"]').click(); await tick();
  ok('+  suppression d\'un groupe : nommé dans la confirmation', /groupe « .+ »/.test($('.modal-message').textContent) && /Supprimer le groupe/.test($('.modal [data-r="ok"]').textContent));
  $('.modal [data-r="ok"]').click(); await tick(3);
  ok('15. supprimer un groupe', lastCall('DELETE', '/api/source/sound_groups')[2].group_slug === 'toc');
  // 16 : webhook
  const wh = $('#src-mic [data-field="webhook_url"]'); change(wh, 'http://homeassistant:8123/api/webhook/x'); await tick();
  ok('16. webhook enregistré', lastCall('PATCH', '/api/sources/mic/mic')[2].webhook_url === 'http://homeassistant:8123/api/webhook/x');
  $('#src-mic [data-action="test-webhook"]').click(); await tick();
  ok('16. tester le webhook', lastCall('POST', '/api/webhook/test')[2].url === 'http://homeassistant:8123/api/webhook/x');
  $('#src-mic [data-action="close-settings"]').click(); await tick(2);
  ok('+  groupe créé visible sur la carte après fermeture', !!$('#src-mic .group-live[data-slug="snap"]'));
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
  ok('+  groupe vide : réglages ouverts sur « Ajouter les sons entendus »',
     $('#src-rtsp-new_1 dialog.source-settings').open && d.activeElement && d.activeElement.getAttribute('data-role') === 'auto-add');
  $('#src-rtsp-new_1 [data-action="close-settings"]').click(); await tick(3);
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
  const advPuts = calls.filter(c => c[1] === '/api/settings/advanced').length;
  $('#adv-defaults').click(); await tick();
  ok('+  valeurs par défaut : confirmation avec valeurs actuelles et par défaut',
     /Actuelles : .*force ×4\. Par défaut : .*force ×3/.test($('.modal-message').textContent) &&
     calls.filter(c => c[1] === '/api/settings/advanced').length === advPuts);
  $('.modal [data-r="ok"]').click(); await tick(2);
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
  const histBefore = histGets();
  emit('connect'); emit('connect'); await tick(3);
  ok('20. resynchronisation à la reconnexion', calls.filter(c => c[1] === '/api/settings').length >= 1);
  ok('+  historique rechargé à la reconnexion seulement (pas à la 1re connexion)', histGets() === histBefore + 1);
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
  ok('+  pas de « mise à jour » si le serveur n\'a pas redémarré', !$$('#toasts .toast').slice(toastsBefore).some(t => /mise à jour/.test(t.textContent)));
  w.__restart = 'ok';
  await CT.withRestart(CT.patchSource({ kind: 'mic', key: 'mic' }, { enabled: true }));
  ok('+  « mise à jour » quand le serveur l\'indique', $$('#toasts .toast').some(t => /mise à jour/.test(t.textContent)));
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
  // --- lot performance 6.51
  const liveSrc = CT.sourceList().find(x => d.querySelector('#' + x.domId + ' .group-live'));
  const liveRow = d.querySelector('#' + liveSrc.domId + ' .group-live');
  const liveSlug = liveRow.getAttribute('data-slug');
  $('#tab-settings').click(); await tick();
  const before = liveRow.querySelector('.group-live-score').textContent;
  emit('group_scores', { source_id: liveSrc.sourceId, scores: { [liveSlug]: 0.93 } }); await tick();
  ok('+  pas de dessin pendant l\'onglet Réglages', liveRow.querySelector('.group-live-score').textContent === before);
  // --- lot 6.66 : une rafale de sons nouveaux = un seul rechargement des exclusions
  const exGets = () => calls.filter(c => c[0] === 'GET' && c[1] === '/api/sound_exclusions').length;
  const exBefore = exGets();
  ['Bird', 'Cat', 'Knock'].forEach(label => emit('sound_seen', { source_id: 'mic', label }));
  await tick(2);
  ok('+  exclusions : pas de requête par son entendu', exGets() === exBefore);
  await tick(45);
  ok('+  exclusions : un seul rechargement pour la rafale', exGets() === exBefore + 1);
  $('#tab-listen').click(); await tick();
  ok('+  courbes rattrapées au retour sur Écoute', d.querySelector('#' + liveSrc.domId + ' .group-live[data-slug="' + liveSlug + '"] .group-live-score').textContent === '93 %');
  // --- lot 6.53
  // Assistant RTSP sans adresse : l'etape ne reste pas bloquee
  $('#add-source').click(); await tick(); $('.choice[data-kind="rtsp"]').click(); await tick();
  $('#wz-url').value = ''; $('[data-w="ok"]').click(); await tick(2);
  ok('+  assistant RTSP sans adresse : étape utilisable', !$('[data-w="ok"]').disabled && !/Ajout en cours/.test($('[data-w="ok"]').textContent) && /adresse/.test($('#toasts').textContent));
  $('[data-w="close"]').click(); await tick(3);
  // Assistant micro : une seule requete, focus sur la nouvelle carte
  const patchesBefore = calls.filter(c => c[0] === 'PATCH').length;
  $('#add-source').click(); await tick(); $('.choice[data-kind="mic"]').click(); await tick(2);
  $('[data-w="ok"]').click(); await tick(5);
  const micPost = lastCall('POST', '/api/microphone');
  ok('+  assistant micro : une seule requête', !!micPost && micPost[2].enabled === true && !!micPost[2].device && calls.filter(c => c[0] === 'PATCH').length === patchesBefore);
  // --- 6.75 : consigne de fin selon que le groupe reconnait deja des sons
  {
    const micGroups = (dom.window.CT.state.settings.microphone.sound_groups || []);
    const ready = micGroups.some(g => g.enabled !== false && dom.window.CT.groupHasSounds(g));
    ok('+  assistant : consigne de fin adaptée (groupe déjà prêt ou à remplir)',
       ready ? /reconnaît déjà les applaudissements/.test($('#wz-hint').textContent) : /Ajouter les sons entendus/.test($('#wz-hint').textContent));
  }
  $('[data-w="done"]').click(); await tick(5);
  // le micro a un groupe vide (« Snap », cree plus haut) : reglages ouverts sur ce groupe
  ok('+  assistant terminé : réglages de la nouvelle carte ouverts sur le groupe vide',
     $('#wizard').hidden && $('#src-mic dialog.source-settings').open &&
     d.activeElement && d.activeElement.closest('.group-card') && d.activeElement.getAttribute('data-role') === 'auto-add');
  $('#src-mic [data-action="close-settings"]').click(); await tick(3);
  ok('+  assistant terminé : focus rendu à la carte', d.activeElement === $('#src-mic .source-settings-open'));
  // Focus apres renommage / suppression de groupe
  const gSrc = CT.sourceList().find(x => x.kind === 'mic');
  CT.state.settings.microphone.sound_groups = JSON.parse(JSON.stringify(base.microphone.sound_groups));
  CT.render();
  const tocName = d.querySelector('#' + gSrc.domId + ' .group-card[data-slug="toc"] [data-role="group-name"]');
  tocName.focus(); CT.render();
  ok('+  focus restauré sur le nom du bon groupe', d.activeElement && d.activeElement.getAttribute('data-role') === 'group-name' && d.activeElement.closest('.group-card').getAttribute('data-slug') === 'toc');
  d.querySelector('#' + gSrc.domId + ' .group-card[data-slug="toc"] [data-group-action="delete"]').focus();
  CT.state.settings.microphone.sound_groups = CT.state.settings.microphone.sound_groups.filter(g => g.slug !== 'toc');
  CT.render();
  ok('+  groupe supprimé : focus sur « Nouveau groupe »', d.activeElement && d.activeElement.getAttribute('data-action') === 'add-group');
  // Recherche de sons : focus et texte gardes
  const many = {}; ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J'].forEach(l => { many[l] = false; });
  CT.state.settings.microphone.sound_groups[0].sound_whitelist = many; CT.render();
  const search = d.querySelector('#' + gSrc.domId + ' [data-role="sound-search"]');
  search.focus(); CT.render();
  ok('+  recherche de sons : focus gardé', d.activeElement && d.activeElement.getAttribute('data-role') === 'sound-search');
  // Retour sur l'onglet sans changement : pas de reconstruction
  CT.state.settings = JSON.parse(JSON.stringify(settings));
  d.activeElement.blur(); CT.render();
  const cardBefore = $('#src-mic');
  await CT.resync(); await tick();
  ok('+  retour sur l\'onglet sans changement : grille gardée', $('#src-mic') === cardBefore);
  ok('+  « Effacer » désactivé si l\'historique est vide', $('#history-clear').disabled === ($('#history-list').children.length === 0));
  const thr2 = d.querySelector('.threshold');
  ok('+  seuil lu en % (aria-valuetext)', / %$/.test(thr2.getAttribute('aria-valuetext') || ''));
  ok('+  messages d\'information sans rôle en double', !d.querySelector('#toasts .toast:not(.toast-error)[role]'));
  // --- lot 6.54 : ajout des sons entendus par groupe
  CT.state.settings = JSON.parse(JSON.stringify(settings));
  CT.state.settings.microphone.sound_groups = [{ slug: 'clap', name: 'Clap', ha_entities: [1], sound_whitelist: {} },
                                               { slug: 'toc', name: 'Toc', ha_entities: [1], sound_whitelist: {}, auto_add_sounds: true }];
  CT.render();
  const micCard = $('#src-mic');
  const autoClap = micCard.querySelector('.group-card[data-slug="clap"] [data-role="auto-add"]');
  ok('+  interrupteur « Ajouter les sons entendus » désactivé par défaut', !!autoClap && !autoClap.checked && /Activez « Ajouter les sons entendus »/.test(micCard.querySelector('.group-card[data-slug="clap"]').textContent));
  emit('sound_seen', { source_id: 'mic', label: 'Dog' }); await tick();
  ok('+  son entendu ajouté seulement aux groupes qui l\'ont activé', !micCard.querySelector('.group-card[data-slug="clap"] [data-label="Dog"]') && !!$('#src-mic .group-card[data-slug="toc"] [data-label="Dog"]'));
  change($('#src-mic .group-card[data-slug="clap"] [data-role="auto-add"]'), true); await tick(2);
  const autoPut = lastCall('PUT', '/api/source/sound_groups');
  ok('+  activer l\'ajout des sons entendus (serveur)', !!autoPut && autoPut[2].group_slug === 'clap' && autoPut[2].auto_add_sounds === true);
  // --- lot 6.55 : groupe actif / desactive
  const enSw = $('#src-mic .group-card[data-slug="clap"] [data-role="group-enabled"]');
  ok('+  interrupteur « Actif » par groupe', !!enSw && enSw.checked);
  change(enSw, false); await tick(2);
  const enPut = lastCall('PUT', '/api/source/sound_groups');
  ok('+  désactiver un groupe (serveur)', !!enPut && enPut[2].group_slug === 'clap' && enPut[2].enabled === false);
  CT.state.settings.microphone.sound_groups[0].enabled = false; CT.render();
  ok('+  groupe désactivé signalé sur la carte', !!$('#src-mic .group-live.is-off[data-slug="clap"]'));
  // --- lot 6.56 : calibrage et comptage
  const modeSel = $('#src-mic .group-card[data-slug="clap"] [data-role="count-mode"]');
  ok('+  choix du comptage par groupe', !!modeSel);
  change(modeSel, 'tenus'); await tick(2);
  ok('+  comptage « sons tenus » (serveur)', lastCall('PUT', '/api/source/sound_groups')[2].count_mode === 'tenus');
  CT.state.settings.microphone.enabled = true; CT.state.settings.microphone.sound_groups[0].enabled = true; CT.render();
  $('#src-mic [data-action="calibrate"]').click(); await tick();
  ok('+  calibrage : fenêtre ouverte', !$('#calibration').hidden && !!$('#calibration [data-c="start"]'));
  $('#cal-reps').value = '2'; $('#cal-bg').checked = false;
  $('#calibration [data-c="start"]').click(); await tick(3);
  ok('+  calibrage : démarré sur la source', lastCall('POST', '/api/calibration/start')[2].source === 'mic' && /prise 1\//.test($('#calibration-title').textContent));
  const nTakes = calls.filter(c => c[1] === '/api/calibration/record').length;
  $('#calibration [data-c="record"]').click(); await tick(3);
  ok('+  calibrage : prise enregistrée', calls.filter(c => c[1] === '/api/calibration/record').length === nTakes + 1 && /Applaudissement 85 %/.test($('#calibration').textContent));
  while ($('#calibration [data-c="record"]')) { $('#calibration [data-c="record"]').click(); await tick(3); }
  ok('+  calibrage : propositions affichées', /Seuil : 50 % → 70 %/.test($('#calibration').textContent) && !!$('#cal-apply-gain'));
  $('#calibration [data-c="apply"]').click(); await tick(4);
  const ap = lastCall('POST', '/api/calibration/apply');
  ok('+  calibrage : appliqué', !!ap && ap[2].gain === true && ap[2].groups.length >= 1 && $('#calibration').hidden);
  // --- lot 6.57 : reglages de la source en fenetre
  const dlg = () => $('#src-mic dialog.source-settings');
  if (!dlg().open) { $('#src-mic .source-settings-open').click(); await tick(); }
  $('#src-mic [data-action="close-settings"]').click();
  await tick();
  ok('+  fenêtre de réglages fermée', !dlg().open && d.activeElement === $('#src-mic .source-settings-open'));
  $('#src-mic .source-settings-open').click(); await tick();
  CT.render(); await tick();
  ok('+  fenêtre de réglages gardée ouverte après un rendu', dlg().open === true);
  $('#src-mic [data-group-action="delete"]').click();
  await tick();
  ok('+  confirmation ouverte dans la fenêtre de réglages', !!dlg().querySelector('.modal-backdrop'));
  const cancel = dlg().querySelector('.modal-backdrop [data-r="cancel"]');
  if (cancel) cancel.click();
  await tick();
  ok('+  messages au premier plan pendant les réglages', dlg().contains($('#toasts')));
  const cardOpen = $('#src-mic');
  await CT.refresh(); await tick();
  ok('+  grille non reconstruite pendant les réglages', $('#src-mic') === cardOpen && dlg().open);
  $('#src-mic [data-action="close-settings"]').click(); await tick(2);
  ok('+  messages rendus à la page et grille à jour après fermeture', $('#toasts').parentNode === d.body && $('#src-mic') !== cardOpen);
  // --- lot 6.64 : interface
  CT.state.settings = JSON.parse(JSON.stringify(settings));
  CT.state.settings.microphone.sound_groups = [{ slug: 'clap', name: 'Clap', ha_entities: [1], sound_whitelist: { Clapping: true, Dog: false, Bird: false, Water: false } },
                                               { slug: 'vide', name: 'Vide', ha_entities: [1], sound_whitelist: {} }];
  CT.render(); await tick();
  ok('+  groupe vide signalé sur la carte', /Aucun son choisi/.test($('#src-mic .group-live[data-slug="vide"]').textContent));
  ok('+  barre d\'état : groupe sans son', /groupes? sans son/.test($('#status-detail').textContent));
  $('#src-mic [data-action="choose-sounds"]').click(); await tick();
  ok('+  « Choisir les sons » ouvre le groupe dans les réglages',
     $('#src-mic dialog.source-settings').open && d.activeElement.closest('.group-card').getAttribute('data-slug') === 'vide');
  const order = () => $$('#src-mic .group-card[data-slug="clap"] .chip input').map(i => i.getAttribute('data-label')).join(',');
  const chipsBefore = order();
  change($('#src-mic .group-card[data-slug="clap"] .chip input[data-label="Water"]'), true); await tick(2);
  ok('+  pastilles fixes après une coche', order() === chipsBefore);
  $('#src-mic [data-action="close-settings"]').click(); await tick(3);
  const menuOf = id => $('#' + id + ' [data-action="menu"]');
  menuOf('src-mic').click(); await tick();
  // carte a identifiant unique (le faux serveur du test cree des doublons)
  const ids = $$('.source-card').map(c => c.id);
  const otherCard = ids.find(id => id !== 'src-mic' && ids.indexOf(id) === ids.lastIndexOf(id));
  menuOf(otherCard).click(); await tick();
  ok('+  menu fermé : aria-expanded remis à false',
     menuOf('src-mic').getAttribute('aria-expanded') === 'false' && menuOf(otherCard).getAttribute('aria-expanded') === 'true');
  // --- 6.69 : format audio recu d'une camera (reglages de la source)
  const cam = CT.sourceList().find(x => x.kind === 'rtsp');
  const fmtEl = () => $('#' + cam.domId + ' [data-role="audio-format"]');
  ok('+  format audio : inconnu tant que la caméra n\'est pas écoutée', !!fmtEl() && /Inconnu pour l'instant/.test(fmtEl().textContent));
  // Page ouverte pendant la detection : le format vient du statut du serveur
  status = { running: true, sources: [cam.sourceId], audio_formats: { [cam.sourceId]: { codec: 'pcm_alaw', sample_rate: 8000, channels: 'mono' } } };
  await dom.window.CT.reloadStatus(); dom.window.CT.renderStatus(); await tick();
  ok('+  format audio repris du statut (détection en cours)', /G\.711 A-law · 8 kHz/.test(fmtEl().textContent));
  // Source hors de la session (pas dans `sources`) : testable pendant la detection
  status = { running: true, sources: ['autre'] }; await dom.window.CT.reloadStatus(); dom.window.CT.renderStatus(); await tick();
  ok('+  source non écoutée : « Tester le son » actif pendant la détection',
     !$('#' + cam.domId + ' dialog [data-action="test"]').disabled && /sans lancer la détection/.test($('#' + cam.domId + ' [data-role="test-hint"]').textContent));
  status = { running: false, sources: [] }; await dom.window.CT.reloadStatus(); dom.window.CT.renderStatus(); await tick();
  delete dom.window.CT.state.audioFormats[cam.sourceId];   // etat propre pour la suite
  // --- 6.70 : extrait sonore (detection en cours, source ecoutee)
  const recBtn = () => $('#' + cam.domId + ' [data-action="record"]');
  ok('+  extrait : bouton grisé hors détection, avec la raison', recBtn().disabled &&
     /pendant la détection/.test($('#' + cam.domId + ' [data-role="record-hint"]').textContent));
  status = { running: true, sources: [cam.sourceId] }; await dom.window.CT.reloadStatus(); dom.window.CT.renderStatus(); await tick();
  ok('+  extrait : actif quand la détection écoute la source', !recBtn().disabled);
  recBtn().click(); await tick(3);
  const recCall = lastCall('POST', '/api/source/record');
  ok('+  extrait : demande au serveur (source, 5 s)', !!recCall && recCall[2].source_id === cam.sourceId && recCall[2].seconds === 5);
  const recOutNow = () => $('#' + cam.domId + ' [data-role="record-result"]');
  ok('+  extrait : lecteur, téléchargement, sons reconnus en français, saturation signalée',
     !!recOutNow().querySelector('audio') && !!recOutNow().querySelector('a[download$=".wav"]') &&
     /Applaudissement 72 %/.test(recOutNow().textContent) && recOutNow().querySelectorAll('.record-timeline li').length === 2 &&
     /déjà saturé/.test(recOutNow().textContent) && recBtn().textContent === 'Enregistrer 5 s' && !recBtn().disabled);
  dom.window.CT.render(); await tick();
  ok('+  extrait conservé après reconstruction de la carte', !!recOutNow().querySelector('audio'));
  status = { running: false, sources: [] }; await dom.window.CT.reloadStatus(); dom.window.CT.renderStatus(); await tick();
  emit('source_format', { source_id: cam.sourceId, format: { codec: 'pcm_mulaw', sample_rate: 8000, channels: 'mono' } }); await tick();
  ok('+  format audio 8 kHz : affiché et signalé', /G\.711 µ-law · 8 kHz · mono/.test(fmtEl().textContent) &&
     /au-dessus de|dépasse 4 kHz/.test(fmtEl().textContent) && !!fmtEl().querySelector('.text-warn'));
  emit('source_format', { source_id: cam.sourceId, format: { codec: 'aac', sample_rate: 16000, channels: 'stereo' } }); await tick();
  ok('+  format audio 16 kHz : suffisant', /AAC · 16 kHz · stéréo/.test(fmtEl().textContent) && !fmtEl().querySelector('.text-warn'));
  // --- 6.73 : calibrage du comptage (parcours complet, API simulee)
  $('#tab-settings').click(); await tick(2);
  $('#count-calibrate').click(); await tick();
  const ccBox = () => $('#count-calibration');
  ok('+  calibrage du comptage : fenêtre ouverte avec choix de la source', !ccBox().hidden && !!$('#cc-source') && $$('#cc-source option').length >= 1);
  ccBox().querySelector('[data-c="start"]').click(); await tick(3);
  ok('+  calibrage du comptage : session démarrée sur la source choisie', !!lastCall('POST', '/api/calibration/count/start'));
  let guard = 0;
  while (ccBox().querySelector('[data-c="record"]') && guard++ < 15) { ccBox().querySelector('[data-c="record"]').click(); await tick(3); }
  const recs = calls.filter(c => c[1] === '/api/calibration/count/record').map(c => c[2].kind);
  ok('+  calibrage du comptage : prises guidées (fond, 3 simples, 3 doubles, 2 triples, séparées)',
     recs.join() === 'fond,simple,simple,simple,double,double,double,triple,triple,separe');
  await tick(2);
  ok('+  calibrage du comptage : actuel / proposé, score et avertissement', /4\/9/.test(ccBox().textContent) && /9\/9/.test(ccBox().textContent) &&
     $('#cc-delay').value === '1.4' && /Entre deux commandes/.test(ccBox().textContent));
  change($('#cc-delay'), '1.6');
  ccBox().querySelector('[data-c="apply"]').click(); await tick(3);
  const ccApply = lastCall('POST', '/api/calibration/count/apply');
  ok('+  calibrage du comptage : valeurs ajustées appliquées, champs mis à jour', !!ccApply && ccApply[2].values.delay === 1.6 &&
     ccApply[2].values.peak_ratio === 6.2 && ccBox().hidden && $('#adv-delay').value === '1.6');
  // --- 6.77 : prises enchainees automatiquement (compte a rebours, pause, choix memorise)
  w.CT.AUTO_DELAY = 1;
  $('#count-calibrate').click(); await tick();
  ok('+  prises enchaînées : option décochée par défaut', !!$('#cc-auto') && !$('#cc-auto').checked);
  $('#cc-auto').checked = true;
  const ccRecs = () => calls.filter(c => c[1] === '/api/calibration/count/record').length;
  const ccBefore = ccRecs();
  ccBox().querySelector('[data-c="start"]').click(); await tick(3);
  const ccState = () => ccBox().querySelector('.cal-countdown').getAttribute('data-state');
  ok('+  couleur de l\'étape : orange avant une prise enchaînée', ccState() === 'wait');
  ok('+  prises enchaînées : compte à rebours avant la prise', /Début de la prise dans 1 s/.test(ccBox().textContent) &&
     ccRecs() === ccBefore && !!ccBox().querySelector('[data-c="pause"]'));
  await new Promise(r => setTimeout(r, 1200)); await tick(3);
  ok('+  prises enchaînées : prise lancée sans clic, la suivante se prépare', ccRecs() === ccBefore + 1 &&
     /prise 2\//.test($('#count-calibration-title').textContent) && /Début de la prise dans/.test(ccBox().textContent));
  ccBox().querySelector('[data-c="pause"]').click();
  await new Promise(r => setTimeout(r, 1200)); await tick();
  ok('+  prises enchaînées : pause, rien n\'est lancé', ccRecs() === ccBefore + 1 && /En pause/.test(ccBox().textContent));
  ok('+  couleur de l\'étape : neutre en pause', ccState() === null);
  w.__hold = { url: '/api/calibration/count/record' };
  ccBox().querySelector('[data-c="record"]').click(); await tick();
  ok('+  couleur de l\'étape : vert pendant l\'écoute', ccState() === 'listen');
  w.__hold.release(); w.__hold = null; await tick(3);
  ok('+  couleur de l\'étape : orange de nouveau avant la prise suivante', ccState() === 'wait');
  ccBox().querySelector('[data-c="cancel"]').click(); await tick();
  $('#count-calibrate').click(); await tick();
  ok('+  prises enchaînées : choix mémorisé', $('#cc-auto').checked);
  ccBox().querySelector('[data-c="cancel"]').click(); await tick();
  $('#src-mic [data-action="calibrate"]').click(); await tick();
  ok('+  prises enchaînées : même option dans le calibrage d\'une source', !!$('#cal-auto') && $('#cal-auto').checked);
  $('#cal-reps').value = '2';
  $('#calibration [data-c="start"]').click(); await tick(3);
  const calState = () => $('#calibration .cal-countdown').getAttribute('data-state');
  ok('+  couleur de l\'étape (source) : orange avant la prise', calState() === 'wait');
  $('#calibration [data-c="pause"]').click(); await tick();
  w.__hold = { url: '/api/calibration/record' };
  $('#calibration [data-c="record"]').click(); await tick();
  ok('+  couleur de l\'étape (source) : vert pendant l\'écoute', calState() === 'listen');
  w.__hold.release(); w.__hold = null; await tick(3);
  $('#calibration [data-c="pause"]').click(); await tick();
  $('#calibration [data-c="cancel"]').click(); await tick();
  const probe = d.createElement('p');
  w.CT.recordCountdown(probe, 1, 'Écoute… ');
  const listening = probe.getAttribute('data-state');
  await new Promise(r => setTimeout(r, 1100));
  ok('+  couleur de l\'étape : bleu pendant l\'analyse', listening === 'listen' && probe.getAttribute('data-state') === 'analyze' &&
     /Analyse de la prise/.test(probe.textContent));
  // --- 6.82 : interface
  // « Afficher » : sans secrets par defaut, complet sur demande avec avertissement
  $('#tab-settings').click(); await tick(2);
  $('#show-config').click(); await tick(3);
  const ta = () => d.querySelector('.modal textarea');
  ok('+  afficher : sans secrets par défaut', !!ta() && /\*\*\*@/.test(ta().value) && !/secret/.test(ta().value) && /sans secrets/.test($('.modal [data-r="note"]').textContent));
  $('.modal [data-r="alt"]').click(); await tick(3);
  ok('+  afficher : sauvegarde complète avec avertissement', /admin:secret/.test(ta().value) && $('.modal [data-r="note"]').classList.contains('text-warn'));
  $('.modal [data-r="close"]').click(); await tick();
  $('#tab-listen').click(); await tick(2);
  // Demarrage : « Démarrage… », bouton jamais desactive (focus conserve)
  const tbtn = $('#toggle-detection');
  w.__hold = { url: '/api/detection/start' };
  tbtn.focus(); tbtn.click(); await tick();
  ok('+  démarrage : retour visuel et bouton occupé', /Démarrage…/.test(tbtn.textContent) && /Démarrage…/.test($('#status-title').textContent) &&
     tbtn.getAttribute('aria-busy') === 'true' && !tbtn.disabled && d.activeElement === tbtn);
  emit('source_status', { source_id: 'mic', status: 'connected' }); await tick();
  ok('+  démarrage : un statut reçu ne réactive pas le bouton', tbtn.getAttribute('aria-disabled') === 'true');
  w.__hold.release(); w.__hold = null; await tick(4);
  ok('+  démarrage : état final', tbtn.getAttribute('aria-busy') === 'false' && !/…/.test(tbtn.textContent));
  status = { running: false, sources: [] }; await w.CT.reloadStatus(); w.CT.renderStatus(); await tick();
  // Gain : valeur changee pendant l'enregistrement precedent, envoyee ensuite
  // (carte du micro : celles des cameras sont liees deux fois par le faux serveur)
  const gEl = $('#src-mic [data-field="volume"]');
  const gUrl = '/api/sources/mic/mic';
  const gainPatches = () => calls.filter(c => c[0] === 'PATCH' && c[1] === gUrl && c[2] && 'volume' in c[2]).map(c => c[2].volume);
  const g0 = gainPatches().length;
  w.__hold = { url: gUrl };
  change(gEl, '55'); await wait(650);
  change(gEl, '60'); await wait(650);
  ok('+  gain : une seule requête à la fois', gainPatches().length === g0 + 1);
  if (w.__hold.release) w.__hold.release();
  w.__hold = null; await tick(6);
  ok('+  gain : dernière valeur envoyée après la première', gainPatches().slice(g0).join() === '55,60');
  // Assistant : Entree valide l'etape
  $('#add-source').click(); await tick(); $('.choice[data-kind="rtsp"]').click(); await tick();
  $('#wz-url').value = '';
  $('#wz-url').dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true })); await tick(2);
  ok('+  assistant : Entrée valide l\'étape', /adresse/.test($('#toasts').textContent));
  $('[data-w="close"]').click(); await tick(3);
  // Rendu differe pendant un appui (pastille visee non remplacee)
  let ran = 0;
  d.dispatchEvent(new w.Event('pointerdown'));
  w.CT.whenIdle('test-pointer', () => false, () => { ran += 1; });
  ok('+  rendu différé pendant un appui', ran === 0);
  d.dispatchEvent(new w.Event('pointerup')); await tick(2);
  ok('+  rendu attendu jusqu\'au clic qui suit le relâchement', ran === 0);
  d.dispatchEvent(new w.Event('click')); await tick(2);
  ok('+  rendu fait après le clic', ran === 1);
  w.CT.whenIdle('test-pointer', () => false, () => { ran += 1; });
  ok('+  rendu immédiat juste après le relâchement (pas d\'attente du toucher suivant)', ran === 2);
  // Calibrage : la reponse d'une analyse abandonnee ne s'affiche pas ailleurs
  w.CT.openCalibration(w.CT.sourceList().filter(x => x.kind === 'mic')[0]); await tick();
  $('#cal-reps').value = '2'; $('#cal-bg').checked = false; $('#cal-auto').checked = false;
  $('#calibration [data-c="start"]').click(); await tick(3);
  w.__hold = { url: '/api/calibration/analyze' };   // analyse retenue : fenetre fermee pendant l'attente
  while ($('#calibration [data-c="record"]')) { $('#calibration [data-c="record"]').click(); await tick(3); }
  ok('+  calibrage : analyse en attente', /Analyse des prises/.test($('#calibration').textContent) && !!w.__hold.release);
  $('#calibration [data-c="close"]').click(); await tick();
  w.CT.openCalibration(w.CT.sourceList().filter(x => x.kind === 'mic')[0]); await tick();
  ok('+  calibrage : nouvelle ouverture sur l\'écran de départ', !!$('#calibration [data-c="start"]'));
  if (w.__hold.release) w.__hold.release();
  w.__hold = null; await tick(3);
  ok('+  calibrage : réponse ancienne ignorée', !!$('#calibration [data-c="start"]') && !$('#calibration [data-c="apply"]'));
  $('#calibration [data-c="cancel"]').click(); await tick();
  // --- 6.86 : plusieurs pieces
  $('#tab-settings').click(); await tick(2);
  ok('+  pièces : option désactivée par défaut, attente grisée', $('#rooms-toggle') && !$('#rooms-toggle').checked && $('#rooms-window').disabled);
  change($('#rooms-toggle'), true); await tick(3);
  ok('+  pièces : option enregistrée', lastCall('PUT', '/api/settings/rooms')[2].enabled === true);
  $('#tab-listen').click(); await tick(2);
  emit('clap', { source_id: 'mic', clap_count: 2, group_name: 'Clap', score: 0.8, ignored: true, room: 'Cuisine', timestamp: 1e9 }); await tick();
  ok('+  pièces : historique « entendu plus fort dans »', /entendu plus fort dans « Cuisine »/.test($('#history-list').textContent));
  // --- 6.80 : diagnostic de la detection sur la carte
  status = { running: true, source: 'x', since: Date.now() / 1000, sources: ['mic'],
             source_stats: { mic: { analyses_sautees: 3, coupures: 2, analyses: 100 } } };
  await w.CT.reloadStatus(); w.CT.renderStatus(); await tick();
  const diag = $('#src-mic [data-role="diag"]');
  ok('+  diagnostic : analyses sautées et coupures sur la carte', !!diag && !diag.hidden &&
     /3 analyse\(s\) ou bloc\(s\) perdu\(s\)/.test(diag.textContent) && /2 coupure\(s\)/.test(diag.textContent));
  status = { running: false, sources: [] }; await w.CT.reloadStatus(); w.CT.renderStatus(); await tick();
  ok('+  diagnostic : masqué détection arrêtée', diag.hidden);
  $('#tab-listen').click(); await tick(2);
  // --- 6.75 : ajouter un son connu par la recherche ; confirmation : focus sur Annuler
  {
    const micCard = $('#src-mic');
    if (!micCard.querySelector('dialog').open) { micCard.querySelector('[data-role="settings-open"]').click(); await tick(2); }
    const search = micCard.querySelector('.group-card [data-role="sound-search"]');
    const slug = search.closest('.group-card').getAttribute('data-slug');
    search.value = 'si'; search.dispatchEvent(new w.Event('input', { bubbles: true })); await tick();
    const errsBefore = errors.length;
    // 2e frappe avec des suggestions affichees : le filtre des pastilles
    // plantait sur les boutons « + son » (classe chip, sans case a cocher).
    search.value = 'siffl'; search.dispatchEvent(new w.Event('input', { bubbles: true })); await tick();
    ok('+  recherche : frappes successives avec suggestions, sans erreur', errors.length === errsBefore);
    const add = micCard.querySelector('.group-card[data-slug="' + slug + '"] [data-group-action="add-sound"][data-label="Whistling"]');
    ok('+  recherche : sons connus proposés à l’ajout (« Sifflement »)', !!add);
    if (add) { add.click(); await tick(3); }
    const put = lastCall('PUT', '/api/source/sound_whitelist');
    ok('+  son connu ajouté coché au groupe', !!put && put[2].label === 'Whistling' && put[2].enabled === true && put[2].group_slug === slug);
    const del = micCard.querySelector('[data-group-action="delete"]');
    if (del) {
      del.click(); await tick();
      ok('+  confirmation de suppression : focus sur « Annuler »', d.activeElement && d.activeElement.getAttribute('data-r') === 'cancel');
      $('.modal-backdrop [data-r="cancel"]').click(); await tick();
    }
    micCard.querySelector('[data-action="close-settings"]').click(); await tick(2);
  }
  // --- 6.71 : reglages recharges fusionnes dans les objets existants
  {
    const M = dom.window.CT.mergeInto;
    const g1 = { slug: 'clap', name: 'Clap', sound_whitelist: { A: true } }, g2 = { slug: 'old', name: 'Old' };
    const src1 = { id: 'c1', name: 'Cam', sound_groups: [g1, g2], gone: 1 };
    const target = { rtsp_sources: [src1], global: { delay: 1.5, sound_exclusions: ['X'] } };
    M(target, { global: { sound_exclusions: ['Y'], delay: 2 }, rtsp_sources: [{ id: 'c1', name: 'Salon', sound_groups: [{ slug: 'clap', name: 'Clap', sound_whitelist: { B: true } }, { slug: 'new', name: 'New' }] }, { id: 'c2', name: 'Deux' }] });
    ok('+  fusion : la source et son groupe gardent le même objet', target.rtsp_sources[0] === src1 && src1.sound_groups[0] === g1 && src1.name === 'Salon');
    ok('+  fusion : valeurs, clés et éléments retirés à jour', JSON.stringify(g1.sound_whitelist) === '{"B":true}' && !('gone' in src1) &&
       src1.sound_groups.map(g => g.slug).join() === 'clap,new' && target.rtsp_sources.length === 2 && target.global.sound_exclusions.join() === 'Y');
    ok('+  fusion : clés dans l’ordre du serveur', Object.keys(target).join() === 'global,rtsp_sources' && Object.keys(target.global).join() === 'sound_exclusions,delay');
    const dupe = { list: [{ id: 'd' }] };
    M(dupe, { list: [{ id: 'd', n: 1 }, { id: 'd', n: 2 }] });
    ok('+  fusion : identifiant en double, deux objets distincts', dupe.list[0] !== dupe.list[1] && dupe.list[0].n === 1 && dupe.list[1].n === 2);
  }
  // --- 6.71 : fenetre ouverte pendant un rechargement des reglages
  const camCard = () => $('#' + cam.domId);
  if (!camCard().querySelector('dialog').open) { camCard().querySelector('[data-role="settings-open"]').click(); await tick(2); }
  const srv = settings.rtsp_sources.find(r => r.id === cam.key);
  srv.name = 'Salon'; srv.sound_groups[0].sound_whitelist = { Knock: false };
  await dom.window.CT.reloadSettings(); await tick(3);
  ok('+  fenêtre ouverte : titre mis à jour après un rechargement', /Réglages · Salon/.test(camCard().querySelector('dialog .modal-head h2').textContent));
  const knock = camCard().querySelector('dialog .chip input[data-label="Knock"]');
  ok('+  fenêtre ouverte : groupes refaits depuis les réglages rechargés', !!knock);
  if (knock) { change(knock, true); await tick(3); }
  ok('+  son coché après un rechargement : état local à jour (plus d’objet périmé)',
     dom.window.CT.findSource(x => x.domId === cam.domId).data.sound_groups[0].sound_whitelist.Knock === true);
  // --- 6.71 : test d'une source non ecoutee, pas coupe par un statut d'une autre source
  // (carte du micro : identifiant unique, le faux serveur cree des cameras en double)
  status = { running: true, sources: [cam.sourceId] }; await dom.window.CT.reloadStatus(); dom.window.CT.renderStatus(); await tick();
  dom.window.CT.stopTest(); await tick();   // aucun test en cours (un clic l'arreterait)
  $('#src-mic dialog [data-action="test"]').click(); await tick(2);
  emit('source_status', { source_id: cam.sourceId, status: 'connected' }); await tick(2);
  ok('+  test d’une source non écoutée : pas coupé par le statut d’une autre source', !!dom.window.CT.state.testing && dom.window.CT.state.testing.domId === 'src-mic');
  dom.window.CT.stopTest();
  status = { running: false, sources: [] }; await dom.window.CT.reloadStatus(); dom.window.CT.renderStatus(); await tick();
  camCard().querySelector('[data-action="close-settings"]').click(); await tick(2);
  // --- 6.71 : ignorer partout un son utilise par un groupe demande confirmation
  settings.global.sound_exclusions = [];
  $('#tab-settings').click(); await tick(3);
  const putsBefore = calls.filter(c => c[0] === 'PUT' && c[1] === '/api/sound_exclusions').length;
  const usedChip = $('#available-list [data-exclude="Knock"]');
  ok('+  son utilisé signalé dans « Ignorer un autre son »', !!usedChip && /Salon/.test(usedChip.textContent));
  usedChip.click(); await tick(2);
  const confirmBox = $('.modal-backdrop [data-r="ok"]') && $('.modal-backdrop [data-r="ok"]').closest('.modal');
  ok('+  ignorer un son utilisé : confirmation demandée', !!confirmBox && /utilisé par/.test(confirmBox.textContent));
  $('.modal-backdrop [data-r="cancel"]').click(); await tick(2);
  ok('+  confirmation refusée : rien n’est ignoré', calls.filter(c => c[0] === 'PUT' && c[1] === '/api/sound_exclusions').length === putsBefore);
  $('#tab-listen').click(); await tick(2);
  d.body.click(); await tick();
  ok('+  clic hors des menus : tous fermés', $$('.menu-list').every(m => m.hidden) &&
     menuOf(otherCard).getAttribute('aria-expanded') === 'false');
  ok('JS : aucune erreur', errors.length === 0);
  results.forEach(r => console.log(r.join(' ')));
  if (errors.length) console.log(errors);
  const failures = results.filter(r => r[0] === 'KO ').length + (errors.length ? 1 : 0);
  console.log(failures + ' échec(s) sur ' + results.length);
  process.exit(failures ? 1 : 0);
})();
