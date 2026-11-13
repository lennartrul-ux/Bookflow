"""Fase 4 — Scoring op 100 punten volgens de briefing.

Met ANTHROPIC_API_KEY: Claude scoort op basis van uitsluitend de verzamelde data
(gestructureerde JSON-output). Zonder sleutel: transparante regelgebaseerde score.
Totaal en prioriteit worden altijd in code berekend uit de deelscores.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Callable

from . import db

CRITERIA = {  # sleutel: (maximum, omschrijving)
    "financiele_draagkracht": (25, "financiële draagkracht / marketingbudget"),
    "contentbehoefte": (20, "concrete contentbehoefte"),
    "maandelijks_potentieel": (20, "potentieel voor maandelijkse samenwerking"),
    "groei_urgentie": (15, "groei / urgentie"),
    "bereikbaarheid": (10, "bereikbaarheid van de beslisser"),
    "visuele_match": (10, "creatieve / visuele match"),
}
BUDGETS = ["< €1.500/maand", "€1.500–€2.500/maand", "€2.500–€5.000/maand",
           "> €5.000/maand", "onbekend"]
PRIORITY_GROUPS = {"horeca", "fitness", "lifestyle"}
# Basispunten 'maandelijks potentieel' per sector (vastgoed: elk pand is nieuwe content).
POTENTIAL_BASE = {"horeca": 10, "fitness": 10, "lifestyle": 10, "immo": 10, "verzekering": 6,
                  "interieur": 9, "bouw": 8, "esthetiek": 10, "padelgolf": 9, "events": 10,
                  "food": 9, "tuin": 8, "auto": 9}

BRIEFING = """\
The Format House is een Belgisch creatief content agency voor professionele short-form video
(Instagram Reels, TikTok, YouTube Shorts). Doelklanten: groeiende, commercieel actieve bedrijven
die social media begrijpen en al in marketing of ads investeren, maar waarvan de videocontent
onvoldoende consistent, kwalitatief of strategisch is. Budget voor terugkerende videoproductie:
ca. €1.500–€5.000/maand. Richtwaarden (geen harde uitsluiting bij ontbrekende data): 5–75
medewerkers (max. 150), jaaromzet €1–20 miljoen, professionele website, actieve socials,
bereikbare marketingverantwoordelijke of beslisser.

Absolute prioriteit: horecagroepen, trendy restaurants, premium hospitality, boutique hotels,
fitnessclubs, premium gyms, pilatesstudio's, wellness, beauty en esthetische centra, lifestyle- en
fashionbrands, e-commerce, consumer brands. Daarnaast: immokantoren en vastgoed (elk pand is nieuwe
content), verzekeringsmakelaars, premium automotive, recruitment,
events/festivals/entertainment en groeiende B2B-bedrijven met terugkerende contentbehoefte.

Extra punten voor: regelmatige lanceringen (producten, vestigingen, events, campagnes), actief
aanwerven, meerdere locaties, Meta-advertenties, bestaande investering in branding/social.
Koopsignaal: actief in de laatste 30 dagen maar 0–8 Reels/maand, inconsistent posten of visueel
onder het potentieel. Lage frequentie is een signaal, geen verplicht criterium.

Uitsluiten: geen aantoonbare commerciële draagkracht, zeer kleine organisaties met lage marges,
geen recente marketingactiviteit, alleen eenmalige video nodig, sterke interne videoafdeling zonder
capaciteitsnood. Sluit NIET uit alleen omdat data ontbreekt.
"""

SYSTEM = f"""Je bent een nuchtere sales-analist voor The Format House.

{BRIEFING}
Je beoordeelt één lead. Redeneer UITSLUITEND op de meegegeven data. Elk veld heeft een status:
'feit' (geverifieerd, met bron), 'inschatting' (afgeleid, met toelichting) of 'onbekend'.
Verzin niets: noem geen cijfers, namen, campagnes of feiten die niet in de data staan. Is iets
onbekend, ga dan uit van een neutrale middenscore voor dat aspect en zeg dat in de motivatie.

