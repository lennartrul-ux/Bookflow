"""Rooktest van de app: lokaal (zonder login) en online (met wachtwoord)."""
import pytest

from formathouse import config, webapp

LOCAL = "http://127.0.0.1:8765"
H = {"X-FH": "1"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADS_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    webapp.app.config["TESTING"] = True
    return webapp.app.test_client()


def test_page_and_state(client):
    assert client.get("/", base_url=LOCAL).status_code == 200
    st = client.get("/api/state", base_url=LOCAL).get_json()
    assert st["region"] == "vlaanderen" and "Antwerpen" in st["cities"]
    assert all(not f["value"] for f in st["settings"] if f["secret"])  # sleutels nooit terugsturen


def test_only_local_and_header(client):
    assert client.get("/api/state", base_url="http://evil.example").status_code == 403
    assert client.post("/api/settings", json={}, base_url=LOCAL).status_code == 403


def test_save_settings(client, tmp_path):
    r = client.post("/api/settings", json={"SMTP_HOST": "smtp.test", "FOO": "x"}, headers=H, base_url=LOCAL)
    assert r.get_json()["ok"]
    st = client.get("/api/state", base_url=LOCAL).get_json()
    assert next(f for f in st["settings"] if f["key"] == "SMTP_HOST")["value"] == "smtp.test"
    if not config.hosted():
        text = (tmp_path / ".env").read_text()
        assert "SMTP_HOST=smtp.test" in text and "FOO" not in text


def test_downloads(client):
    r = client.get("/download/leads.xlsx", base_url=LOCAL)
    assert r.status_code == 200 and r.data[:2] == b"PK"


def test_online_login_and_cron(client, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "geheim")
    monkeypatch.setenv("CRON_TOKEN", "tok")
    url = "https://fh.example.com"
    assert client.get("/", base_url=url).status_code == 302           # → /login
    assert client.get("/api/state", base_url=url).status_code == 401
    assert client.post("/login", data={"password": "fout"}, base_url=url).status_code == 200
    assert client.get("/api/state", base_url=url).status_code == 401
    assert client.post("/login", data={"password": "geheim"}, base_url=url).status_code == 302
    assert client.get("/api/state", base_url=url).get_json()["login"] is True
    assert client.post("/api/cron/tick", base_url=url).status_code == 403
    r = client.post("/api/cron/tick", headers={"X-Cron-Token": "tok"}, base_url=url)
    assert r.get_json()["ok"]
