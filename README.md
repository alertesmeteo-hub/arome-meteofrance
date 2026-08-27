# AROME Météo-France France — cartes et prévisions WordPress

Ce dépôt construit une chaîne directe **Météo-France AROME 0,01° → GitHub → WordPress/Avada**. Il publie les cartes interactives et les prévisions horaires de 34 746 communes françaises sur une branche `data`, sans Open-Meteo ni autre intermédiaire météorologique.

## Ce que produit le workflow

- AROME France 0,01° (environ 1,3 km), échéances horaires jusqu'à +48 h ;
- température, point de rosée, refroidissement éolien et humidex ;
- pluie horaire/cumulée, neige, graupel et cumul de neige fraîche estimé ;
- vent moyen, rafales, pression au sol et pression mer estimée ;
- nébulosité totale, basse, moyenne et élevée, humidité ;
- MUCAPE et réflectivité maximale directes AROME ;
- altitude du point de grille pour chaque commune ;
- 22 couches cartographiques à plages nettes, frontières vectorielles, noms de communes, zoom jusqu'à 6 400 % et valeur sous la souris.

Les champs directs et les diagnostics dérivés sont distingués dans `index.json` et dans l'interface. Aucun champ vertical ou indice non présent dans les paquets ouverts SP1/SP2/SP3 n'est inventé.

## Installation du dépôt GitHub

1. Créez un dépôt GitHub, puis copiez tout le contenu de ce dossier à sa racine.
2. Dans **Settings → Actions → General → Workflow permissions**, choisissez **Read and write permissions**.
3. Lancez **Actions → Mise à jour AROME France → Run workflow**.
4. À la fin du premier lancement, vérifiez la présence de la branche `data` et de son fichier `index.json`.

Le workflow est aussi lancé toutes les heures. Le script compare le run publié et ne reconstruit rien lorsqu'il n'existe pas de nouveau run complet.

Les paquets AROME 0,01° utilisés ici sont publics : **aucune clé API
Météo-France n'est nécessaire**. Pendant la publication d'un nouveau run,
data.gouv.fr peut momentanément présenter SP1, SP2 et SP3 avec des horaires
différents. Le pipeline v1.0.1 vérifie chaque horodatage, patiente jusqu'à trois
minutes et, si la synchronisation n'est pas terminée, clôt proprement le passage
sans toucher à la branche `data` ; le lancement horaire suivant reprend tout seul.

Commande équivalente en local :

```bash
python -m pip install -r requirements.txt
python scripts/update_arome_france.py \
  --catalog config/communes-france.json \
  --output-dir build/national \
  --forecast-hours 48
```

Le traitement télécharge successivement les paquets SP1 et SP2, ainsi que SP3 à +0 h pour l'altitude. Il ne conserve pas tous les GRIB simultanément afin de limiter l'espace disque du runner.

## Installation du module WordPress/Avada

Le ZIP du module se trouve dans la livraison séparée. Dans WordPress :

1. ouvrez **Extensions → Ajouter une extension → Téléverser une extension** ;
2. installez et activez le ZIP `arome-meteofrance-france-v1.0.0.zip` ;
3. ouvrez **Réglages → AROME Météo-France** et adaptez l'URL de la branche `data` si votre dépôt n'est pas `alertesmeteo-hub/arome-meteofrance` ;
4. dans Avada Builder, ajoutez un élément **Code Block** ou **Text Block** contenant :

```text
[arome_meteo]
```

Tout est intégré dans ce shortcode unique : recherche par commune ou code postal, géolocalisation, altitude, cartes, prévisions générales, tableaux orages/neige et quatre graphiques.

Exemple avec une ville initiale différente :

```text
[arome_meteo ville="Paris" code="75056" departement="75" heures="48"]
```

## Structure publiée

```text
data/
├── index.json
├── departements/
│   ├── 01.json
│   ├── …
│   └── 95.json
└── maps/
    ├── index.json
    ├── communes.json
    ├── frontieres.svg
    ├── temperature/
    ├── pluie_1h/
    ├── reflectivite/
    └── values/
```

## Source et licence des données

Les données proviennent directement des [paquets AROME 0,01° de Météo-France](https://www.data.gouv.fr/datasets/paquets-arome-resolution-0-01deg), publiés sous Licence Ouverte 2.0. Les communes proviennent de l'API officielle de découpage administratif française.

Site : [www.alertes-meteo.com](https://www.alertes-meteo.com/) — module v1.2.0.

## Cartes disponibles

26 cartes réparties par catégorie (Températures, Précipitations, Vent, Nuages et humidité, Pression, Instabilité, Relief). Le menu de sélection distingue les paramètres essentiels (toujours visibles) des paramètres secondaires, repliés derrière un bouton « Voir plus de paramètres ».

Cartes en attente du paquet AROME **HP1** (niveaux de pression), non téléchargé aujourd'hui — seuls les paquets de surface SP1/SP2/SP3 sont utilisés : températures et vent à 850/500/300 hPa, géopotentiel, humidité en altitude, ainsi que les indices d'instabilité composites (SBCAPE, MLCAPE, CIN, K-Index, Total Totals, Theta-E Lapse Rate, Bulk Shear, hélicité SRH, paramètres supercellule/tornade/grêle significative), vitesse verticale, vorticité et divergence. Leur configuration d'affichage (libellés, couleurs, groupes) existe déjà dans `scripts/arome_maps.py` — il ne manque que les données sources.

## Outils de la carte

La barre d'outils de la carte propose deux modes, à côté du sélecteur de paramètre et de la navigation dans les échéances :

- **Zoom interactif** : affiche des outils supplémentaires — capture PNG de la vue affichée et épinglage de la valeur au clic (en plus du survol).
- **Diagramme** : un clic sur la carte affiche un mini-diagramme (température et précipitations horaires) pour la commune la plus proche du point cliqué, à partir des mêmes données que l'onglet « Prévisions générales ».

Radiosondage et coupes verticales ne sont pas encore disponibles : ces vues nécessitent les niveaux de pression AROME (paquet **HP1**), que le pipeline ne télécharge pas encore — seuls SP1/SP2/SP3 (champs de surface) sont utilisés aujourd'hui.
