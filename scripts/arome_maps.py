#!/usr/bin/env python3
"""Produit des cartes WebP depuis la grille native AROME Europe.

Les champs ne sont jamais reconstruits depuis les communes. Les points natifs
du GRIB sont reprojetés sur une image Web Mercator couvrant l'Europe de l'Ouest,
puis les côtes, frontières nationales et limites départementales françaises
sont ajoutées dans une surcouche indépendante.
"""

from __future__ import annotations

import gzip
import json
import math
import struct
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.spatial import cKDTree


MAP_SCHEMA_VERSION = 6
MODULE_VERSION = "1.2.7"
# Une valeur numérique tous les deux pixels cartographiques : le survol reste
# précis à l'échelle d'une commune sans multiplier déraisonnablement le poids
# de la branche de données.
PROBE_DOWNSAMPLE = 2
PROBE_MAGIC = b"HKV1"
STATIC_FIELDS = {"altitude_m"}
CONTOUR_STEPS = {
    "temperature_c": 1.0,
    "surface_temperature_c": 1.0,
    "wind_chill_c": 1.0,
    "wet_bulb_c": 1.0,
    "dewpoint_c": 1.0,
    "humidex": 1.0,
    "wind_speed_kmh": 5.0,
    "wind_gust_kmh": 5.0,
    "pressure_hpa": 2.0,
    "surface_pressure_hpa": 2.0,
    "cloud_cover_pct": 5.0,
    "cloud_low_pct": 5.0,
    "cloud_mid_pct": 5.0,
    "cloud_high_pct": 5.0,
    "humidity_pct": 5.0,
    "cape_jkg": 100.0,
    "reflectivity_dbz": 2.0,
    "altitude_m": 50.0,
}
DEFAULT_BOUNDS = {
    "south": 38.0,
    "west": -12.0,
    "north": 57.0,
    "east": 18.0,
}
FIXED_MAP_BOUNDS = {
    "south": 40.0,
    "west": -6.5,
    "north": 52.5,
    "east": 11.0,
}
FIXED_MAP_WIDTH = 1280
FIXED_MAP_INTERVAL_HOURS = 3
FIXED_TEMPERATURE_VALUES_KEY = "temperature_valeurs"
FIXED_MAP_KEYS = {
    "temperature",
    "pluie_1h",
    "pluie_cumul",
    "neige",
    "rafales",
    "pression",
    "nebulosite",
    "mucape",
    "reflectivite",
}


def _iter_shapefile_parts(path: Path):
    """Lit les lignes/polygones ESRI Shapefile sans dépendance externe.

    Les couches Natural Earth embarquées n'ont besoin que des coordonnées X/Y
    et des indices de parties. Les éventuelles valeurs Z/M peuvent donc être
    ignorées en toute sécurité.
    """

    with path.open("rb") as handle:
        header = handle.read(100)
        if len(header) != 100 or struct.unpack_from(">i", header, 0)[0] != 9994:
            raise ValueError(f"En-tête Shapefile invalide : {path}")

        while True:
            record_header = handle.read(8)
            if not record_header:
                break
            if len(record_header) != 8:
                raise ValueError(f"Enregistrement Shapefile tronqué : {path}")

            _record_number, content_words = struct.unpack(">2i", record_header)
            content_size = content_words * 2
            content = handle.read(content_size)
            if len(content) != content_size:
                raise ValueError(f"Contenu Shapefile tronqué : {path}")
            if len(content) < 4:
                continue

            shape_type = struct.unpack_from("<i", content, 0)[0]
            if shape_type == 0:
                continue
            if shape_type not in {3, 5, 13, 15, 23, 25} or len(content) < 44:
                continue

            part_count, point_count = struct.unpack_from("<2i", content, 36)
            if part_count <= 0 or point_count <= 0:
                continue
            required_size = 44 + 4 * part_count + 16 * point_count
            if len(content) < required_size:
                raise ValueError(f"Géométrie Shapefile tronquée : {path}")

            part_starts = list(
                struct.unpack_from(f"<{part_count}i", content, 44)
            )
            points_offset = 44 + 4 * part_count
            part_ends = part_starts[1:] + [point_count]
            for start, end in zip(part_starts, part_ends):
                if start < 0 or end > point_count or start >= end:
                    continue
                yield [
                    struct.unpack_from("<2d", content, points_offset + index * 16)
                    for index in range(start, end)
                ]


@dataclass(frozen=True)
class LayerSpec:
    key: str
    label: str
    unit: str
    field: str
    stops: tuple[tuple[float, str], ...]
    group: str = "Autres"
    decimals: int = 0
    transparent_below: float | None = None
    opacity: int = 244
    discrete: bool = False
    source_key: str | None = None
    range_mode: str | None = None


PRECIPITATION_STOPS = (
    (0.1, "#f5f5f7"),
    (1, "#c9e6ff"),
    (2, "#7fbbff"),
    (3, "#438fff"),
    (5, "#1bd0ef"),
    (7, "#00b8bd"),
    (10, "#00ca76"),
    (15, "#32e300"),
    (20, "#86ed00"),
    (25, "#d2ef00"),
    (30, "#fff000"),
    (40, "#ffd000"),
    (50, "#ff9900"),
    (60, "#ff6500"),
    (70, "#ff2e00"),
    (80, "#ef0054"),
    (90, "#d000a7"),
    (100, "#a000e8"),
    (125, "#6900dc"),
    (150, "#4b00b4"),
    (175, "#291078"),
    (200, "#661070"),
    (250, "#a548bd"),
    (300, "#d487e1"),
    (400, "#f0c8f2"),
    (500, "#ffffff"),
)


