# ClapTrap

ClapTrap écoute un micro, le son d'une caméra RTSP ou un flux VBAN, reconnaît
les sons avec le modèle YAMNet et crée des entités Home Assistant qui
s'allument à chaque clap (1, 2, 3 ou 4 claps).

## Prérequis

- **Home Assistant OS ou Supervised**, version 2025.10 ou plus récente,
  sur une machine **amd64** ou **aarch64** (Raspberry Pi 4 et 5, par exemple).
- **Un broker MQTT** : ClapTrap crée ses entités par MQTT.
  1. Installez l'add-on **Mosquitto broker** (boutique officielle) et
     démarrez-le. Vous utilisez déjà un autre broker (NAS, Docker…) ?
     Renseignez-le dans l'onglet **Configuration** de ClapTrap (voir
     « Broker MQTT externe »).
  2. Ajoutez l'intégration **MQTT** (Paramètres > Appareils et services) :
     Home Assistant la propose automatiquement une fois Mosquitto démarré.

Sans l'intégration MQTT, l'add-on fonctionne mais aucune entité n'apparaît.

## Installation

1. **Paramètres > Modules complémentaires > Boutique des modules
   complémentaires**, menu **⋮ > Dépôts**, puis ajoutez :
   `https://github.com/powange/ClapTrap-HA-Addon`
2. Recherchez **ClapTrap**, cliquez sur **Installer**, puis **Démarrer**.
   L'image est précompilée : l'installation ne prend que le temps du
   téléchargement (environ 120 Mo, 450 Mo une fois installée ; compter
   quelques minutes sur un Raspberry Pi).
3. Activez **Afficher dans la barre latérale** et, si vous le souhaitez,
   **Watchdog** (Home Assistant relance alors l'add-on s'il s'arrête : le
   contrôle vérifie que le port de l'interface reste ouvert).
4. Ouvrez l'interface (**Ouvrir l'interface utilisateur Web**) et ajoutez
   votre première source.

L'interface n'est accessible que depuis Home Assistant (ingress) : passez
par le panneau de l'add-on. Le serveur n'écoute que sur le réseau interne de
Home Assistant : `http://homeassistant:16045` depuis le réseau local est
refusé avant même d'atteindre ClapTrap (connexion refusée, rien n'écoute sur
cette adresse). Sur le réseau interne, seul le proxy d'ingress du Supervisor
est accepté ; toute autre adresse reçoit « 403 ». Si cette interface interne
manque (installation inhabituelle), le serveur écoute sur toutes les
interfaces : le port répond alors depuis le réseau local, mais toujours par
« 403 », et le journal le signale au démarrage.

## Ajouter une source

Bouton **Ajouter une source**, puis :

- **Micro** : un micro branché sur la machine Home Assistant (USB le plus
  souvent). Un seul micro possible. S'il n'apparaît pas, vérifiez qu'il est
  listé dans Paramètres > Système > Matériel, puis rouvrez l'assistant.
- **Caméra RTSP** : l'adresse du flux, par exemple
  `rtsp://utilisateur:motdepasse@192.168.1.20:554/stream` (voir l'application
  ou la documentation de la caméra). Seuls `rtsp://` et `rtsps://` (chiffré)
  sont acceptés. Si le son se coupe (Wi-Fi, paquets perdus), cochez
  **Transport TCP** dans les réglages de la caméra (certaines caméras ne le
  gèrent pas). Seule la piste audio est demandée à la caméra : la vidéo ne
  circule pas sur le réseau. Codecs audio pris en charge : AAC, G.711
  (µ-law, A-law), G.726, G.722, Opus, MP3/MP2, AMR, GSM, AC-3, Vorbis,
  Speex et PCM, soit ceux des caméras IP courantes. Les réglages de la
  caméra affichent le **format audio reçu** (par exemple « G.711 µ-law ·
  8 kHz · mono ») dès qu'elle est écoutée (« Tester le son » ou détection).
- **Flux VBAN** : un PC qui diffuse du son avec Voicemeeter. Réglez
  l'émetteur pour qu'il envoie vers l'adresse IP de Home Assistant, port 6980 :
  le flux apparaît alors dans la liste. Pour un flux multicast ou un émetteur
  qui ne diffuse pas encore, utilisez **Ajouter à la main** (le nom du flux
  doit être celui configuré dans Voicemeeter).
  Le nom affiché d'une source VBAN se modifie dans ses réglages ; le nom du
  flux et l'adresse de l'émetteur, eux, sont fixés à l'ajout.

