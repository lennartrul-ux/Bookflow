"""Fase 5a — E-mail outreach: concept → jouw goedkeuring per batch → gespreid versturen.

- `draft`: maakt een batch met per lead een eerste mail + 2 opvolgmails (dag 4 en 9).
- `send --batch N` toont wat er zou gebeuren (dry-run). Pas met `--confirm` wordt de batch
  goedgekeurd en ingepland: willekeurige tijdstippen tijdens kantooruren (ma–vr 9–17u),
  max. OUTREACH_DAILY_MAX (≤ 30) per dag over alle batches heen.
- `tick` (bv. elke 10 min via cron) verstuurt wat gepland en goedgekeurd is.
- Opvolgmails stoppen bij een antwoord (`replied`) of uitschrijving (`unsubscribe`).
"""
from __future__ import annotations

import csv
import datetime as dt
import random
import smtplib
import sqlite3
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font

from . import db
from .copywriter import email_footer, write_emails

TZ = ZoneInfo("Europe/Brussels")
OFFICE_START, OFFICE_END = 9, 17
FOLLOWUP_DAYS = {1: 4, 2: 9}
STEP_KEYS = {0: "mail", 1: "opvolging_1", 2: "opvolging_2"}


def _domain(email: str) -> str:
    return "@" + email.split("@", 1)[1].lower()


def is_suppressed(con: sqlite3.Connection, email: str) -> bool:
    e = email.lower()
    return con.execute("SELECT 1 FROM suppression WHERE value IN (?,?)",
                       (e, _domain(e))).fetchone() is not None


def suppress(con: sqlite3.Connection, value: str, reason: str) -> int:
    """Voeg toe aan de suppressielijst en stop alle geplande mails naar dat adres/domein."""
    v = value.strip().lower()
    con.execute("INSERT OR IGNORE INTO suppression(value, reason, added_at) VALUES (?,?,?)",
                (v, reason, db.now()))
    like = f"%{v}" if v.startswith("@") else v
    n = con.execute("""UPDATE outreach_messages SET status='gestopt', error=?
                       WHERE lower(email) LIKE ? AND status IN ('concept','gepland')""",
                    (f"suppressie: {reason}", like)).rowcount
    con.commit()
    return n


def mark_replied(con: sqlite3.Connection, email: str) -> int:
    rows = con.execute("SELECT DISTINCT company_id FROM outreach_messages WHERE lower(email)=?",
                       (email.lower(),)).fetchall()
    for r in rows:
        con.execute("INSERT OR IGNORE INTO replies VALUES (?,?)", (r[0], db.now()))
        con.execute("""UPDATE outreach_messages SET status='gestopt', error='antwoord ontvangen'
                       WHERE company_id=? AND status IN ('concept','gepland')""", (r[0],))
    con.commit()
    return len(rows)


def lead_for_copy(con: sqlite3.Connection, c: sqlite3.Row) -> dict:
    v = lambda f: db.value(con, c["id"], f)  # noqa: E731
    lead = {"bedrijf": c["name"], "stad": c["city"], "sectorgroep": c["sector_group"]}
    for f in ("sector", "website", "salesinsteek", "contentbehoefte", "groeisignalen",
              "koopsignaal", "contentactiviteit", "recente_lanceringen", "categorie",
              "google_categorie", "prijsniveau"):
        if v(f):
            lead[f] = v(f)
    return lead


def candidates(con: sqlite3.Connection, n: int) -> list[sqlite3.Row]:
    rows = con.execute("""
        SELECT c.*, s.rank, s.top50 FROM companies c JOIN scores s ON s.company_id=c.id
        WHERE c.excluded=0 AND s.priority IN ('hoog','interessant') AND s.rank IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM outreach_messages m WHERE m.company_id=c.id)
          AND NOT EXISTS (SELECT 1 FROM ig_queue q WHERE q.company_id=c.id)
        ORDER BY s.top50 DESC, s.rank""").fetchall()
    out = []
    for c in rows:
        email = db.value(con, c["id"], "algemeen_email")
        if email and not is_suppressed(con, email):
            out.append(c)
        if len(out) >= n:
            break
    return out