Deelscores (gehele getallen): financiele_draagkracht 0–25, contentbehoefte 0–20,
maandelijks_potentieel 0–20, groei_urgentie 0–15, bereikbaarheid 0–10, visuele_match 0–10.
Motivatie per criterium: één of twee korte zinnen, met verwijzing naar de gebruikte velden.
beslisser_functie: alleen een FUNCTIE (bv. 'zaakvoerder', 'marketingverantwoordelijke'), nooit een naam.
salesinsteek: 2–3 zinnen, concreet en gebaseerd op de data, in het Nederlands (Vlaamse toon).
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "deelscores": {
            "type": "object",
            "properties": {
                k: {"type": "object",
                    "properties": {"score": {"type": "integer"}, "motivatie": {"type": "string"}},
                    "required": ["score", "motivatie"], "additionalProperties": False}
                for k in CRITERIA
            },
            "required": list(CRITERIA), "additionalProperties": False,
        },
        "uitsluiten": {"type": "boolean"},
        "uitsluitreden": {"type": "string"},
        "budgetcategorie": {"type": "string", "enum": BUDGETS},
        "contentbehoefte": {"type": "string"},
        "beslisser_functie": {"type": "string"},
        "salesinsteek": {"type": "string"},
    },
    "required": ["deelscores", "uitsluiten", "uitsluitreden", "budgetcategorie",
                 "contentbehoefte", "beslisser_functie", "salesinsteek"],
    "additionalProperties": False,
}

SKIP_FIELDS = {"bron_vermelding", "google_maps_url", "advertenties_check_url"}


def lead_payload(con: sqlite3.Connection, c: sqlite3.Row) -> dict:
    fields = {}
    for r in con.execute("SELECT * FROM fields WHERE company_id=? ORDER BY field", (c["id"],)):
        if r["field"] in SKIP_FIELDS:
            continue
        entry = {"status": r["status"]}
        if r["value"] is not None:
            entry["waarde"] = r["value"]
        if r["note"]:
            entry["toelichting"] = r["note"]
        fields[r["field"]] = entry
    return {"bedrijf": c["name"], "sectorgroep": c["sector_group"], "stad": c["city"],
            "velden": fields}


def priority(total: int) -> str:
    return "hoog" if total >= 75 else "interessant" if total >= 55 else "laag"


def finalize(raw: dict) -> dict:
    """Klem deelscores binnen hun maximum en bereken totaal en prioriteit."""
    total = 0
    for k, (mx, _) in CRITERIA.items():
        s = raw["deelscores"][k]
        s["score"] = max(0, min(int(s["score"]), mx))
        total += s["score"]
    raw["totaal"] = total
    raw["prioriteit"] = "uitgesloten" if raw.get("uitsluiten") else priority(total)
    return raw


# ── Claude ───────────────────────────────────────────────────────────

