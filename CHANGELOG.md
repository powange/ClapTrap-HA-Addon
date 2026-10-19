# Changelog

## 6.94.0

### AppArmor en mode strict

- Le profil AppArmor de ClapTrap passe en mode **strict** : les accès hors du
  profil sont refusés, et non plus seulement journalisés. ClapTrap ne peut
  lire, écrire ou lancer que ce dont il a besoin.
- Les règles ont été établies sur une installation réelle : mesure de 26
  démarrages avec toutes les fonctions, règles ajoutées en 6.93, puis
  nouvelle mesure vide après redémarrage.
- La CI démarre chaque image de pull request sous le profil strict, avec un
  fuseau horaire et un faux Supervisor. L'add-on démarre alors complètement :
  bashio, Gunicorn, MQTT, plus l'autotest YAMNet et ffmpeg. La CI échoue si
  un accès est refusé ou si l'interface ne répond pas. Elle détecte ainsi une
  image de base ou des paquets Debian qui changeraient le démarrage.
- **En cas de problème** (une fonction qui échoue depuis cette version), la
  section « Sécurité (AppArmor) » de la documentation montre comment voir
  les accès refusés. Elle donne aussi la procédure de mesure pour étendre
  le profil.

## 6.93.0

### AppArmor : profil complété (avant le mode strict)

- Accès relevés sur une vraie installation, en mode « complain », sur 26
  démarrages et toutes les fonctions. Ils sont maintenant autorisés :
  - dossier `/data/` lui-même : écriture durable des réglages et des sons
    appris ;
  - réglage du fuseau horaire au démarrage par l'image de base (lien
    `/etc/localtime`, `/etc/timezone`, `dpkg-reconfigure tzdata`, debconf).
    Sans ces règles, ce réglage aurait échoué en mode strict ;
  - lecture des scripts exécutés (with-contenv, scripts de dpkg).
- Correction : la règle des signaux entre processus de ClapTrap visait le
  nom `claptrap`, mais le Supervisor charge le profil sous le nom
  `<dépôt>_claptrap`. Elle utilise maintenant un motif.
- Le profil reste en mode « complain ». Le mode strict viendra en 6.94, si
  une nouvelle mesure ne signale plus rien. La procédure de mesure dans la
  documentation est corrigée.

## 6.92.0

### Réduction du bruit de fond

- Nouveau réglage par source : **Réduction du bruit de fond** (Désactivée,
  Légère, Moyenne, Forte ; désactivée par défaut). Il atténue un bruit
  continu (souffle, ventilation, réfrigérateur) avant YAMNet, par
  « spectral gating ». Le traitement se fait en continu sur le son brut,
  avant le gain. Pour chaque fréquence, le niveau du bruit est le plus faible
  des 30 dernières secondes : une alarme qui bipe ou des pleurs ne sont donc
  jamais pris pour du bruit.
- Essai sur un bruit stationnaire : de 5 à 8 dB de mieux selon le niveau,
  avec le clap gardé à environ 90 %.
- Le comptage des claps garde le son brut.
- **Enregistrer 5 s** donne l'extrait avant la réduction (même gain) et
  après, exactement ce que YAMNet reçoit, chacun téléchargeable.
- Chaque son appris indique le niveau de réduction actif quand il a été
  appris.
- Essai sur un extrait réel de caméra : moins de classes parasites
  (« Animal » de 0,41 à 0,15, « Klaxon » de 0,50 à 0,15).
  Les aigus coupés par une caméra à 8 kHz ne sont pas recréés.

## 6.91.0

### Sons appris

- Nouvelle section **Sons appris** (onglet Réglages) : **Apprendre un son…**
  enregistre le bruit de fond puis 3 à 10 exemples d'un son précis (sonnette,
  bip du lave-linge) sur une source. Le son devient une classe de plus,
  « Appris : <nom> », à cocher dans un groupe comme n'importe quel son
  (seuil, comptage, alerte, sons ignorés), sur toutes les sources.
- Empreinte : les scores des 521 classes de YAMNet (aucun calcul de plus),
  compressés et centrés sur le bruit de fond, comparés aux exemples.
  Essai avec le vrai modèle sur des sons de synthèse :
  - la sonnette apprise obtient 0,8 à 1 ;
  - une autre tonalité, elle aussi rangée par YAMNet dans « Music », obtient
    0,04 à 0,44 ;
  - des bips et le bruit de fond obtiennent 0.
- Bilan avant l'enregistrement : exemples reconnus entre eux, avertissement
  si le son ressemble trop au bruit de fond.
- Le classifieur rend désormais les 521 classes, et le reste de ClapTrap en
  garde les 30 meilleures, comme avant.

## 6.90.0

### Assistant RTSP : « Vérifier l'adresse »

- Nouveau bouton dans l'assistant d'ajout d'une caméra : ClapTrap s'y connecte
  et lit 2 s de son avant l'ajout. Il affiche le format audio (codec,
  fréquence, canaux), alerte sous 16 kHz (sons aigus mal reconnus), et sinon
  explique l'échec : identifiants refusés, chemin du flux introuvable,
  caméra injoignable ou nom inconnu, port fermé, aucune piste audio, piste
  annoncée mais muette (un autre lecteur a l'audio). Le format vérifié
  s'affiche aussitôt sur la carte de la caméra.
- Vérifié sur de vrais flux RTSP (mediamtx) : 16 kHz, 8 kHz, vidéo seule,
  chemin absent, nom inconnu, adresse injoignable.

## 6.89.0

### Détection plus réactive et plus économe

- **Décision anticipée** : quand un seul groupe est en cours et qu'il a déjà
  son nombre maximal de claps (un groupe « 1 clap » dès le premier),
  l'événement part 0,5 à 1 s après le dernier clap (calme confirmé par une
  analyse) au lieu d'attendre la fin de la fenêtre : environ 1 s de moins
  pour un clap simple. Un double clap rapide sur un groupe « 1 clap » reste
  « 2 claps », comme avant ; un clap plus tardif dans la même fenêtre est
  ignoré. Désactivée avec l'arbitrage entre pièces (les sources doivent
  finir en même temps pour être comparées).