LAYER_SPECS = (
    LayerSpec(
        "temperature",
        "Température à 2 m",
        "°C",
        "temperature_c",
        (
            (-25, "#482173"),
            (-15, "#303fa5"),
            (-5, "#3478c5"),
            (0, "#55b7dd"),
            (5, "#53c6a8"),
            (10, "#70cf66"),
            (15, "#cbd83f"),
            (20, "#f2d43d"),
            (25, "#f2a331"),
            (30, "#ea652b"),
            (35, "#d93435"),
            (40, "#a71f57"),
            (45, "#5b1037"),
        ),
        group="Températures",
        decimals=1,
    ),
    LayerSpec(
        "temperature_ressentie",
        "Refroidissement éolien",
        "°C",
        "wind_chill_c",
        (
            (-35, "#27145d"),
            (-25, "#482173"),
            (-15, "#303fa5"),
            (-5, "#3478c5"),
            (0, "#55b7dd"),
            (5, "#53c6a8"),
            (10, "#70cf66"),
            (15, "#cbd83f"),
            (20, "#f2d43d"),
        ),
        group="Températures",
        decimals=1,
    ),
    LayerSpec(
        "temperature_surface",
        "Température de surface",
        "°C",
        "surface_temperature_c",
        (
            (-25, "#482173"), (-15, "#303fa5"), (-5, "#3478c5"),
            (0, "#55b7dd"), (5, "#53c6a8"), (10, "#70cf66"),
            (15, "#cbd83f"), (20, "#f2d43d"), (25, "#f2a331"),
            (30, "#ea652b"), (35, "#d93435"), (40, "#a71f57"),
            (45, "#5b1037"),
        ),
        group="Températures",
        decimals=1,
    ),
    LayerSpec(
        "point_rosee",
        "Point de rosée à 2 m",
        "°C",
        "dewpoint_c",
        (
            (-25, "#57336f"),
            (-15, "#3855a3"),
            (-5, "#398bca"),
            (0, "#56b7d8"),
            (5, "#58c8a2"),
            (10, "#79cf68"),
            (15, "#d5d64a"),
            (20, "#f0a83b"),
            (25, "#df5d3c"),
            (30, "#9f2955"),
        ),
        group="Températures",
        decimals=1,
    ),
    LayerSpec(
        "humidex",
        "Humidex",
        "",
        "humidex",
        (
            (-10, "#3478c5"),
            (0, "#55b7dd"),
            (10, "#53c6a8"),
            (20, "#b9d84c"),
            (25, "#f2d43d"),
            (30, "#f2a331"),
            (35, "#ea652b"),
            (40, "#d93435"),
            (45, "#a71f57"),
            (50, "#5b1037"),
        ),
        group="Températures",
        decimals=1,
    ),
    LayerSpec(
        "thermometre_mouille",
        "Température du thermomètre mouillé",
        "°C",
        "wet_bulb_c",
        (
            (-25, "#57336f"),
            (-15, "#3855a3"),
            (-5, "#398bca"),
            (0, "#56b7d8"),
            (5, "#58c8a2"),
            (10, "#79cf68"),
            (15, "#d5d64a"),
            (20, "#f0a83b"),
            (25, "#df5d3c"),
            (30, "#9f2955"),
        ),
        group="Températures",
        decimals=1,
    ),
    LayerSpec(
        "temperature_850",
        "Température à 850 hPa",
        "°C",
        "temperature_850_c",
        (
            (-40, "#321253"), (-30, "#423c9c"), (-20, "#326eb7"),
            (-10, "#3da6cf"), (0, "#5ac7ad"), (10, "#bcd84e"),
            (20, "#f0a33a"), (30, "#d6403e"), (40, "#701d4c"),
        ),
        group="Températures",
        decimals=1,
    ),
    LayerSpec(
        "temperature_500",
        "Température à 500 hPa",
        "°C",
        "temperature_500_c",
        (
            (-60, "#25104f"), (-50, "#3f3191"), (-40, "#315fae"),
            (-30, "#398fc7"), (-20, "#51bfd0"), (-10, "#6dc89b"),
            (0, "#cbd84b"), (10, "#ef9937"),
        ),
        group="Températures",
        decimals=1,
    ),
    LayerSpec(
        "pluie_1h",
        "Précipitations sur 1 h",
        "mm",
        "precipitation_mm",
        tuple(stop for stop in PRECIPITATION_STOPS if stop[0] <= 100),
        group="Précipitations",
        decimals=1,
        transparent_below=0.03,
        opacity=255,
        discrete=True,
    ),
    LayerSpec(
        "pluie_cumul",
        "Précipitations cumulées sur une période",
        "mm",
        "precipitation_total_mm",
        PRECIPITATION_STOPS,
        group="Précipitations",
        decimals=1,
        transparent_below=0.03,
        opacity=255,
        discrete=True,
        range_mode="difference",
    ),
    LayerSpec(
        "neige",
        "Neige sur 1 h (équivalent eau)",
        "mm",
        "snow_mm",
        tuple(stop for stop in PRECIPITATION_STOPS if stop[0] <= 100),
        group="Précipitations",
        decimals=1,
        transparent_below=0.03,
        opacity=255,
        discrete=True,
    ),
    LayerSpec(
        "neige_au_sol",
        "Cumul de neige fraîche (estimé)",
        "cm",
        "snow_depth_cm",
        (
            (0.1, "#f4f7fb"), (1, "#d7efff"), (2, "#a9d9ff"),
            (5, "#70b8ef"), (10, "#3a91d5"), (20, "#536bc1"),
            (30, "#7048ac"), (50, "#963b92"), (75, "#c65382"),
            (100, "#f0b5cf"),
        ),
        group="Précipitations",
        decimals=1,
        transparent_below=0.05,
        discrete=True,
    ),
    LayerSpec(
        "equivalent_eau_neige",
        "Cumul neigeux (équivalent eau)",
        "mm",
        "snow_water_equivalent_mm",
        tuple(stop for stop in PRECIPITATION_STOPS if stop[0] <= 200),
        group="Précipitations",
        decimals=1,
        transparent_below=0.03,
        discrete=True,
    ),
    LayerSpec(
        "graupel",
        "Graupel",
        "mm",
        "graupel_mm",
        tuple(stop for stop in PRECIPITATION_STOPS if stop[0] <= 100),
        group="Précipitations",
        decimals=1,
        transparent_below=0.03,
        discrete=True,
    ),
    LayerSpec(
        "neige_graupel",
        "Accumulation de neige et graupel",
        "mm",
        "snow_graupel_total_mm",
        tuple(stop for stop in PRECIPITATION_STOPS if stop[0] <= 200),
        group="Précipitations",
        decimals=1,
        transparent_below=0.03,
        discrete=True,
    ),
    LayerSpec(
        "grele",
        "Risque de grêle",
        "",
        "hail_risk_code",
        (
            (0, "#f5f5f7"),
            (1, "#ffe066"),
            (2, "#ff8c42"),
            (3, "#d7263d"),
            (4, "#d7263d"),
        ),
        group="Précipitations",
        decimals=0,
        transparent_below=0.5,
        discrete=True,
    ),
    LayerSpec(
        "type_precipitation_severe",
        "Type de précipitation sévère",
        "",
        "storm_type_code",
        (
            (0, "#f5f5f7"),
            (1, "#ffd166"),
            (2, "#ff9f45"),
            (3, "#ef476f"),
            (4, "#8338ec"),
            (5, "#8338ec"),
        ),
        group="Précipitations",
        decimals=0,
        transparent_below=0.5,
        discrete=True,
    ),
    LayerSpec(
        "vent",
        "Vent moyen à 10 m",
        "km/h",
        "wind_speed_kmh",
        (
            (0, "#eef7ea"),
            (10, "#a7db8d"),
            (20, "#5cc27d"),
            (30, "#38aaa5"),
            (40, "#347cc3"),
            (50, "#6558b8"),
            (60, "#a43e94"),
            (80, "#d63c57"),
            (100, "#7e1736"),
        ),
        group="Vent",
    ),
    LayerSpec(
        "rafales",
        "Rafales à 10 m",
        "km/h",
        "wind_gust_kmh",
        (
            (0, "#edf7e8"),
            (20, "#a9d77d"),
            (40, "#f0cf46"),
            (60, "#ef8b2c"),
            (80, "#db3d3d"),
            (100, "#9e235d"),
            (130, "#4d1647"),
            (160, "#25152e"),
        ),
        group="Vent",
    ),
    LayerSpec(
        "rafales_max",
        "Rafales maximales sur une période",
        "km/h",
        "wind_gust_kmh",
        (
            (0, "#edf7e8"),
            (20, "#a9d77d"),
            (40, "#f0cf46"),
            (60, "#ef8b2c"),
            (80, "#db3d3d"),
            (100, "#9e235d"),
            (130, "#4d1647"),
            (160, "#25152e"),
        ),
        group="Vent",
        source_key="rafales",
        range_mode="maximum",
    ),
    LayerSpec(
        "vent_850",
        "Vent à 850 hPa",
        "km/h",
        "wind_speed_850_kmh",
        (
            (0, "#eef7ea"), (20, "#a7db8d"), (40, "#43b894"),
            (60, "#347cc3"), (80, "#6558b8"), (100, "#a43e94"),
            (140, "#d63c57"), (180, "#7e1736"), (220, "#35132b"),
        ),
        group="Vent",
    ),
    LayerSpec(
        "vent_500",
        "Vent à 500 hPa",
        "km/h",
        "wind_speed_500_kmh",
        (
            (0, "#eef7ea"), (30, "#a7db8d"), (60, "#43b894"),
            (90, "#347cc3"), (120, "#6558b8"), (150, "#a43e94"),
            (200, "#d63c57"), (250, "#7e1736"), (300, "#35132b"),
        ),
        group="Vent",
    ),
    LayerSpec(
        "jet_stream",
        "Vent à 300 hPa (jet stream)",
        "km/h",
        "wind_speed_300_kmh",
        (
            (0, "#eef7ea"), (40, "#a7db8d"), (80, "#43b894"),
            (120, "#347cc3"), (160, "#6558b8"), (200, "#a43e94"),
            (250, "#d63c57"), (300, "#7e1736"), (350, "#35132b"),
        ),
        group="Vent",
    ),
    LayerSpec(
        "pression",
        "Pression au niveau de la mer (estimée)",
        "hPa",
        "pressure_hpa",
        (
            (960, "#562a7c"),
            (975, "#315ab4"),
            (990, "#2f98c5"),
            (1000, "#48b983"),
            (1010, "#c6d64f"),
            (1020, "#f0c646"),
            (1030, "#e57a34"),
            (1045, "#b52f43"),
        ),
        group="Pression et géopotentiel",
    ),
    LayerSpec(
        "pression_surface",
        "Pression au sol",
        "hPa",
        "surface_pressure_hpa",
        (
            (700, "#44205f"), (800, "#3455a6"), (900, "#36a1bd"),
            (950, "#54bf7c"), (1000, "#d6d64c"), (1030, "#ed9a36"),
            (1060, "#b52f43"),
        ),
        group="Pression et géopotentiel",
    ),
    LayerSpec(
        "geopotentiel_500",
        "Géopotentiel à 500 hPa",
        "m",
        "geopotential_500_m",
        (
            (4800, "#3f1d69"), (5000, "#354bab"), (5200, "#3384c3"),
            (5400, "#3cb9aa"), (5600, "#b5d04d"), (5800, "#efad3b"),
            (6000, "#cf493e"),
        ),
        group="Pression et géopotentiel",
    ),
    LayerSpec(
        "geopotentiel_850",
        "Géopotentiel à 850 hPa",
        "m",
        "geopotential_850_m",
        (
            (900, "#3f1d69"), (1100, "#354bab"), (1300, "#3384c3"),
            (1500, "#3cb9aa"), (1700, "#b5d04d"), (1900, "#efad3b"),
            (2100, "#cf493e"),
        ),
        group="Pression et géopotentiel",
    ),
    LayerSpec(
        "nebulosite",
        "Nébulosité totale",
        "%",
        "cloud_cover_pct",
        (
            (0, "#dceef6"),
            (20, "#c8dce5"),
            (40, "#abbac5"),
            (60, "#8997a4"),
            (80, "#626e79"),
            (100, "#343d46"),
        ),
        group="Nuages et humidité",
    ),
    LayerSpec(
        "nuages_bas",
        "Couverture nuageuse basse",
        "%",
        "cloud_low_pct",
        (
            (0, "#e6f4fa"),
            (20, "#cddfe7"),
            (40, "#adbec8"),
            (60, "#8997a4"),
            (80, "#626e79"),
            (100, "#343d46"),
        ),
        group="Nuages et humidité",
    ),
    LayerSpec(
        "nuages_moyens",
        "Couverture nuageuse moyenne",
        "%",
        "cloud_mid_pct",
        (
            (0, "#e6f4fa"),
            (20, "#cddfe7"),
            (40, "#adbec8"),
            (60, "#8997a4"),
            (80, "#626e79"),
            (100, "#343d46"),
        ),
        group="Nuages et humidité",
    ),
    LayerSpec(
        "nuages_eleves",
        "Couverture nuageuse élevée",
        "%",
        "cloud_high_pct",
        (
            (0, "#e6f4fa"),
            (20, "#cddfe7"),
            (40, "#adbec8"),
            (60, "#8997a4"),
            (80, "#626e79"),
            (100, "#343d46"),
        ),
        group="Nuages et humidité",
    ),
    LayerSpec(
        "humidite",
        "Humidité relative à 2 m",
        "%",
        "humidity_pct",
        (
            (0, "#9a5429"),
            (20, "#d19a52"),
            (40, "#e3d16b"),
            (60, "#83ca82"),
            (80, "#48a6b6"),
            (100, "#28569f"),
        ),
        group="Nuages et humidité",
    ),
    LayerSpec(
        "visibilite",
        "Visibilité",
        "km",
        "visibility_km",
        (
            (0, "#7b1f1f"),
            (1, "#cf3d35"),
            (2, "#ed8b33"),
            (5, "#e6ce4f"),
            (10, "#88c681"),
            (20, "#67b8d0"),
            (50, "#d8f1ff"),
        ),
        group="Nuages et humidité",
        decimals=1,
    ),
    LayerSpec(
        "humidite_850",
        "Humidité relative à 850 hPa",
        "%",
        "humidity_850_pct",
        (
            (0, "#9a5429"), (20, "#d19a52"), (40, "#e3d16b"),
            (60, "#83ca82"), (80, "#48a6b6"), (100, "#28569f"),
        ),
        group="Nuages et humidité",
    ),
    LayerSpec(
        "humidite_500",
        "Humidité relative à 500 hPa",
        "%",
        "humidity_500_pct",
        (
            (0, "#9a5429"), (20, "#d19a52"), (40, "#e3d16b"),
            (60, "#83ca82"), (80, "#48a6b6"), (100, "#28569f"),
        ),
        group="Nuages et humidité",
    ),
    LayerSpec(
        "base_nuages",
        "Altitude de la base des nuages",
        "m",
        "cloud_base_m",
        (
            (0, "#5c2447"), (100, "#a33a45"), (200, "#df6b3e"),
            (500, "#e6b846"), (1000, "#9bcb72"), (2000, "#59b3bd"),
            (4000, "#6d75bd"), (8000, "#d4d9ef"),
        ),
        group="Nuages et humidité",
    ),
    LayerSpec(
        "couche_limite",
        "Hauteur de la couche limite",
        "m",
        "mixed_layer_depth_m",
        (
            (0, "#36215e"), (100, "#3f4a9f"), (300, "#397fb9"),
            (500, "#46afad"), (1000, "#a6cf66"), (1500, "#e4c84c"),
            (2500, "#e5813d"), (4000, "#b73549"),
        ),
        group="Autres",
    ),
    LayerSpec(
        "rayonnement_global",
        "Rayonnement solaire global cumulé",
        "MJ/m²",
        "global_radiation_mjm2",
        (
            (0, "#24346f"), (0.5, "#346aa5"), (1, "#3da6b3"),
            (2, "#72c776"), (4, "#d4d74c"), (6, "#f3b53d"),
            (9, "#e36b35"), (12, "#a52f49"),
        ),
        group="Autres",
        decimals=2,
    ),
    LayerSpec(
        "rayonnement_court",
        "Rayonnement net ondes courtes cumulé",
        "MJ/m²",
        "net_shortwave_mjm2",
        (
            (-2, "#352061"), (0, "#345e9f"), (1, "#39a3b5"),
            (2, "#77c66e"), (4, "#d5d54a"), (6, "#f0a33b"),
            (10, "#c63d43"),
        ),
        group="Autres",
        decimals=2,
    ),
    LayerSpec(
        "rayonnement_long",
        "Rayonnement net ondes longues cumulé",
        "MJ/m²",
        "net_longwave_mjm2",
        (
            (-8, "#341c64"), (-5, "#3b57aa"), (-3, "#3ca0bd"),
            (-1, "#7bca7d"), (0, "#e2d34c"), (1, "#e47b38"),
            (3, "#b63148"),
        ),
        group="Autres",
        decimals=2,
    ),
    LayerSpec(
        "flux_sensible",
        "Flux de chaleur sensible cumulé",
        "MJ/m²",
        "sensible_heat_mjm2",
        (
            (-8, "#2c2876"), (-4, "#397fbc"), (-1, "#55c1ba"),
            (0, "#e7e8d0"), (1, "#e7cf4e"), (4, "#e47b38"),
            (8, "#aa3049"),
        ),
        group="Autres",
        decimals=2,
    ),
    LayerSpec(
        "flux_latent",
        "Flux de chaleur latente cumulé",
        "MJ/m²",
        "latent_heat_mjm2",
        (
            (-8, "#2c2876"), (-4, "#397fbc"), (-1, "#55c1ba"),
            (0, "#e7e8d0"), (1, "#e7cf4e"), (4, "#e47b38"),
            (8, "#aa3049"),
        ),
        group="Autres",
        decimals=2,
    ),
    LayerSpec(
        "mucape",
        "MUCAPE instantanée",
        "J/kg",
        "cape_jkg",
        (
            (0, "#f3f5f8"), (100, "#d8ebff"), (300, "#91c8ff"),
            (500, "#41a8df"), (800, "#31c878"), (1200, "#d5e52f"),
            (1800, "#ffc62d"), (2500, "#ff7a22"), (3500, "#e83028"),
            (5000, "#8c1d74"),
        ),
        group="Instabilité",
        transparent_below=25.0,
    ),
    LayerSpec(
        "reflectivite",
        "Réflectivité radar maximale",
        "dBZ",
        "reflectivity_dbz",
        (
            (0, "#f5f5f7"), (5, "#c9e6ff"), (10, "#7fbbff"),
            (15, "#25cbe0"), (20, "#00bd75"), (25, "#5be000"),
            (30, "#d5eb00"), (35, "#ffe500"), (40, "#ffae00"),
            (45, "#ff6500"), (50, "#f32020"), (55, "#d00076"),
            (60, "#9300c6"), (70, "#ffffff"),
        ),
        group="Instabilité",
        transparent_below=5.0,
    ),
    LayerSpec(
        "altitude",
        "Altitude du relief AROME",
        "m",
        "altitude_m",
        (
            (-50, "#d6e8ef"), (0, "#d8e8c1"), (100, "#b8d98c"),
            (300, "#9bc267"), (600, "#c3b563"), (1000, "#b88d58"),
            (1500, "#966b52"), (2200, "#765054"), (3200, "#eeeeee"),
            (4500, "#ffffff"),
        ),
        group="Relief",
    ),
)