def score_with_claude(client, model: str, payload: dict) -> dict:
    resp = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content":
                   "Beoordeel deze lead. Data (JSON):\n" + json.dumps(payload, ensure_ascii=False)}],
        output_config={"effort": "medium",
                       "format": {"type": "json_schema", "schema": SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"Claude weigerde deze lead te beoordelen ({resp.stop_details})")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("antwoord afgekapt (max_tokens)")
    text = next(b.text for b in resp.content if b.type == "text")
    data = json.loads(text)
    data["_model"] = resp.model
    return finalize(data)


# ── Regelgebaseerd (zonder API-sleutel) ──────────────────────────────

def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def score_with_rules(payload: dict) -> dict:
    f = payload["velden"]
    val = lambda k: (f.get(k) or {}).get("waarde")          # noqa: E731
    known = lambda k: (f.get(k) or {}).get("status", "onbekend") != "onbekend"  # noqa: E731
    group = payload["sectorgroep"]

    def crit(score, why):
        return {"score": int(round(score)), "motivatie": why}

    # Financiële draagkracht (25)
    fin, why = 8, ["basis 8 (neutraal)"]
    price = val("prijsniveau") or ""
    if price.count("€") >= 3:
        fin += 8; why.append("prijsniveau €€€+ (+8)")
    elif price.count("€") == 2:
        fin += 4; why.append("prijsniveau €€ (+4)")
    reviews = _num(val("google_reviews"))
    if reviews is not None:
        add = 6 if reviews >= 1000 else 4 if reviews >= 300 else 2 if reviews >= 100 else 0
        if add:
            fin += add; why.append(f"{int(reviews)} Google-reviews (+{add})")
    if (_num(val("vestigingen")) or 0) >= 2:
        fin += 4; why.append("meerdere vestigingen (+4)")
    if known("advertenties"):
        fin += 3; why.append("actieve Meta-advertenties (+3)")

    # Contentbehoefte (20)
    need, nwhy = 0, []
    if known("koopsignaal"):
        need += 12; nwhy.append(f"koopsignaal: {val('koopsignaal')} (+12)")
    elif known("reels_30d"):
        r = _num(val("reels_30d")) or 0
        if r > 12:
            need += 3; nwhy.append(f"al {int(r)} Reels/30 d (+3)")
        else:
            need += 7; nwhy.append(f"{int(r)} Reels/30 d (+7)")
    else:
        need += 8; nwhy.append("Instagram-activiteit onbekend: neutraal (+8)")
    if known("instagram"):
        need += 4; nwhy.append("Instagram-profiel aanwezig (+4)")
    if group in POTENTIAL_BASE and group != "verzekering":
        need += 4; nwhy.append("sector met terugkerende contentbehoefte (+4)")

    # Maandelijks potentieel (20)
    base = POTENTIAL_BASE.get(group, 5)
    pot, pwhy = base, [f"sector {group} (+{base})"]
    if known("recente_lanceringen"):
        pot += 5; pwhy.append("terugkerende lanceringen/events (+5)")
    if (_num(val("vestigingen")) or 0) >= 2:
        pot += 5; pwhy.append("meerdere locaties (+5)")

    # Groei / urgentie (15)
    grow, gwhy = 3, ["basis 3"]
    if known("vacatures"):
        grow += 6; gwhy.append("werft aan (+6)")
    if known("recente_lanceringen"):
        grow += 4; gwhy.append("recente/komende lancering (+4)")
    if known("advertenties"):
        grow += 2; gwhy.append("adverteert (+2)")

    # Bereikbaarheid (10)
    reach, rwhy = 0, []
    if known("algemeen_email"):
        reach += 6; rwhy.append("algemeen e-mailadres (+6)")
    if known("algemeen_telefoon"):
        reach += 2; rwhy.append("algemeen telefoonnummer (+2)")
    if known("instagram"):
        reach += 2; rwhy.append("bereikbaar via Instagram (+2)")

    # Visuele match (10)
    vis, vwhy = 3, ["basis 3"]
    if price.count("€") >= 3:
        vis += 3; vwhy.append("premium positionering (+3)")
    if known("instagram"):
        vis += 2; vwhy.append("visueel kanaal actief (+2)")
    rating = _num(val("google_rating"))
    if rating is not None and rating >= 4.4:
        vis += 2; vwhy.append(f"rating {rating} (+2)")

    exclude = known("contentactiviteit") and str(val("contentactiviteit")).startswith("niet actief") \
        and not known("advertenties") and not known("recente_lanceringen")
    budget = "onbekend"
    if price.count("€") >= 3 or (_num(val("vestigingen")) or 0) >= 3:
        budget = "€2.500–€5.000/maand"
    elif group in POTENTIAL_BASE and known("instagram"):
        budget = "€1.500–€2.500/maand"

    signals = [x for x in (val("koopsignaal"), val("groeisignalen")) if x]
    angle = {
        "horeca": "Laat de sfeer, gerechten en het team zien in korte Reels die reservaties opleveren",
        "fitness": "Toon resultaten, coaches en de community in herkenbare short-form reeksen die leden aantrekken",
        "lifestyle": "Breng collecties en lanceringen tot leven met een vaste stroom productvideo's en Reels",
        "immo": "Laat elk pand, de buurt en het team zien in korte video's die sneller kopers en huurders aantrekken",
        "verzekering": "Maak advies tastbaar met korte, herkenbare uitlegvideo's en geef het kantoor een gezicht",
        "interieur": "Toon elk project van ontwerp tot oplevering met voor/na-video's die nieuwe klanten overtuigen",
        "bouw": "Maak van elke werf content: korte projectvideo's en updates die vertrouwen en aanvragen opleveren",
        "esthetiek": "Laat resultaten, behandelingen en het team zien in korte, betrouwbare video's",
        "padelgolf": "Breng de club tot leven met lessen, events en community in een vaste stroom Reels",
        "events": "Laat de locatie elke week verkopen met sfeervideo's van trouwfeesten en events",
        "food": "Vertel het ambacht achter de producten met korte video's uit atelier en winkel",
        "tuin": "Toon tuinen en zwembaden van ontwerp tot resultaat in voor/na-video's",
        "auto": "Zet nieuwe wagens, leveringen en het team in de kijker met korte, strakke video's",
    }.get(group, "Bouw een vaste stroom short-form video rond jullie aanbod")
    insteek = angle + (f". Aanknopingspunt: {signals[0]}." if signals else ".")

    data = {
        "deelscores": {
            "financiele_draagkracht": crit(fin, "; ".join(why)),
            "contentbehoefte": crit(need, "; ".join(nwhy)),
            "maandelijks_potentieel": crit(pot, "; ".join(pwhy)),
            "groei_urgentie": crit(grow, "; ".join(gwhy)),
            "bereikbaarheid": crit(reach, "; ".join(rwhy) or "geen algemeen contact gevonden"),
            "visuele_match": crit(vis, "; ".join(vwhy)),
        },
        "uitsluiten": bool(exclude),
        "uitsluitreden": "geen recente marketingactiviteit gevonden" if exclude else "",
        "budgetcategorie": budget,
        "contentbehoefte": {
            "horeca": "Wekelijkse Reels: gerechten, sfeer, team, events",
            "fitness": "Reeksen met coaches, transformaties, lessen en community",
            "lifestyle": "Productvideo's, lanceringen en behind-the-scenes",
            "immo": "Pandvideo's en -tours, buurtvideo's, team en 'te koop/verkocht'-Reels",
            "verzekering": "Korte uitlegvideo's, klantverhalen en team",
            "interieur": "Voor/na-video's, projecttours, maatwerk in beeld",
            "bouw": "Projectvideo's, werfupdates, opleveringen",
            "esthetiek": "Resultaten (voor/na), uitleg van behandelingen, team",
            "padelgolf": "Lessen, toernooien, events en community",
            "events": "Sfeervideo's van trouwfeesten, events en de locatie",
            "food": "Ambacht in het atelier, nieuwe producten, seizoenscollecties",
            "tuin": "Voor/na-video's, projecttours, seizoensinspiratie",
            "auto": "Nieuwe modellen, leveringen, walkarounds",
        }.get(group, "Terugkerende short-form video rond aanbod en team"),
        "beslisser_functie": "zaakvoerder of marketingverantwoordelijke",
        "salesinsteek": insteek,
    }
    return finalize(data)


# ── Opslag, rangschikking, orchestratie ──────────────────────────────

def store(con: sqlite3.Connection, cid: int, data: dict, method: str) -> None:
    con.execute(
        """INSERT INTO scores(company_id, total, priority, detail, method, scored_at)
           VALUES (?,?,?,?,?,?) ON CONFLICT(company_id) DO UPDATE SET total=excluded.total,
             priority=excluded.priority, detail=excluded.detail, method=excluded.method,
             scored_at=excluded.scored_at""",
        (cid, data["totaal"], data["prioriteit"], json.dumps(data, ensure_ascii=False), method, db.now()))
    how = "Claude-scoring" if method.startswith("claude") else "regelgebaseerde scoring (geen API-sleutel)"
    motiv = " | ".join(f"{CRITERIA[k][1]}: {v['score']}/{CRITERIA[k][0]} — {v['motivatie']}"
                       for k, v in data["deelscores"].items())
    db.set_field(con, cid, "leadscore", data["totaal"], "inschatting", note=f"{how}. {motiv}")
    db.set_field(con, cid, "prioriteit", data["prioriteit"], "inschatting",
                 note=data.get("uitsluitreden") or f"{how}: ≥75 hoog, 55–74 interessant, <55 laag")
    for fld in ("budgetcategorie", "contentbehoefte", "beslisser_functie", "salesinsteek"):
        if data.get(fld) and data[fld] != "onbekend":
            db.set_field(con, cid, fld, data[fld], "inschatting", note=how)
        else:
            db.set_field(con, cid, fld, None, "onbekend")
    con.commit()


def rank(con: sqlite3.Connection, top_n: int = 50) -> int:
    """Rangschik op leadscore, dan maandelijks potentieel en contentbehoefte; markeer top N."""
    rows = []
    for r in con.execute("""SELECT s.* FROM scores s JOIN companies c ON c.id=s.company_id
                            WHERE c.excluded=0"""):
        d = json.loads(r["detail"])
        ds = d["deelscores"]
        excluded = r["priority"] == "uitgesloten"
        rows.append((excluded, -r["total"], -ds["maandelijks_potentieel"]["score"],
                     -ds["contentbehoefte"]["score"], r["company_id"]))
    rows.sort()
    con.execute("UPDATE scores SET rank=NULL, top50=0")
    for i, row in enumerate(rows, start=1):
        excluded, cid = row[0], row[-1]
        con.execute("UPDATE scores SET rank=?, top50=? WHERE company_id=?",
                    (None if excluded else i, int(not excluded and i <= top_n), cid))
    con.commit()
    return min(top_n, sum(1 for r in rows if not r[0]))


SOCIAL_FIELDS = ("instagram", "tiktok", "facebook", "linkedin")


def _no_socials(con: sqlite3.Connection, cid: int) -> bool:
    """Alleen True als de website echt bezocht werd en er geen enkele social link was."""
    step = con.execute("SELECT status FROM pipeline WHERE company_id=? AND step='enrich'",
                       (cid,)).fetchone()
    if not step or step[0] != "ok":
        return False
    return not any(db.value(con, cid, f) for f in SOCIAL_FIELDS)


def run(con: sqlite3.Connection, *, api_key: str = "", model: str = "claude-opus-5-5",
        client=None, limit: int | None = None, redo: bool = False,
        log: Callable = print) -> dict[str, int]:
    stats = {"claude": 0, "regels": 0, "fout": 0}
    if api_key and client is None:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
    for c in db.pending(con, "score", redo=redo, limit=limit):
        payload = lead_payload(con, c)
        try:
            if client is not None:
                data = score_with_claude(client, model, payload)
                method = f"claude:{data.pop('_model', model)}"
                db.log_call(con, source="claude", method="POST", url="messages",
                            params={"bedrijf": c["name"], "model": model}, status_code=200,
                            result_summary=f"score {data['totaal']}")
                stats["claude"] += 1
            else:
                data = score_with_rules(payload)
                method = "regels"
                stats["regels"] += 1
        except Exception as e:
            db.log_call(con, source="claude", method="POST", url="messages",
                        params={"bedrijf": c["name"]}, status_code=None, result_summary=None,
                        error=repr(e)[:500])
            db.mark_step(con, c["id"], "score", "fout", repr(e)[:300])
            stats["fout"] += 1
            log(f"  ✗ {c['name']}: {e!r}")
            continue
        if _no_socials(con, c["id"]) and not db.value(con, c["id"], "algemeen_email") \
                and not data.get("uitsluiten"):
            data["uitsluiten"] = True
            data["uitsluitreden"] = "geen social media en geen algemeen e-mailadres gevonden"
            data["prioriteit"] = "uitgesloten"
        store(con, c["id"], data, method)
        db.mark_step(con, c["id"], "score", "ok", method)
        log(f"  {data['totaal']:>3}  {data['prioriteit']:12} {c['name']}")
    stats["top"] = rank(con)
    return stats
