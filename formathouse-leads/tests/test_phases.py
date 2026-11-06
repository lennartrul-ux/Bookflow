"""Tests voor Fase 2–6 met nagebootste websites/API's en fictieve bedrijven."""
import csv
import datetime as dt
import json
import random
from types import SimpleNamespace

import openpyxl
import pytest

from formathouse import db, discover, enrich, export, ig_queue, outreach, score, social
from formathouse.briefing import CITIES, GROUPS

ANTWERPEN = CITIES[0]


@pytest.fixture
def con(tmp_path):
    return db.connect(tmp_path / "t.db")


def add_company(con, i, *, website="https://testzaak{i}.be", group="horeca"):
    c = discover.Candidate(
        source="google", source_id=f"pid{i}", source_url=f"https://maps.google.com/?cid={i}",
        name=f"Testzaak {i}", address=f"Teststraat {i}, 2000 Antwerpen", city="Antwerpen",
        postal_code="2000", country="BE", website=website.format(i=i) if website else None,
        category="Restaurant", rating=4.6, reviews=800, price_level="€€€", phone="03 000 00 00")
    cid, outcome = discover.ingest(con, c, GROUPS[group], "brasserie", ANTWERPEN)
    assert outcome == "nieuw"
    return cid


# ── Fase 1: OpenStreetMap ─────────────────────────────────────────────

def test_osm_parsing_and_filters(con):
    el = {"type": "node", "id": 42, "tags": {
        "name": "Testbar", "amenity": "bar", "website": "https://testbar.be",
        "addr:street": "Testlei", "addr:housenumber": "1", "addr:postcode": "2018",
        "addr:city": "Antwerpen", "contact:instagram": "testbar"}}
    c = discover.from_osm(el)
    assert c.socials["instagram"] == "https://www.instagram.com/testbar/"
    assert c.source_url == "https://www.openstreetmap.org/node/42"
    cid, outcome = discover.ingest(con, c, GROUPS["horeca"], "osm", ANTWERPEN)
    assert outcome == "nieuw"                       # geen reviews-drempel voor OSM
    f = db.get_fields(con, cid)
    assert f["google_reviews"]["status"] == "onbekend"
    assert f["instagram"]["status"] == "feit"
    chain = discover.from_osm({"type": "node", "id": 43, "tags": {
        "name": "Ketenzaak", "amenity": "cafe", "brand": "Keten", "website": "https://k.be",
        "addr:postcode": "2000"}})
    assert discover.ingest(con, chain, GROUPS["horeca"], "osm", ANTWERPEN)[1] == "afgewezen"
    q = discover.overpass_query("horeca", ANTWERPEN)
    assert "around:15000,51.2194,4.4025" in q and '"website"' in q


# ── Fase 2: websites ──────────────────────────────────────────────────

class FakeResp:
    def __init__(self, url, text, code=200, ctype="text/html"):
        self.url, self.text, self.status_code = url, text, code
        self.headers = {"Content-Type": ctype}
        self.content = text.encode()


class FakeWeb:
    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def request(self, method, url, **kw):
        self.requested.append(url)
        if url in self.pages:
            body = self.pages[url]
            return FakeResp(url, body, ctype="text/plain" if url.endswith("robots.txt") else "text/html")
        return FakeResp(url, "niet gevonden", 404)


HOME = """<html><body>
<a href="/contact">Contact</a> <a href="/over-ons">Over ons</a> <a href="/jobs">Werken bij</a>
<a href="https://www.instagram.com/testzaak1/">IG</a> <a href="https://www.instagram.com/p/abc/">post</a>
<a href="https://www.tiktok.com/@testzaak1">TikTok</a>
<a href="https://www.linkedin.com/company/testzaak-1/">LinkedIn</a>
<p>Grand opening van onze nieuwe vestiging in Gent op 12 december 2026!</p>
<p>BTW BE 0403.170.701</p></body></html>"""
CONTACT = """<html><body><a href="mailto:hello@testzaak1.be">mail</a>
jan.peeters@testzaak1.be  info@testzaak1.be
Teststraat 1, 2000 Antwerpen — Veldstraat 5, 9000 Gent</body></html>"""
JOBS = "<html><body><h1>Vacatures</h1><p>We zoeken een zaalverantwoordelijke (m/v/x)</p></body></html>"


