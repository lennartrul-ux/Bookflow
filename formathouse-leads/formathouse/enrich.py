"""Fase 2 — Verrijking via de eigen bedrijfswebsite en KBO Open Data.

Website: homepage + (max.) contact-, over-ons- en vacaturepagina. robots.txt wordt
gerespecteerd (inclusief Crawl-delay), met een rustige vaste pauze per verzoek.
Alleen algemene zakelijke e-mailadressen (info@, hello@ …) worden bewaard.
"""
from __future__ import annotations

import csv
import datetime as dt
import re
import sqlite3
import time
import urllib.robotparser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from . import db
from .httpclient import LoggedClient
from .normalize import company_domain

# Alleen deze lokale delen gelden als 'algemeen zakelijk' (AVG): nooit voornaam@.
GENERIC_LOCALPARTS = [
    "hello", "hallo", "hi", "info", "contact", "welkom", "welcome", "marketing",
    "events", "event", "pers", "press", "office", "studio", "team", "shop", "sales",
    "reservaties", "reservatie", "reservations", "reservation", "booking", "bookings",
    "boeking", "klantendienst", "service", "support", "admin", "algemeen", "mail",
    "communicatie", "partnerships", "collab", "hr", "jobs", "vacatures",
]
# Voorkeur voor outreach (eerste die gevonden wordt).
OUTREACH_PREFERENCE = ["marketing", "hello", "hallo", "hi", "info", "contact", "welkom",
                       "communicatie", "partnerships", "collab", "office", "team", "studio",
                       "events", "event", "pers", "press", "mail", "algemeen"]

PAGE_PATTERNS = {
    "contact": re.compile(r"contact|bereik|route|locati|vind-ons|find-us", re.I),
    "over": re.compile(r"over-ons|over_ons|overons|about|ons-verhaal|our-story|wie-zijn", re.I),
    "vacatures": re.compile(r"vacature|jobs?\b|werken-bij|werkenbij|careers?|join|team-wanted", re.I),
}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BE_ENT_RE = re.compile(r"(?:\bBE\s?|\b)([01]\d{3}|\d{3})[.\s]?(\d{3})[.\s]?(\d{3})\b")
# Adres = straat + huisnummer (+ bus) + postcode + gemeente; zo tellen jaartallen niet mee.
POSTAL_CITY_RE = re.compile(
    r"[A-Za-zÀ-ÿ.'\-]{3,}\s+\d{1,4}[A-Za-z]?(?:\s*(?:bus|b\.|/)\s*\w{1,5})?\s*,?\s+"
    r"(?:BE-?|B-)?([1-9]\d{3})\s+([A-Z][a-zà-ÿ'\-]+(?:[ -][A-Z][a-zà-ÿ'\-]+)?)")
SOCIAL_RES = {
    "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]{2,30})/?", re.I),
    "tiktok": re.compile(r"https?://(?:www\.)?tiktok\.com/@([A-Za-z0-9_.]{2,30})", re.I),
    "linkedin": re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(company|school)/([A-Za-z0-9_\-%.]+)", re.I),
    "facebook": re.compile(r"https?://(?:www\.|m\.)?facebook\.com/([A-Za-z0-9_.\-]{2,})/?", re.I),
}
IG_RESERVED = {"p", "reel", "reels", "explore", "stories", "accounts", "tv", "share", "direct"}
FB_RESERVED = {"sharer", "sharer.php", "share", "dialog", "plugins", "tr", "groups", "events"}
JOB_WORDS = re.compile(r"\b(vacature|we zoeken|wij zoeken|we're hiring|we are hiring|m/v/x|"
                       r"solliciteer|join our team|word deel van ons team|job openings?)\b", re.I)
LAUNCH_WORDS = re.compile(r"\b(nieuwe? (vestiging|locatie|collectie|menu|kaart|studio|zaak)|"
                          r"opening|grand opening|heropening|launch|lancering|pop-?up|event|"
                          r"evenement|workshop|masterclass|nu open|binnenkort open)\b", re.I)


