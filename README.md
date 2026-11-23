# ecoledirecte-dav

Passerelle **WebDAV locale, en lecture seule**, qui affiche dans Finder les clouds de tes
espaces de travail EcoleDirecte.

```
EcoleDirecte (localhost)/
├── Espaces de travail/      ← un dossier par espace qui a un cloud
│   ├── Maths 2nde B/
│   └── Club Théâtre/
└── Maths 2nde B/            ← favori : raccourci direct vers un espace
```

> EcoleDirecte n'a pas d'API publique. Cet outil utilise l'API interne du site, documentée
> par la communauté ([EduWireApps](https://github.com/EduWireApps/ecoledirecte-api-docs),
> [Scolup](https://github.com/Scolup/docsdirecte)). Elle peut changer sans prévenir.
> L'outil ne fait que lire : aucun fichier n'est modifié côté EcoleDirecte.

## Installation

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

Toutes les commandes suivantes s'écrivent `.venv/bin/ecoledirecte-dav …`. Tu peux aussi
activer le venv avec `source .venv/bin/activate` pour taper seulement `ecoledirecte-dav …`.

## 1. Se connecter (une fois)

```bash
ecoledirecte-dav login
```

Tu saisis ton identifiant et ton mot de passe, puis tu réponds à la question de sécurité
d'EcoleDirecte si elle apparaît. Le mot de passe et le jeton de vérification sont rangés
dans le **Trousseau macOS**, jamais dans un fichier. Pour les effacer :
`ecoledirecte-dav logout`.

## 2. Voir ses espaces

```bash
ecoledirecte-dav espaces          # espaces avec un cloud (★ = favori)
ecoledirecte-dav espaces --all    # tous les espaces
```

## 3. Favoris (raccourcis à la racine)

```bash
ecoledirecte-dav fav add "maths"                 # id, titre ou partie du titre
ecoledirecte-dav fav add 1234 --nom "Maths"      # avec un nom court
ecoledirecte-dav fav list
ecoledirecte-dav fav remove "Maths"
```

Le serveur relit la configuration tout seul. Dans Finder, ⌘R ou rouvrir le dossier suffit.

## 4. Lancer la passerelle et l'ouvrir dans Finder

```bash
ecoledirecte-dav serve
```

Dans Finder : **Aller → Se connecter au serveur…** (⌘K), saisir `http://localhost:8080`,
puis choisir **Invité** si une fenêtre d'identification s'affiche. Le volume est monté
en lecture seule. Tu peux le glisser dans la barre latérale.

### Démarrage automatique

```bash
ecoledirecte-dav install-agent      # lance le serveur à chaque ouverture de session
ecoledirecte-dav uninstall-agent
```

Le journal est dans `~/Library/Logs/ecoledirecte-dav.log`. La première fois, macOS peut
demander l'autorisation d'accès au Trousseau : choisis « Toujours autoriser ».

Pour que Finder remonte le volume tout seul : **Réglages Système → Général → Ouverture**,
puis ajouter le volume `localhost` aux éléments ouverts à la connexion.

## Configuration

Fichier : `~/.config/ecoledirecte-dav/config.toml`

| clé           | défaut      | rôle                                                 |
|---------------|-------------|------------------------------------------------------|
| `port`        | `8080`      | port du serveur                                      |
| `host`        | `127.0.0.1` | à ne pas changer : le serveur n'a pas de mot de passe |
| `api_version` | `4.91.0`    | paramètre `v=` envoyé à l'API                        |
| `cache_ttl`   | `60`        | durée de vie (s) des listings en mémoire             |
| `cache_max_mo`| `1024`      | taille max du cache de fichiers, en Mo (0 = sans limite) |

## Cache des fichiers

Un fichier n'est téléchargé que lorsqu'on l'ouvre (ou que Finder en affiche l'aperçu),
puis il est gardé dans `~/Library/Caches/ecoledirecte-dav/files/`. Au-delà de la limite,
les fichiers ouverts le moins récemment sont supprimés ; ils seront retéléchargés au besoin.

```bash
ecoledirecte-dav cache            # taille actuelle et limite
ecoledirecte-dav cache limit 500  # limite à 500 Mo (0 = sans limite)
ecoledirecte-dav cache clear      # tout vider
```

Le serveur prend en compte une nouvelle limite sans redémarrage.

## Dépannage

- **« Version d'API refusée »** : mets une version plus récente dans `api_version`. Le
  numéro se trouve dans l'onglet Réseau des outils de développement de ecoledirecte.com
  (paramètre `v=` des requêtes).
- **« question de sécurité » / erreur 503 dans Finder** : relance `ecoledirecte-dav login`.
- **Format inattendu** : `ecoledirecte-dav espaces --debug` affiche les réponses brutes de
  l'API (espaces et racine de chaque cloud).
- **Tester sans compte** : `ecoledirecte-dav serve --demo` sert des données factices.

## Tests

```bash
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```