- **Fenêtres sur l'horloge audio** : la fin d'une fenêtre de comptage est
  vérifiée tous les 100 ms (dans le fil d'analyse, après une analyse en
  attente), et non plus à l'analyse suivante (jusqu'à 0,5 s de moins).
- **Économie de processeur** : sans onglet ouvert, YAMNet ne tourne qu'après
  un pic sonore, quand ses résultats peuvent servir ; dans une pièce calme,
  presque toutes les analyses disparaissent (Raspberry Pi plus disponible).
  Analyse continue pour les groupes d'alerte, sans nombre de claps ou qui
  ajoutent les sons entendus.

## 6.88.0

### Diagnostics de performance

- **Capteurs Home Assistant par source** (diagnostic, toutes les 30 s) :
  durée moyenne d'une analyse YAMNet, analyses perdues faute de processeur,
  coupures et reconnexions du flux. Avec l'historique de HA, on voit tout de
  suite si une source de trop dégrade un Raspberry Pi.
- **Processeur et mémoire de l'add-on** (`sensor.claptrap_processeur`,
  `sensor.claptrap_memoire`), ffmpeg et parecord compris.
- Dans l'interface : durée d'analyse (moyenne et maximum) et reconnexions
  sur la carte de chaque source, processeur et mémoire dans la barre d'état
  pendant la détection.

## 6.87.0

### Blueprints Home Assistant (retours d'usage)

- **Claps pour faire défiler des scènes** (nouveau) : chaque déclenchement
  active la scène suivante d'une liste (scènes Hue ou de Home Assistant),
  puis revient à la première ; option : tout éteindre après la dernière
  (lumières, prises, ventilateurs). La scène en cours est la dernière
  activée par Home Assistant, sans entrée auxiliaire ; on repart de la
  première après 10 minutes sans clap (réglable).
- **Claps pour lancer des actions au choix** (nouveau) : script, commande RF
  ou infrarouge, télécommande… pour tout ce que les autres ne couvrent pas.
- **Claps pour basculer une lumière** : accepte aussi les ventilateurs et
  les interrupteurs virtuels (`input_boolean`).
- Vérifiés sur un vrai Home Assistant : ventilateur basculé, action lancée,
  huit claps qui donnent scène A, B, C, extinction, A, B, C, extinction (même
  avec une lumière qui reste allumée), et retour à A après la pause.

## 6.86.0

### Plusieurs pièces

- **Seule la source la plus proche déclenche** (option de l'onglet Réglages,
  désactivée par défaut ; aussi par MQTT, `set_rooms`) : un clap entendu par
  plusieurs sources (micro du salon et caméra du couloir) ne déclenche que
  celle qui l'entend le plus fort par rapport à son bruit de fond. Les autres
  sont notées « entendu plus fort dans … » dans l'historique. Seuls les
  claps de groupes de même nom se comparent, et une source dont le nombre de
  claps déclenche une entité passe en priorité. Attente réglable de 0,3 à
  3 s (1 s par défaut, seulement si plusieurs sources sont connectées).
- **Capteur « Pièce du dernier clap »** (`sensor.claptrap_derniere_piece`) :
  la source du dernier clap qui a déclenché une entité, avec le nombre de
  claps, le groupe et l'heure ; une seule automatisation pour toute la
  maison (exemple dans la documentation).
- Événement et webhook : nouveau champ `strength` (force du clap par rapport
  au bruit de fond de la source).

## 6.85.0

### Blueprints Home Assistant

- **Quatre automatisations prêtes à l'emploi**, importables en un clic
  (boutons dans la documentation, dossier `blueprints/` du dépôt) :
  - **Claps pour basculer une lumière** (ou une prise) : toutes dans le même
    sens (si l'une est allumée, tout s'éteint) ;
  - **Alerte sonore sur le téléphone** : notification de l'application
    mobile quand un capteur d'alerte s'allume (fumée, pleurs, aboiement,
    sonnette), aussi après une reconnexion pendant l'alarme ; pause réglable
    entre deux notifications ; flash des lumières en option, qui reprennent
    ensuite leur état ;
  - **Confirmation vocale après un clap** : une enceinte dit une phrase ;
  - **Un clap pour parler à Assist** : lance une conversation sur un
    satellite vocal.
- Vérifiés sur un vrai Home Assistant 2025.10 : les quatre se chargent, et un
  clap simulé éteint bien deux prises (l'une allumée, l'autre éteinte).
- Seuls les capteurs de claps (ou d'alerte) sont proposés, pas le capteur
  « Détection ».
## 6.84.0

### Modèles de groupes et mode « alerte »

- **Nouveau groupe à partir d'un modèle** : Applaudissements, Claquement de
  doigts, Toc-toc, Sifflement, Détecteur de fumée, Bris de glace, Pleurs de
  bébé, Aboiement, Sonnette, Alarme ou sirène. Les bons sons sont déjà
  cochés, le seuil et le comptage réglés ; plus besoin de connaître YAMNet.
  Un son déjà utilisé par un autre groupe est signalé. Aussi par MQTT
  (`add_group` avec `preset`).
- **Mode « alerte »** (alarmes, pleurs, aboiements…) : un capteur
  `binary_sensor.claptrap_<source>_<groupe>_alerte` reste allumé tant que le
  son est reconnu, et s'éteint après 5 s de silence ou à l'arrêt de la
  détection, au lieu d'impulsions « N claps » de 2 s. Confirmation réglable
  (1 à 4 reconnaissances sur 2 s) et type du capteur (fumée, sécurité, son,
  problème, présence). Événement `claptrap_alert` et webhook `"event":
  "alert"` au début et à la fin. Une alerte n'empêche pas un clap d'être
  compté. L'état reste juste si Home Assistant redémarre pendant l'alarme.

## 6.83.0

### Build, tests et documentation

- **Les pull requests construisent aussi l'image aarch64** (Raspberry Pi), sur
  une machine ARM : une casse propre à cette architecture apparaît avant la
  publication, et non après, quand l'image amd64 était déjà publiée.
- Contrôle des dépendances de l'image : il ne fera plus échouer le build le
  jour où toutes les dépendances de mediapipe seront satisfaites.
- Une pull request qui ne touche que les tests ou la documentation ne
  reconstruit plus les images.
- Outils de test (pytest, scipy…) suivis par Dependabot.
- Registre de HA : une réponse noyée parmi d'autres messages ne fait plus
  attendre jusqu'à deux fois le délai prévu.
- Taille installée de l'image corrigée dans la documentation (environ
  450 Mo).
