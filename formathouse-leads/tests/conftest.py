"""Met FH_TEST_PG=<postgres-url> draaien alle tests ook tegen Postgres (zoals Supabase)."""
import os

import pytest


@pytest.fixture(autouse=True)
def _fresh_db(monkeypatch):
    url = os.getenv("FH_TEST_PG")
    if not url:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        yield
        return
    import psycopg

    from formathouse import db
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    db._PG_READY.clear()
    monkeypatch.setenv("DATABASE_URL", url)
    yield
