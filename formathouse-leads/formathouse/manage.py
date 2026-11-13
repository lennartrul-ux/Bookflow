"""Leads beheren: verwijderen, terugzetten en opnieuw beginnen.

Wie al een mail of DM kreeg, of zich uitschreef, wordt ook na 'opnieuw beginnen'
nooit opnieuw benaderd (tabellen contact_history en suppression blijven bewaard).
"""
from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
from pathlib import Path

from . import db
from .score import rank

REMOVED = "handmatig verwijderd"
# Volgorde respecteert de verwijzingen tussen tabellen.
LEAD_TABLES = ["fields", "pipeline", "scores", "outreach_messages", "outreach_batches", "replies",
               "ig_queue", "ig_log", "discovery_queries", "api_calls", "companies"]


def remove_lead(con: sqlite3.Connection, cid: int) -> str:
    row = con.execute("SELECT name, excluded FROM companies WHERE id=?", (cid,)).fetchone()
    if not row:
        raise KeyError(f"lead {cid} bestaat niet")
    # blijft als 'verwijderd' in de database, zodat een nieuwe zoekopdracht hem niet terugbrengt
    con.execute("UPDATE companies SET excluded=1, exclude_reason=?, updated_at=? WHERE id=?",
                (REMOVED, db.now(), cid))
    con.execute("""UPDATE outreach_messages SET status='gestopt', error='lead verwijderd'
                   WHERE company_id=? AND status IN ('concept','gepland')""", (cid,))
    con.execute("DELETE FROM ig_queue WHERE company_id=? AND status='te_versturen'", (cid,))
    con.commit()
    rank(con)
    return row["name"]


def restore_lead(con: sqlite3.Connection, cid: int) -> str:
    row = con.execute("SELECT name, exclude_reason FROM companies WHERE id=?", (cid,)).fetchone()
    if not row or row["exclude_reason"] != REMOVED:
        raise KeyError(f"lead {cid} staat niet bij de verwijderde leads")
    con.execute("UPDATE companies SET excluded=0, exclude_reason=NULL, updated_at=? WHERE id=?",
                (db.now(), cid))
    con.commit()
    rank(con)
    return row["name"]


def removed_leads(con: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return con.execute("""SELECT id, name, city, updated_at FROM companies WHERE exclude_reason=?
                          ORDER BY updated_at DESC LIMIT ?""", (REMOVED, limit)).fetchall()


def remember_contacts(con: sqlite3.Connection) -> int:
    """Bewaar wie al benaderd werd (e-mail of Instagram) vóór er iets gewist wordt."""
    n = 0
    for r in con.execute("""SELECT DISTINCT lower(email) AS v FROM outreach_messages
                            WHERE status='verstuurd'""").fetchall():
        con.execute("INSERT OR IGNORE INTO contact_history(kind, value, at) VALUES ('email', ?, ?)",
                    (r["v"], db.now()))
        n += 1
    for r in con.execute("""SELECT DISTINCT handle FROM ig_queue
                            WHERE status <> 'te_versturen'""").fetchall():
        con.execute("INSERT OR IGNORE INTO contact_history(kind, value, at) VALUES ('instagram', ?, ?)",
                    (r["handle"].lower(), db.now()))
        n += 1
    con.commit()
    return n


def already_contacted(con: sqlite3.Connection, kind: str, value: str | None) -> bool:
    if not value:
        return False
    return con.execute("SELECT 1 FROM contact_history WHERE kind=? AND value=?",
                       (kind, value.lower())).fetchone() is not None


def backup(db_path: Path, backup_dir: Path) -> Path | None:
    if not db_path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"leads_{dt.datetime.now():%Y-%m-%d_%H%M%S}.db"
    shutil.copy2(db_path, dest)
    return dest


def reset_all(con: sqlite3.Connection) -> dict[str, int]:
    """Wis alle leads en hun historiek. Instellingen, suppressielijst, KBO-gegevens en de
    lijst van reeds benaderde contacten blijven bewaard."""
    remembered = remember_contacts(con)
    counts = {}
    for t in LEAD_TABLES:
        counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        con.execute(f"DELETE FROM {t}")
    con.commit()
    counts["contacten_onthouden"] = remembered
    return counts