- Nouveaux tests : client WebSocket du registre de HA (authentification,
  réponses intercalées, délais, fermeture), lecture des messages d'appareil
  retenus et message de naissance de HA, bouton « Supprimer les entités
  orphelines » (refus en mode dégradé, seules les orphelines), relance à
  chaud de la détection, test du son VBAN. Couverture : 92 %.

## 6.82.0

### Interface

- **Calibrage** : le résultat d'une analyse abandonnée (fenêtre fermée pendant
  « Analyse des prises… ») ne s'affiche plus dans le calibrage d'une autre
  source, où « Appliquer » aurait visé la mauvaise session.
- **Pastilles de son fiables au toucher** : un son découvert (ou la réponse à
  une coche précédente) pendant l'appui reconstruisait la liste et le toucher
  était perdu. La liste attend maintenant le relâchement.
- **Démarrage de la détection** : « Démarrage… » / « Arrêt… » sur le bouton et
  dans la barre d'état pendant les quelques secondes de la requête ; un
  statut reçu entre-temps ne réactive plus le bouton.
- **Clavier** : le focus reste sur l'interrupteur d'une source et sur le
  bouton de détection pendant leur requête (il repartait au début de la
  page).
- **« Afficher » la configuration sans secrets** par défaut (identifiants des
  caméras et adresses des webhooks masqués), pour la copier sur mobile et
  demander de l'aide ; « Afficher la sauvegarde complète » sur demande, avec
  un avertissement.
- **Assistant** : Entrée (ou « OK » du clavier mobile) valide l'étape.
- Gain et volume : enregistrés 600 ms après le dernier changement, une seule
  requête à la fois (une par flèche du clavier auparavant).
- « Rétablir les valeurs par défaut » du comptage demande confirmation, avec
  les valeurs actuelles et par défaut ; la suppression d'un groupe le nomme.
- Moins de travail pour le navigateur : les cartes ne réécrivent que ce qui
  change, et le thème n'est plus recalculé quand la page est cachée.

## 6.81.0

### Home Assistant, MQTT et API

- **Interrupteur « Détection »** : l'allumer alors qu'elle tourne déjà
  (automatisation du matin) n'affiche plus « erreur » dans « Dernière
  commande ».
- **Noms de sons vérifiés** (API, MQTT) : « clapping » devient « Clapping »
  (la détection compare exactement, le groupe ne réagissait jamais) ; un son
  inconnu de YAMNet est refusé avec une suggestion.
- **Webhook non modifiable par MQTT** : un client du broker pouvait faire
  envoyer par l'add-on une requête à chaque clap vers l'adresse de son choix.
  Elle se règle dans l'interface.
- Les entités de réglage (interrupteurs, seuils, noms) répondent même
  pendant une commande longue (prise de calibrage par MQTT).
- Un nouveau groupe ne reprend plus l'entity_id d'un groupe dont le
  renommage dans HA est en attente (entités « _2 » pour toujours).
- Capteur « Configuration » allégé : nombre de sons par groupe (la liste
  est dans le capteur « sons »), plus de liste des commandes (commande
  `help`), plafond sous la limite du recorder de HA.
- Messages d'appareil plus courts (abréviations de HA : environ 40 % de
  moins), vérifiés sur un vrai Home Assistant.
- Broker qui refuse la connexion : attente croissante jusqu'à 5 min et un
  message toutes les 10 min au lieu d'environ 11 000 lignes par jour ;
  message explicite si les identifiants sont refusés.
- Documentation : une seule installation de ClapTrap par broker.

## 6.80.0

### Détection audio

- **Plus d'analyses perdues** (régression de la 6.77) : l'horloge audio
  était calée pile sur le seuil qui signale une coupure du flux. La moindre
  milliseconde de retard d'un bloc passait pour une coupure et vidait la
  fenêtre d'analyse : YAMNet restait aveugle au moins 1 s, et 2 à 33 % des
  analyses étaient perdues selon le flux (claps ratés). L'horloge suit
  maintenant l'heure en douceur ; seule une vraie coupure (plus de 0,5 s)
  remet la fenêtre à zéro.
- **Plus de clap déclenché en retard après une coupure** : un geste reconnu
  juste avant qu'une caméra se reconnecte partait au retour du flux, des
  dizaines de secondes plus tard. Il est maintenant envoyé dès la reprise
  avec les claps déjà comptés, ou abandonné si la coupure dépasse 5 s.
- **Paquets VBAN perdus compensés** (Wi-Fi) : remplacés par du silence
  d'après le compteur de trames, un paquet arrivé en retard est ignoré. Le
  son n'est plus raccourci : l'écart entre deux claps reste juste.
- Une erreur pendant la préparation d'une source (micro) s'affiche sur sa
  carte, et YAMNet est libéré au lieu de tourner pour rien.
- Une source dont tous les groupes sont désactivés n'est plus analysée (sauf
  onglet de l'interface ouvert, pour les sons entendus en direct).
- Un Home Assistant lent ne retarde plus l'entité du clap suivant
  (événement et webhook envoyés à part).
