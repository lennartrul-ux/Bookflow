"""Fase 6 — Export naar CSV en .xlsx, met per veld de status feit / inschatting / onbekend.

Tabbladen: Alle leads, Top 50, E-mail outreach, Instagram-wachtrij, Bronnen, Legenda.
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# (kolomkop, veldsleutel). Wat (nog) niet verzameld is, staat op 'onbekend'.
COLUMNS: list[tuple[str, str]] = [
    ("Bedrijfsnaam", "bedrijfsnaam"),
    ("Sector", "sector"),
    ("Stad", "stad"),
    ("Website", "website"),
    ("Instagram", "instagram"),
    ("TikTok", "tiktok"),
    ("LinkedIn", "linkedin"),
    ("Medewerkers (indicatief)", "medewerkers"),
    ("Omzet (geverifieerd)", "omzet"),
    ("Aantal vestigingen", "vestigingen"),
    ("Recente contentactiviteit", "contentactiviteit"),
    ("Reels laatste 30 dagen", "reels_30d"),
    ("Zichtbare advertenties", "advertenties"),
    ("Groeisignalen", "groeisignalen"),
    ("Algemeen e-mail", "algemeen_email"),
    ("Algemeen telefoon", "algemeen_telefoon"),
    ("Beslissersfunctie", "beslisser_functie"),
    ("Vermoedelijke contentbehoefte", "contentbehoefte"),
    ("Budgetcategorie", "budgetcategorie"),
    ("Leadscore", "leadscore"),
    ("Prioriteit", "prioriteit"),
    ("Salesinsteek", "salesinsteek"),
    ("Koopsignaal", "koopsignaal"),
    ("Vacatures", "vacatures"),
    ("Recente lanceringen/events", "recente_lanceringen"),
    ("Ondernemingsnummer", "ondernemingsnummer"),
    ("Rechtsvorm", "rechtsvorm"),
    ("Categorie (bron)", "google_categorie"),
    ("Google-rating", "google_rating"),
    ("Google-reviews", "google_reviews"),
    ("Prijsniveau", "prijsniveau"),
    ("Adres", "adres"),
    ("Bronvermelding", "bron_vermelding"),
    ("Ad Library (handmatig)", "advertenties_check_url"),
]
FILLS = {
    "feit": PatternFill("solid", fgColor="D9EAD3"),
    "inschatting": PatternFill("solid", fgColor="FFF2CC"),
    "onbekend": PatternFill("solid", fgColor="EEEEEE"),
}
BOLD = Font(bold=True)


def _leads(con: sqlite3.Connection, include_excluded: bool, where_extra: str = ""):
    where = "WHERE 1=1" if include_excluded else "WHERE c.excluded = 0"
    sql = f"""SELECT c.*, s.rank, s.top50, s.method FROM companies c
              LEFT JOIN scores s ON s.company_id = c.id {where} {where_extra}
              ORDER BY s.rank IS NULL, s.rank, c.sector_group, c.id"""
    for c in con.execute(sql).fetchall():
        fields = {r["field"]: r for r in
                  con.execute("SELECT * FROM fields WHERE company_id=?", (c["id"],))}
        if "google_categorie" not in fields and "categorie" in fields:
            fields["google_categorie"] = fields["categorie"]
        yield c, fields


def _cell(fields, key):
    r = fields.get(key)
    if r is None:
        return None, "onbekend", None
    return r["value"], r["status"], r


def _sources_and_date(fields) -> list:
    urls = sorted({r["source_url"] for r in fields.values() if r["source_url"]})
    dates = [r["verified_at"] for r in fields.values() if r["verified_at"]]
    return [" | ".join(urls), max(dates)[:10] if dates else ""]


def _num(v):
    for cast in (int, float):
        try:
            return cast(v)
        except (TypeError, ValueError):
            pass
    return v


def _widths(ws, cap: int = 50) -> None:
    for col in ws.columns:
        width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(max(width, 8), cap)


def _lead_sheet(ws, rows, include_excluded: bool) -> None:
    head = ["Rang", "Top 50", "id"] + [h for h, _ in COLUMNS] + ["Bronnen", "Verificatiedatum",
                                                                 "Scoremethode"]
    if include_excluded:
        head += ["Uitgesloten", "Reden uitsluiting"]
    ws.append(head)
    for cell in ws[1]:
        cell.font = BOLD
    for c, fields in rows:
        r = ws.max_row + 1
        ws.cell(r, 1, c["rank"])
        ws.cell(r, 2, "ja" if c["top50"] else "")
        ws.cell(r, 3, c["id"])
        for i, (_, key) in enumerate(COLUMNS, start=4):
            v, st, raw = _cell(fields, key)
            cell = ws.cell(r, i, _num(v) if v is not None else "onbekend")
            cell.fill = FILLS[st]
            if st != "onbekend":
                tip = st
                if raw["source_url"]:
                    tip += f"\nbron: {raw['source_url']}"
                if raw["note"]:
                    tip += f"\n{raw['note']}"
                cell.comment = Comment(tip[:1500], "fh", width=420, height=180)
        extra = _sources_and_date(fields) + [c["method"] or ""]
        if include_excluded:
            extra += [c["excluded"], c["exclude_reason"]]
        for j, val in enumerate(extra, start=len(COLUMNS) + 4):
            ws.cell(r, j, val)
    ws.freeze_panes = "E2"
    _widths(ws)


def export(con: sqlite3.Connection, out_dir: Path, *, include_excluded: bool = False) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = list(_leads(con, include_excluded))

    # ── CSV: waarde + status per kolom ──
    csv_path = out_dir / "leads.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        header = ["rang", "top50", "id"]
        for h, _ in COLUMNS:
            header += [h, f"{h} [status]"]
        header += ["Bronnen", "Verificatiedatum", "Uitgesloten", "Reden uitsluiting"]
        w.writerow(header)
        for c, fields in rows:
            line = [c["rank"] or "", int(bool(c["top50"])), c["id"]]
            for _, key in COLUMNS:
                v, st, _r = _cell(fields, key)
                line += [v or "", st]
            line += _sources_and_date(fields) + [c["excluded"], c["exclude_reason"] or ""]
            w.writerow(line)

    wb = Workbook()
    _lead_sheet(wb.active, rows, include_excluded)
    wb.active.title = "Alle leads"
    _lead_sheet(wb.create_sheet("Top 50"), [x for x in rows if x[0]["top50"]], False)

    # ── E-mail outreach ──
    ws = wb.create_sheet("E-mail outreach")
    ws.append(["Batch", "Bericht-id", "Bedrijf", "E-mail", "Stap", "Onderwerp", "Tekst", "Status",
               "Gepland (UTC)", "Verstuurd (UTC)", "Opmerking"])
    for cell in ws[1]:
        cell.font = BOLD
    for m in con.execute("""SELECT m.*, c.name FROM outreach_messages m JOIN companies c
                            ON c.id=m.company_id ORDER BY m.batch_id, m.company_id, m.step"""):
        ws.append([m["batch_id"], m["id"], m["name"], m["email"],
                   ["eerste mail", "opvolging dag 4", "opvolging dag 9"][m["step"]], m["subject"],
                   m["body"], m["status"], m["planned_at"], m["sent_at"], m["error"]])
    _widths(ws, 60)
    for row in ws.iter_rows(min_row=2):
        row[6].alignment = Alignment(wrap_text=True, vertical="top")

    # ── Instagram-wachtrij ──
    ws = wb.create_sheet("Instagram-wachtrij")
    ws.append(["Leadscore", "Bedrijfsnaam", "Handle", "Profiel-URL", "DM", "Status",
               "Datum in wachtrij", "Laatste wijziging"])
    for cell in ws[1]:
        cell.font = BOLD
    for q in con.execute("""SELECT q.*, c.name FROM ig_queue q JOIN companies c ON c.id=q.company_id
                            ORDER BY q.status='te_versturen' DESC, q.score DESC"""):
        ws.append([q["score"], q["name"], "@" + q["handle"], q["profile_url"], q["dm"], q["status"],
                   q["added_on"], q["status_at"]])
    _widths(ws, 70)

    # ── Bronnen ──
    src = wb.create_sheet("Bronnen")
    src.append(["id", "Bedrijfsnaam", "Veld", "Waarde", "Status", "Bron-URL", "Verificatiedatum",
                "Toelichting"])
    for cell in src[1]:
        cell.font = BOLD
    for c, fields in rows:
        for key, r in sorted(fields.items()):
            src.append([c["id"], c["name"], key, r["value"], r["status"], r["source_url"],
                        r["verified_at"], r["note"]])
    _widths(src, 60)

    leg = wb.create_sheet("Legenda")
    leg.append(["Status", "Betekenis"])
    for st, txt in [
        ("feit", "Rechtstreeks uit een bron; bron-URL en verificatiedatum in tabblad Bronnen."),
        ("inschatting", "Afgeleid; motivatie in de celopmerking en in tabblad Bronnen."),
        ("onbekend", "Niet gevonden of niet verzameld. Nooit zelf ingevuld."),
    ]:
        leg.append([st, txt])
        leg.cell(leg.max_row, 1).fill = FILLS[st]
    leg.append([])
    leg.append(["Bronnen", "Google Places API, © OpenStreetMap-bijdragers (ODbL), bedrijfswebsites, "
                "KBO Open Data, Instagram Graph API, Meta Ad Library API."])
    _widths(leg, 110)

    xlsx_path = out_dir / "leads.xlsx"
    wb.save(xlsx_path)
    return {"csv": csv_path, "xlsx": xlsx_path}
