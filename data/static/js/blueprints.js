/* Onglet Blueprints : blueprints Home Assistant fournis avec ClapTrap, leur
 * etat dans Home Assistant, et ajout ou mise a jour d'un clic (blueprints.py). */
(function () {
    'use strict';
    var CT = window.CT;
    var esc = CT.esc;
    var items = [], busy = {}, loading = false, failed = '';
    var IMPORT = 'https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=';
    var STATUS = {
        absent: ['Pas encore dans Home Assistant', ''],
        mise_a_jour: ['Mise à jour disponible', 'is-warn'],
        a_jour: ['Dans Home Assistant, à jour', 'is-ok']
    };

    function title(name) { return String(name || '').replace(/^ClapTrap\s+—\s+/, ''); }

    function render() {
        var box = CT.$('#bp-list');
        if (!box) return;
        if (failed) {
            // Hors de l'add-on, ou Home Assistant injoignable : import par le
            // site my.home-assistant (comme dans la documentation).
            box.innerHTML = '<p class="hint text-warn">' + esc(failed) + '</p>' +
                (items.length ? '<ul class="bp-fallback">' + items.map(function (b) {
                    return '<li><a href="' + esc(IMPORT + encodeURIComponent(b.source_url)) + '" target="_blank" rel="noopener">' +
                        esc(title(b.name)) + '</a></li>';
                }).join('') + '</ul>' : '') +
                '<button type="button" class="btn btn-ghost" data-bp-retry>Réessayer</button>';
            return;
        }
        if (!items.length) { box.innerHTML = '<p class="hint">' + (loading ? 'Chargement…' : 'Aucun blueprint fourni.') + '</p>'; return; }
        box.innerHTML = items.map(function (b) {
            var st = STATUS[b.status] || STATUS.absent;
            var action = b.status === 'absent' ? 'Ajouter à Home Assistant' : b.status === 'mise_a_jour' ? 'Mettre à jour' : '';
            return '<article class="bp-item" data-file="' + esc(b.file) + '"><div class="bp-main">' +
                '<h3>' + esc(title(b.name)) + '</h3><p class="hint">' + esc(b.description) + '</p>' +
                '<span class="pill ' + st[1] + '">' + st[0] + '</span></div>' +
                (action ? '<button type="button" class="btn ' + (b.status === 'absent' ? 'btn-primary' : 'btn-ghost') + '" data-bp-install="' + esc(b.file) + '"' +
                    (busy[b.file] ? ' disabled' : '') + '>' + (busy[b.file] ? 'Envoi…' : action) + '</button>' : '') +
                '</article>';
        }).join('');
    }

    function load() {
        if (loading) return;
        loading = true;
        render();
        CT.api('GET', '/api/blueprints').then(function (d) {
            items = d.blueprints || [];
            failed = d.error || '';
        }).catch(function (err) {
            failed = err.message;
        }).finally(function () {
            loading = false;
            render();
        });
    }
    CT.onBlueprintsOpen = load;

    function install(file) {
        var b = items.filter(function (x) { return x.file === file; })[0];
        if (!b || busy[file]) return;
        var go = b.status === 'mise_a_jour'
            ? CT.dialog('Mettre à jour « ' + title(b.name) + ' » ? Les automatisations qui l\'utilisent passent tout de suite à la ' +
                        'nouvelle version (si elle demande un nouveau réglage, ouvrez-les pour le renseigner).', {okLabel: 'Mettre à jour'})
            : Promise.resolve(true);
        go.then(function (ok) {
            if (!ok) return;
            busy[file] = true;
            render();
            return CT.api('POST', '/api/blueprints/install', {file: file}).then(function (d) {
                b.status = 'a_jour';
                b.path = d.path;
                CT.success('« ' + title(b.name) + ' » ' + (d.updated ? 'mis à jour' : 'ajouté à Home Assistant'));
            }).catch(function (err) {
                CT.error('Blueprint non envoyé : ' + err.message);
            }).finally(function () {
                delete busy[file];
                render();
            });
        });
    }

    document.addEventListener('click', function (e) {
        var t = e.target.closest && e.target.closest('[data-bp-install],[data-bp-retry]');
        if (!t) return;
        if (t.hasAttribute('data-bp-retry')) load();
        else install(t.getAttribute('data-bp-install'));
    });
})();
