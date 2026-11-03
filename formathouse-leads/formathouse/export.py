"""Export naar CSV en .xlsx, met per veld de status feit / inschatting / onbekend."""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

# (kolomkop, veldsleutel). Velden die nog niet verzameld zijn, staan op 'onbekend'.
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
    ("Google-categorie", "google_categorie"),
    ("Google-rating", "google_rating"),
    ("Google-reviews", "google_reviews"),
    ("Prijsniveau", "prijsniveau"),
    ("Adres", "adres"),
    ("Google Maps", "google_maps_url"),
]
FILLS = {
    "feit": PatternFill("solid", fgColor="D9EAD3"),
    "inschatting": PatternFill("solid", fgColor="FFF2CC"),
    "onbekend": PatternFill("solid", fgColor="EEEEEE"),
}


def _leads(con: sqlite3.Connection, include_excluded: bool):
    where = "" if include_excluded else "WHERE c.excluded = 0"
    companies = con.execute(
        f"SELECT * FROM companies c {where} ORDER BY c.sector_group, c.id"
    ).fetchall()
    for c in companies:
        fields = {r["field"]: r for r in
                  con.execute("SELECT * FROM fields WHERE company_id=?", (c["id"],))}
        yield c, fields


def _cell(fields, key):
    r = fields.get(key)
    if r is None:
        return None, "onbekend", None
    return r["value"], r["status"], r


def export(con: sqlite3.Connection, out_dir: Path, *, include_excluded: bool = False) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = list(_leads(con, include_excluded))

    # ── CSV: waarde + status per kolom ──
    csv_path = out_dir / "leads.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        header = ["id"]
        for h, _ in COLUMNS:
            header += [h, f"{h} [status]"]
        header += ["Bronnen", "Verificatiedatum", "Uitgesloten", "Reden uitsluiting"]
        w.writerow(header)
        for c, fields in rows:
            line = [c["id"]]
            for _, key in COLUMNS:
                v, st, _r = _cell(fields, key)
                line += [v or "", st]
            line += _sources_and_date(fields) + [c["excluded"], c["exclude_reason"] or ""]
            w.writerow(line)

    # ── XLSX ──
    wb = Workbook()
    ws = wb.active
    ws.title = "Alle leads"
    ws.append(["id"] + [h for h, _ in COLUMNS] + ["Bronnen", "Verificatiedatum"]
              + (["Uitgesloten", "Reden uitsluiting"] if include_excluded else []))
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for c, fields in rows:
        r = ws.max_row + 1
        ws.cell(r, 1, c["id"])
        for i, (_, key) in enumerate(COLUMNS, start=2):
            v, st, raw = _cell(fields, key)
            cell = ws.cell(r, i, _num(v) if v is not None else "onbekend")
            cell.fill = FILLS[st]
            tip = st
            if raw is not None and raw["source_url"]:
                tip += f"\nbron: {raw['source_url']}"
            if raw is not None and raw["note"]:
                tip += f"\n{raw['note']}"
            if st != "onbekend":
                cell.comment = Comment(tip, "fh")
        extra = _sources_and_date(fields)
        if include_excluded:
            extra += [c["excluded"], c["exclude_reason"]]
        for j, val in enumerate(extra, start=len(COLUMNS) + 2):
            ws.cell(r, j, val)
    ws.freeze_panes = "C2"
    _widths(ws)

    src = wb.create_sheet("Bronnen")
    src.append(["id", "Bedrijfsnaam", "Veld", "Waarde", "Status", "Bron-URL",
                "Verificatiedatum", "Toelichting"])
    for cell in src[1]:
        cell.font = Font(bold=True)
    for c, fields in rows:
        for key, r in sorted(fields.items()):
            src.append([c["id"], c["name"], key, r["value"], r["status"], r["source_url"],
                        r["verified_at"], r["note"]])
    _widths(src)

    leg = wb.create_sheet("Legenda")
    leg.append(["Status", "Betekenis"])
    for st, txt in [
        ("feit", "Rechtstreeks uit een bron; bron-URL en verificatiedatum in tabblad Bronnen."),
        ("inschatting", "Afgeleid; motivatie staat in de celopmerking en in Bronnen."),
        ("onbekend", "Niet gevonden of nog niet verzameld. Nooit zelf ingevuld."),
    ]:
        leg.append([st, txt])
        leg.cell(leg.max_row, 1).fill = FILLS[st]
    _widths(leg)

    xlsx_path = out_dir / "leads.xlsx"
    wb.save(xlsx_path)
    return {"csv": csv_path, "xlsx": xlsx_path}


def _sources_and_date(fields) -> list:
    urls = sorted({r["source_url"] for r in fields.values() if r["source_url"]})
    dates = [r["verified_at"] for r in fields.values() if r["verified_at"]]
    return [" | ".join(urls), max(dates)[:10] if dates else ""]


def _num(v: str):
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return float(v)
        except (TypeError, ValueError):
            return v


def _widths(ws) -> None:
    for col in ws.columns:
        width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(max(width, 8), 50)