- Événement et webhook : nouveau champ `clap_time`, l'instant du premier
  clap (`timestamp` est celui de l'envoi, 1,5 à 2,5 s plus tard).
- **Diagnostic** : analyses ou blocs perdus (processeur trop chargé) et
  coupures du flux affichés sur la carte de la source (relus toutes les
  30 s), et dans l'attribut `diagnostic` du capteur « Dernière classe »
  (avec la durée des analyses).
- Le fil d'analyse s'arrête toujours avec sa source.

## 6.79.0

### Home Assistant

- **Découverte MQTT par appareil** : un seul message retenu par appareil
  (« ClapTrap » et un par source) au lieu d'un par entité (33 messages pour
  un micro et une caméra, 3 maintenant). Publication plus légère, appareil
  et entités créés ou supprimés d'un coup.
- **Migration automatique** au premier démarrage : les entités existantes
  sont reprises par leur appareil sans être recréées. Vérifié sur un vrai
  Home Assistant (2025.10) : mêmes 33 entités, entity_id personnalisés,
  noms, icônes et historique conservés, aucun doublon ; les anciens messages
  sont ensuite effacés du broker.
- Une source supprimée pendant que l'add-on était arrêté est retirée au
  démarrage avec son appareil et ses états retenus.

## 6.78.0

### Calibrages

- **Couleur de l'étape** : orange pendant le compte à rebours d'une prise
  enchaînée (« Début de la prise dans 3 s »), vert pendant l'écoute, bleu
  pendant l'analyse ; neutre en pause. Dans les deux calibrages.

### Documentation

- Taille de l'image actualisée (environ 120 Mo à télécharger, 370 Mo une
  fois installée) ; ordre du calibrage (le bruit de fond d'abord) ;
  `<groupe>` des entity_id suit le nom du groupe ; option `rtsp_tcp` de la
  commande `add_rtsp`.
- Journal des modifications allégé (164 Ko → 27 Ko) : les versions 6.59.0
  et antérieures sont dans `CHANGELOG-archive.md`, lié en bas de cet onglet.

### Tests

- Écoute VBAN sur un vrai socket UDP : paquets reçus et routés, purge d'une
  source muette, port déjà pris.
- Vraie page d'accueil et fichiers versionnés (cache long, traversée
  refusée), démarrage automatique, erreurs de démarrage de la détection
  (« déjà en cours », paramètres invalides, réglages illisibles).

## 6.77.0

### Reconnaissance plus rapide et plus fiable

- **YAMNet analyse la dernière seconde toutes les 0,5 s** (au lieu de
  secondes disjointes, une fois par seconde) : la reconnaissance arrive
  environ deux fois plus tôt, et un clap à cheval sur deux secondes est
  entendu en entier dans l'une des fenêtres au lieu de l'être à moitié dans
  chacune. Sur un extrait de la caméra de la chambre, un claquement manqué
  par l'ancien découpage est maintenant reconnu (« Slap, smack », « Cap
  gun »).
- Chaque résultat est daté exactement à la fin de la seconde analysée, même
  quand ffmpeg livre le son par rafales.
- Consommation processeur : deux analyses par seconde au lieu d'une, encore
  faible (environ 4 ms par analyse sur un PC).

### Calibrages

- **Enchaîner les prises automatiquement** : une coche, dans le calibrage
  d'une source comme dans celui du comptage, lance chaque prise seule après
  un compte à rebours de 3 s, sans cliquer sur « Enregistrer » entre deux
  prises. Bouton **Pause** pour souffler ; le choix est mémorisé par le
  navigateur.

## 6.76.0

### Home Assistant

- **Option « Commandes MQTT »** (onglet Configuration de l'add-on, cochée par
  défaut) : décochée, le topic `claptrap/command` (ajout, modification,
  suppression de sources) n'est plus écouté ; les entités de réglage restent
  utilisables. La documentation explique aussi comment restreindre ce topic
  par les ACL du broker.
- « Vérifier le certificat » s'affiche coché : la vérification était active
  mais l'option paraissait décochée.