@dataclass
class SiteFindings:
    pages: dict[str, str] = field(default_factory=dict)        # soort → URL
    socials: dict[str, tuple[str, str]] = field(default_factory=dict)  # platform → (url, bron)
    emails: dict[str, str] = field(default_factory=dict)        # e-mail → bron-URL
    enterprise_number: tuple[str, str] | None = None            # (nummer, bron)
    addresses: dict[str, str] = field(default_factory=dict)     # 'postcode stad' → bron
    jobs: tuple[str, str] | None = None                         # (snippet, bron)
    launches: list[tuple[str, str]] = field(default_factory=list)
    blocked_by_robots: bool = False
    errors: list[str] = field(default_factory=list)


def valid_be_enterprise_number(digits: str) -> bool:
    """Controlegetal KBO: laatste 2 cijfers = 97 − (eerste 8 mod 97)."""
    if len(digits) == 9:
        digits = "0" + digits
    if len(digits) != 10 or digits[0] not in "01":
        return False
    return 97 - int(digits[:8]) % 97 == int(digits[8:])


def is_generic_email(email: str) -> bool:
    local = email.split("@", 1)[0].lower()
    return local in GENERIC_LOCALPARTS


def pick_outreach_email(emails: list[str]) -> str | None:
    by_local = {e.split("@")[0].lower(): e for e in emails}
    for pref in OUTREACH_PREFERENCE:
        if pref in by_local:
            return by_local[pref]
    return None


class Crawler:
    """Beleefde crawler: robots.txt, Crawl-delay, vaste pauze, max. 4 pagina's per site."""

    def __init__(self, client: LoggedClient, user_agent: str, delay: float,
                 sleep: Callable = time.sleep):
        self.client = client
        self.ua = user_agent
        self.delay = delay
        self.sleep = sleep
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _robots_for(self, url: str):
        base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.client.request("GET", base + "/robots.txt",
                                        headers={"User-Agent": self.ua}, timeout=15,
                                        log_params={"doel": "robots.txt"})
                if r.status_code in (401, 403):
                    rp.disallow_all = True
                elif r.status_code >= 400:
                    rp.allow_all = True
                else:
                    rp.parse(r.text.splitlines())
            except requests.RequestException:
                rp.allow_all = True  # onbereikbare robots.txt = geen restricties (RFC 9309)
            self._robots[base] = rp
        return self._robots[base]

    def allowed(self, url: str) -> bool:
        return self._robots_for(url).can_fetch(self.ua, url)

    def get(self, url: str) -> tuple[str, str] | None:
        """Geeft (finale URL, html) of None. Respecteert robots.txt en Crawl-delay."""
        if not self.allowed(url):
            return None
        rp = self._robots_for(url)
        delay = max(self.delay, float(rp.crawl_delay(self.ua) or 0))
        self.sleep(delay)
        r = self.client.request("GET", url, headers={"User-Agent": self.ua}, timeout=20,
                                allow_redirects=True, log_params={"doel": "pagina"})
        ctype = r.headers.get("Content-Type", "")
        if r.status_code >= 400 or "html" not in ctype.lower():
            return None
        return r.url, r.text[:2_000_000]


def _text(soup: BeautifulSoup) -> str:
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


def _snippet(text: str, m: re.Match, width: int = 70) -> str:
    s = max(0, m.start() - width)
    return text[s:m.end() + width].strip()


