"""Klantprofiel van The Format House, vertaald naar zoekconfiguratie.

Alles hier is aanpasbaar: zoektermen, steden, quota en voorfilters.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class City:
    name: str
    country: str  # ISO: BE / NL
    region: str   # VL (Vlaanderen) / BXL (Brussel) / NL
    lat: float
    lng: float
    radius_m: int = 15000  # "+ omgeving"


# Volgorde = prioriteit. Welke regio's meedoen, bepaalt REGIO in .env.
CITIES: list[City] = [
    City("Antwerpen", "BE", "VL", 51.2194, 4.4025),
    City("Gent", "BE", "VL", 51.0543, 3.7174),
    City("Mechelen", "BE", "VL", 51.0259, 4.4776),
    City("Brussel", "BE", "BXL", 50.8503, 4.3517),
    City("Leuven", "BE", "VL", 50.8798, 4.7005),
    City("Hasselt", "BE", "VL", 50.9307, 5.3325),
    City("Brugge", "BE", "VL", 51.2093, 3.2247),
    City("Breda", "NL", "NL", 51.5719, 4.7683),
    City("Eindhoven", "NL", "NL", 51.4416, 5.4697),
    City("Rotterdam", "NL", "NL", 51.9244, 4.4777),
    City("Utrecht", "NL", "NL", 52.0907, 5.1214),
    City("Amsterdam", "NL", "NL", 52.3676, 4.9041),
]


# Werkgebied (REGIO in .env) → toegelaten regio's van de zoeksteden.
REGIONS: dict[str, set[str]] = {
    "vlaanderen": {"VL"},
    "belgie": {"VL", "BXL"},
    "be_nl": {"VL", "BXL", "NL"},
}
DEFAULT_REGION = "vlaanderen"


def flemish_postal_code(code: str | None) -> bool:
    """Vlaamse postcodes: 1500–3999 (Vlaams-Brabant, Antwerpen, Limburg)
    en 8000–9999 (West- en Oost-Vlaanderen)."""
    if not code or not code.isdigit() or len(code) != 4:
        return False
    n = int(code)
    return 1500 <= n <= 3999 or 8000 <= n <= 9999


def in_region(region: str, country: str | None, postal_code: str | None) -> bool:
    allowed = REGIONS[region]
    if country == "NL":
        return "NL" in allowed
    if country != "BE":
        return False
    if flemish_postal_code(postal_code):
        return "VL" in allowed
    if postal_code and postal_code.isdigit() and 1000 <= int(postal_code) <= 1299:
        return "BXL" in allowed
    return False  # Wallonië of postcode onbekend


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
