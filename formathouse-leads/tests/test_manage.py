"""Leads verwijderen, terugzetten en opnieuw beginnen."""
import datetime as dt
import random
from types import SimpleNamespace

import pytest

from formathouse import db, discover, ig_queue, manage, outreach, score
from formathouse.briefing import CITIES, GROUPS


@pytest.fixture
def con(tmp_path):
    return db.connect(tmp_path / "t.db")


def lead(con, i, email=True):
    c = discover.Candidate(source="google", source_id=f"p{i}", source_url="x", name=f"Testzaak {i}",
                           city="Antwerpen", postal_code="2000", country="BE",
                           website=f"https://testzaak{i}.be", reviews=500, price_level="€€€")
    cid, _ = discover.ingest(con, c, GROUPS["horeca"], "t", CITIES[0])
    db.set_field(con, cid, "instagram", f"https://www.instagram.com/testzaak{i}/", "feit", source_url="x")
    if email:
        db.set_field(con, cid, "algemeen_email", f"info@testzaak{i}.be", "feit", source_url="x")
    return cid


def test_remove_and_restore(con):
    a, b = lead(con, 1), lead(con, 2)
    score.run(con, log=lambda *_: None)
    manage.remove_lead(con, a)
    assert con.execute("SELECT rank FROM scores WHERE company_id=?", (a,)).fetchone()[0] is None
    assert con.execute("SELECT rank FROM scores WHERE company_id=?", (b,)).fetchone()[0] == 1
    # komt niet terug bij een nieuwe zoekopdracht
    again = discover.Candidate(source="google", source_id="p1", source_url="x", name="Testzaak 1",
                               postal_code="2000", country="BE", website="https://testzaak1.be", reviews=500)
    assert discover.ingest(con, again, GROUPS["horeca"], "t", CITIES[0])[1] == "dubbel"
    assert [r["id"] for r in manage.removed_leads(con)] == [a]
    manage.restore_lead(con, a)
    assert manage.removed_leads(con) == []
    with pytest.raises(KeyError):
        manage.restore_lead(con, b)          # was niet verwijderd


def test_reset_keeps_contact_history(con, tmp_path):
    a = lead(con, 1)
    h = lead(con, 2, email=False)
    score.run(con, log=lambda *_: None)
    s = SimpleNamespace(claude_model="", unsubscribe_email="", signature="T")
    bid, _ = outreach.draft(con, tmp_path, n=5, settings=s, log=lambda *_: None)
    con.execute("UPDATE outreach_messages SET status='verstuurd', sent_at=? WHERE step=0",
                (dt.datetime.now(dt.timezone.utc).isoformat(),))
    ig_queue.build(con, log=lambda *_: None)
    ig_queue.mark(con, "testzaak2", "verstuurd")
    outreach.suppress(con, "nee@bedrijf.be", "test")
    saved = manage.backup(tmp_path / "t.db", tmp_path / "backups")
    counts = manage.reset_all(con)
    import os
    assert counts["companies"] == 2
    assert (saved is None) if os.getenv("DATABASE_URL") else saved.exists()
    assert con.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM suppression").fetchone()[0] == 1
    # opnieuw gevonden → nooit opnieuw benaderd
    a2, h2 = lead(con, 1), lead(con, 2, email=False)
    score.run(con, log=lambda *_: None)
    assert outreach.candidates(con, 10) == []
    assert ig_queue.build(con, log=lambda *_: None) == 0
