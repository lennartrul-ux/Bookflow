"""Fase 1 — Discovery via Google Places API (New), Text Search.

Per (sectorgroep × zoekterm × stad) wordt een query bijgehouden in
`discovery_queries`, zodat een onderbroken run exact verder kan.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import requests

from . import db
from .briefing import CITIES, GROUPS, City, SectorGroup
from .httpclient import LoggedClient
from .normalize import (
    company_domain, norm_enterprise_number, norm_text, social_platform,
)

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
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


class MissingApiKey(RuntimeError):
    pass


@dataclass
class RunStats:
    calls: int = 0
    seen: int = 0
    new_accepted: int = 0
    new_rejected: int = 0
    duplicates: int = 0
    rejected_reasons: dict[str, int] = field(default_factory=dict)


# ── Places API ───────────────────────────────────────────────────────

def search_page(client: LoggedClient, api_key: str, term: str, city: City,
                page_token: str | None = None) -> tuple[list[dict], str | None]:
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
        "POST", PLACES_URL,
        json=body,
        headers={"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELD_MASK},
        log_params={k: v for k, v in body.items() if k != "pageToken"}
                   | ({"page": "vervolg"} if page_token else {}),
        summarize=lambda r: f"{len(r.json().get('places', []))} plaatsen",
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("places", []), data.get("nextPageToken")


# ── Interpretatie van één resultaat ──────────────────────────────────

def _component(place: dict, kind: str, short: bool = False) -> str | None:
    for c in place.get("addressComponents", []):
        if kind in c.get("types", []):
            return c.get("shortText" if short else "longText")
    return None


def evaluate(place: dict, group: SectorGroup) -> tuple[bool, str | None]:
    """Transparante voorfilter. Geeft (geaccepteerd, reden_van_afwijzing)."""
    name = (place.get("displayName") or {}).get("text", "")
    country = _component(place, "country", short=True)
    if country not in ("BE", "NL"):
        return False, "buiten BE/NL"
    if place.get("businessStatus", "OPERATIONAL") != "OPERATIONAL":
        return False, "niet operationeel volgens Google"
    low = f" {name.lower()} "
    for pat in group.exclude_name_patterns:
        if pat in low:
            return False, "grote keten (buiten profiel 5–150 medewerkers)"
    if not place.get("websiteUri"):
        return False, "geen website in Google-profiel"
    reviews = place.get("userRatingCount") or 0
    if reviews < group.min_reviews:
        return False, f"minder dan {group.min_reviews} reviews"
    return True, None


def _find_existing(con: sqlite3.Connection, *, place_id: str, domain: str | None,
                   ent_no: str | None, norm_name: str, norm_city: str) -> sqlite3.Row | None:
    q = [("place_id = ?", place_id)]
    if domain:
        q.append(("domain = ?", domain))
    if ent_no:
        q += [("kbo = ?", ent_no), ("kvk = ?", ent_no)]
    for cond, val in q:
        row = con.execute(f"SELECT * FROM companies WHERE {cond}", (val,)).fetchone()
        if row:
            return row
    if norm_name and norm_city:
        return con.execute(
            "SELECT * FROM companies WHERE norm_name=? AND norm_city=?",
            (norm_name, norm_city),
        ).fetchone()
    return None


def ingest(con: sqlite3.Connection, place: dict, group: SectorGroup, term: str,
           search_city: City) -> tuple[int, str]:
    """Sla één Places-resultaat op. Geeft (company_id, 'nieuw'|'afgewezen'|'dubbel')."""
    name = (place.get("displayName") or {}).get("text") or "?"
    city = _component(place, "locality") or _component(place, "postal_town")
    country = _component(place, "country", short=True)
    website = place.get("websiteUri")
    domain = company_domain(website)
    nn, nc = norm_text(name), norm_text(city)

    existing = _find_existing(con, place_id=place["id"], domain=domain, ent_no=None,
                              norm_name=nn, norm_city=nc)
    if existing:
        # Vul eventueel ontbrekende sleutels aan, maar wijzig sector/status niet.
        con.execute(
            "UPDATE companies SET domain=COALESCE(domain, ?), updated_at=? WHERE id=?",
            (domain, db.now(), existing["id"]),
        )
        con.commit()
        return existing["id"], "dubbel"

    accepted, reason = evaluate(place, group)
    ts = db.now()
    cur = con.execute(
        """INSERT INTO companies(name, norm_name, city, norm_city, country, sector_group,
               domain, place_id, search_city, excluded, exclude_reason, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (name, nn, city, nc, country, group.key, domain, place["id"], search_city.name,
         0 if accepted else 1, reason, ts, ts),
    )
    cid = cur.lastrowid
    src = place.get("googleMapsUri") or f"https://www.google.com/maps/place/?q=place_id:{place['id']}"

    def fact(fld: str, value):
        if value in (None, ""):
            db.set_field(con, cid, fld, None, "onbekend", verified_at=ts)
        else:
            db.set_field(con, cid, fld, value, "feit", source_url=src, verified_at=ts)

    fact("bedrijfsnaam", name)
    fact("adres", place.get("formattedAddress"))
    fact("stad", city)
    fact("land", country)
    fact("google_maps_url", place.get("googleMapsUri"))
    fact("google_categorie", (place.get("primaryTypeDisplayName") or {}).get("text")
         or place.get("primaryType"))
    fact("google_rating", place.get("rating"))
    fact("google_reviews", place.get("userRatingCount"))
    fact("prijsniveau", PRICE_LEVELS.get(place.get("priceLevel") or ""))
    fact("algemeen_telefoon", place.get("nationalPhoneNumber"))

    platform = social_platform(website)
    if platform in ("instagram", "tiktok", "linkedin"):
        # Google-profiel verwijst naar een socialprofiel i.p.v. een eigen website.
        fact(platform, website)
        fact("website", None)
    else:
        fact("website", website if domain else None)

    db.set_field(
        con, cid, "sector", group.label, "inschatting", verified_at=ts,
        note=f"afgeleid uit zoekterm '{term}' en Google-categorie "
             f"'{(place.get('primaryTypeDisplayName') or {}).get('text', '?')}'",
    )
    con.commit()
    return cid, "nieuw" if accepted else "afgewezen"


