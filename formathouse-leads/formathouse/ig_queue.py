"""Fase 5b — Instagram-wachtrij (de tool verstuurt zelf NOOIT DM's).

- `build`: zet max. 15 nieuwe leads per dag in de wachtrij (zonder algemeen e-mailadres,
  met Instagram), gesorteerd op leadscore, met een klaargeschreven DM (≤ 400 tekens).
- `today`: toont de lijst en schrijft `output/ig_queue_today.csv` — dat bestand opent
  Claude tijdens de computer use-sessie en zet daar de status op 'verstuurd'.
- `sync`: neemt statuswijzigingen uit dat bestand over in de database.
- `mark`: status handmatig bijwerken.
Elke statuswijziging komt in `ig_log` én in `output/ig_verstuurd_log.csv`; een handle
die ooit in de wachtrij stond, wordt nooit opnieuw toegevoegd.
"""
from __future__ import annotations

import csv
import datetime as dt
import sqlite3
from pathlib import Path
from typing import Callable

from . import db
from .copywriter import DM_MAX, write_dm
from .outreach import lead_for_copy
from .social import handle_from_url

STATUSES = ("te_versturen", "verstuurd", "antwoord", "geen_interesse")
TODAY_FILE = "ig_queue_today.csv"
ALL_FILE = "ig_queue.csv"
LOG_FILE = "ig_verstuurd_log.csv"
COLUMNS = ["prioriteit", "bedrijfsnaam", "handle", "profiel_url", "dm", "status", "datum"]


def _today() -> str:
    return dt.date.today().isoformat()


def build(con: sqlite3.Connection, *, daily_max: int = 15, settings=None, client=None,
          today: str | None = None, log: Callable = print) -> int:
    today = today or _today()
    added_today = con.execute("SELECT COUNT(*) FROM ig_queue WHERE added_on=?", (today,)).fetchone()[0]
    room = max(0, min(daily_max, 15) - added_today)
    if room == 0:
        log(f"Vandaag al {added_today} items toegevoegd (max. {daily_max}).")
        return 0
    rows = con.execute("""
        SELECT c.*, s.total, s.rank FROM companies c JOIN scores s ON s.company_id=c.id
        WHERE c.excluded=0 AND s.priority IN ('hoog','interessant') AND s.rank IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM outreach_messages m WHERE m.company_id=c.id)
          AND NOT EXISTS (SELECT 1 FROM ig_queue q WHERE q.company_id=c.id)
        ORDER BY s.total DESC, s.rank""").fetchall()
    model = getattr(settings, "claude_model", "")
    n = 0
    for c in rows:
        if n >= room:
            break
        if db.value(con, c["id"], "algemeen_email"):
            continue  # die gaan via e-mail
        handle = handle_from_url(db.value(con, c["id"], "instagram"))
        if not handle or con.execute("SELECT 1 FROM ig_queue WHERE handle=?", (handle,)).fetchone():
            continue
        try:
            dm, how = write_dm(lead_for_copy(con, c), client=client, model=model)
        except Exception as e:
            log(f"  ! {c['name']}: Claude-DM mislukt ({e!r}), sjabloon gebruikt")
            dm, how = write_dm(lead_for_copy(con, c))
        assert len(dm) <= DM_MAX
        con.execute("""INSERT INTO ig_queue(handle, company_id, score, profile_url, dm, status,
                           added_on, status_at) VALUES (?,?,?,?,?,?,?,?)""",
                    (handle, c["id"], c["total"], f"https://www.instagram.com/{handle}/", dm,
                     "te_versturen", today, db.now()))
        _log(con, handle, "te_versturen", "build")
        n += 1
        log(f"  + @{handle} ({c['name']}, score {c['total']}, {how})")
    con.commit()
    return n


def _log(con: sqlite3.Connection, handle: str, status: str, via: str,
         out_dir: Path | None = None) -> None:
    ts = db.now()
    con.execute("INSERT INTO ig_log(ts, handle, status, via) VALUES (?,?,?,?)", (ts, handle, status, via))
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / LOG_FILE
        new = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=";")
            if new:
                w.writerow(["tijdstip", "handle", "status", "via"])
            w.writerow([ts, handle, status, via])


def mark(con: sqlite3.Connection, handle: str, status: str, *, via: str = "cli",
         out_dir: Path | None = None) -> str:
    handle = handle.lstrip("@").lower()
    if status not in STATUSES:
        raise ValueError(f"status moet een van {', '.join(STATUSES)} zijn")
    row = con.execute("SELECT status FROM ig_queue WHERE handle=?", (handle,)).fetchone()
    if not row:
        raise KeyError(f"@{handle} staat niet in de wachtrij")
    if row["status"] == status:
        return "ongewijzigd"
    if status == "te_versturen" and row["status"] != "te_versturen":
        raise ValueError(f"@{handle} is al '{row['status']}' — terugzetten kan niet (geen dubbele DM's)")
    con.execute("UPDATE ig_queue SET status=?, status_at=? WHERE handle=?", (status, db.now(), handle))
    _log(con, handle, status, via, out_dir)
    con.commit()
    return f"{row['status']} → {status}"


def today_list(con: sqlite3.Connection, *, limit: int = 15) -> list[sqlite3.Row]:
    return con.execute("""SELECT q.*, c.name FROM ig_queue q JOIN companies c ON c.id=q.company_id
                          WHERE q.status='te_versturen' ORDER BY q.score DESC, q.added_on
                          LIMIT ?""", (limit,)).fetchall()


def write_files(con: sqlite3.Connection, out_dir: Path, *, limit: int = 15) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = today_list(con, limit=limit)
    path = out_dir / TODAY_FILE
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(COLUMNS)
        for i, r in enumerate(rows, start=1):
            w.writerow([i, r["name"], r["handle"], r["profile_url"], r["dm"], r["status"], r["added_on"]])
    with open(out_dir / ALL_FILE, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(COLUMNS + ["laatste_wijziging"])
        for r in con.execute("""SELECT q.*, c.name FROM ig_queue q JOIN companies c ON c.id=q.company_id
                                ORDER BY q.status='te_versturen' DESC, q.score DESC"""):
            w.writerow(["", r["name"], r["handle"], r["profile_url"], r["dm"], r["status"],
                        r["added_on"], r["status_at"]])
    return path


def sync(con: sqlite3.Connection, out_dir: Path, log: Callable = print) -> int:
    """Neem statussen over die in output/ig_queue_today.csv aangepast werden."""
    path = out_dir / TODAY_FILE
    if not path.exists():
        log(f"{path} bestaat niet.")
        return 0
    n = 0
    with open(path, encoding="utf-8-sig", newline="") as f:
        sample = f.read(2048)
        f.seek(0)
        delim = ";" if sample.count(";") >= sample.count(",") else ","
        for r in csv.DictReader(f, delimiter=delim):
            handle, status = (r.get("handle") or "").strip(), (r.get("status") or "").strip()
            if not handle or not status:
                continue
            try:
                res = mark(con, handle, status, via="ig_queue_today.csv", out_dir=out_dir)
            except (KeyError, ValueError) as e:
                log(f"  ! {e}")
                continue
            if res != "ongewijzigd":
                n += 1
                log(f"  @{handle}: {res}")
    return n
