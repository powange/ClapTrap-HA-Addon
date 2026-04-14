# ClapTrap - Documentation

## A propos

ClapTrap est un add-on Home Assistant pour la detection d'applaudissements et de sons en temps reel. Il utilise le modele YAMNet pour classifier les sons captes par differentes sources audio.

## Sources audio supportees

### Microphone USB

Branchez un microphone USB sur votre machine HAOS. Il apparaitra automatiquement dans la liste des sources audio de l'interface ClapTrap.

### Flux RTSP

Ajoutez l'URL d'un flux RTSP (camera IP, etc.) dans la section "Flux RTSP" de l'interface. Format : `rtsp://ip:port/chemin`.

### Sources VBAN

ClapTrap detecte automatiquement les sources VBAN sur le reseau local. Cliquez sur "Rafraichir" pour scanner les sources disponibles, puis ajoutez celles souhaitees.

### Wyoming STT (ESPHome voice_assistant)

ClapTrap peut se placer en intercepteur Wyoming entre vos appareils ESPHome equipes de `voice_assistant` (Atom Echo, M5 Atom S3R, etc.) et votre serveur Whisper Wyoming. Chaque flux audio :

1. est analyse par YAMNet pour detecter les claps (par appareil) ;
2. est transmis tel quel au serveur Whisper reel ;
3. la reponse STT + TTS (Piper) est renvoyee vers l'Atom d'origine, sans broadcast.

Le serveur Wyoming est **multi-client asynchrone** : plusieurs Atoms peuvent parler en meme temps sans se genera.

#### Activation

Dans l'interface ClapTrap, ouvrir la section **&#x1F399;&#xFE0F; Wyoming STT (ESPHome voice_assistant)** :

- **Activer** : demarre le serveur (redemarrer l'addon apres modification).
- **Port d'ecoute** : port expose aux Atoms (defaut `10700`).
- **Whisper - host / port** : adresse et port du serveur Whisper Wyoming reel
  (ex : `core-whisper` + `10300` pour l'addon officiel Home Assistant).
- **Seuil de detection** : seuil YAMNet applique aux claps sur ces flux.
- **Webhook** : URL appelee lors d'une detection. Payload :

  ```json
  { "event": "clap_detected", "source": "atom-chambre", "count": 2 }
  ```

  Le champ `source` est le hostname resolu par DNS inverse de l'Atom
  (fallback sur l'IP si non resolu).

#### Configuration ESPHome (Atom Echo S3R)

```yaml
# atom-chambre.yaml
esphome:
  name: atom-chambre

micro_wake_word:
  models:
    - okay_nabu

voice_assistant:
  microphone: atom_mic
  speaker: atom_speaker
  use_wake_word: true
  noise_suppression_level: 2
  auto_gain: 31dBFS

# microphone / speaker : configuration standard I2S de l'Atom Echo
# (omise ici pour la brievete - cf. doc ESPHome)
```

Le hostname ESPHome (ici `atom-chambre`) est celui qui apparaitra dans le champ
`source` du webhook et dans les logs ClapTrap. Assurez-vous qu'il est resolvable
par DNS inverse depuis l'addon (sinon l'IP sera utilisee).

#### Configuration Home Assistant (pipeline vocal)

1. **Parametres > Modules complementaires** : installer et demarrer l'addon
   Whisper (ou un autre serveur Wyoming STT) ; noter son host + port (ex :
   `core-whisper:10300`).
2. Dans l'interface ClapTrap, renseigner ces valeurs dans la section Wyoming,
   activer, et redemarrer l'addon.
3. **Parametres > Integrations > Wyoming Protocol > Ajouter** : pointer vers
   `IP-de-ClapTrap:10700` (et non vers Whisper directement). L'integration
   Wyoming verra alors une entree ASR "ClapTrap".
4. **Parametres > Assistants & pipelines vocaux** : creer / modifier votre
   pipeline et selectionner cet ASR ClapTrap. Garder Piper (ou autre) comme TTS.
5. Dans chaque Atom, pointer le pipeline voice_assistant vers cet assistant.

Lors d'un echange vocal, l'audio passe par ClapTrap (YAMNet ecoute les claps
en parallele) puis transparent vers Whisper. La reponse TTS est renvoyee
automatiquement au bon Atom.

#### Ce qui arrive si Whisper est injoignable

Le serveur Wyoming continue d'accepter les connexions et la detection YAMNet
reste active (webhook + entites HA mis a jour). Une erreur est loggee, mais
l'addon ne crash pas.

## Configuration

### Parametres de detection

- **Precision (seuil)** : Valeur entre 0 et 1. Plus la valeur est elevee, plus la detection est stricte (defaut : 0.5).
- **Delai entre detections** : Temps minimum en secondes entre deux detections (defaut : 1.0).

### Webhooks

Chaque source audio peut etre associee a une URL webhook. Lorsqu'un applaudissement est detecte, une requete POST est envoyee a l'URL configuree avec les informations de l'evenement.

Format du webhook :

```json
{
  "event": "clap",
  "source": "nom_de_la_source",
  "timestamp": "2024-01-01T12:00:00"
}
```

Pour tester un webhook, cliquez sur le bouton "Tester" a cote de l'URL.

## Acces a l'interface

L'interface est accessible via le panneau Home Assistant (ingress). Cliquez sur "Ouvrir l'interface utilisateur Web" dans la page de l'add-on.

## Depannage

### Le microphone USB n'apparait pas

1. Verifiez que le micro est detecte dans **Parametres > Systeme > Materiel**
2. Redemarrez l'add-on apres avoir branche le micro
3. Verifiez que l'option audio est bien activee dans la configuration de l'add-on

### La detection ne demarre pas

1. Verifiez qu'au moins une source audio est configuree et activee
2. Verifiez que les webhooks sont valides (commencent par `http://` ou `https://`)
3. Consultez les logs de l'add-on pour plus de details
