<p align="center">
  <img src="icon.png" width="120" alt="NAKA Presence">
</p>

<h1 align="center">NAKA Presence</h1>

<p align="center">
  Affiche la présence du serveur Discord <b>NAKA</b> sur ton profil, pilotée depuis la barre des tâches Windows.
</p>

<p align="center">
  <a href="https://github.com/Wakizashi2313/naka-presence/releases/latest/download/NakaPresence-Setup.exe"><b>⬇ Télécharger l'installateur</b></a>
  &nbsp;·&nbsp;
  <a href="https://github.com/Wakizashi2313/naka-presence/releases/latest">Notes de version</a>
</p>

<p align="center">
  <img src="docs/menu.png" width="340" alt="Menu de NAKA Presence">
</p>

## Fonctionnalités

- **Présence Discord** avec la description du serveur, un minuteur et deux boutons : « Visiter le site » et « Rejoindre le serveur ».
- **Icône dans la zone de notification** : double-clic pour activer ou désactiver, clic droit pour le menu.
- **Type d'affichage au choix** : Joue, Écoute, Regarde ou Participe.
- **Démarrage avec Windows** en option.
- **Accès rapide** aux pages du serveur : site, musique et statistiques.
- **Mises à jour intégrées** : recherche au lancement ou sur demande, pastille quand une version est disponible, installation après confirmation.
- **Légère** : quelques Mo de mémoire au repos.
- **Désinstallation propre** : rien ne reste sur le système.

## Installation

1. Télécharge [`NakaPresence-Setup.exe`](https://github.com/Wakizashi2313/naka-presence/releases/latest/download/NakaPresence-Setup.exe).
2. Lance-le. Aucun droit administrateur n'est nécessaire.
3. Garde **Discord pour ordinateur** ouvert (la version navigateur ne fonctionne pas).

> **Windows affiche « Windows a protégé votre ordinateur » ?**
> L'installateur n'est pas signé numériquement, car un certificat est payant. Clique sur
> **Informations complémentaires**, puis sur **Exécuter quand même**. Le code source est
> entièrement disponible ici.

### Prérequis

- Windows 10 ou 11 (64 bits). Les coins arrondis et la transparence du menu sont plus beaux sous Windows 11.
- Discord pour ordinateur, avec **Paramètres → Confidentialité des activités → Partager mon activité détectée** activé.

## Utilisation

| Action | Effet |
|---|---|
| Double-clic sur l'icône | Active ou désactive la présence |
| Clic droit sur l'icône | Ouvre le menu |
| Menu → Affichage sur le profil | Choisit « Joue », « Écoute », « Regarde » ou « Participe » |
| Menu → Mise à jour | Cherche une nouvelle version et propose de l'installer |
| Menu → Redémarrer | Relance l'application |
| Menu → Désinstaller | Supprime l'application et tous ses fichiers |

Discord n'affiche qu'une mise à jour de présence toutes les 15 secondes environ. Deux changements rapprochés peuvent donc mettre quelques secondes à apparaître.

### Personnaliser les textes

Les réglages sont dans `%APPDATA%\NakaPresence\config.json` et sont pris en compte automatiquement en quelques secondes :

| Clé | Rôle |
|---|---|
| `client_id` | Application ID du portail développeur Discord (nom affiché : celui de l'application) |
| `details`, `state` | Les deux lignes de texte (128 caractères maximum chacune) |
| `buttons` | Jusqu'à 2 boutons `{"label": …, "url": …}` (visibles par les autres, pas par toi) |
| `large_image`, `small_image` | Noms des images envoyées dans « Rich Presence → Art Assets » |
| `activity_type` | `0` Joue, `2` Écoute, `3` Regarde, `5` Participe |

## Désinstallation

Depuis le menu (**Désinstaller**) ou depuis **Paramètres Windows → Applications installées**. Sont supprimés :

- le dossier de l'application (`%LOCALAPPDATA%\Programs\NAKA Presence`) ;
- la configuration (`%APPDATA%\NakaPresence`) ;
- le raccourci du menu Démarrer ;
- l'entrée « Démarrer avec Windows » du registre ;
- l'entrée de désinstallation du registre.

## Compiler soi-même

Il faut Python 3.12 ou plus récent et [Inno Setup](https://jrsoftware.org/isinfo.php) 6.7 ou plus récent (`winget install JRSoftware.InnoSetup`).

```powershell
.\build.ps1
```

Le résultat est `dist\NakaPresence-Setup.exe`. Pour lancer sans compiler : `python naka_presence.py`.

## Licence

[MIT](LICENSE)