La dernière étape affiche le niveau sonore : tapez dans vos mains, il doit
monter nettement. Lancez ensuite la détection avec **Démarrer la détection** (ou
cochez **Démarrer au lancement de l'add-on**).

Le groupe « Clap » d'une nouvelle source reconnaît déjà les applaudissements
(« Applaudissement », « Mains » et « Applaudissements » cochés) : démarrez la
détection et tapez deux fois dans vos mains pour essayer. Pour d'autres sons,
ouvrez les réglages de la source : la recherche d'un groupe propose tous les
sons connus (« siffl » propose « Sifflement »), ajoutés d'un toucher ; ou
activez **Ajouter les sons entendus**, faites le son pendant la détection,
puis cochez-le. Un groupe sans son le signale sur la carte (« Aucun son
choisi ») avec un bouton **Choisir les sons**.

Le mot de passe d'une adresse RTSP est masqué à l'écran : le bouton
**Afficher le mot de passe** révèle l'adresse complète pour la modifier. Il reste dans la
sauvegarde « Exporter ».

Activer, désactiver, ajouter ou supprimer une source (ou changer son
adresse ou son micro) ne relance que cette source : les autres continuent
d'écouter sans interruption. Une source en erreur est signalée dans la barre d'état (point
orange, « 1 en erreur »).

Sur chaque carte, l'état de la source est affiché : « À l'écoute »,
« Connectée », « Flux perdu, reconnexion… », « Aucun paquet reçu » (VBAN :
rien reçu depuis 10 s)…

## Groupes de sons

Chaque source a un ou plusieurs **groupes de sons**. Un groupe réunit les
sons qui doivent le déclencher et a son propre **seuil de confiance** (score
YAMNet minimal, réglable avec la poignée sur la carte).

La liste des sons ne se remplit pas toute seule. L'interrupteur **Ajouter les
sons entendus** (désactivé par défaut, réglable par groupe) ajoute à la liste
du groupe, non cochés, les sons que la source entend pendant la détection :
cochez ceux qui doivent déclencher le groupe, puis désactivez-le une fois la
liste faite.

**Nouveau groupe** : choisissez un **modèle**, qui coche déjà les bons sons
et règle le comptage, ou un groupe vide à nommer. Tout reste modifiable.

| Modèle | Sons | Fonctionnement |
|---|---|---|
| Applaudissements | Clapping, Hands, Applause | 1 et 2 claps |
| Claquement de doigts | Finger snapping | 1 et 2 claps |
| Toc-toc | Knock, Tap | 2 et 3 coups |
| Sifflement | Whistling, Whistle | sons tenus, 1 |
| Détecteur de fumée | Smoke detector, Fire alarm | alerte (fumée) |
| Bris de glace | Glass, Shatter | alerte (sécurité), dès 1 reconnaissance |
| Pleurs de bébé | Baby cry, Crying | alerte (son) |
| Aboiement | Bark, Dog | alerte (son) |
| Sonnette | Doorbell, Ding-dong | alerte (son), dès 1 reconnaissance |
| Alarme ou sirène | Alarm, Siren, Car alarm… | alerte (sécurité) |

Un son déjà coché dans un autre groupe de la source n'est pas coché dans le
nouveau (signalé à la création). Ces modèles aident à surveiller une maison ;
ce n'est pas un système de sécurité certifié (une alarme à la télévision est
aussi reconnue).

**Comptage** (par groupe) : « claps, coups » compte les attaques nettes (un
clap = un pic) ; « sons tenus » convient aux sifflements et aux sons qui
montent doucement : tant que le son dure, un nouveau pic est compté toutes
les 0,3 s (un sifflement d'une seconde compte environ 3).

**Alerte** (détecteur de fumée, pleurs, aboiement, sonnette…) : pas de
comptage ni de capteurs « N claps », mais un capteur
`binary_sensor.claptrap_<source>_<groupe>_alerte` qui **reste allumé tant que
le son est reconnu** et s'éteint après 5 secondes de silence (ou à l'arrêt de
la détection). Il s'allume après 1 à 4 reconnaissances sur les 4 dernières
analyses (2 secondes) : 1 pour un son bref comme un bris de glace, 3 pour un
son qui dure, comme une alarme. Son **type** (fumée, sécurité, son, problème,
présence) donne son icône et ses libellés dans Home Assistant. Une alerte
n'empêche pas un clap d'être compté, et inversement.

