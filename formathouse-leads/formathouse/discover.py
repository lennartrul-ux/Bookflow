"""Fase 1 — Discovery.

Twee bronnen, dezelfde voorfilter en ontdubbeling:
- `google`: Google Places API (New), Text Search — vereist GOOGLE_PLACES_API_KEY;
- `osm`: OpenStreetMap via de publieke Overpass API — geen sleutel nodig,
  maar zonder Google-rating/reviews (die blijven 'onbekend').

Per (bron × sectorgroep × zoekterm × stad) wordt een query bijgehouden in
`discovery_queries`, zodat een onderbroken run exact verder kan.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Callable

import requests

from . import db
from .briefing import CITIES, DEFAULT_REGION, GROUPS, REGIONS, City, SectorGroup, in_region
from .httpclient import LoggedClient
from .normalize import company_domain, norm_text, social_platform

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
MAX_PAGES = 3  # Text Search geeft maximaal 60 resultaten (3 × 20)
FIELD_MASK = ",".join([
    "places.id", "places.displayName", "places.formattedAddress",
    "places.addressComponents", "places.websiteUri", "places.primaryType",
    "places.primaryTypeDisplayName", "places.types", "places.rating",
    "places.userRatingCount", "places.priceLevel", "places.businessStatus",
    "places.nationalPhoneNumber", "places.googleMapsUri", "nextPageToken",
])
PRICE_LEVELS = {
    "PRICE_LEVEL_FREE": "gratis", "PRICE_LEVEL_INEXPENSIVE": "€",
    "PRICE_LEVEL_MODERATE": "€€", "PRICE_LEVEL_EXPENSIVE": "€€€",
    "PRICE_LEVEL_VERY_EXPENSIVE": "€€€€",
}

# OpenStreetMap-tags per sectorgroep (één Overpass-query per groep × stad).
OSM_FILTERS: dict[str, list[str]] = {
    "horeca": [
        '["amenity"~"^(restaurant|bar|cafe|pub|biergarten)$"]',
        '["tourism"~"^(hotel|guest_house)$"]',
    ],
    "fitness": [
        '["leisure"~"^(fitness_centre|sports_centre)$"]',
        '["sport"~"^(pilates|yoga|crossfit|fitness)$"]',
        '["shop"~"^(beauty|massage)$"]',
        '["amenity"~"^(spa|public_bath)$"]',
        '["leisure"~"^(spa|sauna)$"]',
    ],
    "lifestyle": [
        '["shop"~"^(clothes|boutique|fashion|shoes|jewelry|interior_decoration|'
        'cosmetics|perfumery|bag|fashion_accessories|houseware)$"]',
    ],
    "overig": [
        '["office"~"^(estate_agent|employment_agency|event_management)$"]',
        '["shop"="car"]',
    ],
}
OSM_LABELS = {
    "restaurant": "Restaurant", "bar": "Bar", "cafe": "Café", "pub": "Café",
    "biergarten": "Biergarten", "hotel": "Hotel", "guest_house": "Gastenverblijf",
    "fitness_centre": "Fitnesscentrum", "sports_centre": "Sportcentrum",
    "beauty": "Schoonheidssalon", "massage": "Massagesalon", "spa": "Spa",
    "sauna": "Sauna", "public_bath": "Wellness", "clothes": "Kledingwinkel",
    "boutique": "Boetiek", "shoes": "Schoenenwinkel", "jewelry": "Juwelier",
    "interior_decoration": "Interieurwinkel", "cosmetics": "Cosmeticawinkel",
    "perfumery": "Parfumerie", "estate_agent": "Vastgoedkantoor",
    "employment_agency": "Uitzend-/recruitmentbureau", "car": "Autodealer",
    "event_management": "Eventbureau",
}


class MissingApiKey(RuntimeError):
    pass


@dataclass
class Candidate:
    """Bron-onafhankelijke kandidaat."""
    source: str                 # google / osm
    source_id: str              # bv. 'ChIJ…' of 'osm:node/123'
    source_url: str             # link naar de bronvermelding
    name: str
    address: str | None = None
    city: str | None = None
    postal_code: str | None = None
    country: str | None = None
    website: str | None = None
    category: str | None = None
    rating: float | None = None
    reviews: int | None = None
    price_level: str | None = None
    phone: str | None = None
    operational: bool = True
    is_chain_tag: bool = False  # OSM 'brand'-tag
    socials: dict[str, str] = field(default_factory=dict)


@dataclass
class RunStats:
    calls: int = 0
    seen: int = 0
    new_accepted: int = 0
    new_rejected: int = 0
    duplicates: int = 0
    rejected_reasons: dict[str, int] = field(default_factory=dict)


# ── Google Places ────────────────────────────────────────────────────

def _component(place: dict, kind: str, short: bool = False) -> str | None:
    for c in place.get("addressComponents", []):
        if kind in c.get("types", []):
            return c.get("shortText" if short else "longText")
    return None


def from_google(place: dict) -> Candidate:
    return Candidate(
        source="google",
        source_id=place["id"],
        source_url=place.get("googleMapsUri")
            or f"https://www.google.com/maps/place/?q=place_id:{place['id']}",
        name=(place.get("displayName") or {}).get("text") or "?",
        address=place.get("formattedAddress"),
        city=_component(place, "locality") or _component(place, "postal_town"),
        postal_code=_component(place, "postal_code"),
        country=_component(place, "country", short=True),
        website=place.get("websiteUri"),
        category=(place.get("primaryTypeDisplayName") or {}).get("text") or place.get("primaryType"),
        rating=place.get("rating"),
        reviews=place.get("userRatingCount"),
        price_level=PRICE_LEVELS.get(place.get("priceLevel") or ""),
        phone=place.get("nationalPhoneNumber"),
        operational=place.get("businessStatus", "OPERATIONAL") == "OPERATIONAL",
    )


def google_page(client: LoggedClient, api_key: str, term: str, city: City,
                page_token: str | None = None) -> tuple[list[Candidate], str | None]:
    body = {
        "textQuery": f"{term} in {city.name}",
        "languageCode": "nl",
        "regionCode": city.country,
        "pageSize": 20,
        "locationBias": {"circle": {
            "center": {"latitude": city.lat, "longitude": city.lng},
            "radius": float(city.radius_m),
        }},
    }
    if page_token:
        body["pageToken"] = page_token
    resp = client.request(
        "POST", PLACES_URL, json=body,
        headers={"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELD_MASK},
        log_params={k: v for k, v in body.items() if k != "pageToken"}
                   | ({"page": "vervolg"} if page_token else {}),
        summarize=lambda r: f"{len(r.json().get('places', []))} plaatsen",
    )
    resp.raise_for_status()
    data = resp.json()
    return [from_google(p) for p in data.get("places", [])], data.get("nextPageToken")


# ── OpenStreetMap / Overpass ─────────────────────────────────────────

def overpass_query(group_key: str, city: City) -> str:
    around = f"(around:{city.radius_m},{city.lat},{city.lng})"
    parts = []
    for f in OSM_FILTERS[group_key]:
        for site_key in ("website", "contact:website"):
            parts.append(f'nwr{f}["name"]["{site_key}"]{around};')
    return f"[out:json][timeout:120];({''.join(parts)});out center tags;"


def from_osm(el: dict) -> Candidate:
    t = el.get("tags", {})
    kind = next((t[k] for k in ("amenity", "tourism", "leisure", "shop", "office", "sport")
                 if k in t), None)
    street = " ".join(x for x in (t.get("addr:street"), t.get("addr:housenumber")) if x)
    city = t.get("addr:city")
    postal = t.get("addr:postcode")
    address = ", ".join(x for x in (street, " ".join(y for y in (postal, city) if y)) if x) or None
    socials = {}
    for key, platform in (("contact:instagram", "instagram"), ("contact:tiktok", "tiktok"),
                          ("contact:linkedin", "linkedin")):
        v = t.get(key)
        if v:
            socials[platform] = v if v.startswith("http") else None
            if platform == "instagram" and not v.startswith("http"):
                socials[platform] = f"https://www.instagram.com/{v.lstrip('@')}/"
    return Candidate(
        source="osm",
        source_id=f"osm:{el['type']}/{el['id']}",
        source_url=f"https://www.openstreetmap.org/{el['type']}/{el['id']}",
        name=t.get("name", "?"),
        address=address,
        city=city,
        postal_code=postal,
        country=(t.get("addr:country") or "BE").upper(),
        website=t.get("website") or t.get("contact:website"),
        category=OSM_LABELS.get(kind or "", kind),
        phone=t.get("phone") or t.get("contact:phone"),
        operational=not any(k.startswith(("disused:", "was:")) for k in t),
        is_chain_tag=bool(t.get("brand") or t.get("brand:wikidata")),
        socials={k: v for k, v in socials.items() if v},
    )


def _osm_richness(c: Candidate) -> int:
    """Volgorde binnen één OSM-antwoord: eerst zaken met meer zichtbare aanwezigheid."""
    return 2 * bool(c.socials) + bool(c.phone) + bool(c.address)


def osm_fetch(client: LoggedClient, group_key: str, city: City) -> list[Candidate]:
    q = overpass_query(group_key, city)
    resp = client.request(
        "POST", OVERPASS_URL, data={"data": q}, timeout=180,
        log_params={"groep": group_key, "stad": city.name, "radius_m": city.radius_m},
        summarize=lambda r: f"{len(r.json().get('elements', []))} elementen",
    )
    resp.raise_for_status()
    cands = [from_osm(e) for e in resp.json().get("elements", [])]
    return sorted(cands, key=_osm_richness, reverse=True)


# ── Voorfilter en opslag ─────────────────────────────────────────────

def evaluate(c: Candidate, group: SectorGroup, region: str = DEFAULT_REGION,
             search_city: City | None = None) -> tuple[bool, str | None]:
    """Transparante voorfilter. Geeft (geaccepteerd, reden_van_afwijzing)."""
    if c.postal_code or c.source == "google":
        if not in_region(region, c.country, c.postal_code):
            return False, f"buiten werkgebied ({region})"
    elif not (search_city and search_city.region in REGIONS[region]):
        return False, f"buiten werkgebied ({region})"
    if not c.operational:
        return False, "niet operationeel volgens de bron"
    low = f" {c.name.lower()} "
    if c.is_chain_tag or any(p in low for p in group.exclude_name_patterns):
        return False, "grote keten (buiten profiel 5–150 medewerkers)"
    if not c.website:
        return False, "geen website in bronprofiel"
    if c.source == "google" and (c.reviews or 0) < group.min_reviews:
        return False, f"minder dan {group.min_reviews} reviews"
    return True, None


def _find_existing(con, *, source_id: str, domain: str | None, norm_name: str,
                   norm_city: str) -> sqlite3.Row | None:
    q = [("place_id = ?", source_id)]
    if domain:
        q.append(("domain = ?", domain))
    for cond, val in q:
        row = con.execute(f"SELECT * FROM companies WHERE {cond}", (val,)).fetchone()
        if row:
            return row
    if norm_name and norm_city:
        return con.execute("SELECT * FROM companies WHERE norm_name=? AND norm_city=?",
                           (norm_name, norm_city)).fetchone()
    return None


def ingest(con: sqlite3.Connection, cand: Candidate | dict, group: SectorGroup, term: str,
           search_city: City, region: str = DEFAULT_REGION) -> tuple[int, str]:
    """Sla één kandidaat op. Geeft (company_id, 'nieuw'|'afgewezen'|'dubbel')."""
    c = from_google(cand) if isinstance(cand, dict) else cand
    domain = company_domain(c.website)
    city = c.city or (search_city.name if c.source == "osm" and not c.postal_code else None)
    nn, nc = norm_text(c.name), norm_text(c.city)

    existing = _find_existing(con, source_id=c.source_id, domain=domain, norm_name=nn,
                              norm_city=nc)
    if existing:
        con.execute("UPDATE companies SET domain=COALESCE(domain, ?), updated_at=? WHERE id=?",
                    (domain, db.now(), existing["id"]))
        con.commit()
        return existing["id"], "dubbel"

    accepted, reason = evaluate(c, group, region, search_city)
    ts = db.now()
    cur = con.execute(
        """INSERT INTO companies(name, norm_name, city, norm_city, country, sector_group,
               domain, place_id, search_city, excluded, exclude_reason, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (c.name, nn, city, nc, c.country, group.key, domain, c.source_id, search_city.name,
         0 if accepted else 1, reason, ts, ts),
    )
    cid = cur.lastrowid

    def fact(fld: str, value):
        if value in (None, ""):
            db.set_field(con, cid, fld, None, "onbekend", verified_at=ts)
        else:
            db.set_field(con, cid, fld, value, "feit", source_url=c.source_url, verified_at=ts)

    fact("bedrijfsnaam", c.name)
    fact("adres", c.address)
    fact("postcode", c.postal_code)
    if c.city:
        fact("stad", c.city)
    elif city:
        db.set_field(con, cid, "stad", city, "inschatting", verified_at=ts,
                     note=f"geen adres in OpenStreetMap; gevonden binnen 15 km van {city}")
    else:
        fact("stad", None)
    fact("land", c.country)
    fact("bron_vermelding", c.source_url)
    fact("google_maps_url", c.source_url if c.source == "google" else None)
    fact("google_categorie" if c.source == "google" else "categorie", c.category)
    fact("google_rating", c.rating)
    fact("google_reviews", c.reviews)
    fact("prijsniveau", c.price_level)
    fact("algemeen_telefoon", c.phone)

    platform = social_platform(c.website)
    if platform in ("instagram", "tiktok", "linkedin"):
        fact(platform, c.website)  # bronprofiel verwijst naar socials i.p.v. eigen website
        fact("website", None)
    else:
        fact("website", c.website if domain else None)
    for plat, url in c.socials.items():
        fact(plat, url)

    db.set_field(con, cid, "sector", group.label, "inschatting", verified_at=ts,
                 note=f"afgeleid uit zoekopdracht '{term}' en categorie '{c.category or '?'}'")
    con.commit()
    return cid, "nieuw" if accepted else "afgewezen"