def draft(con: sqlite3.Connection, out_dir: Path, *, n: int = 25, settings=None, client=None,
          log: Callable = print) -> tuple[int | None, Path | None]:
    leads = candidates(con, n)
    if not leads:
        log("Geen nieuwe leads met algemeen e-mailadres (en prioriteit hoog/interessant).")
        return None, None
    model = getattr(settings, "claude_model", "")
    unsub = getattr(settings, "unsubscribe_email", "")
    sig = getattr(settings, "signature", "Team The Format House")
    bid = con.execute("INSERT INTO outreach_batches(created_at) VALUES (?)", (db.now(),)).lastrowid
    for c in leads:
        email = db.value(con, c["id"], "algemeen_email")
        src = con.execute("SELECT source_url FROM fields WHERE company_id=? AND field='algemeen_email'",
                          (c["id"],)).fetchone()[0]
        try:
            texts, how = write_emails(lead_for_copy(con, c), client=client, model=model)
        except Exception as e:  # val terug op het sjabloon
            log(f"  ! {c['name']}: Claude-tekst mislukt ({e!r}), sjabloon gebruikt")
            texts, how = write_emails(lead_for_copy(con, c))
        footer = email_footer(email=email, source_url=src, unsubscribe=unsub)
        for step, key in STEP_KEYS.items():
            body = texts[key]["tekst"].rstrip() + f"\n\nGroeten,\n{sig}" + footer
            con.execute(
                """INSERT INTO outreach_messages(batch_id, company_id, email, step, delay_days,
                       subject, body) VALUES (?,?,?,?,?,?,?)""",
                (bid, c["id"], email, step, FOLLOWUP_DAYS.get(step, 0), texts[key]["onderwerp"], body))
        log(f"  ✉ {c['name']} <{email}> ({how})")
    con.commit()
    path = export_batch(con, bid, out_dir)
    return bid, path