def _hex_to_rgb(value: str) -> np.ndarray:
    clean = value.lstrip("#")
    return np.asarray(
        tuple(int(clean[index : index + 2], 16) for index in (0, 2, 4))
    )


def _mercator(latitude: np.ndarray | float) -> np.ndarray | float:
    radians = np.radians(np.clip(latitude, -85.0, 85.0))
    return np.log(np.tan(np.pi / 4.0 + radians / 2.0))


def _inverse_mercator(value: np.ndarray) -> np.ndarray:
    return np.degrees(2.0 * np.arctan(np.exp(value)) - np.pi / 2.0)


def _map_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    names = (
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "Arial Bold.ttf" if bold else "Arial.ttf",
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


class AromeMapRenderer:
    """Rend les champs AROME natifs et les frontières cartographiques."""

    def __init__(
        self,
        latitudes: np.ndarray,
        longitudes: np.ndarray,
        output_directory: Path,
        *,
        width: int = 2200,
        height: int = 1640,
        bounds: dict[str, float] | None = None,
        source_max_distance: float = 0.22,
        france_latitudes: np.ndarray | None = None,
        france_longitudes: np.ndarray | None = None,
        france_departments: Sequence[str] | None = None,
        boundary_directory: Path | None = None,
        pregridded: bool = False,
    ) -> None:
        self.latitudes = np.asarray(latitudes, dtype=np.float64)
        self.longitudes = np.asarray(longitudes, dtype=np.float64)
        self.pregridded = bool(pregridded)
        if self.latitudes.shape != self.longitudes.shape or self.latitudes.ndim != 1:
            raise ValueError("Coordonnées cartographiques invalides")
        if not self.pregridded and len(self.latitudes) < 4:
            raise ValueError("Au moins quatre points AROME sont nécessaires")

        self.output_directory = Path(output_directory)
        self.output_directory.mkdir(parents=True, exist_ok=True)
        self.width = int(width)
        self.height = int(height)
        self.bounds = dict(bounds or DEFAULT_BOUNDS)
        self.source_max_distance = float(source_max_distance)
        self.boundary_directory = (
            Path(boundary_directory) if boundary_directory is not None else None
        )
        self.france_latitudes = (
            np.asarray(france_latitudes, dtype=np.float64)
            if france_latitudes is not None
            else None
        )
        self.france_longitudes = (
            np.asarray(france_longitudes, dtype=np.float64)
            if france_longitudes is not None
            else None
        )
        self.france_departments = (
            list(france_departments) if france_departments is not None else None
        )
        self.steps: list[dict[str, Any]] = []
        self.fixed_steps: list[dict[str, Any]] = []
        self.available_layers: set[str] = set()
        self._static_assets: dict[str, tuple[str, str]] = {}
        self._fixed_line_cache: dict[str, list[list[tuple[int, int]]]] = {}

        self._prepare_interpolation()
        self._write_static_maps()

    def _fixed_pixel(self, latitude: float, longitude: float) -> tuple[int, int]:
        bounds = FIXED_MAP_BOUNDS
        west = float(bounds["west"])
        east = float(bounds["east"])
        north_y = float(_mercator(float(bounds["north"])))
        south_y = float(_mercator(float(bounds["south"])))
        crop_west, crop_north = self._pixel(float(bounds["north"]), west)
        crop_east, crop_south = self._pixel(float(bounds["south"]), east)
        crop_width = max(1, crop_east - crop_west)
        crop_height = max(1, crop_south - crop_north)
        fixed_height = max(1, round(FIXED_MAP_WIDTH * crop_height / crop_width))
        x = (longitude - west) / (east - west) * (FIXED_MAP_WIDTH - 1)
        y = (north_y - float(_mercator(latitude))) / (north_y - south_y)
        y *= fixed_height - 1
        return int(round(x)), int(round(y))

    def _draw_fixed_shapefile(
        self,
        draw: ImageDraw.ImageDraw,
        path: Path,
        *,
        fill: str,
        width: int,
    ) -> None:
        if not path.is_file():
            return
        cache_key = str(path.resolve())
        cached = self._fixed_line_cache.get(cache_key)
        if cached is not None:
            for segment in cached:
                draw.line(segment, fill=fill, width=width, joint="curve")
            return
        bounds = FIXED_MAP_BOUNDS
        south = float(bounds["south"]) - 1.0
        north = float(bounds["north"]) + 1.0
        west = float(bounds["west"]) - 1.0
        east = float(bounds["east"]) + 1.0
        rendered_segments: list[list[tuple[int, int]]] = []
        for points in _iter_shapefile_parts(path):
            segment: list[tuple[int, int]] = []
            for longitude, latitude in points:
                if west <= longitude <= east and south <= latitude <= north:
                    segment.append(self._fixed_pixel(latitude, longitude))
                elif segment:
                    if len(segment) >= 2:
                        rendered_segments.append(segment)
                    segment = []
            if len(segment) >= 2:
                rendered_segments.append(segment)
        self._fixed_line_cache[cache_key] = rendered_segments
        for segment in rendered_segments:
            draw.line(segment, fill=fill, width=width, joint="curve")

    def _write_fixed_map(
        self,
        image: Image.Image,
        spec: LayerSpec,
        lead_hour: int,
        valid_time: datetime,
        *,
        field: np.ndarray | None = None,
        output_key: str | None = None,
        show_values: bool = False,
        label_override: str | None = None,
    ) -> str:
        bounds = FIXED_MAP_BOUNDS
        left, top = self._pixel(float(bounds["north"]), float(bounds["west"]))
        right, bottom = self._pixel(float(bounds["south"]), float(bounds["east"]))
        left = max(0, min(self.width - 1, left))
        right = max(left + 1, min(self.width, right))
        top = max(0, min(self.height - 1, top))
        bottom = max(top + 1, min(self.height, bottom))
        crop = image.crop((left, top, right, bottom))
        field_crop = None
        if field is not None:
            field_crop = np.asarray(field[top:bottom, left:right], dtype=np.float32)
        map_height = max(1, round(FIXED_MAP_WIDTH * crop.height / crop.width))
        crop = crop.resize((FIXED_MAP_WIDTH, map_height), Image.Resampling.BILINEAR)

        header_height = 62
        footer_height = 70
        canvas = Image.new(
            "RGBA",
            (FIXED_MAP_WIDTH, header_height + map_height + footer_height),
            "#ffffff",
        )
        map_background = Image.new("RGBA", crop.size, "#e8f0f7")
        map_background.alpha_composite(crop)
        map_draw = ImageDraw.Draw(map_background)
        if self.boundary_directory is not None:
            self._draw_fixed_shapefile(
                map_draw,
                self.boundary_directory / "ne_50m_admin_0_boundary_lines_land.shp",
                fill="#555c63",
                width=2,
            )
            self._draw_fixed_shapefile(
                map_draw,
                self.boundary_directory / "ne_50m_coastline.shp",
                fill="#2d3338",
                width=2,
            )
        canvas.alpha_composite(map_background, (0, header_height))
        draw = ImageDraw.Draw(canvas)
        title_font = _map_font(21, bold=True)
        meta_font = _map_font(16)
        small_font = _map_font(14)
        draw.rectangle((0, 0, FIXED_MAP_WIDTH, header_height), fill="#ffffff")
        draw.text((16, 9), "AROME 1,3 km • Météo-France", fill="#18384b", font=title_font)
        layer_label = label_override or spec.label
        draw.text((16, 37), layer_label, fill="#35586b", font=meta_font)
        valid_label = valid_time.strftime("%d/%m/%Y %Hh UTC")
        lead_label = f"Échéance +{lead_hour:02d} h • {valid_label}"
        lead_width = draw.textbbox((0, 0), lead_label, font=title_font)[2]
        draw.text(
            (FIXED_MAP_WIDTH - lead_width - 16, 18),
            lead_label,
            fill="#c91f24",
            font=title_font,
        )

        brand = "www.alertes-meteo.com"
        brand_box = draw.textbbox((0, 0), brand, font=small_font)
        brand_width = brand_box[2] - brand_box[0]
        brand_y = header_height + map_height - 34
        draw.rounded_rectangle(
            (FIXED_MAP_WIDTH - brand_width - 28, brand_y,
             FIXED_MAP_WIDTH - 10, brand_y + 26),
            radius=4,
            fill="#1d2730cc",
        )
        draw.text(
            (FIXED_MAP_WIDTH - brand_width - 19, brand_y + 5),
            brand,
            fill="#ffffff",
            font=small_font,
        )

        if show_values and field_crop is not None and field_crop.size:
            value_font = _map_font(11, bold=True)
            target_spacing = 44
            row_step = max(1, round(field_crop.shape[0] * target_spacing / map_height))
            column_step = max(
                1,
                round(field_crop.shape[1] * target_spacing / FIXED_MAP_WIDTH),
            )
            for row in range(row_step // 2, field_crop.shape[0], row_step):
                y = header_height + round(
                    row / max(1, field_crop.shape[0] - 1) * (map_height - 1)
                )
                for column in range(
                    column_step // 2,
                    field_crop.shape[1],
                    column_step,
                ):
                    value = float(field_crop[row, column])
                    if not np.isfinite(value):
                        continue
                    x = round(
                        column / max(1, field_crop.shape[1] - 1)
                        * (FIXED_MAP_WIDTH - 1)
                    )
                    text = f"{value:.0f}"
                    box = draw.textbbox((0, 0), text, font=value_font, stroke_width=1)
                    text_width = box[2] - box[0]
                    text_height = box[3] - box[1]
                    draw.text(
                        (x - text_width / 2, y - text_height / 2),
                        text,
                        fill="#111820",
                        font=value_font,
                        stroke_width=1,
                        stroke_fill="#ffffff",
                    )

        legend_top = header_height + map_height + 12
        legend_left = 18
        legend_right = FIXED_MAP_WIDTH - 18
        stops = list(spec.stops)
        sample_indexes = np.linspace(0, len(stops) - 1, min(14, len(stops))).round().astype(int)
        sampled = [stops[index] for index in sample_indexes]
        swatch_width = (legend_right - legend_left) / len(sampled)
        for index, (value, colour) in enumerate(sampled):
            x0 = round(legend_left + index * swatch_width)
            x1 = round(legend_left + (index + 1) * swatch_width)
            draw.rectangle((x0, legend_top, x1, legend_top + 18), fill=colour)
            if index in {0, len(sampled) - 1} or index % 2 == 0:
                draw.text((x0, legend_top + 22), f"{value:g}", fill="#283b46", font=small_font)
        unit_label = spec.unit or ""
        unit_width = draw.textbbox((0, 0), unit_label, font=meta_font)[2]
        draw.text(
            (FIXED_MAP_WIDTH - unit_width - 18, legend_top + 22),
            unit_label,
            fill="#283b46",
            font=meta_font,
        )

        destination_key = output_key or spec.key
        destination_directory = self.output_directory / "fixed" / destination_key
        destination_directory.mkdir(parents=True, exist_ok=True)
        destination = destination_directory / f"{lead_hour:03d}.png"
        canvas.convert("RGB").save(destination, "PNG", optimize=True, compress_level=7)
        return f"maps/fixed/{destination_key}/{destination.name}"

    def _prepare_interpolation(self) -> None:
        south = float(self.bounds["south"])
        north = float(self.bounds["north"])
        west = float(self.bounds["west"])
        east = float(self.bounds["east"])
        mercator_rows = np.linspace(_mercator(north), _mercator(south), self.height)
        grid_latitudes = _inverse_mercator(mercator_rows)
        grid_longitudes = np.linspace(west, east, self.width)
        longitude_grid, latitude_grid = np.meshgrid(grid_longitudes, grid_latitudes)
        self._target_latitudes = latitude_grid
        self._target_longitudes = longitude_grid
        latitude_midpoint = (south + north) / 2.0
        self._longitude_scale = math.cos(math.radians(latitude_midpoint))

        if self.pregridded:
            self._coverage_mask = np.ones((self.height, self.width), dtype=bool)
            self._indexes = None
            self._weights = None
            return

        source = np.column_stack(
            (self.longitudes * self._longitude_scale, self.latitudes)
        )
        target = np.column_stack(
            (
                longitude_grid.ravel() * self._longitude_scale,
                latitude_grid.ravel(),
            )
        )
        neighbour_count = min(4, len(source))
        distances, indexes = cKDTree(source).query(
            target,
            k=neighbour_count,
            workers=-1,
        )
        if neighbour_count == 1:
            distances = distances[:, None]
            indexes = indexes[:, None]
        self._indexes = indexes.astype(np.int32, copy=False)
        self._weights = (
            1.0 / np.maximum(distances, 1.0e-4) ** 2
        ).astype(np.float32, copy=False)
        self._coverage_mask = (
            distances[:, 0].reshape(self.height, self.width)
            <= self.source_max_distance
        )

    def _interpolate(self, values: np.ndarray) -> np.ndarray:
        source = np.asarray(values, dtype=np.float64)
        if self.pregridded:
            if source.shape == (self.height, self.width):
                return source.astype(np.float32, copy=False)
            if source.ndim == 1 and source.size == self.height * self.width:
                return source.reshape(self.height, self.width).astype(
                    np.float32, copy=False
                )
            raise ValueError(
                "Le champ AROME préinterpolé ne correspond pas à la carte"
            )
        if source.shape != self.latitudes.shape:
            raise ValueError("Le champ ne correspond pas à la grille AROME native")
        selected = source[self._indexes]
        finite = np.isfinite(selected)
        weights = self._weights * finite
        denominator = np.sum(weights, axis=1)
        numerator = np.sum(np.where(finite, selected, 0.0) * weights, axis=1)
        result = np.full(len(denominator), np.nan, dtype=np.float32)
        valid = denominator > 0
        result[valid] = numerator[valid] / denominator[valid]
        return result.reshape(self.height, self.width)

    def _image_from_field(self, field: np.ndarray, spec: LayerSpec) -> Image.Image:
        stop_values = np.asarray([item[0] for item in spec.stops], dtype=np.float32)
        stop_colours = np.asarray([_hex_to_rgb(item[1]) for item in spec.stops])
        finite_field = np.isfinite(field)
        clipped = np.clip(
            np.where(finite_field, field, stop_values[0]),
            stop_values[0],
            stop_values[-1],
        )
        # Les cartes AROME de référence utilisent des plages colorées nettes,
        # pas un agrandissement flou des pixels du raster. La quantification se
        # fait après l'interpolation pleine résolution ; les frontières des
        # plages restent donc lisses même lors d'un zoom important.
        contour_step = CONTOUR_STEPS.get(spec.field)
        if contour_step:
            clipped = np.floor(clipped / contour_step) * contour_step
        upper = np.searchsorted(stop_values, clipped, side="right")
        upper = np.clip(upper, 1, len(stop_values) - 1)
        lower = upper - 1
        if spec.discrete:
            rgb = stop_colours[lower].astype(np.uint8)
        else:
            low_values = stop_values[lower]
            high_values = stop_values[upper]
            fraction = np.divide(
                clipped - low_values,
                high_values - low_values,
                out=np.zeros_like(clipped),
                where=(high_values != low_values),
            )
            rgb = (
                stop_colours[lower] * (1.0 - fraction[..., None])
                + stop_colours[upper] * fraction[..., None]
            ).astype(np.uint8)
        alpha = np.full(field.shape, spec.opacity, dtype=np.uint8)
        valid = self._coverage_mask & finite_field
        if spec.transparent_below is not None:
            valid &= field >= spec.transparent_below
        alpha[~valid] = 0
        return Image.fromarray(np.dstack((rgb, alpha)), mode="RGBA")

    def _write_probe_field(
        self,
        field: np.ndarray,
        spec: LayerSpec,
        destination: Path,
    ) -> None:
        """Écrit une grille numérique compacte pour la valeur sous le pointeur.

        La grille conserve la résolution utile du modèle tout en évitant de
        publier un second raster pleine définition. Les valeurs sont
        quantifiées sur 16 bits puis compressées en gzip ; 65535 représente
        un point hors domaine ou manquant.
        """

        sampled = np.asarray(
            field[::PROBE_DOWNSAMPLE, ::PROBE_DOWNSAMPLE],
            dtype=np.float32,
        )
        coverage = self._coverage_mask[
            ::PROBE_DOWNSAMPLE,
            ::PROBE_DOWNSAMPLE,
        ]
        minimum = float(spec.stops[0][0])
        maximum = float(spec.stops[-1][0])
        if not maximum > minimum:
            raise ValueError(f"Échelle cartographique invalide : {spec.key}")

        valid = coverage & np.isfinite(sampled)
        encoded = np.full(sampled.shape, 65535, dtype="<u2")
        normalized = (
            np.clip(sampled[valid], minimum, maximum) - minimum
        ) / (maximum - minimum)
        encoded[valid] = np.rint(normalized * 65534.0).astype("<u2")

        destination.parent.mkdir(parents=True, exist_ok=True)
        header = struct.pack(
            "<4sHHff",
            PROBE_MAGIC,
            encoded.shape[1],
            encoded.shape[0],
            minimum,
            maximum,
        )
        with destination.open("wb") as raw:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw,
                compresslevel=6,
                mtime=0,
            ) as compressed:
                compressed.write(header)
                compressed.write(encoded.tobytes(order="C"))

    def _pixel(self, latitude: float, longitude: float) -> tuple[int, int]:
        west = float(self.bounds["west"])
        east = float(self.bounds["east"])
        north_y = float(_mercator(float(self.bounds["north"])))
        south_y = float(_mercator(float(self.bounds["south"])))
        x = (longitude - west) / (east - west) * (self.width - 1)
        y = (north_y - float(_mercator(latitude))) / (north_y - south_y)
        y *= self.height - 1
        return int(round(x)), int(round(y))

    def _shapefile_svg_path(self, path: Path) -> str:
        if not path.is_file():
            return ""
        south = float(self.bounds["south"]) - 1
        north = float(self.bounds["north"]) + 1
        west = float(self.bounds["west"]) - 1
        east = float(self.bounds["east"]) + 1
        paths: list[str] = []
        for points in _iter_shapefile_parts(path):
            segment: list[tuple[float, float]] = []
            for longitude, latitude in points:
                if west <= longitude <= east and south <= latitude <= north:
                    segment.append(self._pixel(latitude, longitude))
                elif segment:
                    if len(segment) >= 2:
                        paths.append(
                            "M" + " L".join(
                                f"{x:.1f},{y:.1f}" for x, y in segment
                            )
                        )
                    segment = []
            if len(segment) >= 2:
                paths.append(
                    "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in segment)
                )
        return " ".join(paths)

    @staticmethod
    def _true_runs(mask: np.ndarray):
        padded = np.concatenate(
            (np.asarray([False]), np.asarray(mask, dtype=bool), np.asarray([False]))
        )
        changes = np.flatnonzero(padded[1:] != padded[:-1])
        return zip(changes[::2], changes[1::2])

    def _department_svg_path(self) -> str:
        if (
            self.france_latitudes is None
            or self.france_longitudes is None
            or self.france_departments is None
            or len(self.france_departments) != len(self.france_latitudes)
        ):
            return ""

        source = np.column_stack(
            (
                self.france_longitudes * self._longitude_scale,
                self.france_latitudes,
            )
        )
        target = np.column_stack(
            (
                self._target_longitudes.ravel() * self._longitude_scale,
                self._target_latitudes.ravel(),
            )
        )
        distances, indexes = cKDTree(source).query(target, k=1, workers=-1)
        codes = {
            code: index + 1
            for index, code in enumerate(sorted(set(self.france_departments)))
        }
        encoded = np.asarray(
            [codes.get(code, 0) for code in self.france_departments]
        )
        departments = encoded[indexes].reshape(self.height, self.width)
        france = distances.reshape(self.height, self.width) <= 0.18

        changes_between_columns = (
            france[:, 1:]
            & france[:, :-1]
            & (departments[:, 1:] != departments[:, :-1])
        )
        changes_between_rows = (
            france[1:, :]
            & france[:-1, :]
            & (departments[1:, :] != departments[:-1, :])
        )
        paths: list[str] = []
        for x in range(changes_between_columns.shape[1]):
            for start, end in self._true_runs(changes_between_columns[:, x]):
                coordinate = x + 0.5
                paths.append(
                    f"M{coordinate:.1f},{start:.1f} L{coordinate:.1f},{end:.1f}"
                )
        for y in range(changes_between_rows.shape[0]):
            for start, end in self._true_runs(changes_between_rows[y, :]):
                coordinate = y + 0.5
                paths.append(
                    f"M{start:.1f},{coordinate:.1f} L{end:.1f},{coordinate:.1f}"
                )
        return " ".join(paths)

    def _write_static_maps(self) -> None:
        base = Image.new("RGB", (self.width, self.height), "#a5a6b0")
        base.save(self.output_directory / "fond.webp", "WEBP", quality=86, method=4)

        national_path = ""
        coastline_path = ""
        if self.boundary_directory is not None:
            national_path = self._shapefile_svg_path(
                self.boundary_directory / "ne_50m_admin_0_boundary_lines_land.shp",
            )
            coastline_path = self._shapefile_svg_path(
                self.boundary_directory / "ne_50m_coastline.shp",
            )
        department_path = self._department_svg_path()
        svg = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.width} '
            f'{self.height}" preserveAspectRatio="none" '
            'shape-rendering="geometricPrecision">\n'
            f'<path d="{department_path}" fill="none" stroke="#20242b" '
            'stroke-opacity="0.58" stroke-width="0.8" '
            'vector-effect="non-scaling-stroke"/>\n'
            f'<path d="{national_path}" fill="none" stroke="#111116" '
            'stroke-width="1.45" stroke-linejoin="round" stroke-linecap="round" '
            'vector-effect="non-scaling-stroke"/>\n'
            f'<path d="{coastline_path}" fill="none" stroke="#050507" '
            'stroke-width="2" stroke-linejoin="round" stroke-linecap="round" '
            'vector-effect="non-scaling-stroke"/>\n'
            '</svg>\n'
        )
        (self.output_directory / "frontieres.svg").write_text(
            svg,
            encoding="utf-8",
        )

    def render_step(
        self,
        *,
        lead_hour: int,
        valid_time: datetime,
        fields: dict[str, np.ndarray],
    ) -> None:
        files: dict[str, str] = {}
        probes: dict[str, str] = {}
        fixed_files: dict[str, str] = {}
        for spec in LAYER_SPECS:
            if spec.source_key is not None:
                continue
            values = fields.get(spec.field)
            if values is None or not np.any(np.isfinite(values)):
                continue
            if spec.field in STATIC_FIELDS and spec.key in self._static_assets:
                files[spec.key], probes[spec.key] = self._static_assets[spec.key]
                self.available_layers.add(spec.key)
                continue
            field = self._interpolate(values)
            destination_directory = self.output_directory / spec.key
            destination_directory.mkdir(parents=True, exist_ok=True)
            file_stem = "statique" if spec.field in STATIC_FIELDS else f"{lead_hour:03d}"
            destination = destination_directory / f"{file_stem}.webp"
            image = self._image_from_field(field, spec)
            image.save(destination, "WEBP", quality=86, method=5)
            files[spec.key] = f"maps/{spec.key}/{destination.name}"
            if (
                spec.key in FIXED_MAP_KEYS
                and lead_hour % FIXED_MAP_INTERVAL_HOURS == 0
            ):
                fixed_files[spec.key] = self._write_fixed_map(
                    image,
                    spec,
                    lead_hour,
                    valid_time,
                )
                if spec.key == "temperature":
                    fixed_files[FIXED_TEMPERATURE_VALUES_KEY] = self._write_fixed_map(
                        image,
                        spec,
                        lead_hour,
                        valid_time,
                        field=field,
                        output_key=FIXED_TEMPERATURE_VALUES_KEY,
                        show_values=True,
                        label_override="Température à 2 m avec valeurs",
                    )
            probe_destination = (
                self.output_directory
                / "values"
                / spec.key
                / f"{file_stem}.hkv.gz"
            )
            self._write_probe_field(field, spec, probe_destination)
            probes[spec.key] = (
                f"maps/values/{spec.key}/{probe_destination.name}"
            )
            if spec.field in STATIC_FIELDS:
                self._static_assets[spec.key] = (
                    files[spec.key], probes[spec.key]
                )
            self.available_layers.add(spec.key)

        for spec in LAYER_SPECS:
            if spec.source_key is None or spec.source_key not in files:
                continue
            files[spec.key] = files[spec.source_key]
            probes[spec.key] = probes[spec.source_key]
            self.available_layers.add(spec.key)

        self.steps.append(
            {
                "lead_hour": int(lead_hour),
                "valid_time": valid_time.isoformat().replace("+00:00", "Z"),
                "files": files,
                "probes": probes,
            }
        )
        if fixed_files:
            self.fixed_steps.append(
                {
                    "lead_hour": int(lead_hour),
                    "valid_time": valid_time.isoformat().replace("+00:00", "Z"),
                    "files": fixed_files,
                }
            )

    def write_manifest(
        self,
        *,
        generated_at: str,
        run_time: str | None,
        places_path: str | None = None,
    ) -> dict[str, Any]:
        layers = {
            spec.key: {
                "label": spec.label,
                "unit": spec.unit,
                "group": spec.group,
                "decimals": spec.decimals,
                "transparent_below": spec.transparent_below,
                "discrete": spec.discrete,
                "opacity": spec.opacity,
                "source_key": spec.source_key,
                "range_mode": spec.range_mode,
                "stops": [
                    {"value": value, "color": colour}
                    for value, colour in spec.stops
                ],
            }
            for spec in LAYER_SPECS
            if spec.key in self.available_layers
        }
        manifest = {
            "schema_version": MAP_SCHEMA_VERSION,
            "status": "ok",
            "module_version": MODULE_VERSION,
            "generated_at": generated_at,
            "run_time": run_time,
            "projection": "EPSG:3857",
            "bounds": self.bounds,
            "width": self.width,
            "height": self.height,
            "background": "maps/fond.webp",
            "overlay": "maps/frontieres.svg",
            "layers": layers,
            "steps": self.steps,
        }
        if places_path:
            manifest["places"] = places_path
        manifest["fixed_manifest"] = "maps/fixed/index.json"
        destination = self.output_directory / "index.json"
        with destination.open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        fixed_layer_keys = {
            key
            for step in self.fixed_steps
            for key in step.get("files", {})
        }
        fixed_layers = {
            spec.key: {
                "label": spec.label,
                "unit": spec.unit,
                "group": spec.group,
                "stops": [
                    {"value": value, "color": colour}
                    for value, colour in spec.stops
                ],
            }
            for spec in LAYER_SPECS
            if spec.key in fixed_layer_keys
        }
        if FIXED_TEMPERATURE_VALUES_KEY in fixed_layer_keys:
            temperature_spec = next(
                spec for spec in LAYER_SPECS if spec.key == "temperature"
            )
            fixed_layers[FIXED_TEMPERATURE_VALUES_KEY] = {
                "label": "Température à 2 m avec valeurs",
                "unit": temperature_spec.unit,
                "group": "Températures",
                "stops": [
                    {"value": value, "color": colour}
                    for value, colour in temperature_spec.stops
                ],
            }
        fixed_manifest = {
            "schema_version": 1,
            "status": "ok" if self.fixed_steps else "unavailable",
            "module_version": MODULE_VERSION,
            "generated_at": generated_at,
            "run_time": run_time,
            "region": "france",
            "coverage": "France métropolitaine et Corse",
            "bounds": FIXED_MAP_BOUNDS,
            "layers": fixed_layers,
            "steps": self.fixed_steps,
        }
        fixed_directory = self.output_directory / "fixed"
        fixed_directory.mkdir(parents=True, exist_ok=True)
        with (fixed_directory / "index.json").open("w", encoding="utf-8") as handle:
            json.dump(
                fixed_manifest,
                handle,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            handle.write("\n")
        return manifest