# ── Orchestratie ─────────────────────────────────────────────────────

def accepted_count(con: sqlite3.Connection, group_key: str) -> int:
    return con.execute("SELECT COUNT(*) FROM companies WHERE sector_group=? AND excluded=0",
                       (group_key,)).fetchone()[0]


def _query_row(con, group: SectorGroup, term: str, city: City) -> sqlite3.Row:
    con.execute("INSERT OR IGNORE INTO discovery_queries(sector_group, term, city) VALUES (?,?,?)",
                (group.key, term, city.name))
    return con.execute(
        "SELECT * FROM discovery_queries WHERE sector_group=? AND term=? AND city=?",
        (group.key, term, city.name)).fetchone()


def _select(group_keys, city_names, region):
    if region not in REGIONS:
        raise ValueError(f"onbekende REGIO {region!r}; kies uit {', '.join(REGIONS)}")
    groups = [GROUPS[k] for k in (group_keys or [k for k, g in GROUPS.items() if g.quota > 0])]
    wanted = {c.lower() for c in city_names} if city_names else None
    cities = [c for c in CITIES if c.region in REGIONS[region]
              and (wanted is None or c.name.lower() in wanted)]
    if wanted and not cities:
        raise ValueError(f"stad niet gevonden binnen werkgebied {region}: {', '.join(city_names)}")
    return groups, cities