Un son ne peut être coché que dans un groupe par source. Si plusieurs groupes
reconnaissent le même événement sonore, seul celui qui a le meilleur score
déclenche ses entités (les autres sont notés « ignoré » dans l'historique).

**Sons ignorés partout** (onglet Réglages) : ces sons ne déclenchent aucun
groupe, sur aucune source.

## Calibrer une source

Menu **⋯** de la carte › **Calibrer…** : un assistant règle la source à partir
de vos propres bruits.

1. Cochez les groupes à calibrer, ou saisissez le nom d'un nouveau bruit
   (un groupe sera créé), avec le nombre de pics faits à chaque essai
   (2 pour « 2 claps »).
2. D'abord quelques secondes du bruit de fond habituel de la pièce (5 s,
   option « Enregistrer aussi le bruit de fond », recommandée), puis, pour
   chaque bruit, les essais demandés (5 par défaut, 4 s chacun).
   Pendant le calibrage, la source ne déclenche rien. Avec **Enchaîner les prises
   automatiquement**, chaque prise commence seule après un compte à rebours
   de 3 s, sans cliquer sur « Enregistrer » (bouton **Pause** pour souffler) ;
   le choix est mémorisé par le navigateur, pour les deux calibrages.
3. ClapTrap propose :
   - le **gain** de la source (ou le volume du micro), appliqué automatiquement
     pour que le bruit le plus fort soit net sans saturer ;
   - pour chaque groupe, les **sons** à cocher et à retirer (un son présent
     aussi dans les essais d'un autre groupe est écarté), le **seuil** qui
     reconnaît ses essais sans se déclencher sur les autres, et le **mode de
     comptage** qui retrouve le nombre de pics attendu ;
   - la reconnaissance et les fausses détections avant / après, mesurées sur
     les mêmes essais.
4. **Appliquer** enregistre les réglages choisis.

La détection est démarrée le temps du calibrage si elle était arrêtée. Le
calibrage se pilote aussi par commandes MQTT (voir plus bas).

## Entités Home Assistant

Chaque source a son propre appareil (« ClapTrap Salon », « ClapTrap Micro »),
rattaché à l'appareil **ClapTrap** : vous pouvez lui affecter une pièce. Les
entités globales (détection, configuration, dernière commande) restent sur
**ClapTrap**. Le bouton **Visiter** de la fiche d'un appareil ouvre
l'interface de ClapTrap.

Découverte MQTT par appareil : un seul message retenu par appareil
(`homeassistant/device/claptrap/<appareil>/config`, `hub` pour ClapTrap), qui
décrit toutes ses entités. Supprimer une source retire son appareil et ses
entités d'un coup.

| Entité | Rôle |
|---|---|
| `event.claptrap_<source>_<groupe>_evenement` | un événement par détection : type `1clap`, `2claps`… (nombres cochés du groupe), attributs `score` et `sons` |
| `binary_sensor.claptrap_<source>_<groupe>_1clap` | 1 clap détecté |
| `binary_sensor.claptrap_<source>_<groupe>_2claps` | 2 claps, etc. jusqu'à `4claps` (au-delà de 4, aucune entité : c'est du bruit, pas un geste) |
| `binary_sensor.claptrap_detection` | la détection tourne (attribut `sources`) |

- `<source>` vaut `mic` pour le micro, `rtsp_` suivi des 8 premiers caractères
  de l'identifiant de la caméra, `vban_` suivi du nom affiché saisi à l'ajout
  du flux (avec 4 caractères de son identifiant en plus si deux flux portent
  le même nom ; renommer ensuite la source ne le change pas) ; `<groupe>` vient
  du nom du groupe (`clap` pour le groupe par défaut) et suit ses renommages
  (voir « Renommer un groupe »). Les identifiants
  exacts sont affichés dans les réglages de chaque groupe, avec un bouton pour
  les copier.
- Les nombres de claps publiés se choisissent par groupe (1 et 2 par défaut).
  Un groupe sans aucun nombre coché ne crée pas d'entité mais envoie toujours
  l'événement et le webhook.
- À chaque détection, l'entité passe à **activé pendant 2 secondes**. Un
  nouveau clap pendant ces 2 s produit un bref retour à « désactivé » puis
  « activé » : un déclencheur `to: "on"` se déclenche bien à chaque fois.
- Les entités sont **indisponibles** tant que leur source n'écoute pas
  (détection arrêtée, source désactivée, flux RTSP en erreur, VBAN muet).
  Elles sont conservées, avec leurs personnalisations (nom, zone, icône).

Exemples d'automatisation, avec l'entité event (historique lisible : type et
score de chaque détection) ou avec le capteur à impulsion :

