"""Fase 3 — Social-analyse, uitsluitend via officiële Meta API's.

- Instagram Graph API, Business Discovery: volgers, aantal posts, recente media
  (timestamp + media_product_type) → Reels laatste 30 dagen, postfrequentie.
- Meta Ad Library API: actieve advertenties (EU). Geen eenduidige match = 'onbekend'.
TikTok en LinkedIn: alleen de profiel-URL (uit Fase 2). Geen scraping.
"""
from __future__ import annotations

import datetime as dt
import re
import sqlite3
from typing import Callable

from . import db
from .httpclient import LoggedClient
from .normalize import norm_text

GRAPH = "https://graph.facebook.com"
HANDLE_RE = re.compile(r"instagram\.com/([A-Za-z0-9_.]{2,30})", re.I)


def handle_from_url(url: str | None) -> str | None:
    m = HANDLE_RE.search(url or "")
    return m.group(1).lower() if m else None


def business_discovery(client: LoggedClient, *, version: str, ig_user_id: str, token: str,
                       handle: str) -> dict | None:
    fields = (f"business_discovery.username({handle})"
              "{username,name,website,biography,followers_count,media_count,"
              "media.limit(50){timestamp,media_product_type,media_type,permalink}}")
    r = client.request(
        "GET", f"{GRAPH}/{version}/{ig_user_id}",
        params={"fields": fields, "access_token": token},
        log_params={"endpoint": "business_discovery", "handle": handle},
        summarize=lambda r: "ok",
    )
    if r.status_code >= 400:
        return None  # bv. persoonlijk account (geen business/creator) of niet gevonden
    return r.json().get("business_discovery")


def analyse_media(bd: dict, today: dt.date | None = None) -> dict:
    today = today or dt.datetime.now(dt.timezone.utc).date()
    since = today - dt.timedelta(days=30)
    media = (bd.get("media") or {}).get("data", [])
    stamps = []
    reels_30 = posts_30 = 0
    for m in media:
        try:
            d = dt.datetime.strptime(m["timestamp"][:19], "%Y-%m-%dT%H:%M:%S").date()
        except (KeyError, ValueError):
            continue
        stamps.append(d)
        if d >= since:
            posts_30 += 1
            if m.get("media_product_type") == "REELS":
                reels_30 += 1
    last = max(stamps) if stamps else None
    per_week = None
    if len(stamps) >= 2:
        span = max((max(stamps) - min(stamps)).days, 1)
        per_week = round(len(stamps) / span * 7, 1)
    gaps = sorted((a - b).days for a, b in zip(sorted(stamps, reverse=True), sorted(stamps, reverse=True)[1:]))
    irregular = bool(gaps) and gaps[-1] >= 21  # langste pauze ≥ 3 weken in recente reeks
    return {
        "followers": bd.get("followers_count"),
        "media_count": bd.get("media_count"),
        "posts_30d": posts_30,
        "reels_30d": reels_30,
        "last_post": last.isoformat() if last else None,
        "posts_per_week": per_week,
        "irregular": irregular,
        "active_30d": bool(last and last >= since),
        "sample": len(stamps),
    }


def use_profile(con: sqlite3.Connection, cid: int, bd: dict, src: str, log: Callable) -> None:
    """Website en algemeen e-mailadres uit het officiële Instagram-bedrijfsprofiel halen.
    Krijgt het bedrijf zo een (nieuwe) website, dan wordt die bij de volgende
    verrijkingsronde bezocht om daar het e-mailadres te zoeken."""
    from .enrich import EMAIL_RE, is_generic_email
    from .normalize import company_domain
    note = "uit het Instagram-bedrijfsprofiel (Graph API)"
    site = (bd.get("website") or "").strip()
    if site and company_domain(site):
        db.set_field(con, cid, "ig_website", site, "feit", source_url=src, note=note)
        if not db.value(con, cid, "website"):
            db.set_field(con, cid, "website", site, "feit", source_url=src, note=note)
            con.execute("UPDATE companies SET domain=? WHERE id=?", (company_domain(site), cid))
            con.execute("DELETE FROM pipeline WHERE company_id=? AND step='enrich'", (cid,))
            log(f"    website gevonden via Instagram: {site}")
    if not db.value(con, cid, "algemeen_email"):
        for e in EMAIL_RE.findall(bd.get("biography") or ""):
            e = e.strip(".").lower()
            if is_generic_email(e):
                db.set_field(con, cid, "algemeen_email", e, "feit", source_url=src, note=note)
                db.set_field(con, cid, "email_bron", src, "feit", source_url=src, note=note)
                log(f"    e-mail gevonden in Instagram-bio: {e}")
                break


