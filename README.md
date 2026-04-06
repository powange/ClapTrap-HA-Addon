# 🎉 ClapTrap Add-on pour Home Assistant 🎉

**ClapTrap** est un add-on puissant pour Home Assistant qui permet la détection d'applaudissements en temps réel 👏 à partir de diverses sources audio 🎤. Il s'appuie sur l'IA 🤖 et le modèle YAMNet pour offrir une reconnaissance audio précise et rapide, tout en prenant en charge des intégrations personnalisées via des webhooks 🌐.

## ✨ Fonctionnalités principales

- 🔊 **Détection des sons** : Reconnaît les applaudissements à partir de microphones locaux, flux RTSP 📹 ou sources VBAN 🌐.
- 🔗 **Webhook configurable** : Envoie une notification aux URL définies lorsqu'un événement est détecté.
- 🖥️ **Interface intuitive** : Configuration facile des paramètres audio et des webhooks.
- ⚡ **Support multi-sources** : Gère plusieurs flux simultanément avec des réglages indépendants.

## 📋 Prérequis

- 🏠 **Home Assistant x86 installé**

## 🚀 Installation

### Étape 1 : Ajout du dépôt
1. Ouvrez Home Assistant et allez dans **Paramètres** > **Add-ons, Backups & Supervisor** > **Add-on Store**.
2. Cliquez sur **Menu (⋮)** > **Dépôt** et ajoutez l'URL de votre dépôt GitHub contenant cet add-on.

### Étape 2 : Installation de l'add-on
1. Recherchez **ClapTrap** dans l'Add-on Store.
2. Cliquez sur **Installer** 🛠️, (ATTENTION la compilation peut prendre plusieurs minutes), puis sur **Démarrer** ▶️.

### Étape 3 : Configuration
1. Configurez vos sources audio 🎙️ et les webhooks associés selon vos besoins directement dans l'interface web de l'add-on 🌐.

## 🛠️ Utilisation

1. Accédez à l'interface de gestion via l'interface web dans l'add-on Home Assistant 🏠.
2. Configurez les paramètres audio :
   - **Sources** : Sélectionnez vos microphones 🎤, flux RTSP 📹 ou sources VBAN 🌐.
   - **Paramètres de détection** : Ajustez le seuil de sensibilité 📈 et les délais entre détections ⏱️.
   - **Webhooks** : Définissez les URL 🌍 qui recevront les notifications.
3. Cliquez sur **Démarrer la détection** ▶️ pour lancer le service.
4. Visualisez les détections en temps réel 👀 et recevez les événements sur vos webhooks configurés 🔔.

## ⚙️ Paramètres

- 🎙️ **Sources audio** :  
  - Microphone local 🎤  
  - Flux RTSP 📹  
  - Sources VBAN 🌐  
- 🔗 **Webhook URL** : Obligatoire, commence par `http://` ou `https://`.
- 📈 **Seuil de détection** : Valeur entre 0 et 1 (par défaut : 0.5).
- ⏱️ **Délai entre détections** : Temps minimum en secondes (par défaut : 2).

## 🤝 Contribution

Vous souhaitez contribuer ? 🛠️ Consultez le fichier `DEV_BOOK.md` 📘 pour en savoir plus sur la structure du projet et les étapes de développement.
Big thanks to @korben qui a entierement developpé le systeme de reconnaisance en Python.

## 🆘 Support

Si vous rencontrez des problèmes, consultez la documentation complète dans `DOCUMENTATION.md` 📖 ou ouvrez une issue sur le dépôt GitHub 🐙.
