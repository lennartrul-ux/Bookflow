"""Tests met een nagebootste Places API (geen echte calls, fictieve bedrijven)."""
import json

import openpyxl
import pytest
import requests

from formathouse import db, discover, export
from formathouse.httpclient import LoggedClient
from formathouse.normalize import company_domain, norm_text


def place(i, *, name=None, website="https://testzaak{i}.be", reviews=250, country="BE",
          status="OPERATIONAL", city="Antwerpen", postal="2000"):
    return {
        "id": f"pid{i}",
        "displayName": {"text": name or f"Testzaak {i}"},
        "formattedAddress": f"Teststraat {i}, 2000 {city}, België",
        "addressComponents": [
            {"longText": city, "shortText": city, "types": ["locality", "political"]},
            {"longText": "België", "shortText": country, "types": ["country", "political"]},
            {"longText": postal, "shortText": postal, "types": ["postal_code"]},
        ],
        "websiteUri": website.format(i=i) if website else None,
        "primaryTypeDisplayName": {"text": "Restaurant"},
        "rating": 4.5,
        "userRatingCount": reviews,
        "priceLevel": "PRICE_LEVEL_EXPENSIVE",
        "businessStatus": status,
        "nationalPhoneNumber": "03 000 00 00",
        "googleMapsUri": f"https://maps.google.com/?cid={i}",
    }


class FakeResp:
    def __init__(self, data, code=200):
        self._d, self.status_code, self.headers = data, code, {}
        self.content = json.dumps(data).encode()
        self.text = self.content.decode()

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(self.status_code)


class FakeSession:
    """Geeft per zoekterm 2 pagina's van 20 plaatsen terug."""
    def __init__(self, pages_by_query):
        self.pages = pages_by_query
        self.calls = []

    def request(self, method, url, json=None, headers=None, timeout=None):
        assert headers["X-Goog-Api-Key"] == "TESTKEY"
        self.calls.append(json)
        q, tok = json["textQuery"], json.get("pageToken")
        pages = self.pages.get(q, [[]])
        idx = int(tok) if tok else 0
        data = {"places": pages[idx]}
        if idx + 1 < len(pages):
            data["nextPageToken"] = str(idx + 1)
        return FakeResp(data)


@pytest.fixture
def con(tmp_path):
    return db.connect(tmp_path / "t.db")


def make_pages():
    pages, n = {}, 0
    for term in discover.GROUPS["horeca"].terms:
        q = f"{term} in Antwerpen"
        pages[q] = []
        for _ in range(2):
            pages[q].append([place(n + k) for k in range(20)])
            n += 20
    return pages


def test_normalize():
    assert company_domain("https://www.Example.be/contact") == "example.be"
    assert company_domain("https://instagram.com/foo") is None
    assert norm_text("Café L'Été BV") == "cafe l ete"


def test_field_rules(con):
    cid = con.execute("INSERT INTO companies(name,norm_name,created_at,updated_at) "
                      "VALUES ('x','x','t','t')").lastrowid
    with pytest.raises(ValueError):
        db.set_field(con, cid, "omzet", "1M", "feit")          # feit zonder bron
    with pytest.raises(ValueError):
        db.set_field(con, cid, "omzet", "1M", "inschatting")   # inschatting zonder motivatie
    db.set_field(con, cid, "omzet", "1M", "feit", source_url="https://cbso")
    assert not db.set_field(con, cid, "omzet", None, "onbekend")  # geen downgrade
    assert db.get_fields(con, cid)["omzet"]["value"] == "1M"