```yaml
triggers:
  - trigger: state
    entity_id: event.claptrap_mic_clap_evenement
    not_from: unavailable
conditions:
  - condition: state
    entity_id: event.claptrap_mic_clap_evenement
    attribute: event_type
    state: 2claps
actions:
  - action: light.toggle
    target:
      entity_id: light.salon
```

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.claptrap_mic_clap_2claps
    to: "on"
actions:
  - action: light.toggle
    target:
      entity_id: light.salon
```

Avec l'entité event, préférez un déclencheur sur son état (l'horodatage de
la dernière détection change à chaque fois) : deux « 2claps » de suite
déclenchent bien deux fois.

**Supprimer les entités orphelines** (onglet Réglages) retire les entités
ClapTrap qui ne correspondent plus à aucune source.

## Piloter ClapTrap depuis Home Assistant

Les réglages sont aussi exposés dans Home Assistant : tableaux de bord,
automatisations ou assistant IA (par un serveur MCP qui sait appeler les
services de HA) peuvent les lire et les modifier, sans ouvrir l'interface.

| Entité | Rôle |
|---|---|
| `number.claptrap_<source>_<groupe>_seuil` | seuil de confiance du groupe, en % (modifiable) |
| `switch.claptrap_<source>_<groupe>_groupe_actif` | groupe actif ; désactivé, il ne se déclenche plus (ses entités restent) |
| `switch.claptrap_<source>_source_active` | source activée |
| `switch.claptrap_detection` | démarrer ou arrêter la détection |
| `sensor.claptrap_<source>_<groupe>_score` | dernier score du groupe, même sous le seuil (diagnostic) |
| `text.claptrap_<source>_<groupe>_nom` | nom du groupe (modifiable : voir « Renommer un groupe ») |
| `sensor.claptrap_<source>_<groupe>_sons` | nombre de sons (classes YAMNet) cochés ; attributs : leur liste, le nombre de sons non cochés, le mode de comptage (diagnostic) |
| `sensor.claptrap_<source>_derniere_classe` | classe YAMNet la plus probable du dernier son (hors silence) ; attributs : score, 3 premières classes, score de chaque groupe, `diagnostic` (analyses sautées et blocs perdus faute de processeur, coupures du flux, durée moyenne et maximale d'une analyse) |
| `sensor.claptrap_<source>_top3` | les 3 classes les plus probables, avec leur score |
| `sensor.claptrap_configuration` | résumé ; attributs : sources, groupes (nombre de sons cochés), entités, sons ignorés ; listes complètes par la commande `get_settings` |
| `sensor.claptrap_derniere_commande` | `ok` ou `erreur` ; attributs : détail du résultat (un résultat volumineux, comme `get_settings`, reste sur le topic `claptrap/command/result`) |

Une modification faite dans HA est enregistrée dans les réglages de l'add-on
et appliquée aussitôt ; une modification faite dans l'interface est
republiée dans HA. Les capteurs de diagnostic sont mis à jour au plus toutes
les 2 secondes par source, et seulement quand une valeur change.

**Renommer un groupe** (interface, `text.claptrap_<source>_<groupe>_nom` ou
commande `update_group` avec `name`) : les entity_id du groupe suivent son
nouveau nom. Renommer « Siffler » en « Snap » renomme
`binary_sensor.claptrap_vban_atomsalon_siffler_3claps` en
`binary_sensor.claptrap_vban_atomsalon_snap_3claps`, de même pour le seuil,
l'activation, le score, le nom et les sons. ClapTrap renomme les entités
existantes dans Home Assistant (comme un renommage dans Paramètres ›
Entités) : l'historique, les statistiques et les personnalisations (pièce,
icône) sont conservés, et les réglages du groupe ne changent pas.

**Home Assistant ne met pas à jour les automatisations, scripts, scènes et
tableaux de bord qui citent les anciens entity_id** : adaptez-les après le
renommage. La fenêtre de confirmation de l'interface liste les
automatisations, scripts et scènes concernés ; le journal de l'add-on (et
`sensor.claptrap_derniere_commande`) donne chaque correspondance
`ancien -> nouveau`, les éléments à adapter et un bilan (renommées, déjà à
jour, conservées, conflits, introuvables). Les tableaux de bord ne sont pas
listés : vérifiez-les aussi.

Une entité dont vous avez choisi l'id dans HA garde cet id ; un nouvel id
déjà pris par une autre entité n'est jamais écrasé (pas de doublon `_2`).
Relancer un renommage déjà fait (par exemple à la main dans HA) ne change
rien. Si HA est injoignable, le renommage est fait dès qu'il le redevient. L'identifiant interne du groupe (`group_slug` du webhook
et de l'événement `claptrap_clap`) ne change pas. Deux groupes d'une même
source ne peuvent pas porter le même nom.

**Commandes** : pour ajouter, modifier ou supprimer des sources, des groupes
ou des sons, publiez un JSON sur le topic `claptrap/command` (service
`mqtt.publish`). Le résultat apparaît dans `sensor.claptrap_derniere_commande`
(et sur `claptrap/command/result`, secrets masqués). Les mêmes vérifications
que dans l'interface s'appliquent ; les commandes sont exécutées une par une,
dans l'ordre. Publiez-les sans `retain` : une commande retenue est ignorée.

Tout client autorisé sur le broker peut envoyer ces commandes (c'est le
modèle de confiance de MQTT). Pour les interdire, décochez **Commandes MQTT**
(`mqtt_commands`) dans l'onglet Configuration de l'add-on, ou limitez
l'écriture sur `claptrap/#` par les ACL du broker (option `customize` de
l'add-on Mosquitto) au seul utilisateur de Home Assistant et de ClapTrap.
L'adresse d'un webhook ne se règle pas par MQTT (seulement dans
l'interface) : un client du broker ne peut pas faire envoyer de requêtes par
l'add-on vers l'adresse de son choix. Les noms de sons sont vérifiés (casse
corrigée, son inconnu refusé avec une suggestion). Les entités de réglage
(interrupteurs, seuils, noms) répondent même pendant une commande longue
(prise de calibrage).

```yaml
action: mqtt.publish
data:
  topic: claptrap/command
  payload: >-
    {"action": "update_group", "source": "AtomSalon", "group": "Siffler",
     "threshold": 0.35}
```

| Action | Champs |
|---|---|
| `help`, `get_settings` | liste des commandes ; réglages complets (secrets masqués) |
| `start_detection`, `stop_detection` | — |
| `add_rtsp` | `name`, `url`, `enabled`, `rtsp_tcp` (transport TCP, pour une caméra qui coupe en UDP) |
| `add_vban` | `name`, `ip`, `port` (6980), `stream_name`, `enabled` |
| `add_microphone` | `device`, `enabled` |
| `update_source` | `source` + `name`, `url`, `enabled`, `gain`, `device`, `volume`, `auto_volume`, `rtsp_tcp` (caméra) ; pas `webhook_url` (interface seulement) |
| `delete_source` | `source` |
| `add_group` | `source`, `name`, `threshold` (0 à 1), `ha_entities` (ex. `[1, 2]`), ou `preset` (`claps`, `doigts`, `toc`, `sifflement`, `fumee`, `verre`, `bebe`, `aboiement`, `sonnette`, `alarme`) avec `name` facultatif |
| `update_group` | `source`, `group` + `name`, `threshold`, `ha_entities`, `enabled`, `auto_add_sounds`, `count_mode` (`attaques`, `tenus` ou `alerte`), `alert_confirm` (1 à 4), `device_class` (`sound`, `smoke`, `safety`, `problem`, `occupancy`) |
| `delete_group` | `source`, `group` (le premier groupe est protégé) |
| `set_sound` | `source`, `group`, `label` (classe YAMNet, ex. `Whistling`), `enabled` |
| `clear_unchecked_sounds` | `source`, `group` |
| `set_exclusion` | `label`, `excluded` |
| `set_advanced` | `delay`, `peak_cooldown`, `peak_ratio` |
| `calibration_start` | `source` : commence le calibrage |
| `calibration_record` | `target` (groupe existant, nouveau nom ou `fond`), `expected` (pics par essai), `duration` (s, 4) : une prise, à jouer dès l'envoi |
| `calibration_analyze` | — : propositions (gain, sons, seuil, comptage, reconnaissance avant / après) |
| `calibration_apply` | `gain` (true), `groups` (noms) : applique et termine |
| `calibration_cancel`, `calibration_status` | abandonner ; état et prises enregistrées |

`source` accepte `mic`, `rtsp:<id>`, `vban:<id>`, la clé d'entité (ex.
`vban_atomsalon`) ou le nom de la source ; `group`, l'identifiant ou le nom
du groupe. Un champ `id` facultatif est recopié dans le résultat.