def analyse_page(f: SiteFindings, url: str, html: str, site_domain: str | None) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    hrefs = [a.get("href", "") for a in soup.find_all("a")]
    blob = " ".join(hrefs) + " " + html

    for platform, rx in SOCIAL_RES.items():
        if platform in f.socials:
            continue
        for m in rx.finditer(blob):
            if platform == "instagram" and m.group(1).lower() in IG_RESERVED:
                continue
            if platform == "facebook" and m.group(1).lower() in FB_RESERVED:
                continue
            if platform == "instagram":
                link = f"https://www.instagram.com/{m.group(1)}/"
            elif platform == "tiktok":
                link = f"https://www.tiktok.com/@{m.group(1)}"
            elif platform == "linkedin":
                link = f"https://www.linkedin.com/{m.group(1)}/{m.group(2)}/"
            else:
                link = f"https://www.facebook.com/{m.group(1)}/"
            f.socials[platform] = (link, url)
            break

    candidates = {h[7:].split("?")[0] for h in hrefs if h.lower().startswith("mailto:")}
    candidates |= set(EMAIL_RE.findall(html))
    for e in candidates:
        e = e.strip().strip(".").lower()
        if not EMAIL_RE.fullmatch(e) or e.endswith((".png", ".jpg", ".webp", ".svg")):
            continue
        if is_generic_email(e) and e not in f.emails:
            f.emails[e] = url

    text = _text(soup)
    if not f.enterprise_number:
        for m in BE_ENT_RE.finditer(text):
            digits = ("".join(m.groups())).rjust(10, "0")
            ctx = text[max(0, m.start() - 40):m.start()].lower()
            has_kw = any(k in ctx for k in ("btw", "kbo", "vat", "ondernemingsn", "tva",
                                            "rpr", "ond.nr", "company n"))
            if valid_be_enterprise_number(digits) and (has_kw or m.group(0).upper().startswith("BE")):
                f.enterprise_number = (digits, url)
                break
    for m in POSTAL_CITY_RE.finditer(text):
        key = f"{m.group(1)} {m.group(2)}"
        f.addresses.setdefault(key, url)
    if not f.jobs:
        m = JOB_WORDS.search(text)
        if m:
            f.jobs = (_snippet(text, m), url)
    year = dt.date.today().year
    for m in LAUNCH_WORDS.finditer(text):
        snip = _snippet(text, m)
        if (str(year) in snip or str(year + 1) in snip) and len(f.launches) < 3:
            if all(snip != s for s, _ in f.launches):
                f.launches.append((snip, url))
    return soup


def crawl_site(crawler: Crawler, website: str) -> SiteFindings:
    f = SiteFindings()
    domain = company_domain(website)
    home = website if website.startswith("http") else "https://" + website
    if not crawler.allowed(home):
        f.blocked_by_robots = True
        return f
    try:
        res = crawler.get(home)
    except requests.RequestException as e:
        f.errors.append(f"homepage: {e.__class__.__name__}")
        return f
    if not res:
        f.errors.append("homepage niet beschikbaar")
        return f
    final_url, html = res
    f.pages["home"] = final_url
    soup = analyse_page(f, final_url, html, domain)

    host = urlparse(final_url).netloc
    for a in soup.find_all("a", href=True):
        href = urljoin(final_url, a["href"]).split("#")[0]
        if urlparse(href).netloc != host:
            continue
        label = f"{href} {a.get_text(' ', strip=True)}"
        for kind, rx in PAGE_PATTERNS.items():
            if kind not in f.pages and rx.search(label):
                f.pages[kind] = href
    for kind in ("contact", "over", "vacatures"):
        url = f.pages.get(kind)
        if not url:
            continue
        try:
            res = crawler.get(url)
        except requests.RequestException as e:
            f.errors.append(f"{kind}: {e.__class__.__name__}")
            continue
        if res:
            analyse_page(f, res[0], res[1], domain)
        else:
            f.errors.append(f"{kind}: niet toegestaan of niet beschikbaar")
    return f