def test_pilot_limit_resume_and_dedup(con):
    sess = FakeSession(make_pages())
    client = LoggedClient(con, "google_places", min_interval=0, session=sess)
    stats = discover.run(con, client, "TESTKEY", group_keys=["horeca"],
                         city_names=["Antwerpen"], limit=30, log=lambda *_: None)
    assert stats.new_accepted == 30
    assert discover.accepted_count(con, "horeca") == 30
    # round-robin: de eerste pagina's van meerdere zoektermen zijn gebruikt
    assert len({c["textQuery"] for c in sess.calls}) >= 2
    # elke call is gelogd, zonder API-sleutel
    logged = con.execute("SELECT * FROM api_calls").fetchall()
    assert len(logged) == len(sess.calls)
    assert all("TESTKEY" not in (r["params"] or "") for r in logged)

    # Hervatten: tweede run haalt geen reeds afgewerkte pagina's opnieuw op
    before = len(sess.calls)
    stats2 = discover.run(con, client, "TESTKEY", group_keys=["horeca"],
                          city_names=["Antwerpen"], limit=5, log=lambda *_: None)
    assert stats2.new_accepted == 5
    assert len(sess.calls) - before <= 2


def test_prefilter_and_dedup(con):
    g = discover.GROUPS["horeca"]
    city = discover.CITIES[0]
    _, o1 = discover.ingest(con, place(1), g, "brasserie", city)
    # zelfde domein, andere place_id → dubbel
    dup = place(2, website="https://www.testzaak1.be/menu")
    _, o2 = discover.ingest(con, dup, g, "brasserie", city)
    # zelfde naam + stad, geen website-overlap → dubbel
    _, o3 = discover.ingest(con, place(3, name="TESTZAAK 1 bv", website="https://ander.be"),
                            g, "brasserie", city)
    _, o4 = discover.ingest(con, place(4, reviews=10), g, "brasserie", city)
    _, o5 = discover.ingest(con, place(5, name="McDonald's Meir"), g, "brasserie", city)
    _, o6 = discover.ingest(con, place(6, website=None), g, "brasserie", city)
    _, o7 = discover.ingest(con, place(7, country="FR"), g, "brasserie", city)
    assert (o1, o2, o3) == ("nieuw", "dubbel", "dubbel")
    assert (o4, o5, o6, o7) == ("afgewezen",) * 4
    # buiten Vlaanderen: Brussel en Wallonië
    _, o8 = discover.ingest(con, place(8, postal="1050", city="Elsene"), g, "brasserie", city)
    _, o9 = discover.ingest(con, place(10, postal="4000", city="Luik"), g, "brasserie", city)
    _, o10 = discover.ingest(con, place(11, postal="1800", city="Vilvoorde"), g, "brasserie", city)
    assert (o8, o9, o10) == ("afgewezen", "afgewezen", "nieuw")


def test_region_cities():
    from formathouse.briefing import in_region
    assert in_region("vlaanderen", "BE", "9000")
    assert not in_region("vlaanderen", "BE", "1000")
    assert not in_region("vlaanderen", "NL", "4811")
    assert in_region("belgie", "BE", "1000")
    assert not in_region("vlaanderen", "BE", None)
    with pytest.raises(ValueError):
        discover.run(None, None, "K", city_names=["Brussel"])


def test_instagram_as_website(con):
    g, city = discover.GROUPS["horeca"], discover.CITIES[0]
    cid, _ = discover.ingest(con, place(9, website="https://www.instagram.com/testzaak9"),
                             g, "brasserie", city)
    f = db.get_fields(con, cid)
    assert f["instagram"]["status"] == "feit"
    assert f["website"]["status"] == "onbekend"


def test_export(con, tmp_path):
    g, city = discover.GROUPS["horeca"], discover.CITIES[0]
    discover.ingest(con, place(1), g, "brasserie", city)
    paths = export.export(con, tmp_path / "out")
    wb = openpyxl.load_workbook(paths["xlsx"])
    assert wb.sheetnames == ["Alle leads", "Bronnen", "Legenda"]
    ws = wb["Alle leads"]
    hdr = [c.value for c in ws[1]]
    row = {h: ws.cell(2, i + 1).value for i, h in enumerate(hdr)}
    assert row["Bedrijfsnaam"] == "Testzaak 1"
    assert row["Omzet (geverifieerd)"] == "onbekend"
    assert paths["csv"].read_text(encoding="utf-8-sig").count("\n") == 2