def make_crawler(con, pages, robots="User-agent: *\nDisallow: /admin\n"):
    web = FakeWeb({"https://testzaak1.be/robots.txt": robots, **pages})
    from formathouse.httpclient import LoggedClient
    client = LoggedClient(con, "website", min_interval=0, session=web, sleep=lambda s: None)
    return enrich.Crawler(client, "TestBot/1.0", 0, sleep=lambda s: None), web


def test_enterprise_number_checksum():
    assert enrich.valid_be_enterprise_number("0403170701")
    assert not enrich.valid_be_enterprise_number("0403170702")


def test_crawl_and_store(con):
    cid = add_company(con, 1)
    crawler, web = make_crawler(con, {
        "https://testzaak1.be": HOME, "https://testzaak1.be/contact": CONTACT,
        "https://testzaak1.be/over-ons": "<html>over ons</html>", "https://testzaak1.be/jobs": JOBS})
    stats = enrich.run(con, crawler, log=lambda *_: None)
    assert stats["verrijkt"] == 1
    f = db.get_fields(con, cid)
    assert f["instagram"]["value"] == "https://www.instagram.com/testzaak1/"
    assert f["tiktok"]["status"] == "feit" and f["linkedin"]["status"] == "feit"
    assert f["algemeen_email"]["value"] == "hello@testzaak1.be"
    assert "jan.peeters" not in f["algemene_emails"]["value"]     # nooit persoonlijke adressen
    assert f["ondernemingsnummer"]["value"] == "0403170701"
    assert f["vestigingen"]["value"] == "2" and f["vestigingen"]["status"] == "inschatting"
    assert f["vacatures"]["status"] == "inschatting"
    assert "opening" in f["recente_lanceringen"]["value"].lower()
    assert f["omzet"]["status"] == "onbekend"
    # hervatbaar: tweede run doet niets
    assert enrich.run(con, crawler, log=lambda *_: None)["verrijkt"] == 0


def test_robots_disallow(con):
    add_company(con, 1)
    crawler, web = make_crawler(con, {"https://testzaak1.be": HOME},
                                robots="User-agent: *\nDisallow: /\n")
    stats = enrich.run(con, crawler, log=lambda *_: None)
    assert stats["robots"] == 1
    assert "https://testzaak1.be" not in web.requested


def test_kbo_import(con, tmp_path):
    cid = add_company(con, 1)
    con.execute("UPDATE companies SET kbo='0403170701' WHERE id=?", (cid,))
    d = tmp_path / "kbo"
    d.mkdir()
    (d / "code.csv").write_text('"Category","Code","Language","Description"\n'
                                '"JuridicalForm","610","NL","Besloten vennootschap"\n'
                                '"Status","AC","NL","Actief"\n', encoding="utf-8")
    (d / "enterprise.csv").write_text('"EnterpriseNumber","Status","JuridicalSituation",'
                                      '"TypeOfEnterprise","JuridicalForm","JuridicalFormCAC","StartDate"\n'
                                      '"0403.170.701","AC","000","2","610","","01-01-2015"\n',
                                      encoding="utf-8")
    assert enrich.import_kbo(con, d, log=lambda *_: None) == 1
    assert enrich.apply_kbo(con, cid)
    f = db.get_fields(con, cid)
    assert f["rechtsvorm"]["value"] == "Besloten vennootschap"
    assert "kbopub" in f["rechtsvorm"]["source_url"]


# ── Fase 3: social ────────────────────────────────────────────────────

def test_analyse_media():
    today = dt.date(2026, 11, 3)
    media = [{"timestamp": f"2026-10-{d:02d}T10:00:00+0000",
              "media_product_type": "REELS" if d % 2 else "FEED"} for d in (30, 25, 20, 15, 10)]
    media.append({"timestamp": "2026-08-01T10:00:00+0000", "media_product_type": "FEED"})
    a = social.analyse_media({"followers_count": 1200, "media_count": 300, "media": {"data": media}},
                             today=today)
    assert a["reels_30d"] == 2 and a["posts_30d"] == 5 and a["active_30d"]
    assert a["irregular"]  # pauze augustus → oktober