def ad_library(client: LoggedClient, *, version: str, token: str, name: str,
               country: str = "BE") -> tuple[str, str] | None:
    """Geeft (waarde, toelichting) bij een eenduidige paginanaam-match, anders None."""
    r = client.request(
        "GET", f"{GRAPH}/{version}/ads_archive",
        params={"search_terms": name, "ad_reached_countries": f'["{country}"]',
                "ad_active_status": "ACTIVE", "ad_type": "ALL",
                "fields": "page_id,page_name,ad_delivery_start_time", "limit": 50,
                "access_token": token},
        log_params={"endpoint": "ads_archive", "search_terms": name, "land": country},
        summarize=lambda r: f"{len(r.json().get('data', []))} advertenties",
    )
    if r.status_code >= 400:
        return None
    target = norm_text(name)
    ads = [a for a in r.json().get("data", []) if norm_text(a.get("page_name")) == target]
    pages = {a.get("page_id") for a in ads}
    if len(pages) == 1 and ads:
        return (f"ja — {len(ads)} actieve advertentie(s)",
                f"pagina '{ads[0].get('page_name')}' (id {ads[0].get('page_id')})")
    return None  # geen of meerdere kandidaten: niet als 'nee' rapporteren


def ad_library_url(name: str, country: str = "BE") -> str:
    from urllib.parse import quote
    return ("https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
            f"&country={country}&q={quote(name)}")


def run(con: sqlite3.Connection, client: LoggedClient | None, *, version: str, ig_user_id: str,
        token: str, limit: int | None = None, redo: bool = False,
        log: Callable = print) -> dict[str, int]:
    stats = {"instagram": 0, "geen_ig": 0, "advertenties": 0, "zonder_token": 0}
    have_token = bool(token and ig_user_id and client)
    for c in db.pending(con, "social", redo=redo, limit=limit):
        cid = c["id"]
        ts = db.now()
        handle = handle_from_url(db.value(con, cid, "instagram"))
        if handle:
            db.set_field(con, cid, "instagram_handle", handle, "feit",
                         source_url=f"https://www.instagram.com/{handle}/", verified_at=ts)
        if not have_token:
            for fld in ("ig_volgers", "reels_30d", "posts_30d", "contentactiviteit", "advertenties"):
                db.set_field(con, cid, fld, None, "onbekend", verified_at=ts)
            db.set_field(con, cid, "advertenties_check_url", ad_library_url(c["name"]), "feit",
                         source_url=ad_library_url(c["name"]), verified_at=ts,
                         note="link voor handmatige controle in de Meta Ad Library")
            stats["zonder_token"] += 1
            db.mark_step(con, cid, "social", "overgeslagen", "geen Meta-token")
            continue

        if handle:
            bd = business_discovery(client, version=version, ig_user_id=ig_user_id,
                                    token=token, handle=handle)
            if bd:
                a = analyse_media(bd)
                src = f"https://www.instagram.com/{handle}/"
                note = "Instagram Graph API (Business Discovery)"
                use_profile(con, cid, bd, src, log)
                db.set_field(con, cid, "ig_volgers", a["followers"], "feit", source_url=src, note=note)
                db.set_field(con, cid, "ig_posts_totaal", a["media_count"], "feit", source_url=src, note=note)
                db.set_field(con, cid, "reels_30d", a["reels_30d"], "feit", source_url=src,
                             note=f"{note}; o.b.v. de laatste {a['sample']} posts")
                db.set_field(con, cid, "posts_30d", a["posts_30d"], "feit", source_url=src, note=note)
                db.set_field(con, cid, "laatste_post", a["last_post"], "feit", source_url=src, note=note)
                freq = (f"{a['posts_per_week']} posts/week" if a["posts_per_week"] is not None else "?")
                db.set_field(
                    con, cid, "contentactiviteit",
                    f"{'actief' if a['active_30d'] else 'niet actief'} laatste 30 d; {freq}; "
                    f"{a['reels_30d']} Reels/30 d" + ("; onregelmatig" if a["irregular"] else ""),
                    "feit", source_url=src, note=note)
                signal = a["active_30d"] and a["reels_30d"] <= 8
                if signal or a["irregular"]:
                    db.set_field(con, cid, "koopsignaal",
                                 "; ".join(x for x in (
                                     "actief maar 0–8 Reels/maand" if signal else "",
                                     "onregelmatig posten" if a["irregular"] else "") if x),
                                 "inschatting", note="regel uit briefing: actief + weinig Reels of inconsistent")
                stats["instagram"] += 1
            else:
                for fld in ("ig_volgers", "reels_30d", "posts_30d", "contentactiviteit"):
                    db.set_field(con, cid, fld, None, "onbekend", verified_at=ts,
                                 note="Business Discovery gaf geen resultaat (bv. geen zakelijk account)")
        else:
            stats["geen_ig"] += 1
            for fld in ("ig_volgers", "reels_30d", "posts_30d", "contentactiviteit"):
                db.set_field(con, cid, fld, None, "onbekend", verified_at=ts)

        ads = ad_library(client, version=version, token=token, name=c["name"])
        if ads:
            db.set_field(con, cid, "advertenties", ads[0], "feit",
                         source_url=ad_library_url(c["name"]), note=f"Meta Ad Library API; {ads[1]}")
            stats["advertenties"] += 1
        else:
            db.set_field(con, cid, "advertenties", None, "onbekend", verified_at=ts,
                         note="geen eenduidige match in de Meta Ad Library")
        db.mark_step(con, cid, "social", "ok")
        log(f"  ✓ {c['name']}: IG {'@' + handle if handle else '—'}, advertenties: "
            f"{ads[0] if ads else 'onbekend'}")
    con.commit()
    return stats