def store_findings(con: sqlite3.Connection, cid: int, f: SiteFindings) -> None:
    ts = db.now()
    for platform in ("instagram", "tiktok", "linkedin", "facebook"):
        if platform in f.socials:
            url, src = f.socials[platform]
            db.set_field(con, cid, platform, url, "feit", source_url=src, verified_at=ts)
        else:
            db.set_field(con, cid, platform, None, "onbekend", verified_at=ts)
    emails = list(f.emails)
    best = pick_outreach_email(emails)
    if best:
        db.set_field(con, cid, "algemeen_email", best, "feit", source_url=f.emails[best], verified_at=ts)
        db.set_field(con, cid, "algemene_emails", ", ".join(sorted(emails)), "feit",
                     source_url=f.emails[best], verified_at=ts)
    else:
        db.set_field(con, cid, "algemeen_email", None, "onbekend", verified_at=ts)
    if f.enterprise_number:
        num, src = f.enterprise_number
        db.set_field(con, cid, "ondernemingsnummer", num, "feit", source_url=src, verified_at=ts)
        con.execute("UPDATE companies SET kbo=? WHERE id=?", (num, cid))
    if f.pages.get("vacatures") or f.jobs:
        parts = []
        if f.pages.get("vacatures"):
            parts.append(f"vacature-/jobpagina: {f.pages['vacatures']}")
        if f.jobs:
            parts.append(f"tekst: “{f.jobs[0]}”")
        db.set_field(con, cid, "vacatures", "; ".join(parts), "inschatting", verified_at=ts,
                     note="jobpagina of wervingstekst op de eigen website; actualiteit niet geverifieerd",
                     source_url=(f.jobs[1] if f.jobs else f.pages["vacatures"]))
    else:
        db.set_field(con, cid, "vacatures", None, "onbekend", verified_at=ts)
    if f.launches:
        db.set_field(con, cid, "recente_lanceringen", " | ".join(s for s, _ in f.launches),
                     "inschatting", source_url=f.launches[0][1], verified_at=ts,
                     note="tekstfragmenten met lancering/event + huidig of volgend jaar op de website")
    else:
        db.set_field(con, cid, "recente_lanceringen", None, "onbekend", verified_at=ts)
    n_addr = len(f.addresses)
    if n_addr >= 2:
        db.set_field(con, cid, "vestigingen", n_addr, "inschatting",
                     source_url=next(iter(f.addresses.values())), verified_at=ts,
                     note=f"{n_addr} verschillende adressen (postcode + plaats) op de website: "
                          + ", ".join(list(f.addresses)[:6]))
    elif n_addr == 1:
        db.set_field(con, cid, "vestigingen", 1, "inschatting",
                     source_url=next(iter(f.addresses.values())), verified_at=ts,
                     note="één adres gevonden op de website")
    else:
        db.set_field(con, cid, "vestigingen", None, "onbekend", verified_at=ts)

    groei = []
    if f.pages.get("vacatures") or f.jobs:
        groei.append("actief aan het aanwerven")
    if f.launches:
        groei.append("recente/komende lancering of event")
    if n_addr >= 2:
        groei.append(f"{n_addr} locaties")
    if groei:
        db.set_field(con, cid, "groeisignalen", "; ".join(groei), "inschatting", verified_at=ts,
                     note="samengevat uit vacatures, lanceringen en vestigingen hierboven")
    else:
        db.set_field(con, cid, "groeisignalen", None, "onbekend", verified_at=ts)
    con.commit()


# ── KBO Open Data ────────────────────────────────────────────────────

