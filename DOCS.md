# ClapTrap

ClapTrap écoute un micro, le son d'une caméra RTSP ou un flux VBAN, reconnaît
les sons avec le modèle YAMNet et crée des entités Home Assistant qui
s'allument à chaque clap (1, 2, 3 ou 4 claps).

## Prérequis

- **Home Assistant OS ou Supervised**, version 2025.10 ou plus récente,
  sur une machine **amd64** ou **aarch64** (Raspberry Pi 4 et 5, par exemple).
- **Un broker MQTT** : ClapTrap crée ses entités par MQTT et refuse de
  démarrer sans broker.
  1. Installez l'add-on **Mosquitto broker** (boutique officielle) et
     démarrez-le.
  2. Ajoutez l'intégration **MQTT** (Paramètres > Appareils et services) :
     Home Assistant la propose automatiquement une fois Mosquitto démarré.

Sans l'intégration MQTT, l'add-on fonctionne mais aucune entité n'apparaît.

## Installation

1. **Paramètres > Modules complémentaires > Boutique des modules
   complémentaires**, menu **⋮ > Dépôts**, puis ajoutez :
   `https://github.com/powange/ClapTrap-HA-Addon`
2. Recherchez **ClapTrap**, cliquez sur **Installer**, puis **Démarrer**.
   L'image est précompilée : l'installation ne prend que le temps du
   téléchargement.
3. Activez **Afficher dans la barre latérale** et, si vous le souhaitez,
   **Watchdog** (Home Assistant relance alors l'add-on s'il ne répond plus).
4. Ouvrez l'interface (**Ouvrir l'interface utilisateur Web**) et ajoutez
   votre première source.

L'interface n'est accessible que depuis Home Assistant (ingress). Depuis la
version 6.35, le port 16045 refuse les connexions directes
(`http://homeassistant:16045` répond « accès refusé ») : passez par le panneau
de l'add-on.

## Ajouter une source

Bouton **Ajouter une source**, puis :

- **Micro** : un micro branché sur la machine Home Assistant (USB le plus
  souvent). Un seul micro possible. S'il n'apparaît pas, vérifiez qu'il est
  listé dans Paramètres > Système > Matériel, puis rouvrez l'assistant.
- **Caméra RTSP** : l'adresse du flux, par exemple
  `rtsp://utilisateur:motdepasse@192.168.1.20:554/stream` (voir l'application
  ou la documentation de la caméra). Seul le protocole `rtsp://` est accepté.
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

Le mot de passe d'une adresse RTSP est masqué dans les réglages ; il
s'affiche quand vous modifiez l'adresse.

