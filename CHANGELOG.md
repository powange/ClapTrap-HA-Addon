# Changelog

## 6.19.1

### UI : gestion des groupes de sons additionnels

- Nouveau panneau "Groupes additionnels" dans chaque source audio
  (micro, RTSP, VBAN). Permet de creer, renommer, supprimer des groupes
  et configurer pour chacun : son seuil, ses entites HA (nb de claps),
  et la liste des sons qui declenchent.
- Validation d'exclusivite : un son ne peut etre actif que dans un seul
  groupe d'une meme source. Les autres groupes affichent la case grisee
  avec un tooltip indiquant le groupe ou il est actif. Si l'API retourne
  409, la case est decoche automatiquement.
- Le groupe par defaut "Clap" reste gere par l'UI existante (seuil et
  sons du micro / RTSP / VBAN au niveau source) pour compat retro.
- Nouvelles routes API : POST/PUT/DELETE /api/source/sound_groups.

## 6.19.0

### Backend : groupes de sons par source (UI a venir en 6.19.1)

- Chaque source audio (micro / RTSP / VBAN) peut desormais contenir plusieurs
  groupes de sons independants, chacun avec sa propre whitelist, son seuil
  et ses entites HA. Permet par exemple de differencier "2 claps des mains"
  et "2 toc sur la table" sur le meme micro.
- Migration auto : les sources existantes obtiennent un groupe unique nomme
  "Clap" derive de leurs anciens champs (`sound_whitelist`, `threshold`,
  `ha_entities`). Aucune action utilisateur requise, les entites HA
  existantes restent identiques.
- Topics MQTT : nouveaux groupes utilisent `claptrap/{source}_{groupe}_{n}claps/state`.
  Le groupe par defaut "clap" garde le naming historique
  `claptrap/{source}_{n}claps/state` (compat HA existante).
- Detection : un seul classifier audio par source (pas de surcout CPU), mais
  evaluation independante par groupe avec son propre seuil.
- API : `PUT /api/source/sound_whitelist` accepte un champ `group_slug`
  optionnel. Refuse l'activation d'un son deja active dans un autre groupe
  (regle d'exclusivite, code 409).
- L'UI de gestion des groupes (CRUD, validation visuelle d'exclusivite) sera
  livree en 6.19.1.

## 6.18.2

### Detection : seuil applique par son individuellement

- Le seuil de precision s'applique desormais a chaque son whitelist
  individuellement, plus a la somme cumulee des scores. Au moins un son
  doit depasser le seuil pour declencher un clap.
- Le score affiche/transmis est le meilleur score individuel observe sur
  la fenetre multi-clap (et non plus la somme, qui pouvait depasser 100%).
- L'historique liste uniquement les sons ayant franchi le seuil pendant
  la fenetre.

## 6.18.1

### Historique : afficher les sons ayant declenche le clap

- Chaque entree de l'historique de l'interface web liste desormais les sons
  detectes (label + score max) qui ont contribue a la detection du clap, en
  plus du nombre de claps, du score global et de la source.
- Les labels contributeurs sont accumules sur toute la fenetre multi-clap
  (max score par label) et transmis via socket.io, webhook et l'API
  `/api/detection/history`.

## 6.18.0

### Suppression du module Wyoming STT

- Retrait complet de la fonctionnalite Wyoming STT (ESPHome voice_assistant
  interceptor) : serveur, UI, route API, discovery, documentation.
- Fichiers supprimes : `data/wyoming_server.py`.
- Dependance `wyoming>=1.5` retiree de `requirements.txt`.
- Port 10700 et `discovery: [wyoming]` retires de `config.yaml`.
- Les sources existantes (micro, RTSP, VBAN) ne sont pas affectees.

## 6.17.7

### UI : badge unicast / multicast sur les sources VBAN

- Chaque source VBAN affiche un badge "unicast" (bleu) ou "multicast"
  (orange) a cote du nom dans l'en-tete de son onglet.
- Detection automatique via l'IP : 224.0.0.0/4 → multicast, sinon
  unicast. Aucun champ supplementaire dans settings.

## 6.17.6

### Fix : noms d'entites HA etires sur toute la largeur

- `.ha-entity-id` passait en `display: block` (pleine largeur) au lieu
  de `inline-block` (largeur du texte). `.ha-entities-list` passe en
  `flex-wrap: wrap` pour les aligner cote a cote si l'espace le permet.

## 6.17.5

### UI : bouton "Nettoyer" dans les sons detectes par source

- Bouton poubelle a cote du titre "Sons detectes" qui supprime tous
  les labels non coches d'un coup. Les coches (sons actifs dans la
  detection) sont preserves.
- Endpoint `POST /api/source/sound_whitelist/cleanup {kind, source_key}`.
- Nettoie aussi le "seen set" du detecteur actif pour que les labels
  supprimes puissent etre re-proposes s'ils reapparaissent.

## 6.17.4

### Fix : seuils de logging en dur remplaces par score_threshold

- `hot_labels` log : `score > 0.1` → `score >= self.score_threshold`.
- `score_sum` debug log : `> 0.1` → `> self.score_threshold * 0.2`
  (20% du seuil : log quand on approche d'une detection, pas sur du
  bruit irrelevant).

## 6.17.3

### Fix : labels sous le seuil de precision affiches dans Detection

- Le filtre d'affichage des labels dans la colonne Detection etait
  code en dur a `score > 0.1`. Remplace par `score >= score_threshold`
  (le seuil de precision configure par source) pour ne montrer que
  les sons reellement significatifs.

## 6.17.2

### Fix : les sons exclus apparaissaient dans la colonne Detection

- Le top 3 des labels envoye via socket `labels` (colonne Detection de
  la table des sources) n'etait pas filtre par les exclusions globales.
- Les labels exclus sont maintenant retires avant le tri + troncature
  du top 3, donc ils ne s'affichent plus dans la detection live.

## 6.17.1

### Fix : les sons exclus globalement apparaissaient quand meme dans
les listes "Sons detectes" des sources

- L'auto-decouverte (`sound_seen`) ne verifie pas si le label est dans
  `global.sound_exclusions` avant de l'ajouter a la whitelist de la
  source. Ajout du filtre dans `_build_sound_seen_handler` : les labels
  exclus ne sont plus persistes ni emis via socket.

## 6.17.0

### Fix : detection VBAN multi-source (1 seule source fonctionnait)

- L'ancien design avait UN seul `audio_callback` + UN seul ring buffer
  partage. Chaque `run_vban_source` ecrasait le callback precedent, et
  l'audio de toutes les sources VBAN etait melange dans le meme buffer.
  Resultat : seule la derniere source enregistree detectait.
- Nouveau design : `add_source_callback(ip, callback)` enregistre un
  buffer + callback dedie par IP source. Les paquets VBAN sont routes
  vers le bon buffer selon l'IP source, sans melange.
- `remove_source_callback(ip)` a l'arret de la detection.
- Retro-compatible : si aucun per-IP callback n'est enregistre, le
  legacy `set_audio_callback` fonctionne toujours.

## 6.16.2

### Fix : onglet VBAN non cree apres ajout via la modale

- `buildTabs` et `switchTab` etaient definis dans un IIFE ferme,
  inaccessibles depuis le script de la modale VBAN (scope separee).
  Exposes sur `window` comme `rebuildSourcesTable` l'etait deja.

## 6.16.1

### Fix : suppression d'une source VBAN ne persistait pas

- Le JS envoyait `{source: {...}}` au lieu de `{...}` directement —
  le backend cherchait `ip` et `stream_name` a la racine, ne les
  trouvait pas, renvoyait 400 silencieusement. L'onglet disparaissait
  localement mais revenait au reload.

## 6.16.0

### Nouvelle carte : exclusions de sons globales

- Sous "Reglages de detection", nouvelle carte collapsible "Exclusions
  globales" avec deux listes :
  - **Exclus** : labels ignores par toutes les sources meme s'ils sont
    coches dans la whitelist d'une source. Bouton x pour retirer.
  - **Autres sons detectes** : union de tous les labels deja vus par
    n'importe quelle source (diff: - exclus). Cocher un item le deplace
    dans la liste d'exclusion.
- Nouveau champ persistant `settings.global.sound_exclusions: [...]`.
- Scoring audio modifie pour ignorer les labels exclus :
  `score = sum(cat.score if whitelist[cat] and cat not in exclusions)`.
- Endpoints :
  - `GET  /api/sound_exclusions` -> `{excluded, available}`
  - `PUT  /api/sound_exclusions {label, excluded}`
- Mise a jour live via `update_global_exclusions(labels)` sur tous les
  detecteurs actifs, sans restart.
- Rafraichissement automatique de la liste "autres sons" quand un
  nouveau label est decouvert en auto-discovery (event `sound_seen`).

## 6.15.1

### UI : bouton "Reinitialiser" dans les reglages de detection