def export_batch(con: sqlite3.Connection, batch_id: int, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = con.execute("""SELECT m.*, c.name FROM outreach_messages m JOIN companies c
                          ON c.id=m.company_id WHERE batch_id=? ORDER BY m.company_id, m.step""",
                       (batch_id,)).fetchall()
    header = ["bericht_id", "bedrijf", "e-mail", "stap", "dag", "onderwerp", "tekst", "status",
              "gepland", "verstuurd"]
    data = [[r["id"], r["name"], r["email"], ["eerste mail", "opvolging 1", "opvolging 2"][r["step"]],
             r["delay_days"], r["subject"], r["body"], r["status"], r["planned_at"] or "",
             r["sent_at"] or ""] for r in rows]
    with open(out_dir / f"outreach_batch_{batch_id}.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(header)
        w.writerows(data)
    wb = Workbook()
    ws = wb.active
    ws.title = f"Batch {batch_id}"
    ws.append(header)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for d in data:
        ws.append(d)
    for col, width in zip("ABCDEFGHIJ", (10, 28, 30, 13, 6, 40, 90, 11, 20, 20)):
        ws.column_dimensions[col].width = width
    for row in ws.iter_rows(min_row=2):
        row[6].alignment = Alignment(wrap_text=True, vertical="top")
    path = out_dir / f"outreach_batch_{batch_id}.xlsx"
    wb.save(path)
    return path


# ── Plannen en versturen ─────────────────────────────────────────────

def _office_slot(day: dt.date, rnd: random.Random) -> dt.datetime:
    minute = rnd.randint(0, (OFFICE_END - OFFICE_START) * 60 - 1)
    local = dt.datetime.combine(day, dt.time(OFFICE_START), tzinfo=TZ) + dt.timedelta(minutes=minute)
    return local.astimezone(dt.timezone.utc)


def _workday(d: dt.date) -> dt.date:
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


def _load_on(con: sqlite3.Connection, day: dt.date) -> int:
    """Aantal mails gepland of verstuurd op een lokale kalenderdag."""
    n = 0
    for r in con.execute("""SELECT COALESCE(sent_at, planned_at) AS t FROM outreach_messages
                            WHERE status IN ('gepland','verstuurd') AND COALESCE(sent_at, planned_at)
                            IS NOT NULL"""):
        if dt.datetime.fromisoformat(r["t"]).astimezone(TZ).date() == day:
            n += 1
    return n


def _first_free_day(con, start: dt.date, daily_max: int) -> dt.date:
    d = _workday(start)
    while _load_on(con, d) >= daily_max:
        d = _workday(d + dt.timedelta(days=1))
    return d


def approve(con: sqlite3.Connection, batch_id: int, *, daily_max: int, now: dt.datetime | None = None,
            rnd: random.Random | None = None) -> int:
    """Keur een batch goed en plan de eerste mails in over de komende werkdagen."""
    rnd = rnd or random.Random()
    now = now or dt.datetime.now(dt.timezone.utc)
    local_now = now.astimezone(TZ)
    start = local_now.date()
    if local_now.hour >= OFFICE_END - 1:
        start += dt.timedelta(days=1)
    con.execute("UPDATE outreach_batches SET status='goedgekeurd', approved_at=? WHERE id=?",
                (db.now(), batch_id))
    n = 0
    for m in con.execute("""SELECT * FROM outreach_messages WHERE batch_id=? AND step=0
                            AND status='concept' ORDER BY id""", (batch_id,)).fetchall():
        day = _first_free_day(con, start, daily_max)
        slot = _office_slot(day, rnd)
        if slot <= now:  # vandaag al voorbij dat tijdstip: kies een moment later vandaag
            slot = now + dt.timedelta(minutes=rnd.randint(5, 60))
        con.execute("UPDATE outreach_messages SET status='gepland', planned_at=? WHERE id=?",
                    (slot.isoformat(timespec="seconds"), m["id"]))
        n += 1
    con.commit()
    return n


def smtp_sender(settings) -> Callable[[EmailMessage], None]:
    def send(msg: EmailMessage) -> None:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as s:
            s.starttls()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
    return send


BREVO_URL = "https://api.brevo.com/v3/smtp/email"


def brevo_sender(settings) -> Callable[[EmailMessage], None]:
    """Versturen via de Brevo-API over HTTPS (werkt ook waar SMTP-poorten dicht zijn)."""
    import requests

    def send(msg: EmailMessage) -> None:
        payload = {
            "sender": {"name": settings.from_name, "email": settings.from_email},
            "to": [{"email": msg["To"]}],
            "subject": msg["Subject"],
            "textContent": msg.get_content(),
            "headers": {k: msg[k] for k in ("In-Reply-To", "References", "List-Unsubscribe") if msg[k]},
        }
        r = requests.post(BREVO_URL, json=payload, timeout=30,
                          headers={"api-key": settings.brevo_api_key, "accept": "application/json"})
        if r.status_code >= 300:
            raise RuntimeError(f"Brevo {r.status_code}: {r.text[:200]}")
    return send


def default_sender(settings) -> Callable[[EmailMessage], None]:
    return brevo_sender(settings) if getattr(settings, "brevo_api_key", "") else smtp_sender(settings)


def build_message(m: sqlite3.Row, settings, thread_id: str | None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((settings.from_name, settings.from_email))
    msg["To"] = m["email"]
    msg["Subject"] = m["subject"]
    msg["Message-ID"] = make_msgid(domain=settings.from_email.split("@")[-1] or None)
    if thread_id:
        msg["In-Reply-To"] = thread_id
        msg["References"] = thread_id
    unsub = settings.unsubscribe_email or settings.from_email
    msg["List-Unsubscribe"] = f"<mailto:{unsub}?subject=uitschrijven>"
    msg.set_content(m["body"])
    return msg


def tick(con: sqlite3.Connection, settings, *, send: Callable | None = None,
         now: dt.datetime | None = None, rnd: random.Random | None = None,
         log: Callable = print) -> dict[str, int]:
    """Verstuur alle goedgekeurde, geplande mails waarvan het tijdstip voorbij is."""
    if send is None and not settings.can_send:
        raise RuntimeError("E-mailinstellingen ontbreken (SMTP of Brevo + afzender) — er wordt niets verstuurd.")
    send = send or default_sender(settings)
    rnd = rnd or random.Random()
    now = now or dt.datetime.now(dt.timezone.utc)
    stats = {"verstuurd": 0, "gestopt": 0, "uitgesteld": 0, "fout": 0}
    due = con.execute("""SELECT m.* FROM outreach_messages m JOIN outreach_batches b ON b.id=m.batch_id
                         WHERE b.status='goedgekeurd' AND m.status='gepland' AND m.planned_at <= ?
                         ORDER BY m.planned_at""", (now.isoformat(timespec="seconds"),)).fetchall()
    today = now.astimezone(TZ).date()
    for m in due:
        if is_suppressed(con, m["email"]) or con.execute(
                "SELECT 1 FROM replies WHERE company_id=?", (m["company_id"],)).fetchone():
            con.execute("UPDATE outreach_messages SET status='gestopt', error=? WHERE id=?",
                        ("suppressie of antwoord", m["id"]))
            stats["gestopt"] += 1
            continue
        sent_today = sum(1 for r in con.execute(
            "SELECT sent_at FROM outreach_messages WHERE status='verstuurd'")
            if dt.datetime.fromisoformat(r[0]).astimezone(TZ).date() == today)
        local = now.astimezone(TZ)
        if sent_today >= settings.daily_max or local.weekday() >= 5 or \
                not (OFFICE_START <= local.hour < OFFICE_END):
            nxt = _first_free_day(con, today + dt.timedelta(days=1), settings.daily_max)
            con.execute("UPDATE outreach_messages SET planned_at=? WHERE id=?",
                        (_office_slot(nxt, rnd).isoformat(timespec="seconds"), m["id"]))
            stats["uitgesteld"] += 1
            continue
        thread = None
        if m["step"] > 0:
            first = con.execute("""SELECT message_id FROM outreach_messages WHERE company_id=?
                                   AND step=0 AND status='verstuurd'""", (m["company_id"],)).fetchone()
            prev = con.execute("""SELECT * FROM outreach_messages WHERE company_id=? AND step=?""",
                               (m["company_id"], m["step"] - 1)).fetchone()
            if not first or not prev or prev["status"] != "verstuurd":
                if prev is not None and prev["status"] in ("gestopt", "fout"):
                    con.execute("UPDATE outreach_messages SET status='gestopt', error=? WHERE id=?",
                                (f"vorige stap {prev['status']}", m["id"]))
                continue
            # minimale afstand tot de vorige mail (bv. 9 − 4 = 5 dagen)
            earliest = dt.datetime.fromisoformat(prev["sent_at"]).astimezone(TZ).date() + \
                dt.timedelta(days=m["delay_days"] - prev["delay_days"])
            if today < earliest:
                con.execute("UPDATE outreach_messages SET planned_at=? WHERE id=?",
                            (_office_slot(_workday(earliest), rnd).isoformat(timespec="seconds"),
                             m["id"]))
                stats["uitgesteld"] += 1
                continue
            thread = first[0]
        msg = build_message(m, settings, thread)
        try:
            send(msg)
        except Exception as e:
            con.execute("UPDATE outreach_messages SET status='fout', error=? WHERE id=?",
                        (repr(e)[:300], m["id"]))
            db.log_call(con, source="e-mail", method="SEND", url=settings.smtp_host or "brevo",
                        params={"bericht": m["id"]}, status_code=None, result_summary=None,
                        error=repr(e)[:300])
            stats["fout"] += 1
            continue
        sent = now.isoformat(timespec="seconds")
        con.execute("""UPDATE outreach_messages SET status='verstuurd', sent_at=?, message_id=?
                       WHERE id=?""", (sent, msg["Message-ID"], m["id"]))
        db.log_call(con, source="e-mail", method="SEND", url=settings.smtp_host or "brevo",
                    params={"bericht": m["id"], "stap": m["step"]}, status_code=250,
                    result_summary=f"verstuurd naar {m['email']}")
        if m["step"] == 0:
            for f in con.execute("""SELECT * FROM outreach_messages WHERE company_id=? AND step>0
                                    AND status='concept'""", (m["company_id"],)).fetchall():
                day = _workday(today + dt.timedelta(days=f["delay_days"]))
                con.execute("UPDATE outreach_messages SET status='gepland', planned_at=? WHERE id=?",
                            (_office_slot(day, rnd).isoformat(timespec="seconds"), f["id"]))
        stats["verstuurd"] += 1
        log(f"  ✓ stap {m['step']} → {m['email']}")
        con.commit()
    con.commit()
    return stats
