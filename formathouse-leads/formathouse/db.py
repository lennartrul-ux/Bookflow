"""SQLite-opslag met herkomst per veld (feit / inschatting / onbekend)."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

STATUSES = ("feit", "inschatting", "onbekend")
_RANK = {"onbekend": 0, "inschatting": 1, "feit": 2}

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    norm_name       TEXT NOT NULL,
    city            TEXT,
    norm_city       TEXT,
    country         TEXT,
    sector_group    TEXT,
    domain          TEXT,
    place_id        TEXT UNIQUE,
    kbo             TEXT,
    kvk             TEXT,
    search_city     TEXT,
    excluded        INTEGER NOT NULL DEFAULT 0,
    exclude_reason  TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_companies_domain ON companies(domain);
CREATE INDEX IF NOT EXISTS ix_companies_namecity ON companies(norm_name, norm_city);

CREATE TABLE IF NOT EXISTS fields (
    company_id   INTEGER NOT NULL REFERENCES companies(id),
    field        TEXT NOT NULL,
    value        TEXT,
    status       TEXT NOT NULL CHECK (status IN ('feit','inschatting','onbekend')),
    source_url   TEXT,
    verified_at  TEXT,
    note         TEXT,
    PRIMARY KEY (company_id, field)
);

CREATE TABLE IF NOT EXISTS api_calls (
    id              INTEGER PRIMARY KEY,
    ts              TEXT NOT NULL,
    source          TEXT NOT NULL,
    method          TEXT NOT NULL,
    url             TEXT NOT NULL,
    params          TEXT,
    status_code     INTEGER,
    result_summary  TEXT,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS discovery_queries (
    id               INTEGER PRIMARY KEY,
    sector_group     TEXT NOT NULL,
    term             TEXT NOT NULL,
    city             TEXT NOT NULL,
    pages_done       INTEGER NOT NULL DEFAULT 0,
    next_page_token  TEXT,
    status           TEXT NOT NULL DEFAULT 'open',
    results          INTEGER NOT NULL DEFAULT 0,
    accepted         INTEGER NOT NULL DEFAULT 0,
    last_run         TEXT,
    UNIQUE (sector_group, term, city)
);
"""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con


def set_field(
    con: sqlite3.Connection,
    company_id: int,
    field: str,
    value: Any,
    status: str,
    *,
    source_url: str | None = None,
    note: str | None = None,
    verified_at: str | None = None,
) -> bool:
    """Schrijf een veld. Regels:
    - feit vereist een bron-URL, inschatting vereist een motivatie;
    - een veld wordt nooit 'gedowngraded' (feit → inschatting/onbekend).
    Geeft True terug als er geschreven werd.
    """
    if status not in STATUSES:
        raise ValueError(f"onbekende status {status!r}")
    if status == "feit" and not source_url:
        raise ValueError(f"{field}: 'feit' vereist een bron-URL")
    if status == "inschatting" and not note:
        raise ValueError(f"{field}: 'inschatting' vereist een motivatie")
    if status == "onbekend":
        value = None
    elif value is not None and not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)

    row = con.execute(
        "SELECT status FROM fields WHERE company_id=? AND field=?", (company_id, field)
    ).fetchone()
    if row and _RANK[row["status"]] > _RANK[status]:
        return False
    con.execute(
        """INSERT INTO fields(company_id, field, value, status, source_url, verified_at, note)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(company_id, field) DO UPDATE SET
             value=excluded.value, status=excluded.status, source_url=excluded.source_url,
             verified_at=excluded.verified_at, note=excluded.note""",
        (company_id, field, value, status, source_url, verified_at or now(), note),
    )
    return True


def get_fields(con: sqlite3.Connection, company_id: int) -> dict[str, sqlite3.Row]:
    return {
        r["field"]: r
        for r in con.execute("SELECT * FROM fields WHERE company_id=?", (company_id,))
    }


def log_call(
    con: sqlite3.Connection,
    *,
    source: str,
    method: str,
    url: str,
    params: dict | None,
    status_code: int | None,
    result_summary: str | None,
    error: str | None = None,
) -> None:
    con.execute(
        """INSERT INTO api_calls(ts, source, method, url, params, status_code, result_summary, error)
           VALUES (?,?,?,?,?,?,?,?)""",
        (now(), source, method, url,
         json.dumps(params, ensure_ascii=False) if params else None,
         status_code, result_summary, error),
    )
    con.commit()