Toute personne ou application qui peut publier sur le broker MQTT peut
ainsi modifier ClapTrap, comme elle peut déjà commander les autres
appareils MQTT : réservez l'accès au broker aux comptes de confiance.

## Événement `claptrap_clap`

Chaque détection émet aussi l'événement `claptrap_clap`, souvent plus simple
qu'un webhook :

```json
{
  "source_id": "rtsp_4f2c9a1e-...",
  "entity_key": "rtsp_4f2c9a1e",
  "source_name": "Salon",
  "timestamp": 1790000000.12,
  "clap_time": 1789999998.46,
  "score": 0.82,
  "clap_count": 2,
  "labels": [{"label": "Clapping", "score": 0.82}],
  "group_slug": "clap",
  "group_name": "Clap",
  "ignored": false
}
```

- `entity_key` : même clé que dans les entity_id (`binary_sensor.claptrap_<entity_key>_…`) ;
  c'est le champ le plus pratique pour filtrer une source.
- `timestamp` : instant de l'envoi, en secondes depuis le 1er janvier 1970
  (UTC) ; `clap_time` : instant du premier clap du geste (1,5 à 2,5 s plus
  tôt, la fenêtre de comptage).
- `clap_count` : nombre de claps comptés (0 pour un groupe sans entité qui
  reconnaît un son sans aucun pic).
