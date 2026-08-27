=== AROME Météo-France France ===
Contributors: alertesmeteo
Tags: meteo, arome, meteofrance, carte, previsions, avada
Requires at least: 5.8
Requires PHP: 7.4
Stable tag: 1.2.0
License: GPLv2 or later
License URI: https://www.gnu.org/licenses/gpl-2.0.html

Module unique de cartes interactives et prévisions AROME pour 34 746 communes françaises.

== Description ==

Le shortcode [arome_meteo] affiche dans un seul module :

* une carte AROME interactive avec zoom jusqu'à 6 400 %, noms de communes et valeur au survol ;
* une recherche par ville ou code postal et la géolocalisation ;
* l'altitude du point de grille AROME ;
* les prévisions générales et quatre graphiques ;
* les diagnostics orages et neige.

Les données sont lues depuis la branche data du dépôt GitHub configuré dans Réglages > AROME Météo-France.

== Installation ==

1. Téléversez le ZIP dans Extensions > Ajouter une extension.
2. Activez AROME Météo-France France.
3. Vérifiez l'URL des données dans Réglages > AROME Météo-France.
4. Insérez [arome_meteo] dans un bloc Avada.

== Changelog ==

= 1.2.0 =
* 4 nouvelles cartes : Température du thermomètre mouillé (Températures), Accumulation de neige et graupel, Risque de grêle, Type de précipitation sévère (Précipitations). Ces deux derniers réutilisent les diagnostics déjà affichés dans le tableau orages (aucune donnée inventée).
* Le menu de sélection de carte distingue maintenant les paramètres essentiels (toujours visibles) des paramètres secondaires, repliés derrière un bouton « Voir plus de paramètres ».
* Cartes toujours en attente du paquet AROME HP1 (niveaux de pression) : températures/vent à 850-500-300 hPa, géopotentiel, indices d'instabilité composites (K-Index, SRH, paramètres supercellule/tornade), vitesse verticale, vorticité, divergence.

= 1.1.0 =
* Ajout de la barre d'outils de la carte : « Zoom interactif » (capture PNG de la vue affichée, épinglage de la valeur au clic) et « Diagramme » (clic sur la carte pour afficher un mini-diagramme température/pluie de la commune la plus proche).
* `maps/communes.json` inclut désormais le code INSEE et le département de chaque commune (colonnes supplémentaires, schéma v2) pour permettre ce lien clic → prévisions.
* Radiosondage et Coupes verticales laissés de côté pour l'instant : nécessitent le paquet AROME de niveaux de pression (HP1), non encore branché dans le pipeline.

= 1.0.1 =
* Correctif critique : les handles WordPress `hkw-table`/`hkw-map` et les classes CSS/attributs `hkw-`/`hkm-` copiés du module Harmonie-KNMI entraient en collision avec ce module lorsque les deux plugins étaient actifs simultanément (mêmes noms d'enregistrement `wp_register_style`/`wp_register_script`). Renommés en `amf-`/`amfm-` (namespace propre à AROME).
* Ajout du lien d'action « Shortcodes / Aide » et « Réglages » sur la page Extensions.
* Ajout de la date de version dans le footer du module (front-end).

= 1.0.0 =
* Première version AROME 0,01° entièrement intégrée.
* Cartes, recherche, altitude, tableaux et graphiques dans un shortcode unique.
