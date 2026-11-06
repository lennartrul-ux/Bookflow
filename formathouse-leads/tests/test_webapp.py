"""Rooktest van de lokale app (Flask test client, tijdelijke database)."""
import pytest

from formathouse import config, webapp


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADS_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(webapp, "save_env", lambda u: config.save_env(u, tmp_path / ".env"))
    webapp.app.config["TESTING"] = True
    return webapp.app.test_client()


H = {"X-FH": "1"}


def test_page_and_state(client):
    assert client.get("/", base_url="http://127.0.0.1:8765").status_code == 200
    st = client.get("/api/state", base_url="http://127.0.0.1:8765").get_json()
    assert st["region"] == "vlaanderen" and "Antwerpen" in st["cities"]
    assert all(not f["value"] for f in st["settings"] if f["secret"])  # sleutels nooit terugsturen


def test_only_local_and_header(client):
    assert client.get("/api/state", base_url="http://evil.example").status_code == 403
    assert client.post("/api/settings", json={}, base_url="http://127.0.0.1:8765").status_code == 403


def test_save_settings(client, tmp_path):
    r = client.post("/api/settings", json={"SMTP_HOST": "smtp.test", "FOO": "x"}, headers=H,
                    base_url="http://127.0.0.1:8765")
    assert r.get_json()["ok"]
    text = (tmp_path / ".env").read_text()
    assert "SMTP_HOST=smtp.test" in text and "FOO" not in text


def test_downloads(client):
    r = client.get("/download/leads.xlsx", base_url="http://127.0.0.1:8765")
    assert r.status_code == 200 and r.data[:2] == b"PK"