def test_social_without_token(con):
    cid = add_company(con, 1)
    db.set_field(con, cid, "instagram", "https://www.instagram.com/testzaak1/", "feit", source_url="x")
    social.run(con, None, version="v23.0", ig_user_id="", token="", log=lambda *_: None)
    f = db.get_fields(con, cid)
    assert f["instagram_handle"]["value"] == "testzaak1"
    assert f["reels_30d"]["status"] == "onbekend" and f["advertenties"]["status"] == "onbekend"


def test_social_with_fake_graph(con):
    cid = add_company(con, 1)
    db.set_field(con, cid, "instagram", "https://www.instagram.com/testzaak1/", "feit", source_url="x")
    now = dt.datetime.now(dt.timezone.utc)
    media = [{"timestamp": (now - dt.timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%S+0000"),
              "media_product_type": "REELS"} for d in (2, 9, 16)]

    class Graph:
        def request(self, method, url, params=None, **kw):
            if "ads_archive" in url:
                data = {"data": [{"page_id": "1", "page_name": "Testzaak 1"},
                                 {"page_id": "1", "page_name": "Testzaak 1"}]}
            else:
                data = {"business_discovery": {"followers_count": 5000, "media_count": 400,
                                               "media": {"data": media}}}
            return SimpleNamespace(status_code=200, json=lambda: data, headers={},
                                   content=json.dumps(data).encode(), text=json.dumps(data))
    from formathouse.httpclient import LoggedClient
    client = LoggedClient(con, "meta_graph", min_interval=0, session=Graph())
    social.run(con, client, version="v23.0", ig_user_id="1", token="T", log=lambda *_: None)
    f = db.get_fields(con, cid)
    assert f["reels_30d"]["value"] == "3" and f["reels_30d"]["status"] == "feit"
    assert f["koopsignaal"]["status"] == "inschatting"
    assert f["advertenties"]["value"].startswith("ja")
    assert all("T" != json.loads(r["params"]).get("access_token")
               for r in con.execute("SELECT params FROM api_calls"))


# ── Fase 4: scoring ───────────────────────────────────────────────────

def test_rule_scoring_and_rank(con):
    ids = [add_company(con, i) for i in range(1, 4)]
    db.set_field(con, ids[0], "algemeen_email", "info@testzaak1.be", "feit", source_url="x")
    db.set_field(con, ids[0], "vacatures", "jobpagina", "inschatting", note="n")
    stats = score.run(con, log=lambda *_: None)
    assert stats["regels"] == 3 and stats["top"] == 3
    top = con.execute("SELECT company_id FROM scores WHERE rank=1").fetchone()[0]
    assert top == ids[0]
    f = db.get_fields(con, ids[0])
    assert f["leadscore"]["status"] == "inschatting" and "regelgebaseerd" in f["leadscore"]["note"]


def test_finalize_clamps():
    raw = {"deelscores": {k: {"score": 99, "motivatie": "x"} for k in score.CRITERIA},
           "uitsluiten": False}
    assert score.finalize(raw)["totaal"] == 100 and raw["prioriteit"] == "hoog"


def test_claude_scoring_request_shape(con):
    cid = add_company(con, 1)
    sent = {}
    answer = {"deelscores": {k: {"score": 10, "motivatie": "m"} for k in score.CRITERIA},
              "uitsluiten": False, "uitsluitreden": "", "budgetcategorie": "€1.500–€2.500/maand",
              "contentbehoefte": "Reels", "beslisser_functie": "zaakvoerder", "salesinsteek": "S"}

    class Msgs:
        def create(self, **kw):
            sent.update(kw)
            return SimpleNamespace(stop_reason="end_turn", model=kw["model"], stop_details=None,
                                   content=[SimpleNamespace(type="text", text=json.dumps(answer))])
    client = SimpleNamespace(beta=SimpleNamespace(messages=Msgs()))
    score.run(con, client=client, model="claude-opus-5-5", log=lambda *_: None)
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["fallbacks"] == "default"
    payload = json.loads(sent["messages"][0]["content"].split("\n", 1)[1])
    assert payload["velden"]["google_reviews"]["status"] == "feit"
    assert con.execute("SELECT total, method FROM scores WHERE company_id=?", (cid,)).fetchone()[:] \
        == (60, "claude:claude-opus-5-5")


# ── Fase 5: e-mail ────────────────────────────────────────────────────

def settings(**kw):
    base = dict(claude_model="", unsubscribe_email="stop@outreach.test", signature="Team TFH",
                smtp_host="smtp.test", from_email="hallo@outreach.test", from_name="The Format House",
                daily_max=2, smtp_port=587, smtp_user="", smtp_password="")
    return SimpleNamespace(**(base | kw))


def scored_leads(con, n=3):
    ids = [add_company(con, i) for i in range(1, n + 1)]
    for cid in ids:
        db.set_field(con, cid, "algemeen_email", f"info@testzaak{cid}.be", "feit",
                     source_url=f"https://testzaak{cid}.be/contact")
    score.run(con, log=lambda *_: None)
    return ids


def test_outreach_flow(con, tmp_path):
    ids = scored_leads(con, 3)
    outreach.suppress(con, "@testzaak3.be", "test")
    bid, path = outreach.draft(con, tmp_path, n=10, settings=settings(), log=lambda *_: None)
    assert path.exists()
    msgs = con.execute("SELECT * FROM outreach_messages WHERE batch_id=?", (bid,)).fetchall()
    assert len(msgs) == 6                                  # 2 leads × 3 stappen (3e = suppressie)
    body = msgs[0]["body"]
    assert "uitschrijven" in body and "https://testzaak" in body and "content agency" in body
    # zonder goedkeuring wordt niets verstuurd
    sent = []
    s = settings()
    monday_10 = dt.datetime(2026, 11, 9, 9, 0, tzinfo=dt.timezone.utc)  # 10u Brussel
    assert outreach.tick(con, s, send=sent.append, now=monday_10)["verstuurd"] == 0
    outreach.approve(con, bid, daily_max=2, now=monday_10 - dt.timedelta(hours=2),
                     rnd=random.Random(1))
    late = dt.datetime(2026, 11, 9, 15, 59, tzinfo=dt.timezone.utc)   # 16:59 Brussel
    st = outreach.tick(con, s, send=sent.append, now=late, rnd=random.Random(1))
    assert st["verstuurd"] == 2 and len(sent) == 2
    assert sent[0]["List-Unsubscribe"].startswith("<mailto:stop@outreach.test")
    # opvolging stopt na antwoord
    outreach.mark_replied(con, f"info@testzaak{ids[0]}.be")
    later = dt.datetime(2026, 11, 25, 10, 0, tzinfo=dt.timezone.utc)
    st = outreach.tick(con, s, send=sent.append, now=later, rnd=random.Random(1))
    to = [m["To"] for m in sent[2:]]
    assert f"info@testzaak{ids[0]}.be" not in to
    assert sent[-1]["In-Reply-To"]                       # opvolging in dezelfde thread
    assert len(sent) == 3                                # opvolging 2 niet tegelijk met opvolging 1
    st = outreach.tick(con, s, send=sent.append,
                       now=dt.datetime(2026, 12, 1, 10, 0, tzinfo=dt.timezone.utc), rnd=random.Random(1))
    assert len(sent) == 4 and sent[-1]["To"] == sent[2]["To"]


def test_daily_cap(con, tmp_path):
    scored_leads(con, 3)
    bid, _ = outreach.draft(con, tmp_path, n=10, settings=settings(), log=lambda *_: None)
    monday = dt.datetime(2026, 11, 9, 6, 0, tzinfo=dt.timezone.utc)
    outreach.approve(con, bid, daily_max=2, now=monday, rnd=random.Random(2))
    days = {dt.datetime.fromisoformat(r[0]).astimezone(outreach.TZ).date()
            for r in con.execute("SELECT planned_at FROM outreach_messages WHERE step=0")}
    assert len(days) == 2                                # 3 mails, max 2 per dag
    for r in con.execute("SELECT planned_at FROM outreach_messages WHERE step=0"):
        h = dt.datetime.fromisoformat(r[0]).astimezone(outreach.TZ).hour
        assert 9 <= h < 17


# ── Fase 5: Instagram ─────────────────────────────────────────────────

def test_ig_queue(con, tmp_path):
    ids = [add_company(con, i) for i in range(1, 20)]
    for cid in ids:
        db.set_field(con, cid, "instagram", f"https://www.instagram.com/testzaak{cid}/", "feit",
                     source_url="x")
    db.set_field(con, ids[0], "algemeen_email", "info@testzaak1.be", "feit", source_url="x")
    score.run(con, log=lambda *_: None)
    n = ig_queue.build(con, daily_max=15, today="2026-11-03", log=lambda *_: None)
    assert n == 15                                         # max. 15 per dag
    assert ig_queue.build(con, daily_max=15, today="2026-11-03", log=lambda *_: None) == 0
    q = {r["handle"] for r in con.execute("SELECT handle FROM ig_queue")}
    assert "testzaak1" not in q                            # heeft e-mail → niet in IG-wachtrij
    assert all(len(r[0]) <= 400 for r in con.execute("SELECT dm FROM ig_queue"))
    path = ig_queue.write_files(con, tmp_path)
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig"), delimiter=";"))
    assert len(rows) == 15
    # Claude (computer use) zet status in het bestand op 'verstuurd' → sync
    rows[0]["status"] = "verstuurd"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=ig_queue.COLUMNS, delimiter=";")
        w.writeheader()
        w.writerows(rows)
    assert ig_queue.sync(con, tmp_path, log=lambda *_: None) == 1
    with pytest.raises(ValueError):                        # nooit terug naar te_versturen
        ig_queue.mark(con, rows[0]["handle"], "te_versturen")
    assert (tmp_path / ig_queue.LOG_FILE).exists()
    assert len(ig_queue.today_list(con)) == 14
    # volgende dag: nieuwe items, maar nooit dezelfde handle opnieuw
    assert ig_queue.build(con, daily_max=15, today="2026-11-04", log=lambda *_: None) == 3