Activer, désactiver, ajouter ou supprimer une source redémarre brièvement
toute la détection (la barre d'état affiche « Redémarrage… »).

Sur chaque carte, l'état de la source est affiché : « À l'écoute »,
« Connectée », « Flux perdu, reconnexion… », « Aucun paquet reçu » (VBAN :
rien reçu depuis 10 s)…

## Groupes de sons

Chaque source a un ou plusieurs **groupes de sons**. Un groupe réunit les
sons qui doivent le déclencher (par défaut : applaudissements, mains) et a son
propre **seuil de confiance** (score YAMNet minimal, réglable avec la poignée
sur la carte). Les sons entendus par la source s'ajoutent automatiquement à la
liste, non cochés : cochez ceux qui doivent déclencher le groupe.

Un son ne peut être coché que dans un groupe par source. Si plusieurs groupes
reconnaissent le même événement sonore, seul celui qui a le meilleur score
déclenche ses entités (les autres sont notés « ignoré » dans l'historique).

**Sons ignorés partout** (onglet Réglages) : ces sons ne déclenchent aucun
groupe, sur aucune source.

## Entités Home Assistant

Toutes les entités sont regroupées dans l'appareil **ClapTrap**.

| Entité | Rôle |
|---|---|
| `binary_sensor.claptrap_<source>_<groupe>_1clap` | 1 clap détecté |
| `binary_sensor.claptrap_<source>_<groupe>_2claps` | 2 claps, etc. jusqu'à `4claps` (4 claps ou plus) |
| `binary_sensor.claptrap_detection` | la détection tourne (attribut `sources`) |

- `<source>` vaut `mic` pour le micro, `rtsp_` suivi des 8 premiers caractères
  de l'identifiant de la caméra, `vban_` suivi du nom du flux ; `<groupe>` est
  l'identifiant du groupe (`clap` pour le groupe par défaut). Les identifiants
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

Exemple d'automatisation :

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

**Supprimer les entités orphelines** (onglet Réglages) retire les entités
ClapTrap qui ne correspondent plus à aucune source.

## Événement `claptrap_clap`

Chaque détection émet aussi l'événement `claptrap_clap`, souvent plus simple
qu'un webhook :

```json
{
  "source_id": "rtsp_4f2c9a1e-...",
  "entity_key": "rtsp_4f2c9a1e",
  "source_name": "Salon",
  "timestamp": 1790000000.12,
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
- `timestamp` : secondes depuis le 1er janvier 1970 (UTC).
- `clap_count` : nombre de claps comptés (0 pour un groupe sans entité qui
  reconnaît un son sans aucun pic).
- `labels` : sons reconnus (noms YAMNet en anglais) et leur score.

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
`"event": "clap"`. Le bouton **Tester** envoie le même contenu avec
`"test": true`.

L'URL d'un webhook Home Assistant est un secret : quiconque la connaît peut
déclencher l'automatisation. ClapTrap la masque dans ses journaux.

## Réglages du comptage (onglet Réglages)

| Réglage | Défaut | Rôle |
|---|---|---|
| Fenêtre multi-clap | 1,5 s | Durée pendant laquelle les claps consécutifs sont comptés ensemble (3 claps en 1,5 s = « 3 claps »). |
| Écart minimal entre deux claps | 0,08 s | Filtre les rebonds d'un même clap. |
| Force minimale d'un clap | 3 × le bruit ambiant | Un clap n'est compté que s'il est ce nombre de fois plus fort que le bruit ambiant. Baissez pour un micro faible, montez contre les faux positifs. |

Le seuil de confiance, lui, se règle par groupe, directement sur les cartes.

## Configuration

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
| 6.35 | Interface accessible uniquement par l'ingress (port 16045 fermé) | Ouvrir l'add-on depuis Home Assistant. |
| 6.37 | Réglage « Fin d'un pic » retiré (un clap réverbéré ne compte plus pour deux) | Rien. |
| 6.37 | Un groupe avec entités ne se déclenche plus sans pic sonore | Rien ; un groupe sans entité envoie `clap_count: 0` dans ce cas. |
| 6.38 | Entités indisponibles tant que la source n'écoute pas | Vérifier les automatisations qui réagissaient au passage « indisponible ». |
| 6.39 | Champs `threshold`, `ha_entities`, `sound_whitelist` au niveau de la source retirés (ils sont dans les groupes) | Scripts utilisant l'API : passer par les groupes (`/api/source/sound_groups`). |
| 6.42.1 | Image précompilée téléchargée depuis GHCR | Rien. |

Le détail de chaque version est dans le journal des modifications (onglet
**Journal des modifications** de l'add-on).

## Dépannage

**L'add-on ne démarre pas (« service mqtt manquant »)** : installez et
démarrez l'add-on Mosquitto broker (voir Prérequis).

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

**Un flux VBAN affiche « Aucun paquet reçu »** : vérifiez que l'émetteur
diffuse vers l'IP de Home Assistant, port 6980, et que le nom du flux est
identique (le journal signale un flux reçu de la bonne IP sous un autre nom).

**Les claps ne sont pas comptés** : regardez la carte pendant un clap. Si le
son est reconnu mais que le score reste sous le seuil, baissez le seuil du
groupe. Si « 2 claps » donne « 1 clap », baissez la « force minimale d'un clap » ou
rapprochez le micro.

**Trop de fausses détections** : montez le seuil du groupe, décochez les sons
qui ne sont pas des claps, ou ajoutez-les aux sons ignorés partout.

Pour demander de l'aide, joignez le journal de l'add-on et un export **sans
secrets** : https://github.com/powange/ClapTrap-HA-Addon/issues
