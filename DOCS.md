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