# ── Orchestratie ─────────────────────────────────────────────────────

def accepted_count(con: sqlite3.Connection, group_key: str) -> int:
    return con.execute(
        "SELECT COUNT(*) FROM companies WHERE sector_group=? AND excluded=0", (group_key,)
    ).fetchone()[0]


def _query_row(con, group: SectorGroup, term: str, city: City) -> sqlite3.Row:
    con.execute(
        "INSERT OR IGNORE INTO discovery_queries(sector_group, term, city) VALUES (?,?,?)",
        (group.key, term, city.name),
    )
    return con.execute(
        "SELECT * FROM discovery_queries WHERE sector_group=? AND term=? AND city=?",
        (group.key, term, city.name),
    ).fetchone()


def run(con: sqlite3.Connection, client: LoggedClient, api_key: str, *,
        group_keys: list[str] | None = None, city_names: list[str] | None = None,
        limit: int | None = None, log=print) -> RunStats:
    """Zoek kandidaten tot de quota (of `limit` nieuwe leads in deze run) bereikt zijn.

    Volgorde: groepen → steden in prioriteitsvolgorde → per pagina alle zoektermen
    om de beurt (zodat een kleine run een gevarieerde mix oplevert).
    """
    if not api_key:
        raise MissingApiKey("GOOGLE_PLACES_API_KEY ontbreekt in .env")
    groups = [GROUPS[k] for k in (group_keys or [k for k, g in GROUPS.items() if g.quota > 0])]
    wanted = {c.lower() for c in city_names} if city_names else None
    cities = [c for c in CITIES if wanted is None or c.name.lower() in wanted]
    if wanted and not cities:
        raise ValueError(f"onbekende stad: {', '.join(city_names)}")
    stats = RunStats()

    def done() -> bool:
        return limit is not None and stats.new_accepted >= limit

    for group in groups:
        quota = group.quota if group.quota > 0 else (limit or 0)
        for city in cities:
            for _page in range(MAX_PAGES):
                for term in group.terms:
                    if done() or accepted_count(con, group.key) >= quota:
                        break
                    q = _query_row(con, group, term, city)
                    if q["status"] == "done":
                        continue
                    try:
                        places, token = search_page(client, api_key, term, city,
                                                    q["next_page_token"])
                    except requests.HTTPError:
                        if not q["next_page_token"]:
                            raise
                        # Verlopen paginatoken: query opnieuw vanaf pagina 1
                        # (ontdubbeling vangt herhalingen op).
                        places, token = search_page(client, api_key, term, city)
                        q = dict(q) | {"pages_done": 0}
                    stats.calls += 1
                    acc = 0
                    for place in places:
                        stats.seen += 1
                        if done() or accepted_count(con, group.key) >= quota:
                            break
                        _cid, outcome = ingest(con, place, group, term, city)
                        if outcome == "nieuw":
                            stats.new_accepted += 1
                            acc += 1
                        elif outcome == "afgewezen":
                            stats.new_rejected += 1
                            r = con.execute("SELECT exclude_reason FROM companies WHERE id=?",
                                            (_cid,)).fetchone()[0]
                            stats.rejected_reasons[r] = stats.rejected_reasons.get(r, 0) + 1
                        else:
                            stats.duplicates += 1
                    pages = q["pages_done"] + 1
                    finished = not token or pages >= MAX_PAGES
                    con.execute(
                        """UPDATE discovery_queries SET pages_done=?, next_page_token=?, status=?,
                               results=results+?, accepted=accepted+?, last_run=? WHERE id=?""",
                        (pages, None if finished else token, "done" if finished else "open",
                         len(places), acc, db.now(), q["id"]),
                    )
                    con.commit()
                    log(f"  {group.key:9} {city.name:10} '{term}' p{pages}: "
                        f"{len(places)} resultaten, {acc} nieuw geaccepteerd")
        if done():
            break
    return stats