- « Dernière commande » : un résultat volumineux (`get_settings`, `help`)
  reste sur le topic `claptrap/command/result` sans devenir un attribut (le
  recorder de HA refusait de l'enregistrer au-delà de 16 Ko) ; le bilan d'un
  renommage reste en attributs.
- Nettoyage des entités orphelines : leurs états retenus (seuil, nom, sons…)
  sont effacés avec elles au lieu de rester sur le broker.
- Renommage d'un groupe : l'entité « événement » personnalisée est retrouvée
  dans le registre (elle était comptée « introuvable »).
- Disponibilité d'une source publiée sous le même verrou que sa
  republication (une caméra pouvait rester indisponible après un redémarrage
  de HA) ; connexion au registre fermée quand l'authentification échoue.

### API et import

- L'import refuse ce que l'interface refuse ensuite : deux caméras à la même
  adresse, un nom de source de plus de 80 caractères ; un identifiant de
  source invalide (« / ») est remplacé (la source ne pouvait plus être
  supprimée). À l'inverse, l'API refuse plus de 600 sons ignorés et un nom
  de micro de plus de 200 caractères, qu'un import refusait.
- Calibrage : une seconde prise simultanée (double clic) est refusée, et
  « Annuler » répond pendant une prise au lieu d'attendre sa fin.
- Test du son : gain de la caméra validé (0 à 100) ; un gain VBAN de 0 est
  respecté comme par la détection.
- Ajout du micro : un JSON trop imbriqué renvoie une erreur 400 (500
  auparavant) ; une redirection de webhook n'est plus journalisée deux fois.

## 6.75.0

### Premiers pas

- **Une nouvelle source détecte tout de suite les applaudissements** : son
  groupe « Clap » a déjà « Applaudissement », « Mains » et
  « Applaudissements » cochés. Il était vide, et il fallait ouvrir les
  réglages, activer « Ajouter les sons entendus », lancer la détection,
  taper dans ses mains puis cocher avant de détecter quoi que ce soit. La
  consigne de fin de l'assistant le dit.
- **Ajouter n'importe quel son connu** : la recherche d'un groupe propose
  aussi les sons pas encore entendus, parmi les 521 de YAMNet (« siffl »
  propose « Sifflement »), ajoutés cochés d'un toucher ; sauf ceux d'un autre
  groupe de la source ou ignorés partout.
- Les sources déjà enregistrées ne changent pas : seule une source ajoutée
  (ou un micro remis par l'assistant avec un groupe vide) reçoit ces sons.

### Interface

- Barre d'état lisible sur mobile (jusqu'à trois lignes, la durée et les
  « groupes sans son » étaient coupés) ; sans source, elle dit « Ajoutez
  une source » au lieu de « Activez une source ».
- Schéma « Comptage des claps » lisible (textes de 6 à 9 px auparavant),
  menu « Comptage » à libellés courts, « Ajouter à la main » avec un
  chevron, « 2 canaux » au lieu de « 2 canalx ».
- Contrastes portés à 4,5:1 (étiquette « Micro », textes secondaires en
  sombre) ; « ignorer » partout pour les sons ignorés.
- Confirmation de suppression : le focus est sur « Annuler » ; fermer le
  calibrage pendant une prise explique pourquoi c'est impossible.

## 6.74.0

### Image

- **Image environ 35 % plus légère** (580 Mo installés au lieu de 880) :
  OpenCV et matplotlib ne sont plus installés. MediaPipe les importait
  seulement pour ses outils de dessin vidéo, jamais utilisés par ClapTrap :
  des modules vides les remplacent, et l'autotest du build vérifie que la
  reconnaissance fonctionne. Le chargement de la reconnaissance au
  démarrage de la détection passe d'environ 0,6 s à 0,2 s et de 83 à 30 Mo
  de mémoire.
- **Correctifs de sécurité Debian** : les paquets sont mis à jour à chaque
  construction, et une version construite plus d'une semaine après la
  précédente refait vraiment cette étape (elle restait figée tant que le
  Dockerfile ne changeait pas : 28 paquets en retard dans la 6.70.1).
- **Scan des images publiées** (Trivy, amd64 et aarch64) ajouté à l'audit
  hebdomadaire : entre deux versions, il signale qu'une nouvelle s'impose
  pour embarquer des correctifs. L'outil `tempio` de l'image de base,
  inutilisé et porteur de failles connues, est retiré.
- Outils de test épinglés et cache pip dans la CI.

## 6.73.0

### Calibrer le comptage

- **Nouvel outil** dans Réglages › Comptage des claps : **Calibrer le
  comptage…** propose la fenêtre multi-clap, l'écart minimal entre deux
  claps et la force minimale d'un clap à partir de vos propres gestes,
  mesurés sur la source de votre choix (environ 2 minutes, la source ne
  déclenche rien pendant ce temps) :
  1. 5 s de bruit de fond ;
  2. 3 claps simples (force de vos claps, rebonds) ;
  3. 3 fois « 2 claps » et 2 fois « 3 claps » (durée de vos gestes) ;
  4. au choix, « 2 claps, une pause, 1 clap » : vérifie que deux commandes
     ne sont pas fusionnées.
- **Résultat vérifié** : vos prises sont rejouées avec les réglages actuels
  et proposés (« prises correctement comptées : 4/9 → 9/9 »), avec le détail
  par prise et des conseils (claps trop proches du bruit, pause trop courte
  entre deux commandes). Une prise ratée (clap fait trop tôt, oublié) est
  écartée et signalée au lieu de fausser les valeurs ; la décroissance d'un
  clap n'est pas prise pour un rebond. Les valeurs restent modifiables
  avant « Appliquer ».

## 6.72.0

### Détection

- **Fenêtre de comptage fidèle au réglage** : seuls les claps de la fenêtre
  réglée sont comptés. Elle s'étendait jusqu'au résultat YAMNet suivant
  (jusqu'à environ 1 s de plus) : deux claps puis un clap isolé 1,8 s plus
  tard donnaient un « 3 claps » sans entité au lieu de « 2 claps » puis
  « 1 clap ». Si vous aviez raccourci la fenêtre pour compenser, vous pouvez
  la remettre à la durée de vos gestes. Un groupe désactivé pendant qu'il
  compte ne laisse plus d'état en suspens.
- **Caméra qui n'envoie pas son audio** : au lieu de « Flux perdu,
  reconnexion… » en boucle, la cause est donnée (« la caméra annonce une
  piste audio mais n'envoie aucun son : un autre lecteur RTSP a peut-être
  l'audio »), constaté sur des caméras IMOU qui ne servent l'audio qu'à un
  client. « Tester le son » refuse d'ouvrir une seconde connexion à une
  caméra déjà écoutée.
- **Cause des erreurs sur la carte** : « Erreur de flux » est complétée par
  la raison (identifiants refusés, caméra sans audio, écoute VBAN
  indisponible, reconnaissance en échec).
- **VBAN** : écoute UDP indisponible ou échec d'initialisation de YAMNet
  signalés en erreur (la carte restait sur « Connexion… ») ; un flux
  multicast repris par un autre PC n'est plus ignoré ; plus de trace
  d'erreur à l'arrêt de l'écoute.
- **Calibrage** : une prise simplement atténuée par le limiteur n'est plus
  annoncée « saturée ».

## 6.71.0

### Réglages fiables (fenêtre de réglages d'une source)

- **Plus de modifications sur des données périmées** : la fenêtre relit la
  source à chaque action. Après un rechargement des réglages (retour sur
  l'onglet, renommage, reconnexion), un son coché était bien enregistré sur
  le serveur mais l'écran affichait encore l'ancien état (groupe « sans
  son », pastille décochée à la réouverture, titre périmé). Les réglages
  rechargés sont désormais fusionnés dans les objets existants (une source
  ou un groupe garde le même objet), et le titre et les groupes de la
  fenêtre ouverte sont refaits sur place, seulement s'ils ont changé.
- **Micro désigné par son nom PulseAudio** : si la liste des micros changeait entre
  l'affichage et le choix (micro branché), un autre micro pouvait être
  enregistré. La liste est aussi mise à jour dans la fenêtre ouverte : un
  micro branché apparaît. Même correction dans l'assistant.
- **Adresse RTSP** : après l'enregistrement d'une nouvelle adresse,
  « Afficher le mot de passe » et « Tester le son » utilisaient l'ancienne.
- **Test du son pendant la détection** : celui d'une source non écoutée
  n'est plus coupé au premier changement de statut d'une autre source.
- **Ignorer un son partout** : un son coché dans un groupe est signalé
  (« utilisé par Clap (Micro) ») et une confirmation est demandée ; la liste
  est triée par nom français.

## 6.70.1

### Interface

- **VU-mètre toujours visible dans les réglages d'une source** : avec le
  titre et le bouton de fermeture, il reste collé en haut de la fenêtre
  quand on la fait défiler. Il sortait de l'écran une fois descendu jusqu'à
  « Tester le son » (sur mobile, le test semblait ne rien faire), ou en
  réglant le gain. Le VU-mètre de la carte ne change pas.

## 6.70.0

### Sources

- **Enregistrer un extrait** : pendant la détection, le bouton
  **Enregistrer 5 s** des réglages d'une source garde 5 s du son que reçoit
  la reconnaissance (gain, auto-gain et limiteur compris). L'extrait
  s'écoute directement, se télécharge en WAV, et la liste des sons reconnus
  s'affiche fenêtre par fenêtre (environ 1 s chacune). Un son déjà saturé
  à l'entrée (micro ou caméra) est signalé, ainsi que les sons forts
  atténués par le limiteur. Utile pour comprendre une mauvaise
  reconnaissance (écho, réduction de bruit de la caméra, gain trop fort).
- Calibrage et extraits partagent la même écoute de la source ; les
  écoutes du détecteur choisissent leurs événements.

## 6.69.1

### Interface

- **« Tester le son » pendant la détection** : grisé seulement pour une
  source que la détection écoute, avec la raison écrite sous le bouton
  (elle n'apparaissait qu'au survol de la souris, donc jamais sur mobile) :
  son niveau, et pour une caméra son format audio, sont déjà affichés en
  direct. Une source non écoutée (désactivée, ajoutée depuis le démarrage)
  reste testable sans arrêter la détection.

## 6.69.0

### Caméras

- **Format audio reçu affiché** dans les réglages de la caméra (codec,
  fréquence, mono/stéréo), dès qu'elle est écoutée (« Tester le son » ou
  détection). Sous 16 kHz (G.711 à 8 kHz, très courant), un avertissement
  explique que les sons aigus (claquement de doigts, sifflement) sont mal
  reconnus et propose de choisir AAC 16 kHz ou plus dans la caméra ; le
  journal le signale aussi (première connexion, puis à chaque changement).
- Dépannage « sons aigus mal reconnus » ajouté à la documentation.

## 6.68.0

Simplification du code, sans changement de comportement visible.

### Réglages et API

- **Import en une seule passe** : la fusion et le contrôle des collisions
  d'entités se font une fois, sous le verrou de l'enregistrement (la fusion
  était calculée deux fois, la seconde hors verrou).
- **Bornes définies une fois** : les réglages avancés et l'import partagent
  les mêmes bornes, et la même longueur maximale de nom de groupe ; les
  noms des sources et des groupes passent par un seul validateur dans
  l'API ; types des sections vérifiés une fois.
- **Validation sans effet de bord** : activer le volume automatique vérifie
  le micro sans rien écrire pendant la vérification ; un nom PulseAudio
  périmé est corrigé dans le même enregistrement que l'activation, et
  seulement si elle est acceptée.
- **Filtre ingress** : les adresses refusées gardées en mémoire sont
  plafonnées aux plus récentes ; une adresse est de nouveau signalée au
  journal après une heure.
- Code mort retiré (imports, variables et déclarations inutilisés).

### Interface

- **Thème sombre écrit une fois** : le thème (Home Assistant, sinon système)
  est toujours posé avant l'affichage ; la palette sombre n'est plus
  dupliquée pour la préférence système.
- **Historique chargé une fois** au démarrage (il l'était aussi à la
  première connexion temps réel, qui ne le recharge plus que si ce premier
  chargement a échoué) ; toujours rechargé à chaque reconnexion.
- Factorisés : consigne de fin de l'assistant (3 versions), filtre des
  pastilles de sons, fermeture des menus, comparaison de l'adresse masquée.

## 6.67.0

### Documentation

- **Accès direct au port 16045** : décrit tel qu'il est (connexion refusée
  depuis le réseau local, le serveur n'écoute que sur le réseau interne ;
  « 403 » pour toute adresse interne autre que l'ingress).
- **README** : 1 et 2 claps publiés par défaut (jusqu'à 4), entités event,
  appareil par source, broker externe possible (l'add-on démarre sans
  broker, seules les entités manquent).
- **Caméras** : `rtsps://` accepté, piste audio seule, liste des codecs
  audio pris en charge et dépannage « no decoder found » ; taille de l'image
  à jour ; ligne 6.66 dans « Changements à connaître ».

### Correctif

- **Liste des micros hors Supervisor** (repli `pactl`) : une ligne sans
  index numérique recevait son rang comme index, qui pouvait être celui
  d'un autre micro (même clé de source). Elle est désormais ignorée.

### Tests

- **Plus rapides et plus fiables** : les attentes fixes (`time.sleep`) sont
  remplacées par `wait_until(condition)` : suite en ≈ 30 s au lieu de 45 s
  avec 26 tests de plus, stable sous charge (4 suites en parallèle).
- **Nouveaux tests** : filtre ingress (403, un avertissement par adresse),
  `gunicorn.conf.py` (interface d'écoute et repli, nettoyage au SIGTERM),
  `run.sh` (PulseAudio, lancement de Gunicorn), périphériques audio
  (Supervisor, repli pactl, cache), `vban_manager`, boucle d'auto-volume,
  webhook en arrière-plan. Couverture : audio_utils 45 → 95 %,
  vban_manager 48 → 100 %, auto_volume 68 → 97 %, webhook 80 → 96 %.

## 6.66.0

### Image

- **Image allégée de 42 %** (≈ 208 Mo compressés au lieu de 358, 876 Mo
  installés au lieu de 1,47 Go) : ffmpeg est compilé dans l'image en
  version minimale (lecture audio des flux `rtsp://` et `rtsps://`
  seulement), au lieu du paquet Debian qui tirait Mesa, LLVM, SDL, x265…
  Décodeurs audio des caméras : AAC, G.711 (µ-law, A-law), G.726, G.722,
  Opus, MP3/MP2, AMR, GSM, AC-3, Vorbis, Speex, PCM. Testés sur un vrai
  flux RTSP (UDP et TCP) : AAC, G.711, G.726, G.722, Opus, MP3, PCM.
  Si une caméra utilise un autre codec, le journal affiche « no decoder
  found for: … » : le signaler.
- **Connexion RTSP plus rapide** : seule la piste audio est demandée à la
  caméra (`-allowed_media_types audio`). La vidéo n'est plus envoyée sur le
  réseau, et le premier son arrive en ≈ 0,5 s au lieu d'≈ 5 s.
- **pip retiré** de l'image (inutile à l'exécution).
- Le build vérifie qu'aucune bibliothèque système ne manque et que ffmpeg
  sait lire le RTSP et les codecs principaux ; l'audit hebdomadaire signale
  une nouvelle version corrective de ffmpeg (hors des mises à jour Debian).

### Interface

- **Courbes en direct** : taille des canevas mise en cache (plus de mise en
  page forcée 5 fois par seconde et par ligne) et cartes hors écran non
  dessinées, rattrapées quand elles réapparaissent.
- **Pastilles de sons** : un seul écouteur pour toutes au lieu d'un par
  pastille recréé à chaque coche.
- **Réglages ouverts pendant la découverte** : une rafale de sons nouveaux
  ne recharge plus les exclusions qu'une fois (une requête par son avant).

## 6.65.0

### Interface

- **Adresse du flux sur mobile** : le bouton « Afficher le mot de passe »
  passe sous le champ quand la place manque (le champ était réduit à
  quelques caractères). Même chose pour l'URL du webhook.

### Sécurité

- **Werkzeug 3.1.9** (faille PYSEC-2026-4312 dans 3.1.8).

### Build et publication

- **Audit hebdomadaire des dépendances Python** (`pip-audit` sur
  `requirements.lock`, que Dependabot ne lit pas) ; c'est lui qui a
  signalé la faille de Werkzeug.
- **Autotest du vrai YAMNet dans l'image** : le build classifie quelques
  secondes de signal avec MediaPipe et `yamnet.tflite`, et vérifie le
  nombre de résultats et leur horodatage (la datation des claps en dépend).
  Une mise à jour de mediapipe qui change le découpage échoue au build.
- **Image de base épinglée par digest** pour chaque architecture ;
  labels OCI propres (titre, description, documentation, révision du commit).
- **Actions GitHub épinglées par SHA** (tag en commentaire) et
  **Dependabot** (actions, image de base des deux architectures dans une
  même PR, dépendances du test d'interface).
- **Garde-fous de publication** : `VERSION` doit être au format X.Y.Z et
  supérieure à la version publiée ; une version présente pour une seule
  architecture n'est reprise que depuis le même commit (relance d'un run
  interrompu), sinon refusée au lieu de mélanger deux contenus. Délais maximaux sur chaque job, identifiants git non
  conservés hors du job de publication, Jinja2 des tests épinglé.
- **Tests sans réseau VBAN** : l'import de l'application n'ouvre plus le
  port UDP 6980 pendant les tests.

## 6.64.0

### Interface

- **Groupe vide signalé** : la carte affiche « Aucun son choisi : ce groupe
  ne se déclenchera pas » avec un bouton **Choisir les sons**, et la barre
  d'état compte les groupes sans son. Le bouton ouvre les réglages sur ce
  groupe, avec le focus sur « Ajouter les sons entendus ».
- **Fin de l'assistant** : les réglages de la nouvelle source s'ouvrent sur
  son groupe vide (la consigne renvoyait loin dans la carte). Si la
  détection tourne, le VU-mètre de l'étape 3 suit la détection au lieu
  d'ouvrir une seconde session RTSP ; il ne se fige plus après « Démarrer
  la détection ». Pendant la détection, « Tester le son » est désactivé
  (le niveau de la carte suit déjà le son) et un test en cours s'arrête.
- **Pastilles de sons stables** : triées à l'ouverture des réglages, elles ne
  bougent plus quand on en coche une ou qu'un son arrive (ajouté à la fin).
  Un son entendu pendant une saisie apparaît dès la fin de celle-ci.
- **Ligne en direct** : retour à « En attente de sons… » après 2,5 s sans son
  reconnu (le dernier son restait affiché) ; « Détection arrêtée » quand
  elle ne tourne pas.
- **Accessibilité** : menu fermé qui annonçait encore « développé »,
  coche « ✓ » de l'interrupteur visible, défilement tactile qui bloquait un
  rendu différé, contraste de la piste des interrupteurs, sons regroupés et
  libellés par groupe pour les lecteurs d'écran, titres de cartes de niveau
  2, cibles tactiles de 44 px sur écran tactile.
- **Vocabulaire** : « ignoré » réservé aux sons exclus (historique : « non
  retenu : autre groupe prioritaire »), « Ignorer un autre son entendu »,
  « Démarrage auto » sur mobile ; explication du filtrage par le seuil près
  de « Ajouter les sons entendus ».

## 6.63.0

### API et réglages

- **Plus d'erreur 500 sur une requête forgée** : un JSON très imbriqué
  (import, API, test du son, calibrage) répond 400.
- **Validation complétée** :
  - une caméra par URL, à l'ajout comme à la modification, sans tenir
    compte de la casse du schéma (`RTSP://`) ;
  - noms de source et de périphérique : texte exigé (une liste devenait
    « ['B'] ») ;
  - import : les champs inconnus sont ignorés (et listés dans le journal)
    au lieu d'être enregistrés ;
  - bornes : 32 caméras et 32 flux VBAN, 20 groupes par source, 600 sons
    par liste, noms de son de 200 caractères au plus (un nom de 1,5 Mo était
    accepté puis recopié à chaque lecture) ;
  - micro : changer de périphérique et activer le volume automatique dans
    la même requête est accepté (la validation portait sur l'ancien micro),
    pour un micro réellement présent ;
  - anciennes sauvegardes : sons exclus convertis en texte, entrées vides
    retirées ; un son exclu trop long reste supprimable.
- **Comportements harmonisés** :
  - message de conflit d'entités neutre (il accusait parfois la mauvaise
    source) ;
  - les webhooks des détections ne suivent plus les redirections, comme le
    test (un POST devenait GET) : une redirection est un échec signalé dans
    le journal, avec l'adresse finale à utiliser ;
  - le volume du micro importé est appliqué tout de suite, au micro présent ;
  - le test du micro n'accepte que les micros connus, et `pactl` reçoit
    `--` avant le nom du périphérique.
- **32 fils** pour le serveur web (au lieu de 16) : plusieurs onglets et
  l'application mobile en arrière-plan ne saturent plus l'API.

## 6.62.0

### Meilleure intégration avec Home Assistant

- **Un appareil par source** (« ClapTrap Salon », « ClapTrap Micro »),
  rattaché à l'appareil « ClapTrap » : chaque source peut recevoir sa propre
  pièce. Entity_id, historique et personnalisations inchangés ; les entités
  globales restent sur « ClapTrap ». Noms affichés : « ClapTrap Salon Clap ·
  2 claps », « ClapTrap Salon Source active » (HA les préfixe du nom de
  l'appareil).
- **Entité event par groupe** (`event.claptrap_<source>_<groupe>_evenement`)
  : un événement par détection, de type `1clap`, `2claps`… avec score et
  sons en attributs ; historique et journal lisibles, automatisations
  simples. Les capteurs à impulsion restent publiés.
- **Lien vers l'interface** : bouton « Visiter » sur la fiche des appareils
  ClapTrap.
- **Mises à jour sûres** : `breaking_versions` arrête la mise à jour
  automatique avant les versions qui changent des entity_id, des
  identifiants d'événement ou des routes d'API (6.25, 6.28, 6.32, 6.33,
  6.45).

## 6.61.0

### Home Assistant / MQTT

- **Broker MQTT externe** : l'add-on démarre aussi sans Mosquitto
  (`mqtt:want`). Un broker externe (NAS, Docker…) se renseigne dans l'onglet
  **Configuration** : adresse, port, utilisateur, mot de passe, TLS et
  vérification du certificat (libellés en français et en anglais).
- **TLS** : connexion chiffrée vers un broker externe, et vers Mosquitto
  quand son service l'annonce (la connexion partait en clair et échouait) ;
  certificat auto-signé accepté si « Vérifier le certificat » est décoché.
  Sans broker, l'add-on fonctionne sans entités et le rappelle toutes les
  10 minutes.
- **Disponibilité fiable** : identifiant MQTT fixe propre à l'installation
  (l'« offline » d'une ancienne session arrive avant le nouveau « online ») ;
  un « offline » tardif reçu en direct alors qu'on est connecté est corrigé
  aussitôt.
- **QoS 1 pour les messages retenus** (configurations, suppressions,
  disponibilité, états) : une suppression perdue pendant une micro-coupure
  laissait une entité orpheline. Le nettoyage des orphelines attend la fin
  de l'arrivée des configurations retenues (30 s au plus) au lieu de 3 s
  fixes.
- **Noms d'entités plus courts** : « Salon · Clap · 2 claps » au lieu de
  « VBAN: Salon Clap 2 claps », « Micro » au lieu du nom technique du
  périphérique (qui changeait avec le micro), « Détection en cours ». Les
  entity_id ne changent pas. Deux sources de même nom gardent leur libellé
  complet (« RTSP: Salon », « VBAN: Salon »).
- **AppArmor** : profil complété (certificats pour le TLS) et procédure dans
  la documentation pour relever les accès avant le passage en mode strict ;
  il reste en mode « complain ».

## 6.60.0

### Détection

- **Arbitrage entre groupes** : quand plusieurs groupes reconnaissent le même
  son, un groupe dont le nombre de claps a une entité passe en premier, puis
  un groupe sans entité sur un vrai pic, puis les autres (score à égalité de
  rang). Avant, le meilleur score gagnait même s'il ne pouvait rien
  déclencher (3 claps pour des entités 1 et 2, applaudissements de la télé
  sans pic) et éteignait le groupe qui, lui, aurait déclenché.
- **Plus d'événement à 0 clap** : un son reconnu sans pic par un groupe qui a
  des entités n'envoie plus d'événement `claptrap_clap` ni de webhook
  (`clap_count: 0`). Un groupe sans entité les envoie toujours.
- **Son trop fort réduit au lieu d'être écrêté** : avec un gain élevé
  (caméra ×10), un pic brut au-delà de 0,1 saturait avant YAMNet ; le bloc
  trop fort est désormais ramené sous la saturation, sans atténuer les sons
  qui suivent.
- **Transport TCP par caméra** (assistant d'ajout, réglages de la caméra,
  commandes `add_rtsp` et `update_source` avec `rtsp_tcp`) : plus fiable en
  Wi-Fi ; la caméra est relancée au changement, et le test du son utilise
  le même transport.
- Corrections : un ancien lanceur du micro ne laisse plus l'auto-volume
  actif sur l'ancien périphérique ; la découverte des sons ne lève plus
  d'erreur quand l'option « Ajouter les sons entendus » est désactivée
  pendant la détection ; les routes de test du son répondent 400 (et non
  500) à un corps JSON qui n'est pas un objet.
- Ménage : heuristique de mesure de cadence retirée (MediaPipe 1.0 rend
  toujours un résultat par fenêtre de 975 ms), ancienne branche des sources
  sans groupes retirée.

## Versions précédentes

Les versions 6.59.0 et antérieures sont dans
[CHANGELOG-archive.md](https://github.com/powange/ClapTrap-HA-Addon/blob/main/CHANGELOG-archive.md) (archivées pour que cet onglet reste léger).
