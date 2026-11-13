"""Dunne laag zodat dezelfde (SQLite-)queries ook op Postgres/Supabase werken.

Vertaalt: ?-placeholders → %s, INSERT OR IGNORE/REPLACE → ON CONFLICT,
lastrowid → RETURNING id, en geeft rijen terug die zich als sqlite3.Row gedragen.
"""
from __future__ import annotations

import re
import threading

_ID_TABLES = {"companies", "api_calls", "discovery_queries", "outreach_batches",
              "outreach_messages", "ig_log"}
_REPLACE_KEYS = {"kbo_enterprise": "number", "kbo_meta": "key", "app_settings": "key"}
_INSERT_RE = re.compile(r"^\s*INSERT\s+(OR\s+(IGNORE|REPLACE)\s+)?INTO\s+(\w+)\s*(\(([^)]*)\))?",
                        re.I | re.S)


class Row(tuple):
    """Tuple met toegang op kolomnaam, zoals sqlite3.Row."""
    _keys: tuple[str, ...] = ()

    def __new__(cls, values, keys):
        obj = super().__new__(cls, values)
        obj._keys = keys
        return obj

    def __getitem__(self, k):
        if isinstance(k, str):
            return tuple.__getitem__(self, self._keys.index(k))
        return tuple.__getitem__(self, k)

    def keys(self):
        return list(self._keys)


def schema_for_pg(sqlite_schema: str) -> str:
    s = re.sub(r"\b(id\s+)INTEGER PRIMARY KEY\b", r"\1SERIAL PRIMARY KEY", sqlite_schema)
    return s


def translate(sql: str, columns_for: dict[str, list[str]] | None = None) -> tuple[str, bool]:
    """Geeft (postgres_sql, returns_id)."""
    sql = sql.replace("%", "%%").replace("?", "%s")
    m = _INSERT_RE.match(sql)
    returns_id = False
    if m:
        mode, table, cols = (m.group(2) or "").upper(), m.group(3).lower(), m.group(5)
        sql = re.sub(r"^\s*INSERT\s+OR\s+(IGNORE|REPLACE)\s+INTO", "INSERT INTO", sql, flags=re.I)
        if mode == "IGNORE" and "ON CONFLICT" not in sql.upper():
            sql += " ON CONFLICT DO NOTHING"
        elif mode == "REPLACE":
            key = _REPLACE_KEYS[table]
            names = [c.strip() for c in cols.split(",")] if cols else (columns_for or {}).get(table, [])
            sets = ", ".join(f"{c}=EXCLUDED.{c}" for c in names if c != key)
            sql += f" ON CONFLICT ({key}) DO UPDATE SET {sets}" if sets else f" ON CONFLICT ({key}) DO NOTHING"
        if table in _ID_TABLES and mode != "IGNORE":
            sql += " RETURNING id"
            returns_id = True
    return sql, returns_id


class Cursor:
    def __init__(self, cur, returns_id: bool):
        self._cur = cur
        self.lastrowid = None
        self.rowcount = cur.rowcount
        self._rows = None
        if cur.description is not None:
            keys = tuple(d.name for d in cur.description)
            self._rows = [Row(r, keys) for r in cur.fetchall()]
            if returns_id and self._rows:
                self.lastrowid = self._rows[0][0]

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows or [], []
        return rows

    def __iter__(self):
        return iter(self.fetchall())


class Connection:
    """Eén Postgres-verbinding (autocommit) met een sqlite3-achtige interface."""

    _COLUMNS = {"kbo_enterprise": ["number", "status", "juridical_form", "start_date"],
                "kbo_meta": ["key", "value"], "replies": ["company_id", "noted_at"],
                "app_settings": ["key", "value"]}

    def __init__(self, url: str):
        import psycopg
        self._pg = psycopg.connect(url, autocommit=True, prepare_threshold=None)
        self._lock = threading.Lock()

    def _fix_values_only(self, sql: str) -> str:
        # "INSERT INTO t VALUES (...)" zonder kolomnamen: vul ze in voor ON CONFLICT.
        m = re.match(r"^\s*INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(\w+)\s+VALUES", sql, re.I)
        if m and m.group(1).lower() in self._COLUMNS:
            cols = ", ".join(self._COLUMNS[m.group(1).lower()])
            sql = sql[:m.end(1)] + f"({cols})" + sql[m.end(1):]
        return sql

    def execute(self, sql: str, params=()):
        pg_sql, returns_id = translate(self._fix_values_only(sql))
        with self._lock:
            cur = self._pg.execute(pg_sql, tuple(params) if params else None)
            return Cursor(cur, returns_id)

    def executemany(self, sql: str, seq):
        pg_sql, _ = translate(self._fix_values_only(sql))
        with self._lock, self._pg.cursor() as cur:
            cur.executemany(pg_sql, [tuple(p) for p in seq])

    def executescript(self, script: str):
        with self._lock:
            self._pg.execute(script)

    @property
    def closed(self) -> bool:
        return self._pg.closed or self._pg.broken

    def commit(self):
        pass  # autocommit

    def close(self):
        self._pg.close()
