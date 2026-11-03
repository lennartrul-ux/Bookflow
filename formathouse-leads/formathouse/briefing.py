"""Klantprofiel van The Format House, vertaald naar zoekconfiguratie.

Alles hier is aanpasbaar: zoektermen, steden, quota en voorfilters.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class City:
    name: str
    country: str  # ISO: BE / NL
    lat: float
    lng: float
    radius_m: int = 15000  # "+ omgeving"


# Volgorde = prioriteit: eerst België, dan Nederland.
CITIES: list[City] = [
    City("Antwerpen", "BE", 51.2194, 4.4025),
    City("Gent", "BE", 51.0543, 3.7174),
    City("Mechelen", "BE", 51.0259, 4.4776),
    City("Brussel", "BE", 50.8503, 4.3517),
    City("Leuven", "BE", 50.8798, 4.7005),
    City("Hasselt", "BE", 50.9307, 5.3325),
    City("Brugge", "BE", 51.2093, 3.2247),
    City("Breda", "NL", 51.5719, 4.7683),
    City("Eindhoven", "NL", 51.4416, 5.4697),
    City("Rotterdam", "NL", 51.9244, 4.4777),
    City("Utrecht", "NL", 52.0907, 5.1214),
    City("Amsterdam", "NL", 52.3676, 4.9041),
]


@dataclass(frozen=True)
class SectorGroup:
    key: str
    label: str
    quota: int
    terms: list[str]
    # Voorfilter: minimaal aantal Google-reviews als signaal van commerciële
    # activiteit/zichtbaarheid. Geen oordeel over kwaliteit; instelbaar.
    min_reviews: int
    exclude_name_patterns: list[str] = field(default_factory=list)


# Grote ketens vallen buiten het profiel (5–150 medewerkers, eigen marketingteams).
_CHAINS = [
    "mcdonald", "burger king", "kfc", "quick", "starbucks", "domino", "subway",
    "pizza hut", "exki", "panos", "le pain quotidien", "lunch garden", "o'tacos",
    "basic-fit", "basic fit", "jims", "anytime fitness", "trainmore",
    "ici paris", "kruidvat", "douglas", "zara", "h&m", "primark", "ibis",
    "novotel", "nh ", "holiday inn", "mercure", "radisson", "hilton", "marriott",
]

GROUPS: dict[str, SectorGroup] = {
    "horeca": SectorGroup(
        key="horeca",
        label="Horeca / hospitality",
        quota=120,
        terms=[
            "trendy restaurant", "brasserie", "cocktailbar", "brunch restaurant",
            "rooftop bar", "boutique hotel", "horecagroep", "fine dining restaurant",
        ],
        min_reviews=100,
        exclude_name_patterns=_CHAINS,
    ),
    "fitness": SectorGroup(
        key="fitness",
        label="Fitness / wellness / beauty",
        quota=105,
        terms=[
            "premium gym", "fitnessclub", "pilates studio", "reformer pilates",
            "boutique fitness studio", "wellness centrum", "esthetisch centrum",
            "schoonheidsinstituut",
        ],
        min_reviews=25,
        exclude_name_patterns=_CHAINS,
    ),
    "lifestyle": SectorGroup(
        key="lifestyle",
        label="Lifestyle / consumer brands",
        quota=75,
        terms=[
            "conceptstore", "fashion boutique", "kledingmerk winkel",
            "interieur conceptstore", "cosmeticamerk", "lifestyle brand store",
        ],
        min_reviews=15,
        exclude_name_patterns=_CHAINS,
    ),
    # Alleen mee als ze een duidelijke terugkerende contentbehoefte hebben:
    # quota 0 = niet standaard meegenomen, alleen met --group overig.
    "overig": SectorGroup(
        key="overig",
        label="Overige (vastgoed, automotive, recruitment, events)",
        quota=0,
        terms=[
            "vastgoedontwikkelaar", "premium autodealer", "recruitmentbureau",
            "eventbureau",
        ],
        min_reviews=15,
        exclude_name_patterns=_CHAINS,
    ),
}