# ── Fase 6: export ────────────────────────────────────────────────────

def test_full_export(con, tmp_path):
    scored_leads(con, 3)
    outreach.draft(con, tmp_path, n=10, settings=settings(), log=lambda *_: None)
    paths = export.export(con, tmp_path / "out")
    wb = openpyxl.load_workbook(paths["xlsx"])
    assert wb["Top 50"].max_row == 4
    assert wb["E-mail outreach"].max_row == 1 + 9
    hdr = [c.value for c in wb["Alle leads"][1]]
    for col in ("Bedrijfsnaam", "Reels laatste 30 dagen", "Leadscore", "Salesinsteek", "Bronnen",
                "Verificatiedatum"):
        assert col in hdr


def test_overpass_user_agent_and_mirror(con):
    from formathouse.httpclient import LoggedClient
    calls = []

    class Overpass:
        def request(self, method, url, headers=None, **kw):
            calls.append((url, headers.get("User-Agent")))
            if url == discover.OVERPASS_URL:
                return SimpleNamespace(status_code=406, headers={}, text="Not Acceptable", content=b"",
                                       json=lambda: {}, raise_for_status=lambda: (_ for _ in ()).throw(
                                           __import__("requests").HTTPError("406")))
            data = {"elements": [{"type": "node", "id": 1, "tags": {
                "name": "Testbar", "amenity": "bar", "website": "https://testbar.be"}}]}
            return SimpleNamespace(status_code=200, headers={}, text="", content=b"{}",
                                   json=lambda: data, raise_for_status=lambda: None)
    client = LoggedClient(con, "overpass", min_interval=0, session=Overpass())
    cands = discover.osm_fetch(client, "horeca", ANTWERPEN, "TestUA/1.0 (+https://x.be)")
    assert [c.name for c in cands] == ["Testbar"]
    assert calls[0][1] == "TestUA/1.0 (+https://x.be)" and len(calls) == 2