def import_kbo(con: sqlite3.Connection, folder: Path, log: Callable = print) -> int:
    """Importeer enterprise.csv + code.csv uit de officiële KBO Open Data-extractie."""
    codes: dict[tuple[str, str], str] = {}
    with open(folder / "code.csv", encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            if r["Category"] in ("JuridicalForm", "Status") and r["Language"] == "NL":
                codes[(r["Category"], r["Code"])] = r["Description"]
    n = 0
    con.execute("DELETE FROM kbo_enterprise")
    with open(folder / "enterprise.csv", encoding="utf-8-sig", newline="") as fh:
        batch = []
        for r in csv.DictReader(fh):
            num = re.sub(r"\D", "", r["EnterpriseNumber"])
            batch.append((num, codes.get(("Status", r.get("Status", "")), r.get("Status")),
                          codes.get(("JuridicalForm", r.get("JuridicalForm", "")), r.get("JuridicalForm")),
                          r.get("StartDate")))
            if len(batch) >= 50_000:
                con.executemany("INSERT OR REPLACE INTO kbo_enterprise VALUES (?,?,?,?)", batch)
                n += len(batch)
                batch.clear()
        con.executemany("INSERT OR REPLACE INTO kbo_enterprise VALUES (?,?,?,?)", batch)
        n += len(batch)
    extract = ""
    meta = folder / "meta.csv"
    if meta.exists():
        with open(meta, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("Variable") == "ExtractTimestamp":
                    extract = r.get("Value", "")
    con.execute("INSERT OR REPLACE INTO kbo_meta VALUES ('extract', ?)", (extract or db.now(),))
    con.commit()
    log(f"KBO: {n} ondernemingen geïmporteerd")
    return n


def kbo_url(number: str) -> str:
    return f"https://kbopub.economie.fgov.be/kbopub/zoeknummerform.html?nummer={number}"


def apply_kbo(con: sqlite3.Connection, cid: int) -> bool:
    num = con.execute("SELECT kbo FROM companies WHERE id=?", (cid,)).fetchone()[0]
    if not num:
        return False
    r = con.execute("SELECT * FROM kbo_enterprise WHERE number=?", (num,)).fetchone()
    if not r:
        return False
    extract = (con.execute("SELECT value FROM kbo_meta WHERE key='extract'").fetchone() or [""])[0]
    for fld, val in (("rechtsvorm", r["juridical_form"]), ("kbo_status", r["status"]),
                     ("startdatum", r["start_date"])):
        if val:
            db.set_field(con, cid, fld, val, "feit", source_url=kbo_url(num),
                         note=f"KBO Open Data, extractie {extract}")
    con.commit()
    return True


# ── Orchestratie ─────────────────────────────────────────────────────

def run(con: sqlite3.Connection, crawler: Crawler, *, limit: int | None = None,
        redo: bool = False, log: Callable = print) -> dict[str, int]:
    stats = {"verrijkt": 0, "geen_website": 0, "robots": 0, "fout": 0, "kbo": 0}
    has_kbo = con.execute("SELECT 1 FROM kbo_enterprise LIMIT 1").fetchone() is not None
    for c in db.pending(con, "enrich", redo=redo, limit=limit):
        website = db.value(con, c["id"], "website")
        if not website:
            db.mark_step(con, c["id"], "enrich", "overgeslagen", "geen eigen website")
            stats["geen_website"] += 1
            continue
        try:
            f = crawl_site(crawler, website)
        except Exception as e:  # één kapotte site mag de run niet stoppen
            db.mark_step(con, c["id"], "enrich", "fout", repr(e)[:300])
            stats["fout"] += 1
            log(f"  ✗ {c['name']}: {e!r}")
            continue
        if f.blocked_by_robots:
            db.mark_step(con, c["id"], "enrich", "overgeslagen", "robots.txt staat crawlen niet toe")
            stats["robots"] += 1
            log(f"  – {c['name']}: robots.txt staat crawlen niet toe")
            continue
        if not f.pages:
            db.mark_step(con, c["id"], "enrich", "fout", "; ".join(f.errors))
            stats["fout"] += 1
            log(f"  ✗ {c['name']}: {'; '.join(f.errors)}")
            continue
        store_findings(con, c["id"], f)
        if has_kbo and apply_kbo(con, c["id"]):
            stats["kbo"] += 1
        # Jaarrekening (omzet/personeel) niet automatisch: blijft 'onbekend' tot handmatig
        # of via een gekoppelde CBSO-bron geverifieerd.
        for fld in ("omzet", "medewerkers"):
            db.set_field(con, c["id"], fld, None, "onbekend")
        db.mark_step(con, c["id"], "enrich", "ok", f"{len(f.pages)} pagina's")
        stats["verrijkt"] += 1
        log(f"  ✓ {c['name']}: {len(f.pages)} pagina's, e-mail: "
            f"{pick_outreach_email(list(f.emails)) or '—'}, socials: {', '.join(f.socials) or '—'}")
    return stats