- Bouton en bas de la carte qui repositionne les 4 champs a leurs
  valeurs par defaut (fenetre 1.5s / cooldown 0.08s / ratio 3.0x /
  reset 0.3s) et sauve en une requete PUT /api/settings/advanced.

## 6.15.0

### Nouveau reglage : "Reset du pic"

- Ajout d'un 4e champ dans les reglages de detection : `peak_reset`
  (defaut 0.3 s). Si `above` reste True pendant plus de N secondes
  (ex: bruit de fond ou reverb qui maintient l'amplitude haute), on
  force `above=False` pour pouvoir compter les claps suivants.
- Corrige le cas "2 claps rapides comptes comme 1 seul" quand
  l'ambient entre les deux reste au-dessus de 60% du seuil.
- Renommage : "Parametres avances" -> "Reglages de detection".
- Cable de bout en bout : initialize(), update_advanced_params,
  /api/settings/advanced, /api/detection/start, defaults settings.

## 6.14.6

### Fix : logs "Son fort detecte" continuaient apres arret detection

- Le log etait INFO-level et declenche a chaque paquet VBAN depassant
  0.3 d'amplitude, meme quand la detection est stoppee (le thread
  VBAN ecoute en permanence). Passage en DEBUG.
- `run_vban_source` detache aussi son `audio_callback` en sortie,
  pour liberer la reference au detecteur mort.

## 6.14.5

### Fix : 2 claps rapides comptes comme 1 seul sur les sources VBAN

