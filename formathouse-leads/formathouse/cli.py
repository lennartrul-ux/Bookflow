"""Command-line interface: `fh --help` (of `python -m formathouse --help`)."""
from __future__ import annotations

from typing import Optional

import typer

from . import db, discover, export as export_mod
from .briefing import CITIES, GROUPS
from .config import Settings
from .httpclient import LoggedClient, RateLimited

app = typer.Typer(help="Leadgeneratie- en outreachtool voor The Format House.",
                  no_args_is_help=True)
discover_app = typer.Typer(help="Fase 1 — bedrijven zoeken via Google Places.",
                           no_args_is_help=True)
app.add_typer(discover_app, name="discover")


def _con():
    s = Settings.load()
    return s, db.connect(s.db_path)


@app.command()
def init():
    """Maak de database en de outputmap aan."""
    s, con = _con()
    s.output_dir.mkdir(parents=True, exist_ok=True)
    typer.echo(f"Database: {s.db_path}\nOutput:   {s.output_dir}")
    if not s.google_places_api_key:
        typer.secho("Let op: GOOGLE_PLACES_API_KEY is nog niet ingesteld in .env",
                    fg=typer.colors.YELLOW)


@discover_app.command("run")
def discover_run(
    group: Optional[list[str]] = typer.Option(
        None, "--group", "-g", help=f"Sectorgroep(en): {', '.join(GROUPS)}"),
    city: Optional[list[str]] = typer.Option(
        None, "--city", "-c", help=f"Stad/steden: {', '.join(c.name for c in CITIES)}"),
    limit: Optional[int] = typer.Option(
        None, "--limit", "-n", help="Max. nieuwe geaccepteerde leads in deze run (pilot)."),
):
    """Zoek kandidaten tot de quota bereikt zijn (hervat automatisch)."""
    s, con = _con()
    for g in group or []:
        if g not in GROUPS:
            raise typer.BadParameter(f"onbekende groep {g!r}")
    client = LoggedClient(con, "google_places", min_interval=0.3)
    try:
        stats = discover.run(con, client, s.google_places_api_key, group_keys=group,
                             city_names=city, limit=limit, log=typer.echo)
    except discover.MissingApiKey as e:
        typer.secho(str(e), fg=typer.colors.RED)
        raise typer.Exit(1)
    except RateLimited as e:
        typer.secho(f"{e} — voortgang is bewaard.", fg=typer.colors.YELLOW)
        raise typer.Exit(2)
    typer.echo(
        f"\nAPI-calls: {stats.calls} · resultaten bekeken: {stats.seen} · "
        f"nieuw geaccepteerd: {stats.new_accepted} · afgewezen: {stats.new_rejected} · "
        f"dubbels: {stats.duplicates}"
    )
    for reason, n in sorted(stats.rejected_reasons.items(), key=lambda x: -x[1]):
        typer.echo(f"  afgewezen – {reason}: {n}")


@discover_app.command("status")
def discover_status():
    """Toon de voortgang per sectorgroep t.o.v. de quota."""
    _s, con = _con()
    for g in GROUPS.values():
        n = discover.accepted_count(con, g.key)
        q = f"/{g.quota}" if g.quota else " (alleen op aanvraag)"
        typer.echo(f"{g.label:55} {n}{q}")
    rej = con.execute("SELECT COUNT(*) FROM companies WHERE excluded=1").fetchone()[0]
    typer.echo(f"\nAfgewezen kandidaten (voorfilter): {rej}")


@app.command()
def export(
    include_excluded: bool = typer.Option(
        False, "--include-excluded", help="Ook afgewezen kandidaten (met reden)."),
):
    """Exporteer naar output/leads.csv en output/leads.xlsx."""
    s, con = _con()
    paths = export_mod.export(con, s.output_dir, include_excluded=include_excluded)
    for kind, p in paths.items():
        typer.echo(f"{kind}: {p}")


@app.command("log")
def show_log(n: int = typer.Option(20, "--n", help="Aantal recente calls.")):
    """Toon de laatste externe calls (bron, tijdstip, resultaat)."""
    _s, con = _con()
    for r in con.execute("SELECT * FROM api_calls ORDER BY id DESC LIMIT ?", (n,)):
        res = r["result_summary"] or (r["error"] or "")[:80]
        typer.echo(f"{r['ts']}  {r['source']:14} {r['status_code']}  {res}")
