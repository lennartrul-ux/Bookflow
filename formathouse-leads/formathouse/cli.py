"""Command-line interface: `fh --help` (of `python -m formathouse --help`)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from . import db, discover, enrich as enrich_mod, export as export_mod
from . import ig_queue as igq, outreach as outreach_mod, score as score_mod, social as social_mod
from .briefing import CITIES, GROUPS
from .config import Settings
from .httpclient import LoggedClient, RateLimited

app = typer.Typer(help="Leadgeneratie- en outreachtool voor The Format House.", no_args_is_help=True)
discover_app = typer.Typer(help="Fase 1 — bedrijven zoeken.", no_args_is_help=True)
enrich_app = typer.Typer(help="Fase 2 — verrijking via website en KBO.", no_args_is_help=True)
social_app = typer.Typer(help="Fase 3 — Instagram & advertenties (officiële Meta API's).",
                         no_args_is_help=True)
score_app = typer.Typer(help="Fase 4 — scoring en top 50.", no_args_is_help=True)
outreach_app = typer.Typer(help="Fase 5 — e-mail outreach (dry-run tot jouw --confirm).",
                           no_args_is_help=True)
ig_app = typer.Typer(help="Fase 5 — Instagram-wachtrij (de tool verstuurt zelf niets).",
                     no_args_is_help=True)
for sub, name in ((discover_app, "discover"), (enrich_app, "enrich"), (social_app, "social"),
                  (score_app, "score"), (outreach_app, "outreach"), (ig_app, "ig_queue")):
    app.add_typer(sub, name=name)


def _con():
    s = Settings.load()
    return s, db.connect(s.db_path)


def _claude(s: Settings):
    if not s.anthropic_api_key:
        return None
    import anthropic
    return anthropic.Anthropic(api_key=s.anthropic_api_key)


def _warn(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.YELLOW)


@app.command()
def init():
    """Maak de database en de outputmap aan en toon welke sleutels ontbreken."""
    s, _con_ = _con()
    s.output_dir.mkdir(parents=True, exist_ok=True)
    typer.echo(f"Database: {s.db_path}\nOutput:   {s.output_dir}\nWerkgebied: {s.region}")
    checks = [
        ("Google Places (Fase 1)", s.google_places_api_key, "zonder: --bron osm (OpenStreetMap)"),
        ("Meta Graph (Fase 3)", s.meta_access_token and s.meta_ig_user_id,
         "zonder: Instagram-cijfers en advertenties blijven 'onbekend'"),
        ("Claude API (Fase 4/5)", s.anthropic_api_key, "zonder: regelgebaseerde score + sjabloonteksten"),
        ("E-mail (Fase 5)", s.can_send, "zonder: alleen concepten, niets versturen"),
    ]
    for name, ok, fallback in checks:
        if ok:
            typer.secho(f"  ✓ {name}", fg=typer.colors.GREEN)
        else:
            _warn(f"  – {name}: niet ingesteld ({fallback})")


# ── Fase 1 ───────────────────────────────────────────────────────────

@discover_app.command("run")
def discover_run(
    group: Optional[list[str]] = typer.Option(None, "--group", "-g",
                                              help=f"Sectorgroep(en): {', '.join(GROUPS)}"),
    city: Optional[list[str]] = typer.Option(None, "--city", "-c",
                                             help=f"Stad/steden: {', '.join(c.name for c in CITIES)}"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n",
                                        help="Max. nieuwe geaccepteerde leads in deze run (pilot)."),
    bron: Optional[str] = typer.Option(None, "--bron",
                                       help="google of osm (standaard: google als er een sleutel is)"),
):
    """Zoek kandidaten tot de quota bereikt zijn (hervat automatisch)."""
    s, con = _con()
    for g in group or []:
        if g not in GROUPS:
            raise typer.BadParameter(f"onbekende groep {g!r}")
    source = bron or ("google" if s.google_places_api_key else "osm")
    if source not in ("google", "osm"):
        raise typer.BadParameter("--bron moet google of osm zijn")
    typer.echo(f"Werkgebied: {s.region} · bron: {source}")
    client = LoggedClient(con, "google_places" if source == "google" else "overpass",
                          min_interval=0.3 if source == "google" else 5.0)
    try:
        stats = discover.run(con, client, s.google_places_api_key, source=source, group_keys=group,
                             city_names=city, limit=limit, region=s.region,
                             user_agent=s.crawl_user_agent, log=typer.echo)
    except (discover.MissingApiKey, ValueError) as e:
        typer.secho(str(e), fg=typer.colors.RED)
        raise typer.Exit(1)
    except RateLimited as e:
        _warn(f"{e} — voortgang is bewaard.")
        raise typer.Exit(2)
    typer.echo(f"\nAPI-calls: {stats.calls} · bekeken: {stats.seen} · nieuw geaccepteerd: "
               f"{stats.new_accepted} · afgewezen: {stats.new_rejected} · dubbels: {stats.duplicates}")
    for reason, n in sorted(stats.rejected_reasons.items(), key=lambda x: -x[1]):
        typer.echo(f"  afgewezen – {reason}: {n}")


@discover_app.command("status")
def discover_status():
    """Toon de voortgang per sectorgroep t.o.v. de quota."""
    _s, con = _con()
    for g in GROUPS.values():
        n = discover.accepted_count(con, g.key)
        typer.echo(f"{g.label:55} {n}{f'/{g.quota}' if g.quota else ' (alleen op aanvraag)'}")
    rej = con.execute("SELECT COUNT(*) FROM companies WHERE excluded=1").fetchone()[0]
    typer.echo(f"\nAfgewezen kandidaten (voorfilter): {rej}")


# ── Fase 2 ───────────────────────────────────────────────────────────

def _crawler(s, con):
    return enrich_mod.Crawler(LoggedClient(con, "website", min_interval=0), s.crawl_user_agent,
                              s.crawl_delay)


@enrich_app.command("run")
def enrich_run(limit: Optional[int] = typer.Option(None, "--limit", "-n"),
               redo: bool = typer.Option(False, "--redo", help="Ook reeds verrijkte bedrijven.")):
    """Bezoek de bedrijfswebsites (robots.txt, rustig tempo) en vul KBO-gegevens aan."""
    s, con = _con()
    stats = enrich_mod.run(con, _crawler(s, con), limit=limit, redo=redo,
                           google_key=s.google_places_api_key,
                           google_client=LoggedClient(con, "google_places", min_interval=0.3),
                           log=typer.echo)
    typer.echo(f"\n{stats}")


@enrich_app.command("test-url")
def enrich_test_url(url: str):
    """Analyseer één website zonder iets op te slaan (handig om te testen)."""
    s, con = _con()
    f = enrich_mod.crawl_site(_crawler(s, con), url)
    typer.echo(f"pagina's:   {f.pages}\nsocials:    {f.socials}\ne-mails:    {f.emails}\n"
               f"KBO-nr:     {f.enterprise_number}\nadressen:   {list(f.addresses)[:8]}\n"
               f"vacatures:  {f.jobs}\nlanceringen:{f.launches}\nrobots-blok:{f.blocked_by_robots}"
               f"\nfouten:     {f.errors}")


@enrich_app.command("kbo-import")
def kbo_import(folder: Path = typer.Argument(..., help="Uitgepakte map van de KBO Open Data-extractie")):
    """Importeer enterprise.csv + code.csv uit KBO Open Data (gratis, na registratie)."""
    _s, con = _con()
    enrich_mod.import_kbo(con, folder, log=typer.echo)


# ── Fase 3 ───────────────────────────────────────────────────────────

@social_app.command("run")
def social_run(limit: Optional[int] = typer.Option(None, "--limit", "-n"),
               redo: bool = typer.Option(False, "--redo")):
    """Instagram Business Discovery + Meta Ad Library (zonder token: 'onbekend')."""
    s, con = _con()
    if not (s.meta_access_token and s.meta_ig_user_id):
        _warn("Geen META_ACCESS_TOKEN / META_IG_BUSINESS_ACCOUNT_ID: Instagram-cijfers en "
              "advertenties blijven 'onbekend' (handles en Ad Library-links worden wel opgeslagen).")
    client = LoggedClient(con, "meta_graph", min_interval=1.0)
    stats = social_mod.run(con, client, version=s.meta_graph_version, ig_user_id=s.meta_ig_user_id,
                           token=s.meta_access_token, limit=limit, redo=redo, log=typer.echo)
    typer.echo(f"\n{stats}")


# ── Fase 4 ───────────────────────────────────────────────────────────

@score_app.command("run")
def score_run(limit: Optional[int] = typer.Option(None, "--limit", "-n"),
              redo: bool = typer.Option(False, "--redo", help="Opnieuw scoren.")):
    """Score elke lead op 100 punten en markeer de top 50."""
    s, con = _con()
    if not s.anthropic_api_key:
        _warn("Geen ANTHROPIC_API_KEY: regelgebaseerde scoring (transparant, maar minder genuanceerd).")
    stats = score_mod.run(con, api_key=s.anthropic_api_key, model=s.claude_model, limit=limit,
                          redo=redo, log=typer.echo)
    typer.echo(f"\n{stats}")


@score_app.command("top")
def score_top(n: int = typer.Option(50, "--n")):
    """Toon de hoogst gerangschikte leads."""
    _s, con = _con()
    for r in con.execute("""SELECT s.rank, s.total, s.priority, c.name, c.city FROM scores s
                            JOIN companies c ON c.id=s.company_id WHERE s.rank IS NOT NULL
                            ORDER BY s.rank LIMIT ?""", (n,)):
        typer.echo(f"{r['rank']:>3}. {r['total']:>3}  {r['priority']:12} {r['name']} ({r['city']})")


# ── Fase 5: e-mail ───────────────────────────────────────────────────

@outreach_app.command("draft")
def outreach_draft(n: int = typer.Option(25, "--n", help="Aantal leads in de batch.")):
    """Schrijf een batch concept-mails (+ opvolgmails) om te bekijken. Er wordt niets verstuurd."""
    s, con = _con()
    bid, path = outreach_mod.draft(con, s.output_dir, n=n, settings=s, client=_claude(s), log=typer.echo)
    if bid:
        typer.echo(f"\nBatch {bid} klaar ter controle: {path}\n"
                   f"Versturen na goedkeuring: fh outreach send --batch {bid} --confirm")


@outreach_app.command("send")
def outreach_send(batch: int = typer.Option(..., "--batch"),
                  confirm: bool = typer.Option(False, "--confirm",
                                               help="Zonder deze vlag: alleen dry-run.")):
    """Keur een batch goed, plan hem in over kantooruren en verstuur wat al aan de beurt is."""
    s, con = _con()
    rows = con.execute("""SELECT m.*, c.name FROM outreach_messages m JOIN companies c
                          ON c.id=m.company_id WHERE batch_id=? AND step=0""", (batch,)).fetchall()
    if not rows:
        typer.secho(f"Batch {batch} bestaat niet of is leeg.", fg=typer.colors.RED)
        raise typer.Exit(1)
    if not confirm:
        typer.echo(f"DRY-RUN — batch {batch}: {len(rows)} eerste mails (+ opvolgmails na 4 en 9 dagen)")
        for r in rows:
            sup = " [SUPPRESSIE]" if outreach_mod.is_suppressed(con, r["email"]) else ""
            typer.echo(f"  {r['name']:35} {r['email']}{sup}  — {r['subject']}")
        typer.echo(f"\nMax. {s.daily_max} per dag, willekeurig tussen 9u en 17u (ma–vr). "
                   "Voeg --confirm toe om goed te keuren.")
        return
    if not s.can_send:
        typer.secho("E-mailinstellingen (SMTP of BREVO_API_KEY + OUTREACH_FROM_EMAIL) ontbreken — niets goedgekeurd.",
                    fg=typer.colors.RED)
        raise typer.Exit(1)
    n = outreach_mod.approve(con, batch, daily_max=s.daily_max)
    typer.echo(f"Batch {batch} goedgekeurd: {n} mails ingepland.")
    stats = outreach_mod.tick(con, s, log=typer.echo)
    typer.echo(f"{stats}\nLaat `fh outreach tick` elke 10 minuten lopen (cron) voor de rest.")
    outreach_mod.export_batch(con, batch, s.output_dir)


@outreach_app.command("tick")
def outreach_tick():
    """Verstuur goedgekeurde mails waarvan het geplande tijdstip voorbij is (voor cron)."""
    s, con = _con()
    try:
        stats = outreach_mod.tick(con, s, log=typer.echo)
    except RuntimeError as e:
        typer.secho(str(e), fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.echo(str(stats))


@outreach_app.command("replied")
def outreach_replied(email: str):
    """Noteer een antwoord: alle verdere opvolgmails naar dit bedrijf stoppen."""
    _s, con = _con()
    typer.echo(f"{outreach_mod.mark_replied(con, email)} bedrijf/bedrijven gemarkeerd; opvolging gestopt.")


@outreach_app.command("unsubscribe")
def outreach_unsubscribe(value: str = typer.Argument(..., help="e-mailadres of @domein"),
                         reason: str = typer.Option("uitgeschreven", "--reden")):
    """Zet een adres of domein op de suppressielijst (wordt nooit meer gemaild)."""
    _s, con = _con()
    n = outreach_mod.suppress(con, value, reason)
    typer.echo(f"{value} op suppressielijst; {n} geplande mail(s) gestopt.")


@outreach_app.command("status")
def outreach_status():
    """Overzicht per batch en status."""
    _s, con = _con()
    for r in con.execute("""SELECT b.id, b.status AS bs, m.status, COUNT(*) n FROM outreach_batches b
                            JOIN outreach_messages m ON m.batch_id=b.id
                            GROUP BY b.id, m.status ORDER BY b.id"""):
        typer.echo(f"batch {r['id']} ({r['bs']}): {r['status']:10} {r['n']}")
    n = con.execute("SELECT COUNT(*) FROM suppression").fetchone()[0]
    typer.echo(f"Suppressielijst: {n} adres(sen)/domein(en)")


# ── Fase 5: Instagram ────────────────────────────────────────────────

@ig_app.command("build")
def ig_build():
    """Voeg vandaag max. 15 nieuwe leads toe (zonder e-mail, met Instagram)."""
    s, con = _con()
    n = igq.build(con, daily_max=s.ig_daily_max, settings=s, client=_claude(s), log=typer.echo)
    igq.write_files(con, s.output_dir, limit=s.ig_daily_max)
    typer.echo(f"{n} nieuw in de wachtrij.")


@ig_app.command("today")
def ig_today():
    """Toon de lijst van vandaag en schrijf output/ig_queue_today.csv."""
    s, con = _con()
    path = igq.write_files(con, s.output_dir, limit=s.ig_daily_max)
    rows = igq.today_list(con, limit=s.ig_daily_max)
    if not rows:
        typer.echo("Niets te versturen. Draai eerst `fh ig_queue build`.")
    for i, r in enumerate(rows, start=1):
        typer.echo(f"\n{i}. @{r['handle']} — {r['name']} (score {r['score']})\n   {r['profile_url']}\n"
                   f"   DM: {r['dm']}")
    typer.echo(f"\nBestand: {path}")


@ig_app.command("mark")
def ig_mark(handle: str, status: str = typer.Argument(..., help=" / ".join(igq.STATUSES))):
    """Werk de status van één handle bij (wordt gelogd)."""
    s, con = _con()
    try:
        res = igq.mark(con, handle, status, out_dir=s.output_dir)
    except (KeyError, ValueError) as e:
        typer.secho(str(e), fg=typer.colors.RED)
        raise typer.Exit(1)
    igq.write_files(con, s.output_dir, limit=s.ig_daily_max)
    typer.echo(f"@{handle.lstrip('@')}: {res}")


@ig_app.command("sync")
def ig_sync():
    """Neem statussen over uit output/ig_queue_today.csv (na de computer use-sessie)."""
    s, con = _con()
    n = igq.sync(con, s.output_dir, log=typer.echo)
    igq.write_files(con, s.output_dir, limit=s.ig_daily_max)
    typer.echo(f"{n} status(sen) bijgewerkt.")


# ── Fase 6 en hulpmiddelen ───────────────────────────────────────────

@app.command()
def pipeline(limit: Optional[int] = typer.Option(None, "--limit", "-n")):
    """Verrijk, analyseer socials en scoor alles wat nog niet verwerkt is."""
    enrich_run(limit=limit, redo=False)
    social_run(limit=limit, redo=False)
    enrich_run(limit=limit, redo=False)  # websites die via Instagram gevonden werden
    score_run(limit=limit, redo=False)


@app.command()
def export(include_excluded: bool = typer.Option(False, "--include-excluded",
                                                 help="Ook afgewezen kandidaten (met reden).")):
    """Exporteer naar output/leads.csv en output/leads.xlsx (alle tabbladen)."""
    s, con = _con()
    for kind, p in export_mod.export(con, s.output_dir, include_excluded=include_excluded).items():
        typer.echo(f"{kind}: {p}")


@app.command("log")
def show_log(n: int = typer.Option(20, "--n", help="Aantal recente calls.")):
    """Toon de laatste externe calls (bron, tijdstip, resultaat)."""
    _s, con = _con()
    for r in con.execute("SELECT * FROM api_calls ORDER BY id DESC LIMIT ?", (n,)):
        res = r["result_summary"] or (r["error"] or "")[:80]
        typer.echo(f"{r['ts']}  {r['source']:14} {r['status_code']}  {r['url'][:60]}  {res}")
