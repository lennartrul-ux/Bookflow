"""Normalisatie voor ontdubbeling."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse

SOCIAL_HOSTS = {
    "instagram.com": "instagram",
    "tiktok.com": "tiktok",
    "linkedin.com": "linkedin",
    "facebook.com": "facebook",
    "fb.com": "facebook",
}
# Hosts die niet van het bedrijf zelf zijn: niet bruikbaar als ontdubbelsleutel.
NON_COMPANY_HOSTS = set(SOCIAL_HOSTS) | {
    "linktr.ee", "google.com", "business.site", "sites.google.com", "wixsite.com",
    "tripadvisor.com", "tripadvisor.be", "thefork.be", "resengo.com", "booking.com",
    "treatwell.be", "treatwell.nl", "linkin.bio", "beacons.ai",
}

_LEGAL = r"\b(bv|bvba|nv|vzw|vof|commv|cv|srl|sprl|sa|sas|b\.v\.|n\.v\.|ltd|gmbh)\b"


def _host(url: str | None) -> str | None:
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host or None


def _matches(host: str, base: str) -> bool:
    return host == base or host.endswith("." + base)


def social_platform(url: str | None) -> str | None:
    host = _host(url)
    if not host:
        return None
    for base, name in SOCIAL_HOSTS.items():
        if _matches(host, base):
            return name
    return None


def company_domain(url: str | None) -> str | None:
    """Domein van de eigen bedrijfswebsite, of None als het een platform is."""
    host = _host(url)
    if not host:
        return None
    if any(_matches(host, b) for b in NON_COMPANY_HOSTS):
        return None
    return host


def norm_text(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower().replace("&", " en ")
    s = re.sub(_LEGAL, " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_enterprise_number(s: str | None) -> str | None:
    """KBO (10 cijfers) of KvK (8 cijfers), alleen cijfers."""
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    return digits or None