- `labels` : sons reconnus (noms YAMNet en anglais) et leur score.

Un groupe en mode alerte émet `claptrap_alert` au début et à la fin de
l'alerte, avec les mêmes champs (sans `clap_count`) et `"alert": "on"` ou
`"off"`. Le plus simple reste d'utiliser son capteur `…_alerte`.

```yaml
triggers:
  - trigger: event
    event_type: claptrap_clap
    event_data:
      entity_key: mic
      clap_count: 3
```

## Webhook

Chaque source peut envoyer un **POST JSON** à une URL (facultatif), par
exemple un webhook Home Assistant (`http://homeassistant:8123/api/webhook/<id>`)
ou Node-RED. Le contenu est celui de l'événement ci-dessus, plus
`"event": "clap"` (`"event": "alert"` pour une alerte). Le bouton **Tester** envoie le même contenu avec
`"test": true`.

L'URL d'un webhook Home Assistant est un secret : quiconque la connaît peut
déclencher l'automatisation. ClapTrap la masque dans ses journaux.

## Réglages du comptage (onglet Réglages)

| Réglage | Défaut | Rôle |
|---|---|---|
| Fenêtre multi-clap | 1,5 s | Durée pendant laquelle les claps consécutifs sont comptés ensemble (3 claps en 1,5 s = « 3 claps »). |
| Écart minimal entre deux claps | 0,08 s | Filtre les rebonds d'un même clap. |
| Force minimale d'un clap | 3 × le bruit ambiant | Un clap n'est compté que s'il est ce nombre de fois plus fort que le bruit ambiant. Baissez pour un micro faible, montez contre les faux positifs. |

Le bouton **Calibrer le comptage…** propose ces trois valeurs à partir de vos
propres gestes, mesurés sur une source (environ 2 minutes, la source ne
déclenche rien pendant ce temps) : 5 s de bruit de fond, 3 claps simples,
3 fois « 2 claps », 2 fois « 3 claps » et, au choix, « 2 claps, une pause,
1 clap » pour vérifier que deux commandes ne sont pas fusionnées. Le résultat
compare les réglages actuels et proposés en rejouant vos prises (« prises
correctement comptées : 4/9 avec les réglages actuels, 9/9 avec les réglages
proposés ») ; les valeurs restent modifiables avant « Appliquer ». Les
prises peuvent s'enchaîner automatiquement, comme pour le calibrage d'une
source.

Le seuil de confiance, lui, se règle par groupe, directement sur les cartes.

## Broker MQTT externe

Onglet **Configuration** de l'add-on (laisser vide pour utiliser Mosquitto) :

| Option | Rôle |
|---|---|
| Broker MQTT externe (`mqtt_host`) | adresse du broker |
| Port du broker (`mqtt_port`) | 1883 par défaut, 8883 avec TLS |
| Utilisateur et mot de passe | identifiants du broker |
| Connexion TLS (`mqtt_tls`) | chiffrer la connexion |
| Vérifier le certificat (`mqtt_tls_verify`) | coché par défaut ; décocher seulement pour un certificat auto-signé |
| Commandes MQTT (`mqtt_commands`) | coché par défaut ; décocher pour ignorer `claptrap/command` |

Redémarrez l'add-on après une modification. **L'intégration MQTT de Home
Assistant doit être reliée au même broker** (Paramètres > Appareils et
services > MQTT), sinon les entités n'apparaissent pas.

Mosquitto en TLS est géré automatiquement ; si son certificat n'est pas
reconnu (auto-signé, établi pour un autre nom), décochez « Vérifier le
certificat ».

