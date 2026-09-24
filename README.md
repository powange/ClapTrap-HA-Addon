# ClapTrap, add-on Home Assistant

ClapTrap reconnaît les applaudissements, et plus largement les sons, en temps
réel avec le modèle YAMNet. Il écoute un micro branché sur Home Assistant, le
son d'une caméra RTSP ou un flux VBAN (Voicemeeter), et crée des entités qui
s'allument à chaque clap : tapez deux fois dans vos mains pour allumer le
salon.

## Fonctionnalités

- **Sources** : micro USB, caméras RTSP, flux VBAN, plusieurs en même temps.
- **Entités Home Assistant** (MQTT) : `binary_sensor.claptrap_<source>_<groupe>_2claps`,
  une par nombre de claps (1 à 4), plus l'événement `claptrap_clap`.
- **Groupes de sons** : un groupe par type de son (claps, claquements de
  doigts, sonnette…), chacun avec son seuil et ses entités.
- **Webhook** facultatif par source (Node-RED, webhook Home Assistant…).
- **Interface** dans Home Assistant : niveaux et scores en direct, historique
  des détections, sons en français.

## Prérequis

- Home Assistant OS ou Supervised 2025.10 ou plus récent, sur amd64 ou
  aarch64 (Raspberry Pi 4 et 5).
- L'add-on **Mosquitto broker** et l'intégration **MQTT** : ClapTrap ne
  démarre pas sans broker, et sans l'intégration aucune entité n'apparaît.

## Installation

1. **Paramètres > Modules complémentaires > Boutique des modules
   complémentaires**, menu **⋮ > Dépôts**, ajoutez
   `https://github.com/powange/ClapTrap-HA-Addon`.
2. Installez **ClapTrap** (image précompilée), démarrez-le et ouvrez son
   interface.
3. **Ajouter une source**, tapez dans vos mains pour vérifier le niveau, puis
   **Démarrer la détection**.

La documentation complète (entités, événement, webhook, réglages,
changements entre versions, dépannage) est dans [DOCS.md](DOCS.md), aussi
affichée dans l'onglet **Documentation** de l'add-on.

## Contribuer

Issues et pull requests sur
[GitHub](https://github.com/powange/ClapTrap-HA-Addon) ; l'historique est dans
[CHANGELOG.md](CHANGELOG.md).

Publication : la version à publier se met dans `VERSION` (et le
`CHANGELOG.md`). À chaque push sur `main`, GitHub Actions lance les tests,
construit et publie les images, puis seulement passe `config.yaml` à cette
version : Home Assistant ne propose jamais une version dont l'image manque.

Tests (lancés aussi par GitHub Actions à chaque push et pull request) :

```sh
pip install -r data/requirements-dev.txt
python -m pytest data/tests          # comptage, détecteur, VBAN, sources, session, HA, réglages, routes
cd data/tests/ui && npm ci && python render_page.py && node ui_test.js page.html   # interface
```

Merci à @korben, qui a développé le système de reconnaissance en Python.