def run(con: sqlite3.Connection, client: LoggedClient, api_key: str, *,
        source: str = "google", group_keys: list[str] | None = None,
        city_names: list[str] | None = None, limit: int | None = None,
        region: str = DEFAULT_REGION, log: Callable = print) -> RunStats:
    """Zoek kandidaten tot de quota (of `limit` nieuwe leads in deze run) bereikt zijn."""
    if source == "google" and not api_key:
        raise MissingApiKey("GOOGLE_PLACES_API_KEY ontbreekt in .env (of gebruik --bron osm)")
    groups, cities = _select(group_keys, city_names, region)
    stats = RunStats()

    def full(group: SectorGroup) -> bool:
        quota = group.quota if group.quota > 0 else (limit or 0)
        return (limit is not None and stats.new_accepted >= limit) or \
            accepted_count(con, group.key) >= quota

    def take(cands: list[Candidate], group, term, city) -> int:
        acc = 0
        for cand in cands:
            if full(group):
                break
            stats.seen += 1
            cid, outcome = ingest(con, cand, group, term, city, region)
            if outcome == "nieuw":
                stats.new_accepted += 1
                acc += 1
            elif outcome == "afgewezen":
                stats.new_rejected += 1
                r = con.execute("SELECT exclude_reason FROM companies WHERE id=?", (cid,)).fetchone()[0]
                stats.rejected_reasons[r] = stats.rejected_reasons.get(r, 0) + 1
            else:
                stats.duplicates += 1
        return acc

    for group in groups:
        for city in cities:
            if source == "osm":
                if full(group):
                    break
                term = "osm"
                q = _query_row(con, group, term, city)
                if q["status"] == "done":
                    continue
                cands = osm_fetch(client, group.key, city)
                stats.calls += 1
                acc = take(cands, group, term, city)
                # 'done' pas als alle kandidaten verwerkt zijn; anders bij hervatten opnieuw
                # ophalen (ontdubbeling vangt de reeds verwerkte op).
                status = "open" if full(group) else "done"
                con.execute("""UPDATE discovery_queries SET pages_done=1, status=?,
                               results=?, accepted=accepted+?, last_run=? WHERE id=?""",
                            (status, len(cands), acc, db.now(), q["id"]))
                con.commit()
                log(f"  osm    {group.key:9} {city.name:10}: {len(cands)} kandidaten, "
                    f"{acc} nieuw geaccepteerd")
                continue
            for _page in range(MAX_PAGES):
                for term in group.terms:
                    if full(group):
                        break
                    q = _query_row(con, group, term, city)
                    if q["status"] == "done":
                        continue
                    try:
                        cands, token = google_page(client, api_key, term, city, q["next_page_token"])
                    except requests.HTTPError:
                        if not q["next_page_token"]:
                            raise
                        # verlopen paginatoken: opnieuw vanaf pagina 1 (ontdubbeling vangt dit op)
                        cands, token = google_page(client, api_key, term, city)
                        q = dict(q) | {"pages_done": 0}
                    stats.calls += 1
                    acc = take(cands, group, term, city)
                    pages = q["pages_done"] + 1
                    finished = not token or pages >= MAX_PAGES
                    con.execute(
                        """UPDATE discovery_queries SET pages_done=?, next_page_token=?, status=?,
                               results=results+?, accepted=accepted+?, last_run=? WHERE id=?""",
                        (pages, None if finished else token, "done" if finished else "open",
                         len(cands), acc, db.now(), q["id"]))
                    con.commit()
                    log(f"  google {group.key:9} {city.name:10} '{term}' p{pages}: "
                        f"{len(cands)} resultaten, {acc} nieuw geaccepteerd")
        if limit is not None and stats.new_accepted >= limit:
            break
    return stats