Sans aucun broker, l'add-on fonctionne (détection, webhooks) mais sans
entités, et le journal le rappelle toutes les 10 minutes. Si le broker refuse
la connexion (mauvais identifiants), l'add-on retente avec une attente
croissante (jusqu'à 5 minutes) et ne l'écrit au journal qu'une fois toutes
les 10 minutes.

**Une seule installation de ClapTrap par broker.** Deux Home Assistant qui
partagent un même broker externe, chacun avec ClapTrap, se gêneraient : mêmes
topics (`claptrap/…`, `homeassistant/device/claptrap/…`) et mêmes unique_id,
le nettoyage de l'un supprimerait les appareils de l'autre. Utilisez un
broker par installation (l'add-on Mosquitto de chacune, par exemple).

## Sécurité (AppArmor)

ClapTrap est livré avec un profil AppArmor en mode « complain » : les accès
inhabituels sont journalisés mais pas bloqués. Pour aider à passer en mode
strict, après quelques jours d'utilisation complète (micro, caméras, VBAN,
webhooks), relevez les accès signalés depuis un terminal de l'hôte :

```sh
journalctl _TRANSPORT=audit -g 'apparmor="ALLOWED".*profile="claptrap"'
```

Aucune ligne : le profil couvre tout et peut passer en mode strict. Des
lignes : ouvrez une issue sur le dépôt avec leur contenu.

## Configuration (onglet Réglages)

- **Exporter** : sauvegarde complète (identifiants des caméras et adresses des
  webhooks compris), à garder en lieu sûr.
- **Exporter sans secrets** : copie à partager pour demander de l'aide.
- **Afficher** : la configuration à l'écran, pour la copier si le
  téléchargement est bloqué (application mobile).
- **Importer** : chaque section du fichier remplace la section actuelle ; les
  sections absentes du fichier sont conservées.
- **Journal détaillé** : ajoute les sons reconnus au journal de l'add-on,
  pour diagnostiquer un problème.

## Mises à jour : changements à connaître

| Version | Changement | Que faire |
|---|---|---|
| 6.25 | `source_id` d'une caméra : `rtsp_<id>` (au lieu de l'URL) | Filtrer plutôt sur `entity_key` (6.38). |
| 6.28 | entity_id du micro : `claptrap_mic_…` (au lieu de `claptrap_mic_<index>_…`) | Mettre à jour les automatisations qui utilisaient l'ancien entity_id. |
| 6.32 | `source_id` d'un flux VBAN : `vban_<id>` (au lieu de l'IP) | Filtrer plutôt sur `entity_key` (6.38). |
| 6.33 | entity_id en ASCII (accents translittérés : « Bébé » devient `bebe`) | Mettre à jour les automatisations qui utilisaient un entity_id accentué. |
| 6.33 | `source_id` du micro : `mic` (au lieu de `mic_<index>`) | Adapter les automatisations qui filtraient `claptrap_clap` sur l'ancien identifiant. |
| 6.35 | Interface accessible uniquement par l'ingress (port 16045 fermé au réseau local) | Ouvrir l'add-on depuis Home Assistant. |
| 6.37 | Réglage « Fin d'un pic » retiré (un clap réverbéré ne compte plus pour deux) | Rien. |
| 6.37 | Un groupe avec entités ne se déclenche plus sans pic sonore | Rien ; un groupe sans entité envoie `clap_count: 0` dans ce cas. |
| 6.38 | Entités indisponibles tant que la source n'écoute pas | Vérifier les automatisations qui réagissaient au passage « indisponible ». |
| 6.39 | Champs `threshold`, `ha_entities`, `sound_whitelist` au niveau de la source retirés (ils sont dans les groupes) | Scripts utilisant l'API : passer par les groupes (`/api/source/sound_groups`). |
| 6.42.1 | Image précompilée téléchargée depuis GHCR | Rien. |
| 6.45 | Routes d'API héritées supprimées (`GET /api/rtsp/streams`, `PUT /api/rtsp/stream/<id>`, `/api/vban/update`, `/api/vban/sources`, `/api/vban/saved-sources`, `PUT /api/microphone/*` sauf `auto-start`, `/api/settings/save`, `/api/ha/entities`…) ; sources VBAN désignées par leur identifiant (`DELETE /api/vban/<id>`) | Scripts utilisant l'API : passer par `PATCH /api/sources/<type>/<id>` et les routes de groupes. |
| 6.47 | Au-delà de 4 claps, aucune entité n'est déclenchée (c'était « 4 claps ») | Rien. |
| 6.48 | Un export « sans secrets » est refusé à l'import | Importer une sauvegarde complète (« Exporter »). |
| 6.58 | Renommer un groupe renomme ses entity_id dans HA (historique conservé) | Après un renommage, adapter les automatisations (correspondances dans le journal). |
| 6.61 | Noms affichés des entités raccourcis (« Salon · Clap · 2 claps » au lieu de « VBAN: Salon Clap 2 claps ») ; entity_id inchangés | Rien (un nom personnalisé dans HA est conservé). |
| 6.62 | Un appareil par source dans HA (« ClapTrap Salon ») : noms affichés « ClapTrap Salon Clap · 2 claps » ; nouvelle entité event par groupe ; mise à jour automatique arrêtée avant les versions qui changent des identifiants (`breaking_versions`) | Rien ; affecter éventuellement une pièce à chaque appareil. |
| 6.66 | ffmpeg réduit à l'audio des caméras : codecs listés dans « Ajouter une source » ; seule la piste audio est demandée | Rien, sauf caméra muette avec « no decoder found » au journal : le signaler. |
| 6.79 | Découverte MQTT par appareil (un message par appareil au lieu d'un par entité) ; les entités existantes sont migrées automatiquement au premier démarrage (entity_id, noms, icônes, pièces et historique conservés) | Rien. |

Le détail de chaque version est dans le journal des modifications (onglet
**Journal des modifications** de l'add-on).

## Dépannage

**Aucune entité, journal « aucun broker »** : installez et démarrez l'add-on
Mosquitto broker, ou renseignez votre broker dans l'onglet Configuration
(voir « Broker MQTT externe »).

**Aucune entité dans Home Assistant** : vérifiez que l'intégration MQTT est
configurée, puis redémarrez l'add-on (toutes les entités sont republiées à
chaque connexion au broker et à chaque redémarrage de Home Assistant).

**Le micro n'apparaît pas** : vérifiez qu'il est listé dans Paramètres >
Système > Matériel, que l'audio de l'add-on est activé, puis rouvrez les
réglages du micro (la liste est relue à l'ouverture).

**Une caméra affiche « Erreur de flux »** : vérifiez l'adresse, l'utilisateur
et le mot de passe (le journal de l'add-on indique la cause, par exemple
« 401 Unauthorized »). Le **gain** de la caméra amplifie le son envoyé à la
reconnaissance (10 par défaut).

**Une caméra reste muette, journal « no decoder found for: … »** : son codec
audio n'est pas pris en charge (liste dans « Ajouter une source »). Choisissez
AAC ou G.711 dans les réglages audio de la caméra si elle le permet, et
signalez le codec dans une issue pour qu'il soit ajouté.

**Un flux VBAN affiche « Aucun paquet reçu »** : vérifiez que l'émetteur
diffuse vers l'IP de Home Assistant, port 6980, et que le nom du flux est
identique (le journal signale un flux reçu de la bonne IP sous un autre nom).

**Comprendre ce que la reconnaissance entend** : pendant la détection, le
bouton **Enregistrer 5 s** des réglages d'une source garde 5 secondes du son
reçu par la reconnaissance (gain compris). Faites le son pendant
l'enregistrement : vous pouvez ensuite l'écouter, le télécharger en WAV et voir
les sons reconnus fenêtre par fenêtre. Écho, saturation ou réduction de bruit
de la caméra s'entendent tout de suite.

**Sons aigus mal reconnus sur une caméra** (claquement de doigts pris pour
un animal, applaudissements notés « Mains » ou « Claque ») : regardez le
**format audio reçu** dans les réglages de la caméra. À 8 kHz (G.711, très
courant), tout ce qui dépasse 4 kHz est perdu, et les claquements de doigts
deviennent méconnaissables. Choisissez si possible AAC à 16 kHz ou plus dans
les réglages audio de la caméra, et désactivez sa réduction de bruit. Vérifiez
aussi que le VU-mètre ne reste pas dans le rouge (gain trop fort). « Mains »
et « Claque » sont des sons voisins d'« Applaudissement » : les cocher dans
le groupe est normal (l'assistant de calibrage le fait).

**Les claps ne sont pas comptés** : regardez la carte pendant un clap. Si le
son est reconnu mais que le score reste sous le seuil, baissez le seuil du
groupe. Si « 2 claps » donne « 1 clap », baissez la « force minimale d'un clap » ou
rapprochez le micro.

**Trop de fausses détections** : montez le seuil du groupe, décochez les sons
qui ne sont pas des claps, ou ajoutez-les aux sons ignorés partout.

Pour demander de l'aide, joignez le journal de l'add-on et un export **sans
secrets** : https://github.com/powange/ClapTrap-HA-Addon/issues