- Le recepteur VBAN bufferisait 1 seconde avant d'appeler le
  detecteur. Resultat : `process_audio` voyait un seul peak pour
  l'ensemble du chunk, meme si 2 claps distincts se trouvaient
  dedans (0.7 s d'ecart typiquement).
- Emission par paquets de 100 ms (1600 echantillons a 16 kHz),
  meme granularite que le micro et RTSP, donc le compteur de peaks
  separe correctement les claps rapides.

## 6.14.4

### Fix : mapping RTSP source_id pour la mise a jour live de la whitelist

- `_source_id_for('rtsp', ...)` reconstruisait l'URL en ajoutant
  `rtsp://` si absent, alors que `classify.run_rtsp_source` utilise
  `src['url']` brut. Mismatch quand l'utilisateur entrait une URL
  sans le prefix : settings sauves mais detecteur live pas mis a
  jour, le nouveau son n'etait pris en compte qu'apres restart.
- Desormais les deux cotes utilisent la meme valeur brute.

## 6.14.3

### Retrait d'un son de la whitelist d'une source

- Chaque item de la liste "Sons detectes" a desormais un bouton x
  pour le retirer completement (pas juste le decocher).
- Endpoint `DELETE /api/source/sound_whitelist {kind, source_key, label}`.
- Mise a jour live du detecteur : le label est retire de sa whitelist
  en memoire ET du "seen set", donc si l'auto-decouverte l'entend a
  nouveau au-dessus du seuil il sera re-propose.

## 6.14.2

### UI : logo VBAN plus compact dans les onglets

- Reduction du conteneur du logo VBAN de 4.5em x 1.2em a 2.6em x
  0.85em pour qu'il ne mange plus la largeur des onglets tout en
  restant lisible.

## 6.14.1

### Fix : bouton "Tester le flux" VBAN etire sur toute la largeur

- Le bouton VBAN etait wrappe dans `.setting-item` (grid avec
  `justify-items: stretch`) au lieu de `.mic-test-section` utilise
  cote RTSP. Correction : meme conteneur que RTSP donc meme taille.

## 6.14.0

### Whitelist de sons par source

- Chaque source (micro / RTSP / VBAN) a sa propre liste de sons YAMNet
  consideres comme "clap". Defaut : Clapping, Hands, Applause.
- La section "Sons detectes" dans l'onglet de chaque source affiche
  des cases a cocher : decocher = son ignore par le scoring.
- **Auto-decouverte** : chaque label entendu au-dessus du seuil de
  precision de la source est ajoute automatiquement a sa liste,
  **decoche**. L'utilisateur peut le cocher pour l'ajouter a la
  detection sans toucher au code.
- `category_allowlist` MediaPipe retire : tous les labels YAMNet
  peuvent desormais etre proposes (aboiements, klaxons, sifflet, etc.).
- `CLAP_WEIGHTS` / `NOISE_WEIGHTS` codes en dur remplaces par la
  whitelist utilisateur.
- Application live via `update_source_whitelist(source_id, label, enabled)`
  sans restart de la detection. Endpoint :
  `PUT /api/source/sound_whitelist {kind, source_key, label, enabled}`.
- Socket event `sound_seen` pour rafraichir la liste sans reload.

## 6.13.7

### UI : logo VBAN cadre sur le contenu visible

- Le SVG telecharge etait un bitmap 512x512 avec ~37% de padding
  transparent haut et bas. Recadrage sur la zone visible (512x137)
  et suppression du pad.
- CSS adapte : conteneur rectangulaire (hauteur 1.2em, largeur 4.5em
  ~ ratio 3.74:1) pour que le logo remplisse sa ligne dans les onglets.

## 6.13.6

### Fix : 404 sur l'icone VBAN sous ingress HA

- Le CSS est servi via `/css/<ver>/style.css` (endpoint Flask versionne)
  et la resolution relative `../img/...` tombait sur `/css/img/...` qui
  n'est pas servi.
- Chemin corrige vers `../../static/img/vban-icon.svg` pour taper le
  handler `/static/*` par defaut de Flask (ingress-aware).

## 6.13.5

### UI : logo VBAN officiel a la place de l'emoji

- Telechargement du SVG officiel VBAN (music-assistant.io) dans
  `data/static/img/vban-icon.svg`.
- Classe CSS `.source-type-icon.icon-vban` appliquee partout ou
  l'emoji antenne parabolique etait utilise : menu d'ajout, onglet
  de source, header du panneau VBAN, bouton de test.

## 6.13.4

### UI : icone de type dans le panneau RTSP

- Ajout de l'icone camera (&#x1F4F9;) avant le champ nom de la source
  RTSP selectionnee (mic et VBAN l'avaient deja).

## 6.13.3

### UI : icone de type dans les onglets de sources

- Chaque onglet de source affiche l'icone de son type avant le nom :
  &#x1F3A4; micro, &#x1F4F9; RTSP, &#x1F4E1; VBAN.

## 6.13.2

### UI : icone dediee pour les sources VBAN

- Remplacement de la note de musique (&#x1F3B5;) par une antenne
  parabolique (&#x1F4E1;) : plus representatif de l'audio reseau
  et visuellement distinct des autres sources (mic, camera).

## 6.13.1

### Fix : la layout 2 colonnes n'utilisait pas toute la largeur

- `.dashboard.two-cols` avait toujours `max-width: 1400px` (herite du
  mode une colonne pour confort de lecture). Mis a `none` : les deux
  colonnes remplissent toute la largeur disponible.

## 6.13.0

### UI : layout en deux colonnes sur grand ecran

- A >= 960px de largeur, l'interface s'organise en deux colonnes :
  - Colonne gauche : Gestion des sources / Wyoming STT / Options
  - Colonne droite : Parametres avances / Detection / Historique
- En dessous de 960px, les cartes restent empilees en une colonne
  (tablette/mobile inchange).
- Le reordonnancement est fait en JS au DOM ready (pas de changement
  de l'ordre HTML), les sections gardent ainsi leur ordre semantique
  pour l'accessibilite.

## 6.12.0

### Refactor : suppression de code mort et unification webhooks

- Suppression de `data/vban_processor.py` (VBANAudioProcessor, 379 lignes)
  et de son test associe — remplace depuis longtemps par
  `vban_detector_new.py` + `classify.run_vban_source`.
- Suppression de `data/static/js/modules/rtspSources.js` (306 lignes)
  — aucun import en production.
- Suppression des endpoints `/api/rtsp/webhook` et `/api/rtsp/enabled`
  non appeles (tout passe par le endpoint generique `/api/rtsp/stream/<id>`).
- Unification des webhooks via `webhook.send_webhook_async(url, payload)` :
  un seul `WebhookManager` + un seul `ThreadPoolExecutor` partages. Les
  trois chemins (mic/RTSP/VBAN detection + Wyoming) utilisent le meme
  helper avec retry et validation d'URL.

## 6.11.0

### Refactor : factorisation des handlers sources (mic / RTSP / VBAN)

- Quatre helpers JS introduits dans `bindTabEvents` :
  - `refreshAfterSourceChange()` : refresh table + updateStartButton +
    sync du bouton Start/Stop et du badge source actif.
  - `bindEnabledToggle(el, opts)` : cablage uniforme des switches
    activer/desactiver (mic, RTSP, VBAN) avec PUT + refresh.
  - `bindLiveSlider(el, opts)` : cablage uniforme des sliders (label en
    live + PUT debounce au change), utilise pour threshold + gain.
  - `bindDebouncedInput(el, opts)` : cablage uniforme des input text
    (url/name/webhook) avec PUT debounce.
  - `bindTestButton(el, opts)` : bouton start/stop de VU-metre (RTSP,
    VBAN) avec basculement label + affichage VU.
- Tous les blocs dupliques (mic threshold, mic enabled, mic webhook,
  rtsp threshold/volume/url/name/enabled/webhook/test, vban enabled/
  threshold/gain/webhook/test) sont reecrits en 4-6 lignes chacun.
- Aucun changement fonctionnel ; uniquement organisation du code.

## 6.10.3

### Fix : VU-metre VBAN ignore le gain + table des sources non rafraichie

- Le tap de test VBAN applique maintenant le gain configure (live
  depuis `_vban_gains` ou fallback settings) au peak avant le calcul
  des dB, pour refleter ce que le classifier voit reellement.
- Les toggles enabled/disabled micro et RTSP appellent maintenant
  `rebuildSourcesTable()` pour que la table des sources se mette a
  jour immediatement apres la bascule (meme comportement que VBAN).

## 6.10.2

### Gain audio pour les sources VBAN

- Ajout d'un slider "Volume" (1x–20x) dans l'onglet de chaque source
  VBAN, identique a RTSP.
- Application en temps reel via `update_vban_gain(ip, gain)` — pas de
  redemarrage de detection necessaire.
- Champ `gain` propage via `build_sources_from_settings` et
  `/api/vban/update`.

## 6.10.1

### Fix : toggle enabled d'une source VBAN ne faisait rien

- Le checkbox `.vban-enabled-toggle` n'avait aucun handler JS : cliquer
  changeait uniquement l'apparence, sans requete serveur. Ajout du
  handler qui PUT `/api/vban/update` avec `enabled`.
- `/api/vban/update` relance maintenant la detection si `enabled` ou
  `threshold` change, comme pour RTSP.

## 6.10.0

### Test audio pour les sources VBAN (VU-metre)

- Ajout d'un bouton "Tester le flux" dans l'onglet de chaque source
  VBAN, avec VU-metre temps reel (dB + barre de niveau), identique aux
  onglets micro et RTSP.
- Le detecteur VBAN partage est sollicite via un "tap" filtre par IP
  (pas de second socket UDP, pas de conflit de port).
- Endpoints `/api/vban/test/start` et `/stop`.

## 6.9.3

### Fix : bouton "Tester" webhook inactif pour VBAN et RTSP

- Le handler cherchait `data-source="vban-..."` (tiret) alors que le
  HTML genere `vban_...` (underscore). Resultat : aucun input trouve,
  le clic ne declenchait rien de visible.
- Simplification : le bouton prend le 1er `input[type="url"]` de son
  conteneur `.webhook-input-with-test`. Marche pour mic/rtsp/vban/etc.

## 6.9.2

### Fix : payload POST /api/vban/save enveloppe a tort

- Le JS postait `{source: {...}}` mais le backend lit la racine du
  JSON (`request.json['name']` etc.) → "Donnees manquantes".
- On envoie maintenant directement l'objet source.

## 6.9.1

### UI : modale integree pour l'ajout de sources VBAN

- Remplacement du `window.prompt()` navigateur (liste texte + numero a
  saisir) par une vraie modale avec :
  - liste cliquable des sources detectees (nom + IP + sample rate + channels)
  - badge "multicast" si IP dans 224.0.0.0/4
  - bouton "Rafraichir" pour relancer le scan
  - formulaire "Ajout manuel" (pour IP multicast non auto-decouverte
    ou sources dont on a l'IP hors ligne)

## 6.9.0

### Support des sources VBAN multicast (auto-detecte)

- Si l'IP d'une source VBAN sauvegardee est dans la plage multicast
  (224.0.0.0/4, ex: 239.255.x.x), le socket UDP joint automatiquement
  le groupe via `IP_ADD_MEMBERSHIP`.
- Re-sync periodique (toutes les ~10s) pour prendre en compte les
  ajouts/retraits de sources depuis l'UI sans redemarrage.
- `SO_REUSEPORT` ajoute pour permettre plusieurs consommateurs
  multicast du meme flux sur la meme machine.
- Deconnexion propre (`IP_DROP_MEMBERSHIP`) quand une source multicast
  est supprimee.

## 6.8.7

### Fix : reuse discovery record si URI inchangee

- Plutot que DELETE+POST systematique, on inspecte les entrees existantes :
  si une a deja la bonne URI on la garde (meme UUID), les duplicats et
  URIs obsoletes sont supprimes.
- Pas de churn inutile pour HA -> zero risque de perturber un pipeline
  vocal deja configure sur ClapTrap.

## 6.8.6

### Fix : multiples cartes ClapTrap dans Decouvertes apres redemarrage

- Le UUID de discovery etait perdu a chaque redemarrage du process
  (stocke en memoire), donc le DELETE ne nettoyait rien.
- A chaque start on enumere `/discovery`, on DELETE toutes les entrees
  `service=wyoming` publiees par notre addon, puis on re-publie une
  seule fois.

## 6.8.5

### Fix : les fallbacks de detection Wyoming etaient court-circuites

- Si /discovery renvoyait 401, on sortait immediatement sans jamais
  essayer les fallbacks /addons, mapping connu, ni HA Core.
- Maintenant chaque source est testee independamment, et les erreurs
  sont collectees dans `_meta.errors` pour diagnostic.
- `hassio_role` passe a `admin` (au cas ou /addons exige aussi ce role).

## 6.8.4

### Fix : 4eme fallback de detection Wyoming via HA Core

- Si /discovery et /addons restent vides, on interroge directement
  l'API HA Core (`/core/api/config/config_entries/entry`) pour lister
  les integrations Wyoming deja configurees dans HA et reutiliser
  leur host:port.
- Le selecteur affiche maintenant un compteur de diagnostic quand vide :
  `(addons:N, discovery:N, ha:N)` pour identifier d'ou vient le souci.

## 6.8.3

### Fix : 401 sur /discovery (detection des cibles Wyoming)

- Ajout de `hassio_role: manager` dans config.yaml pour permettre la
  lecture des services discovery et la liste des addons installes.
- Sans ce role, l'endpoint /discovery renvoyait 401 et le selecteur
  "Cible Whisper detectee" restait vide.

> **Permissions** : apres l'update, HA demandera de re-valider les
> permissions de l'addon (banniere "Permissions need attention").

## 6.8.2

### Fix : detection des addons Wyoming meme sans accees /addons/<slug>/info

- Ajout d'un mapping en dur des addons Wyoming connus (Whisper, Faster
  Whisper, Piper, openWakeWord) — repos officiel et community 47701997.
- Le selecteur propose ces cibles si le slug est present dans la liste
  des addons installes (pas besoin de lire les details restreints).
- Hostname HA = slug avec `_` -> `-` (convention Supervisor).

## 6.8.1

### Fix : detection des cibles Wyoming meme si /discovery est vide

- L'endpoint `/api/wyoming/discover-targets` enumere maintenant aussi
  les addons installes et garde ceux qui declarent `wyoming` dans leur
  discovery (Whisper, Piper, openWakeWord, etc.), avec leur hostname et
  port reseau. Marche meme quand l'addon ne s'est pas auto-publie via
  le supervisor /discovery.

## 6.8.0

### Auto-detection des serveurs Whisper Wyoming dans l'UI

- Nouveau endpoint `GET /api/wyoming/discover-targets` qui interroge le
  supervisor (`/discovery`) pour lister les services Wyoming installes
  (Whisper, etc.), avec leur host + port + nom d'addon.
- Nouveau selecteur "Cible Whisper detectee" dans la card Wyoming :
  choisir une option remplit automatiquement host ET port.
- Bouton "rafraichir" pour relancer la detection.

## 6.7.4

### Fix : URI de discovery Wyoming inutilisable en host_network

- Avec `host_network: true`, le hostname docker de l'addon
  (`<id>-claptrap`) n'est pas resolvable depuis le container HA core,
  donc l'integration Wyoming ne pouvait pas se connecter au serveur.
- L'addon detecte maintenant ce mode et publie l'IP primaire de l'hote
  (via `supervisor /network/info`) dans l'URI de discovery, par ex.
  `tcp://192.168.1.20:10700`.

## 6.7.3

### Fix : warning auto-start trompeur quand seul Wyoming est actif

- Le serveur Wyoming est independant du pipeline classify (mic/rtsp/vban).
- Lorsque seules les sources Wyoming tournent, le message "aucune source
  activee" devient un INFO explicatif au lieu d'un WARNING.

## 6.7.2

### Fix : Wyoming visible dans la table sources des l'activation

- Une ligne "Wyoming STT (port N)" apparait dans la table des sources des
  que Wyoming est active dans les settings (avant qu'aucun Atom soit
  connecte). Les Atoms restent ajoutes dynamiquement en lignes filles a
  leur premiere connexion.

## 6.7.1

### Wyoming : auto-decouverte par Home Assistant

- Ajout de `discovery: [wyoming]` dans config.yaml.
- A chaque demarrage du serveur Wyoming, l'addon s'enregistre aupres du
  supervisor (`POST /discovery`) avec son URI `tcp://<hostname>:<port>`.
- Au stop / port change, l'enregistrement est supprime puis re-publie.
- HA cree alors automatiquement une entree "Wyoming Protocol" pointant
  vers ClapTrap, utilisable comme fournisseur STT dans les pipelines vocaux.

## 6.7.0

### Refonte de la barre de detection en table par source

- La premiere card affiche maintenant un tableau (1 ligne par source active)
  avec deux colonnes : Source / Detection.
- Chaque ligne montre le label dominant detecte en temps reel ou un flash
  vert "&#x1F44F; N claps XX%" lors d'une detection.
- Les sources Wyoming apparaissent dynamiquement quand un Atom se connecte.
- La table se reconstruit automatiquement quand une source RTSP/VBAN est
  ajoutee, supprimee ou (de)activee.

## 6.6.2

### Fix : URL d'auto-save Wyoming sous ingress HA

- Le script d'auto-save Wyoming s'executait avant la definition de
  `window.basePath`, produisant une URL sans le prefixe ingress.
- Wrap dans `DOMContentLoaded` + lecture deferee de `basePath` dans le
  handler de save.

## 6.6.1

### Wyoming : auto-save UI + hot-reload du serveur

- Les champs Wyoming (port, forward host/port, threshold, webhook, enabled) sont
  maintenant auto-sauvegardes (debounce 400ms) — plus besoin de bouton "Enregistrer".
- Toggle "Activer" et changement de port appliquent immediatement la configuration
  via restart en place du serveur Wyoming. Plus besoin de redemarrer l'addon.

## 6.6.0

### Nouveau mode source : Wyoming STT (ESPHome voice_assistant)

- Serveur Wyoming asyncio multi-client (port configurable, defaut 10700)
- Intercepte les flux audio des Atom Echo (ou autres appareils ESPHome `voice_assistant`)
- Detection YAMNet par appareil (non-bloquante) avec hostname resolu par reverse DNS
- Forward transparent vers le serveur Whisper Wyoming reel (host + port configurables)
- Reponse STT + TTS renvoyee a l'Atom d'origine (pas de broadcast)
- Webhook par clap : `{"event":"clap_detected","source":"atom-chambre","count":N}`
- Si Whisper est indisponible : erreur loggee, detection YAMNet poursuit sans crash
- Options exposees dans l'interface web ; port 10700 expose dans config.yaml
- Sources existantes (VBAN / RTSP / micro local) inchangees

## 6.0.0

### Entites HA par nombre de claps

- Chaque source cree jusqu'a 4 binary_sensors : `_1clap`, `_2claps`, `_3claps`, `_4claps`
- Chaque entite ne s'active que pour le nombre de claps correspondant
- Checkboxes dans l'interface pour activer/desactiver chaque entite par source (defaut: 1 et 2 claps)
- Les entites sont creees au demarrage de l'addon (pas seulement a la detection)
- Plus simple pour les automations HA : un trigger par entite
- Endpoint PUT /api/microphone/ha-entities

## 5.5.3

- Fix 'os' not defined in cleanup endpoint

## 5.5.2

### Filtrage des labels MediaPipe par allowlist

- Le classifier ne retourne plus que les labels pertinents pour les claps (Hands, Clapping, Applause, Slap, Whack, Knock, Tap, Snap, Bang, Cap gun, Crack) + Silence et bruits a filtrer.
- Plus de "Duck", "Bird", "Fowl", "Animal" dans les resultats — elimines a la source par MediaPipe.
- score_threshold mis a 0 (aucun filtrage en amont, tout est traite par notre scoring).
- Les logs sont plus lisibles car seuls les labels pertinents apparaissent.

## 5.5.1

- Suppression du seuil adaptatif. Le seuil de precision configure par l'utilisateur est applique directement. Le volume de la source permet d'amplifier le signal, la precision permet d'ajuster la sensibilite. Plus simple, plus previsible.

## 5.5.0

### Suppression detection par pic seul (trop de faux positifs)

- Les bruits d'animaux/environnement (oiseaux, etc.) declenchaient des faux claps car la detection par pic seul acceptait tout son non-silence.
- Maintenant seul le classifier YAMNet confirme les claps, avec un seuil adaptatif : si un pic d'energie est detecte, le seuil est abaisse a 15% pour accepter des scores faibles de clap.
- Plus fiable : pas de clap sans que YAMNet voie au moins un label de type "Hands/Clapping/Slap".

## 5.4.9

- Fix faux positifs : seuil minimum de pic monte de 0.008 a 0.05 (les bruits a 0.03 ne declenchent plus). Detection par pic exige que Silence ne soit PAS le top label du classifier.

## 5.4.7

### Detection par pic d'energie

- La normalisation clippait le signal a 1.0, faisant que YAMNet classait les claps comme "Duck/Fowl". Normalisation reduite (cible 0.15, max 5x, seulement pour les signaux < 0.05).
- Nouveau mode de detection : si un pic d'energie est detecte ET que le classifier ne voit pas du "Silence" pur, le clap est confirme meme sans labels "Hands/Clapping". Couvre les cas ou YAMNet classe le clap comme "Sound effect", "Cap gun", "Fowl", etc.

## 5.4.6

- Seuil adaptatif baisse de 30% a 15% quand un pic d'energie est detecte. Un seul Slap/smack a 0.109 suffit maintenant pour confirmer un clap (score 0.065 > seuil 0.045).

## 5.4.5

- Les logs affichent maintenant le nom de la source (ex: "RTSP: Bureau 2") au lieu de l'URL technique dans tous les messages (Audio stats, labels, pics, claps).

## 5.4.4

### Detection hybride pic + classifier

- Un seul clap etait souvent manque car YAMNet le classait comme "Sound effect" ou "Cap gun" au lieu de "Hands/Clapping" (score trop bas pour le seuil).
- Maintenant quand un pic d'energie a ete detecte dans les 2 dernieres secondes, le seuil du classifier est abaisse a 30% de la valeur configuree. Si un pic existe ET que le classifier voit un son impulsif (meme faible), le clap est confirme.
- Ajout de labels "Cap gun", "Snap", "Crack" dans le scoring (sons impulsifs similaires a un clap).

## 5.4.3

- max_results passe de 5 a 10 dans la route de detection et l'auto-start (le classifier retourne plus de labels, meilleure detection avec les labels elargis)

## 5.4.2

- Fix normalisation trop aggressive : n'amplifie plus le bruit de fond. La normalisation ne s'active que si le peak est au moins 2x au-dessus du bruit moyen ET le signal est entre 0.003 et 0.1. Gain max reduit de 30x a 15x. Le silence reste du silence.

## 5.4.1

- Fix crash "cannot access local variable CLAP_WEIGHTS" : la variable etait utilisee dans le log avant sa definition. Deplacee avant le log.

## 5.4.0

### Amelioration majeure de la detection de claps

- **Normalisation automatique du signal** : les signaux faibles (peak < 0.1) sont amplifies automatiquement jusqu'a 30x pour atteindre un niveau optimal pour YAMNet (~0.3). Les cameras RTSP a volume 1x fonctionnent maintenant sans reglage.
- **Labels de clap elargis** : ajout de "Slap, smack" (0.6), "Whack, thwack" (0.5), "Knock" (0.3) en plus de "Hands" et "Clapping". Et penalites pour "Typing" (0.2).
- **max_results augmente** de 5 a 10 pour capturer plus de labels du classifier.
- **Seuil d'affichage labels** baisse de 0.5 a 0.1 : on voit maintenant les labels faibles dans l'interface (utile pour le debug).

## 5.3.7

- Bouton "Nettoyer les anciennes entites HA" dans les parametres avances. Marque toutes les entites ClapTrap comme unavailable. Apres un redemarrage de HA, elles disparaissent.
- Endpoint POST /api/ha/cleanup

## 5.3.6

- Fix entites MQTT qui ne se mettent pas a jour lors d'un clap : le mapping source_id technique → entity_id etait manquant. on_clap_detected ne retrouvait pas le slug.
- Suppression du fallback REST : ne cree plus d'entites orphelines sans unique_id. Seul MQTT Discovery est utilise (necessite Mosquitto).
- Les anciennes entites REST (section "Non groupe") doivent etre supprimees manuellement dans HA (clic sur le X).

## 5.3.5

- Nettoyage automatique des anciennes entites REST au demarrage quand MQTT est disponible. Supprime toutes les entites `claptrap_*` creees par l'API REST (sans unique_id) pour laisser place aux nouvelles entites MQTT Discovery.

## 5.3.4

- Fix MQTT 403 Forbidden : ajout de `services: mqtt:need` dans config.yaml. Sans ca, le Supervisor refusait l'acces aux credentials MQTT. Necessite une reconstruction de l'addon.

## 5.3.3

- Le message "paho-mqtt non installe" est maintenant visible en WARNING (avant il fallait le debug). Si vous voyez ce message, reconstruisez l'addon (Parametres > Modules complementaires > ClapTrap > Reconstruire).

## 5.3.2

- Fix MQTT Discovery : compatibilite paho-mqtt v2.x (callback_api_version). L'ancienne API v1 est aussi supportee.
- Fix MQTT publish : les states sont envoyes en string (pas JSON) pour les state_topics. Les attributes restent en JSON.
- L'erreur MQTT est maintenant loggee en WARNING (visible sans debug) au lieu de DEBUG.

## 5.3.1

- Schema SVG explicatif dans les parametres avances montrant la forme d'onde, les pics de claps, le seuil dynamique, le cooldown et la fenetre multi-clap
- Textes d'aide sous chaque parametre expliquant son effet
- "Ratio pic/bruit" renomme en "Sensibilite pic (ratio minimum)" pour plus de clarte

## 5.3.0

### Parametres avances configurables

- Nouvelle section "Parametres avances" (depliable) dans l'interface avec :
  - **Fenetre multi-clap** (0.5-5s) : duree pendant laquelle les pics sont accumules
  - **Cooldown entre pics** (0.01-0.5s) : temps minimum entre deux pics pour eviter les doublons
  - **Ratio pic/bruit** (1.5-10x) : un pic doit etre N fois au-dessus du bruit moyen pour compter
- Les parametres sont sauvegardes automatiquement et pris en compte au prochain demarrage de la detection
- Deplace depuis la barre de controle vers la section avancee

## 5.2.12

- Fix multi-clap : 3 claps rapides etaient comptes comme 1 car le signal ne redescendait pas assez entre les claps. Seuil de retour assoupli (60% au lieu de 30%), cooldown reduit (80ms au lieu de 120ms), ratio de pic reduit (3x au lieu de 5x). La moyenne glissante exclut les pics pour ne pas gonfler le seuil. Logging debug des pics detectes.

## 5.2.11

- Fix critique : la majorite des resultats du classifier etaient perdus ("source_id introuvable"). Le mapping timestamp→source_id ne fonctionnait pas car le timestamp initial du start() ne correspondait pas. Maintenant chaque detector a une seule source (_active_source_id), plus besoin de mapping timestamp. Tous les resultats sont traites.
- Le score "Hands: 0.148" avec volume 1x est trop faible. Monte le volume RTSP pour ameliorer la detection.

## 5.2.10

- Fix bouton Demarrer/Arreter : le handler etait dans l'ancien module detection.js qui n'est plus appele. Ajoute directement dans le script inline avec sauvegarde des settings avant demarrage.

## 5.2.9

- Fix sources qui ne s'affichent pas : les anciens modules JS (rtspSources, vbanSources, audioSources) ecrasaient les onglets dynamiques. Ils ne sont plus appeles dans script.js.
- Fix erreur JS "source_id is not defined" dans buildMicTab.
- Fix erreurs "Container detectedVBANSources/savedVBANSources non trouve".

## 5.2.8

- Fix erreur "initialisation des parametres" au chargement : le validateur DOM cherchait des elements (threshold, webhook-mic-enabled, micro_source) qui sont maintenant generes dynamiquement par les onglets. La validation DOM est desactivee car inutile avec l'interface dynamique.

## 5.2.7

- L'historique affiche maintenant le nom du flux (ex: "Bureau", "chambre") au lieu de juste "RTSP". Idem pour les micros et VBAN.
- Bouton "Effacer" pour vider l'historique sur l'interface.

## 5.2.6

- Affichage des entites HA liees dans chaque onglet source (binary_sensor et sensor). Les noms sont cliquables pour les copier. Endpoint GET /api/ha/entities.

## 5.2.5

- Toggle debug dans la barre de controle : active/desactive les logs DEBUG en temps reel
- Les logs sont en INFO par defaut (moins verbeux). Le mode debug affiche les stats audio par bloc, les labels du classifier, etc.
- Le parametre est persiste dans les settings (survive au redemarrage)
- classify.py passe de DEBUG permanent a INFO par defaut

## 5.2.4

- Fix VU-metre : suppression de l'amplification artificielle x50 sur les tests micro et RTSP. Le VU-metre montre maintenant le signal reel (avec le gain configure). La voix ne sature plus a 0dB.

## 5.2.3

- Fix multi-clap : seuil de pic dynamique base sur le niveau moyen du bruit de fond (x5). S'adapte automatiquement a chaque source (micro silencieux ou camera bruyante). Un clap est un pic 5x au-dessus du bruit ambiant.

## 5.2.2

- Fix multi-clap aberrant : seuil de pic monte de 0.03 a 0.1 (le bruit ambiant generait des faux pics). Les pics de plus de 2 secondes sont ignores. Le compteur est reset apres chaque emission.

## 5.2.1

### Fix multi-clap : comptage par pics d'energie

- Le multi-clap comptait les resultats du classifier YAMNet, mais celui-ci analyse des fenetres de 975ms. Deux claps rapides tombaient dans la meme fenetre = 1 seul resultat "Clapping".
- Maintenant le comptage se fait sur les **pics d'amplitude** dans les samples audio bruts (independant du classifier). Un clap = un front montant au-dessus du seuil 0.03 suivi d'un retour au silence.
- Emission immediate quand le classifier confirme un "Clapping" (plus de fenetre d'attente).
- Le parametre "Fenetre multi-clap" n'est plus utilise (les pics sont comptes entre deux resultats du classifier).

## 5.2.0

### Entites HA via MQTT Discovery avec appareil ClapTrap

- Si un broker MQTT est disponible (Mosquitto), les entites sont creees via MQTT Discovery et regroupees dans un appareil "ClapTrap"
- Sinon fallback sur l'API REST (entites orphelines comme avant)
- Les slugs d'entites utilisent des IDs stables : `mic_7` pour le micro, `rtsp_a1b2c3d4` (8 premiers chars de l'UUID) pour les RTSP
- Suppression propre des entites quand une source est retiree (via MQTT Discovery)
- Ajout de paho-mqtt dans les dependances

## 5.1.2

- Fix noms d'entites HA : les entites utilisent maintenant des slugs courts (claptrap_mic_1, claptrap_rtsp_1, claptrap_rtsp_2) au lieu de l'URL complete dans le nom. Le friendly_name affiche le label lisible (ex: "ClapTrap chambre").

## 5.1.1

- Fix entites HA : ajout de `homeassistant_api: true` dans config.yaml. Sans ca, le Supervisor ne donne pas acces a l'API HA Core (POST /core/api/states/ echoue silencieusement).

## 5.1.0

### Entites Home Assistant par source

- Chaque source audio cree automatiquement des entites HA via l'API REST Supervisor :
  - `binary_sensor.claptrap_<source>` : ON pendant 2s quand un clap est detecte, OFF sinon
  - `sensor.claptrap_<source>_clap_count` : nombre de claps du dernier evenement (1, 2, 3...)
  - `binary_sensor.claptrap_detection` : ON quand la detection tourne, OFF sinon
- Les entites sont visibles dans les dashboards Lovelace et utilisables dans les automations HA visuelles
- Pas de dependance MQTT : utilise l'API REST du Supervisor
- Les entites sont enregistrees au demarrage de la detection et mises a jour en temps reel

## 5.0.8

- Fix volume RTSP non persiste : le template lisait `d.volume` mais le backend stocke `d.gain`. Le slider revenait toujours a 10x au rechargement.

## 5.0.7

- Fix ajout RTSP : la route refusait la creation d'un flux avec une URL vide (400 Bad Request). Maintenant on peut creer un flux vide et remplir l'URL ensuite.
- Les champs gain et threshold sont inclus dans le stream a la creation.

## 5.0.6

- Fix ajout VBAN : utilise /refresh_vban_sources pour decouvrir les sources, propose un choix, et sauvegarde via /api/vban/save
- Fix suppression VBAN : appelle DELETE /api/vban/remove pour persister la suppression cote backend (avant c'etait seulement local)

## 5.0.5

- Fix ajout/suppression RTSP : le code tentait de recharger les settings via GET /api/settings (route inexistante). Maintenant met a jour window.settings localement avec le stream retourne par l'API et rebuild les onglets.

## 5.0.4

- Fix dropdown Ajouter : overflow-x:auto sur .tabs-header cachait le menu. Change en overflow:visible. Z-index du menu monte a 200 pour passer au-dessus du header.

## 5.0.3

- Fix bouton Ajouter : le dropdown est maintenant positionne juste sous le bouton (CSS relatif au parent, plus de getBoundingClientRect). Les clics sur les options fonctionnent (event listeners individuels au lieu de delegation).

## 5.0.2

- Fix : tous les event handlers sont maintenant bindes dans les onglets dynamiques (test RTSP, sliders precision/volume, toggle enabled, URL, nom, webhook pour chaque source). Avant, seuls les sliders visuels etaient bindes, les appels API manquaient.

## 5.0.1

- Fix rendu UI : le CSS est maintenant servi avec la version dans le chemin (/css/timestamp/style.css) pour contourner le cache du service worker HA. L'ancien CSS etait cache et les nouveaux styles (onglets, barre de controle) n'etaient pas appliques.

## 5.0.0

### Refonte complete de l'interface

- **Systeme d'onglets par source** : chaque source configuree (micro, RTSP, VBAN) a son propre onglet avec sa configuration complete
- **Barre de controle compacte** : cercle de detection, boutons Start/Stop, fenetre multi-clap, auto-start, export/import — tout en haut, toujours visible
- **Bouton "+ Ajouter une source"** : menu deroulant pour choisir le type (Microphone, RTSP, VBAN) avant de configurer
- **Precision par source** : chaque source a son propre slider de precision (score_threshold). Permet d'avoir un seuil bas pour une camera lointaine et un seuil haut pour un micro proche
- **Onglets dynamiques** : construits depuis les settings au chargement, mis a jour automatiquement

### Backend precision par source

- Nouveau champ `threshold` dans les settings de chaque source (microphone, RTSP, VBAN)
- Chaque classifier MediaPipe utilise le seuil de sa source
- Nouvel endpoint PUT /api/microphone/threshold
- Les endpoints RTSP et VBAN acceptent aussi `threshold` dans les updates

## 4.1.0

### Un classifier YAMNet par source audio

- Chaque source (micro, RTSP, VBAN) a maintenant son propre modele MediaPipe YAMNet isole
- Plus de probleme de timestamps partages entre sources
- Chaque source traite ses blocs audio independamment en parallele
- Le stop d'une source n'affecte pas les autres
- Environ 15MB de RAM par source supplementaire

## 4.0.3

- Renommage "Gain audio" en "Volume" sur les flux RTSP pour la coherence avec le micro (les deux font la meme chose : amplifier le signal)

## 4.0.2

- Le parametre "Delai entre detections" est remplace par "Fenetre multi-clap" (0.5s a 5s, defaut 1.5s). C'est la duree pendant laquelle l'addon compte les claps successifs avant d'emettre l'evenement (1 clap, 2 claps, 3 claps...). L'ancien parametre n'avait plus d'effet depuis l'ajout du multi-clap.

## 4.0.1

- Fix : le gain RTSP se met a jour en temps reel aussi pendant le test VU-metre (pas seulement pendant la detection). Bouger le slider de gain pendant un test met a jour le VU-metre immediatement.

## 4.0.0

### Architecture : app.py decoupe en Blueprints Flask

- `app.py` reduit de 1250 a ~300 lignes
- `routes/detection.py` : start/stop detection, status, historique
- `routes/sources.py` : RTSP/VBAN/microphone CRUD, gain, volume, auto-volume
- `routes/settings_routes.py` : sauvegarde, export/import config
- `routes/testing.py` : test micro et test RTSP (VU-metre)

### Export/Import configuration

- Boutons export (telecharger settings.json) et import (restaurer depuis un fichier) dans l'en-tete Configuration
- GET /api/settings/export et POST /api/settings/import

### Indicateur sante RTSP

- Point colore a cote du nom de chaque flux RTSP (gris=inconnu, jaune=connexion, vert=connecte, rouge=erreur)
- Mis a jour en temps reel via socket.io pendant la detection

### Gain RTSP en temps reel

- Changer le gain d'un flux RTSP pendant la detection l'applique immediatement (plus besoin de relancer)

### Sources en liste

- Les sources actives s'affichent en liste verticale (une par ligne) au lieu d'une seule ligne

## 3.3.1

- Fix multi-clap : cooldown reduit de 0.3s a 0.15s (deux claps rapides etaient filtres car trop proches)
- Ajout de logging detaille pour le comptage multi-clap (ouverture/fermeture de fenetre, compteur)

## 3.3.0

### Gain audio par source RTSP

- Slider de gain (1x a 50x, defaut 10x) sur chaque flux RTSP
- Le gain est applique en logiciel sur les samples audio avant le classifier
- Le gain est aussi applique dans le test de flux RTSP (VU-metre)
- Le gain est sauvegarde par flux dans les settings
- Permet de compenser les micros faibles des cameras de surveillance

## 3.2.6

- Fix detection : le seuil MediaPipe est maintenant fixe a 0.05 (tres bas) pour laisser passer tous les labels. Le seuil utilisateur (slider Precision) est applique uniquement sur le scoring custom de clap. Avant, MediaPipe filtrait les labels "Hands" et "Clapping" en amont si leur score etait sous le seuil, rendant la detection impossible.
- Logging ameliore : les labels clap sont logges a chaque fois qu'ils apparaissent (pas seulement tous les 100 blocs)

## 3.2.5

- Fix : le volume du micro sauvegarde est maintenant applique via pactl au demarrage de l'addon (avant, PulseAudio remettait le volume par defaut a chaque redemarrage)

## 3.2.4

- L'interface affiche maintenant la source de chaque detection (Micro, RTSP, VBAN) avec un badge colore
- Les events clap affichent le nombre de claps, le score et la source dans un bandeau vert
- Les labels de classification affichent aussi la source d'ou ils proviennent
- Historique des 10 derniers claps visible dans la zone de labels

## 3.2.3

### Fix multi-source : 3 bugs critiques

- **RTSP shape mismatch** : `read_audio_from_rtsp` renvoyait des arrays (1600,1) au lieu de (1600,). Le ring buffer attendait du 1D. Suppression du `reshape(-1, 1)` + guard `flatten()` dans `process_audio`.
- **Timestamps non monotones** : chaque source avait son propre compteur de timestamps, mais MediaPipe `classify_async` exige un timestamp global strictement croissant. Remplacement par un compteur global atomique protege par lock.
- **source_id introuvable** : consequence des collisions de timestamps, le mapping timestamp→source etait corrompu. Resolu par le fix du timestamp global.

## 3.2.2

- Fix : la detection utilise maintenant les flux RTSP et VBAN sauvegardes sur disque si le frontend ne les envoie pas (le module JS detection.js n'avait pas les RTSP en memoire)

## 3.2.1

- Fix : les flux RTSP et sources VBAN ne sont plus ecrases lors d'une sauvegarde globale des settings. Le save_settings fait maintenant un deep merge et preserve les listes de sources si le frontend envoie un tableau vide.
- Fix : le merge des settings preserves les sous-cles (ex: microphone.pulse_name n'est plus ecrase par un save partiel)

## 3.2.0

### Detection multi-source simultanee

- Toutes les sources activees (micro, RTSP, VBAN) fonctionnent en parallele
- Chaque source tourne dans son propre thread, toutes partagent le meme classifier MediaPipe
- Le badge source affiche toutes les sources actives (ex: "Micro: TONOR + RTSP: chambre")
- L'auto-start collecte aussi toutes les sources activees
- Le code de classify.py a ete entierement reecrit pour supporter le multi-source

## 3.1.8

- Affichage de la source audio active sous le bouton Demarrer/Arreter (badge avec le nom du micro, RTSP ou VBAN)
- La source est affichee au chargement de la page (via /status) et mise a jour en temps reel (via socket detection_status)

## 3.1.7

- Fix definitif du cache service worker HA : la version est maintenant dans le CHEMIN des modules JS (/js/1712490000/script.js) au lieu d'un query string. Le service worker cache par URL, donc un nouveau chemin = cache miss garanti. Tous les imports internes entre modules sont reecrits dynamiquement avec le meme chemin versionne.

## 3.1.6

- Fix cache service worker HA : le script principal est maintenant servi via une route dynamique /js/app.js qui reecrit tous les imports avec un timestamp unique. Les sous-modules sont servis avec Cache-Control no-cache.
- CSS versionne avec cache_bust

## 3.1.5

- Fix cache navigateur/service worker : desactivation du cache Flask sur tous les fichiers statiques (SEND_FILE_MAX_AGE_DEFAULT=0), headers Cache-Control/Pragma/Expires sur les modules JS et CSS
- Suppression de l'importmap (ne fonctionnait pas avec le service worker HA)

## 3.1.4

- Ajout d'un importmap pour versionner tous les modules JS avec le cache_bust. Chaque mise a jour de l'addon force le rechargement de tous les modules JS (plus de probleme de cache navigateur).

## 3.1.3

- Le nom des flux RTSP a maintenant un style "editable" : bordure transparente au repos, bordure visible au hover, focus avec highlight bleu. Plus intuitif qu'un champ standard.

## 3.1.2

- Le formulaire d'ajout d'un flux RTSP a maintenant la meme apparence qu'un flux existant (switch au lieu de checkbox, nom editable inline)

## 3.1.1

- Le nom des flux RTSP est maintenant editable directement dans l'interface (sauvegarde automatique)

## 3.1.0

- Ajout d'un bouton "Tester le flux" sur chaque source RTSP avec VU-metre en temps reel
- Utilise ffmpeg pour lire le flux RTSP et afficher les niveaux audio via socket.io
- Le test utilise l'URL actuellement saisie dans le champ (pas celle sauvegardee)
- Endpoints API: POST /api/rtsp/test/start et POST /api/rtsp/test/stop

## 3.0.4

- Fix : le reglage de volume et l'auto-volume utilisent maintenant le device selectionne dans le dropdown (pas celui sauvegarde)
- Le pulse_name est envoye depuis le frontend dans toutes les requetes micro (test, volume, auto-volume)

## 3.0.3

- Fix : le test micro utilise maintenant le device selectionne dans le dropdown (pas celui sauvegarde). Permet de tester un micro avant de sauvegarder.

## 3.0.2

- Fix PulseAudio : le socket est a /run/audio/pulse.sock (pas /run/pulse/native) dans les addons HA
- Fallback : si aucun socket trouve, extrait le Server String de `pactl info` (qui fonctionne via la config interne du conteneur)
- Applique dans run.sh et app.py

## 3.0.1

- Fix : quand la detection demarre en auto-start, l'UI affiche maintenant le bouton "Arreter" au lieu de "Demarrer"
- L'UI verifie l'etat de detection au chargement de la page via /status
- L'auto-start emet un event socket `detection_status` pour synchroniser les clients connectes

## 3.0.0

### Nouvelles fonctionnalites

- **Detection multi-claps** : compte le nombre de claps dans une fenetre de 1.5s. Les events incluent `clap_count` (1, 2, 3...). Permet des automations differenciees (2 claps = lumiere, 3 claps = scene).
- **Auto-start** : toggle dans la config pour demarrer la detection automatiquement au boot de l'addon (delai de 3s pour PulseAudio).
- **Events Home Assistant natifs** : chaque clap emet un evenement `claptrap_clap` via l'API Supervisor. Plus besoin de webhooks pour les automations HA simples.
- **Webhook avec payload** : les webhooks envoient maintenant un JSON `{event, source_id, timestamp, score, clap_count}` au lieu d'un POST vide.
- **Historique des detections** : endpoint GET /api/detections/history retourne les 50 dernieres detections.
- **Seuil en temps reel** : le score du classifier s'affiche a cote du slider Precision pour aider au reglage.

### Ameliorations detection

- **Scoring pondere** : labels Hands (0.8) et Clapping (1.0) avec penalite Finger snapping (0.5) et Writing (0.3). Suppression de "Cap gun" (faux positifs).
- **Seuil configurable** : le score_threshold et le delay utilisateur sont maintenant effectivement passes au classifier (avant ils etaient ignores).

### Architecture

- **Fix import circulaire** : `get_audio_input_devices()` extrait dans `audio_utils.py` (plus d'import app.py depuis classify.py).
- **Drain stderr parecord** : thread daemon qui vide stderr pour eviter que parecord se bloque quand le pipe est plein.
- **Debounce ecriture auto-volume** : le volume n'est persiste sur disque que toutes les 30s (au lieu de chaque seconde). Reduit l'usure SD card.
- **Code mort supprime** : `vban_signal_processor.py` (300 lignes jamais importees), route dupliquee `/refresh_vban`.
- **Types normalises** : threshold et delay stockes en float (plus en string), device_index en int.

## 2.5.0

### Nouvelle fonctionnalite : Volume automatique du micro (AGC)

- Toggle "Auto" a cote du slider de volume du micro
- Quand active, le systeme ajuste automatiquement le volume PulseAudio pour maintenir un niveau de signal optimal pour la detection de claps
- Algorithme : si le signal est trop faible (peak < 0.005) le volume augmente de 5%, si trop fort (peak > 0.3) il diminue de 10%, intervalle d'ajustement de 1 seconde
- Le slider se met a jour en temps reel via socket.io quand l'AGC ajuste
- Le slider manuel est desactive quand l'auto-volume est actif
- L'AGC piggybacke sur les donnees parecord de la detection (pas de second processus)
- Nouvel endpoint API PUT /api/microphone/auto-volume
- L'auto-volume s'arrete automatiquement quand la detection s'arrete

## 2.4.0

### Fix : 6 bugs critiques dans le pipeline de detection

- **detection_running jamais reset** : quand le thread de detection mourait (parecord crash, erreur...), `detection_running` restait a `True`, empechant tout redemarrage sans restart de l'addon. Ajout d'un `finally` qui reset le flag.
- **Collision de timestamps** : `time.time()` dans le calcul de timestamp causait des collisions qui faisaient silencieusement dropper des resultats du classifier. Remplace par un compteur monotone.
- **score_threshold ignore** : `detector.initialize()` etait appele sans les parametres utilisateur (`max_results`, `score_threshold`), utilisant toujours les valeurs par defaut. Le seuil configure dans l'UI n'avait aucun effet.
- **stderr.read() bloquant** : si parecord fermait stdout sans fermer stderr, `proc.stderr.read()` bloquait indefiniment le thread de detection. Limite a 4096 bytes.
- **detection.js ecoutait le mauvais event** : ecoutait `'detection_event'` au lieu de `'clap'` (ce que le backend emet). L'animation de detection dans l'UI ne se declenchait jamais via ce module.
- Ajout de logging diagnostique dans le pipeline de detection

## 2.3.7

- Ajout de logging INFO dans le flux de detection micro : nombre de blocs lus, peak, etat du detector
- Ajout de logging INFO dans le classifier : resultats recus, top labels, mapping source
- Permet de diagnostiquer si parecord envoie des donnees et si le classifier les traite

## 2.3.6

- Fix "Connection refused" : ne plus surcharger PULSE_SERVER si deja defini par le systeme HA (s6/contenv)
- Ajout diagnostic : listing de /run/pulse/, pactl info, pactl list sources au demarrage
- Suppression du fallback TCP qui ne fonctionnait pas

## 2.3.5

- Remplacement de sounddevice/PortAudio par parecord pour la capture audio micro
- parecord utilise directement libpulse (comme pactl) et bypass la chaine PortAudio/ALSA qui ne voyait aucun device dans le conteneur HA
- Le device TONOR selectionne est passe via --device=pulse_name a parecord
- Applique au test micro ET a la detection

## 2.3.4

- Fix "Error querying device -1" : sounddevice ne trouvait aucun device par defaut. Le code cherche maintenant explicitement un device "pulse" ou "default" dans la liste des devices disponibles, au lieu de passer device=None (qui echoue quand PortAudio n'a pas de default configure).
- Meme fix applique au test micro et a la detection.

## 2.3.3

- Recherche du socket PulseAudio a plusieurs chemins (/run/pulse/native, /run/pulse/pulseaudio.socket, /var/run/pulse/native)
- Fallback TCP vers 172.30.32.1 (host audio HA) si aucun socket unix trouve
- Ajout de timeout sur pactl pour eviter de bloquer le demarrage
- Meme logique de fallback dans app.py

## 2.3.2

- Fix crash au demarrage : le diagnostic pactl dans run.sh pouvait echouer si PulseAudio n'etait pas pret, causant un exit sous bashio (set -e implicite). Le diagnostic est maintenant non-bloquant.

## 2.3.1

- Fix barre VU-metre invisible : fond de track plus contraste, gradient fixe (vert→jaune→rouge proportionnel a la largeur totale), hauteur augmentee, min-width pour toujours afficher un indicateur

## 2.3.0

### Fix majeur : la detection micro fonctionne enfin

- **Bridge ALSA→PulseAudio** : ajout de `libpulse0` dans le Dockerfile et creation de `/etc/asound.conf` pour router ALSA default vers PulseAudio. Sans ca, sounddevice/PortAudio parlait directement a ALSA et ignorait PULSE_SOURCE, recevant du silence.
- **PULSE_SERVER** : `run.sh` configure automatiquement `PULSE_SERVER=unix:/run/pulse/native` pour se connecter au daemon PulseAudio de Home Assistant. Fallback dans `app.py` si non defini.
- **Diagnostic audio au demarrage** : `run.sh` log les infos PulseAudio et les sources disponibles.
- **Fix priorite sources** : RTSP ne prend plus priorite sur le micro quand le micro est active.
- **Fix pulse_name stale** : `_resolve_pulse_name()` verifie maintenant que le pulse_name en cache correspond toujours au device selectionne, et le re-resout sinon.
- **Fix double-reload settings** : `start_detection()` n'ecrase plus l'audio_source avec une relecture du disque.

## 2.2.35

- Layout passe en single-column (flex) pour s'adapter aux panneaux HA ingress etroits
- Detection circle + boutons sur une meme ligne horizontale
- Max-width 720px centre, responsive jusqu'a 320px
- Breakpoint 480px : cards compactes, inputs empiles, header reduit
- Cercle de detection reduit (100px, 80px sur mobile)

## 2.2.34

- Refonte complete de l'interface : design moderne, palette de couleurs plus douce, layout plus compact
- CSS entierement reecrit : suppression de 500+ lignes de code duplique
- Header sticky et plus fin, cards avec ombres subtiles, typographie amelioree
- Slider volume et range inputs avec nouveau style unifie
- Toggle switches plus compacts, animations plus fluides
- Responsive ameliore pour tablettes et mobiles
- Ajout de la meta viewport pour un meilleur rendu mobile

## 2.2.33

- Fix : le slider volume du micro fonctionne maintenant via le script fallback inline
- Le label pourcentage se met a jour en temps reel lors du deplacement du slider
- L'appel API PUT /api/microphone/volume est envoye au relachement du slider

## 2.2.32

- Ajout d'un slider de volume du micro dans l'interface (0% a 150%)
- Le volume est sauvegarde dans les settings et applique via pactl
- Nouvel endpoint API PUT /api/microphone/volume
- Ajout de logging diagnostique dans le callback du test micro

## 2.2.31

- Ajout de pulseaudio-utils (pactl) dans le Dockerfile
- Le volume du micro PulseAudio est automatiquement mis a 100% au demarrage du test et de la detection
- Logging des sources PulseAudio disponibles pour le diagnostic

## 2.2.30

- Fix : resolution automatique du pulse_name depuis l'API Supervisor si absent des settings
- Le pulse_name est sauvegarde automatiquement apres resolution
- Gain VU-metre augmente a 50x pour le diagnostic

## 2.2.29

- Ajout gain 10x sur le VU-metre pour compenser le niveau faible PulseAudio
- Ajout de logging du pulse_name lors du test micro

## 2.2.28

- Fix : affichage de la barre VU-metre (largeur 100% + min-width sur le track)

## 2.2.27

- Fix : ajout d'un script inline fallback pour le bouton test micro (independant des modules ES)

## 2.2.26

- Fix : bouton "Tester le micro" remis dans la section conditionnelle (cache quand micro desactive)
- Fix : delegation d'evenements pour le bouton test micro (fonctionne meme si cache au chargement)

## 2.2.25

- Fix : ajout cache-buster sur script.js pour forcer le rechargement apres mise a jour

## 2.2.24

- Fix : initialisation du test micro et Socket.IO independante de initSettings (evite que le bouton soit inactif)
- Ajout de logging console pour diagnostiquer le test micro

## 2.2.23

- Fix : bouton "Tester le micro" deplace hors de la zone conditionnelle (visible meme si le micro est desactive)

## 2.2.22

- Ajout d'un VU-metre en temps reel pour tester le microphone (bouton "Tester le micro")
- Affichage du niveau audio en dB via WebSocket

## 2.2.21

- Les parametres utilisateur (settings.json) sont maintenant stockes dans /data (volume persistant HA)
- La configuration survit aux mises a jour et rebuilds de l'addon

## 2.2.20

- Fix : utilisation de PULSE_SOURCE pour router PulseAudio vers le bon micro USB dans le container HA
- Le pulse_name du device est maintenant stocke dans les settings et passe au backend

## 2.2.19

- Fix : race condition sur detection_running avec threading.Lock (evite double demarrage)
- Fix : validation des parametres avant de modifier l'etat global
- Fix : webhook avec timeout=5s (evite blocage indefini du ThreadPoolExecutor)
- Fix : source_callback VBAN unifie sur une seule signature
- Fix : escaping JSON dans le template via tojson (securite XSS)
- Fix : load_settings retourne un deepcopy du cache (evite mutations silencieuses)
- Fix : double prefixe rtsp:// sur les URLs RTSP
- Fix : acces thread-safe a self.sources dans audio_detector._handle_result
- Fix : clean_vban_name sentinel ambigu (None au lieu de 0)
- Fix : shallow merge dans audioSources.js preservait les champs microphone existants
- Fix : fallback sur device par defaut PulseAudio si le device n'est pas visible par sounddevice

## 2.2.18

- Fix : ouverture du micro par nom au lieu de l'index numerique (PortAudio ne peut pas ouvrir les devices par index Supervisor)
- Ajout de logging des devices visibles par sounddevice pour le diagnostic

## 2.2.17

- Fix : resolution du micro par nom au lieu de l'index (les index Supervisor et sounddevice different)

## 2.2.16

- Reset de settings.json aux valeurs par defaut (suppression des donnees de test embarquees dans l'image Docker)

## 2.2.15

- Fix : la sauvegarde des parametres ne duplique plus les flux RTSP (sources RTSP exclues du syncWithDOM)

## 2.2.14

- Ajout de `libegl1` dans le Dockerfile pour corriger le crash MediaPipe (`libEGL.so.1` manquant)

## 2.2.13

- Ajout de `libgles2` dans le Dockerfile pour corriger le crash MediaPipe (`libGLESv2.so.2` manquant)

## 2.2.12

- Build 15-25 min plus rapide : ajout --prefer-binary pour pip, .dockerignore, pytest retire du runtime
- Retrait des dependances inutilisees : psutil, pyvban
- Ring buffer numpy pre-alloue dans AudioDetector (zero allocation dans le hot path)
- Import scipy.signal au niveau module (plus de resolution dynamique par appel)
- Webhooks non-bloquants via ThreadPoolExecutor (ne bloque plus le thread MediaPipe)
- Cache TTL 60s sur get_audio_input_devices() et load_flux()
- Eviction automatique des entrees perimees dans _timestamp_to_source
- Guard isEnabledFor sur les logs debug numpy
- VBAN : resample_poly au lieu de FFT, ring buffer numpy au lieu de deque
- Singleton WebhookManager (reutilise le pool de connexions HTTP)

## 2.2.11

- Ajout de la permission `hassio_api` pour corriger le 403 sur l'endpoint Supervisor `/audio/info`

## 2.2.10

- Ajout de logging pour diagnostiquer la detection des peripheriques audio via l'API Supervisor
- Meilleure gestion de la structure de reponse de l'API /audio/info

## 2.2.9

- Suppression du fichier events.py (instance SocketIO morte, code jamais appele)
- Centralisation de la gestion des parametres dans settings_manager.py (cache TTL, une seule source de verite)
- Remplacement du buffer deque par numpy array (suppression allocation list intermediaire dans le hot path)
- Validation des URLs webhook (protection SSRF dans webhook.py)
- Logs frontend gates derriere le flag debug (plus de console.log en production)
- Multi-stage Docker build (build-essential retire de l'image finale, -200 MB)
- Resampling audio anti-aliase via scipy.signal.resample_poly
- Remplacement de tous les print() par logging dans app.py et vban_manager.py
- CORS SocketIO configurable via variable d'environnement CORS_ORIGINS

## 2.2.8

- Correction d'une race condition dans AudioDetector (mauvais routage des detections entre sources)
- YAMNet n'est plus instancie deux fois (economie ~200-400 MB de RAM)
- Suppression de la lecture de settings.json a chaque requete HTTP (init VBAN au demarrage uniquement)
- Reconnexion automatique des flux RTSP avec backoff exponentiel en cas de coupure

## 2.2.7

- Securite : cle secrete Flask generee aleatoirement au lieu d'une valeur en dur
- Performance : suppression du blocage de 2 secondes lors du rafraichissement VBAN
- Docker : retrait de opencv-python et pyaudio (non utilises, -100 MB d'image)
- Securite : suppression de la route /run_tests exposee sans authentification
- Code : WebhookManager factorise dans un module partage (webhook.py)
- Nettoyage des imports morts (cv2, pyaudio, HTTPAdapter, Retry)

## 2.2.6

- Rattrapage du changelog pour toutes les versions precedentes

## 2.2.5

- Correction du cache Docker qui empechait la reconstruction
- Suppression des architectures obsoletes (armhf, armv7, i386)
- Mise a jour de pip avant installation des dependances

## 2.2.4

- Ajout de build-essential et python3-dev pour la compilation des dependances
- Versions des dependances Python relachees pour compatibilite avec l'image de base HA

## 2.2.3

- Mise en conformite avec les conventions HAOS
- Dockerfile base sur l'image HA Debian (`ghcr.io/hassio-addons/debian-base`)
- Ajout de build.yaml, run.sh, CHANGELOG.md, DOCS.md
- Labels Docker Home Assistant

## 2.2.2

- Liste les vrais peripheriques audio via l'API Supervisor HA (micro USB, etc.)
- Fallback sur sounddevice si l'API n'est pas disponible

## 2.2.1

- Ajout du support audio (`audio: true`) et USB (`usb: true`) pour la detection des micros USB
- Ajout de `libasound2-plugins` pour le pont ALSA/PulseAudio

## 2.2.0

- Ajout du support ingress Home Assistant (acces via le panneau HA en HTTPS)
- Suppression du lien webui direct
- Middleware WSGI pour gerer le prefixe ingress sur tous les assets et routes
- Toutes les URLs API et connexions SocketIO supportent l'ingress

## 2.1.0

- Version initiale avec support microphone, RTSP et VBAN
- Interface web de configuration
- Detection en temps reel via YAMNet
- Webhooks configurables par source
