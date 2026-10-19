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
        different: ['Dans Home Assistant, version différente', 'is-warn'],
        a_jour: ['Dans Home Assistant, à jour', 'is-ok']
    };
    var ACTIONS = {
        absent: ['Ajouter à Home Assistant', 'btn-primary'],
        mise_a_jour: ['Mettre à jour', 'btn-primary'],
        different: ['Remplacer par la version de ClapTrap', 'btn-ghost'],
        a_jour: ['Réinstaller', 'btn-ghost']
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
            var act = ACTIONS[b.status] || ACTIONS.absent;
            return '<article class="bp-item" data-file="' + esc(b.file) + '"><div class="bp-main">' +
                '<h3>' + esc(title(b.name)) + '</h3><p class="hint">' + esc(b.description) + '</p>' +
                '<span class="pill ' + st[1] + '">' + st[0] + '</span></div>' +
                '<button type="button" class="btn ' + act[1] + '" data-bp-install="' + esc(b.file) + '"' +
                    (busy[b.file] ? ' disabled' : '') + '>' + (busy[b.file] ? 'Envoi…' : act[0]) + '</button>' +
                '</article>';
        }).join('');
    }

    function load() {
        if (loading) return;
        loading = true;
        render();
        CT.api('GET', '/api/blueprints').then(function (d) {
            // Envoi en cours : l'etat d'avant l'envoi ne remplace pas le sien.
            var before = items;
            items = (d.blueprints || []).map(function (n) {
                return busy[n.file] ? (before.filter(function (o) { return o.file === n.file; })[0] || n) : n;
            });
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
        var name = '« ' + title(b.name) + ' »';
        var msg = {
            mise_a_jour: 'Mettre à jour ' + name + ' ? Les automatisations qui l\'utilisent passent tout de suite à la ' +
                         'nouvelle version (si elle demande un nouveau réglage, ouvrez-les pour le renseigner).',
            different: 'La version de ' + name + ' dans Home Assistant diffère de celle de ClapTrap (importée ailleurs, ' +
                       'modifiée, ou plus récente). La remplacer par celle de ClapTrap ? Les automatisations qui ' +
                       'l\'utilisent la prendront tout de suite.',
            a_jour: 'Réinstaller la version de ClapTrap de ' + name + ' ? Utile si elle a été modifiée dans Home Assistant.'
        }[b.status];
        var go = msg ? CT.dialog(msg, {okLabel: ACTIONS[b.status][0]}) : Promise.resolve(true);
        go.then(function (ok) {
            if (!ok) return;
            busy[file] = true;
            render();
            return CT.api('POST', '/api/blueprints/install', {file: file}).then(function (d) {
                // Element actuel (la liste a pu etre rechargee pendant l'envoi).
                var cur = items.filter(function (x) { return x.file === file; })[0] || b;
                cur.status = 'a_jour';
                cur.path = d.path;
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
