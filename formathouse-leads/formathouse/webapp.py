"""Lokale app: een eenvoudige interface in je browser (alleen op je eigen computer).

Start met het startbestand (dubbelklik) of `python -m formathouse.webapp`.
De app luistert alleen op 127.0.0.1 en is dus niet bereikbaar voor anderen.
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import threading
import time
import traceback
import webbrowser

import hmac
import os
import secrets

from flask import Flask, Response, abort, jsonify, redirect, request, send_file, session

from . import db, discover, enrich, export as export_mod, ig_queue as igq
from . import outreach, score, social
from .briefing import CITIES, GROUPS, REGIONS
from .config import Settings, hosted, save_settings
from .httpclient import LoggedClient

PORT = 8765
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(os.getenv("APP_PASSWORD")),
                  PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30)
_TICKER_STARTED = threading.Event()

# ── Achtergrondtaken ─────────────────────────────────────────────────

JOB = {"name": None, "running": False, "log": collections.deque(maxlen=400),
       "started": None, "finished": None, "error": None}
JOB_LOCK = threading.Lock()


def _log(line: str) -> None:
    JOB["log"].append(f"{dt.datetime.now():%H:%M:%S}  {line}")


def start_job(name: str, fn) -> bool:
    with JOB_LOCK:
        if JOB["running"]:
            return False
        JOB.update(name=name, running=True, started=time.time(), finished=None, error=None)
        JOB["log"].clear()

    def runner():
        _log(f"▶ {name}")
        try:
            fn()
            _log("✓ klaar")
        except Exception as e:  # toon fouten in de app i.p.v. te crashen
            JOB["error"] = str(e)
            _log(f"✗ fout: {e}")
            traceback.print_exc()
        finally:
            JOB.update(running=False, finished=time.time())

    threading.Thread(target=runner, daemon=True).start()
    return True


def _ctx():
    s = Settings.load()
    s.output_dir.mkdir(parents=True, exist_ok=True)
    return s, db.connect(s.db_path)


def _claude(s):
    if not s.anthropic_api_key:
        return None
    import anthropic
    return anthropic.Anthropic(api_key=s.anthropic_api_key)


def job_discover(group: str, city: str | None, limit: int | None):
    s, con = _ctx()
    source = "google" if s.google_places_api_key else "osm"
    _log(f"bron: {'Google Places' if source == 'google' else 'OpenStreetMap'} · werkgebied: {s.region}")
    client = LoggedClient(con, "google_places" if source == "google" else "overpass",
                          min_interval=0.3 if source == "google" else 5.0)
    st = discover.run(con, client, s.google_places_api_key, source=source,
                      group_keys=[group] if group else None,
                      city_names=[city] if city else None, limit=limit, region=s.region,
                      user_agent=s.crawl_user_agent, log=_log)
    _log(f"nieuw: {st.new_accepted} · afgewezen: {st.new_rejected} · dubbel: {st.duplicates}")
    for reason, n in sorted(st.rejected_reasons.items(), key=lambda x: -x[1]):
        _log(f"  afgewezen – {reason}: {n}")


def job_pipeline(limit: int | None):
    s, con = _ctx()
    _log("Stap 2 — websites bezoeken (rustig tempo, kan even duren)…")
    crawler = enrich.Crawler(LoggedClient(con, "website", min_interval=0), s.crawl_user_agent,
                             s.crawl_delay)
    _log(f"  {enrich.run(con, crawler, limit=limit, log=_log)}")
    _log("Stap 3 — Instagram en advertenties…")
    _log(f"  {social.run(con, LoggedClient(con, 'meta_graph', min_interval=1.0), version=s.meta_graph_version, ig_user_id=s.meta_ig_user_id, token=s.meta_access_token, limit=limit, log=_log)}")
    _log("Stap 4 — scoren…")
    _log(f"  {score.run(con, api_key=s.anthropic_api_key, model=s.claude_model, limit=limit, log=_log)}")


def job_rescore():
    s, con = _ctx()
    _log(f"  {score.run(con, api_key=s.anthropic_api_key, model=s.claude_model, redo=True, log=_log)}")


def job_draft(n: int):
    s, con = _ctx()
    bid, path = outreach.draft(con, s.output_dir, n=n, settings=s, client=_claude(s), log=_log)
    if bid:
        _log(f"Batch {bid} klaar ter controle.")


def job_approve(batch: int):
    s, con = _ctx()
    n = outreach.approve(con, batch, daily_max=s.daily_max)
    _log(f"Batch {batch} goedgekeurd: {n} mails ingepland (ma–vr, 9–17u, max {s.daily_max}/dag).")
    _log(f"  {outreach.tick(con, s, log=_log)}")
    outreach.export_batch(con, batch, s.output_dir)


def job_ig_build():
    s, con = _ctx()
    n = igq.build(con, daily_max=s.ig_daily_max, settings=s, client=_claude(s), log=_log)
    igq.write_files(con, s.output_dir, limit=s.ig_daily_max)
    _log(f"{n} nieuw in de wachtrij.")


def ticker():
    """Zolang de app openstaat: elke 10 minuten geplande, goedgekeurde mails versturen."""
    while True:
        time.sleep(600)
        try:
            s, con = _ctx()
            if s.can_send and con.execute(
                    "SELECT 1 FROM outreach_messages WHERE status='gepland' LIMIT 1").fetchone():
                stats = outreach.tick(con, s, log=lambda *_: None)
                if stats.get("verstuurd"):
                    _log(f"automatisch verstuurd: {stats}")
        except Exception:
            traceback.print_exc()


# ── Beveiliging ──────────────────────────────────────────────────────
# Lokaal (geen APP_PASSWORD): alleen bereikbaar via 127.0.0.1.
# Online (APP_PASSWORD ingesteld): iedereen moet eerst inloggen.

def _password() -> str:
    return os.getenv("APP_PASSWORD", "")


@app.before_request
def guard():
    if not _TICKER_STARTED.is_set():
        _TICKER_STARTED.set()
        threading.Thread(target=ticker, daemon=True).start()
    if request.path == "/api/cron/tick":
        return None  # beschermd met CRON_TOKEN
    if not _password():
        host = (request.host or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            abort(403)
    elif request.path not in ("/login", "/healthz") and not session.get("ok"):
        if request.path.startswith("/api/"):
            abort(401)
        return redirect("/login")
    if request.method == "POST" and request.path != "/login" and request.headers.get("X-FH") != "1":
        abort(403)  # alleen verzoeken vanuit de eigen pagina


@app.get("/healthz")
def healthz():
    return "ok"


@app.route("/login", methods=["GET", "POST"])
def login():
    if not _password():
        return redirect("/")
    error = ""
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("password", ""), _password()):
            session.permanent = True
            session["ok"] = True
            return redirect("/")
        time.sleep(1.5)
        error = "Verkeerd wachtwoord."
    return Response(LOGIN_PAGE.replace("__ERROR__", error), mimetype="text/html")


@app.post("/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.post("/api/cron/tick")
def cron_tick():
    """Voor een geplande taak (bv. GitHub Actions): verstuur geplande mails."""
    token = os.getenv("CRON_TOKEN", "")
    if not token or not hmac.compare_digest(request.headers.get("X-Cron-Token", ""), token):
        abort(403)
    s, con = _ctx()
    if not s.can_send:
        return jsonify({"ok": True, "message": "geen e-mailinstellingen"})
    return jsonify({"ok": True, "stats": outreach.tick(con, s, log=lambda *_: None)})


LOGIN_PAGE = """<!doctype html><html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Format House Leads</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f6f5f2;color:#1d1d1b;
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
@media (prefers-color-scheme:dark){body{background:#141413;color:#ecebe6}form{background:#1e1e1c!important;border-color:#33322e!important}
input{background:#1e1e1c;color:#ecebe6;border-color:#33322e!important}button{background:#ecebe6!important;color:#141413!important}}
form{background:#fff;border:1px solid #e4e2dc;border-radius:14px;padding:28px;width:min(340px,90vw);display:grid;gap:12px}
h1{font-size:20px;margin:0}input{font:inherit;padding:10px;border:1px solid #e4e2dc;border-radius:8px}
button{font:inherit;font-weight:600;border:0;border-radius:10px;padding:10px;background:#1d1d1b;color:#fff;cursor:pointer}
.e{color:#b3261e;font-size:14px;min-height:1em}</style></head><body>
<form method="post"><h1>The Format House · Leads</h1>
<input type="password" name="password" placeholder="Wachtwoord" autofocus required>
<div class="e">__ERROR__</div><button>Inloggen</button></form></body></html>"""


# ── API ──────────────────────────────────────────────────────────────

# instellingsnaam → attribuut van Settings (zodat ook waarden uit de database tellen)
_ATTR = {"GOOGLE_PLACES_API_KEY": "google_places_api_key", "ANTHROPIC_API_KEY": "anthropic_api_key",
         "CLAUDE_MODEL": "claude_model", "META_ACCESS_TOKEN": "meta_access_token",
         "META_IG_BUSINESS_ACCOUNT_ID": "meta_ig_user_id", "BREVO_API_KEY": "brevo_api_key",
         "SMTP_HOST": "smtp_host", "SMTP_PORT": "smtp_port", "SMTP_USER": "smtp_user",
         "SMTP_PASSWORD": "smtp_password", "OUTREACH_FROM_EMAIL": "from_email",
         "OUTREACH_UNSUBSCRIBE_EMAIL": "unsubscribe_email", "OUTREACH_DAILY_MAX": "daily_max",
         "CRAWL_USER_AGENT": "crawl_user_agent", "REGIO": "region"}

SETTING_FIELDS = [
    ("GOOGLE_PLACES_API_KEY", "Google Places API-sleutel", True),
    ("ANTHROPIC_API_KEY", "Claude API-sleutel", True),
    ("CLAUDE_MODEL", "Claude-model", False),
    ("META_ACCESS_TOKEN", "Meta access token", True),
    ("META_IG_BUSINESS_ACCOUNT_ID", "Instagram business account-id", False),
    ("BREVO_API_KEY", "Brevo API-sleutel (mail via HTTPS)", True),
    ("SMTP_HOST", "SMTP-server", False),
    ("SMTP_PORT", "SMTP-poort", False),
    ("SMTP_USER", "SMTP-gebruiker", False),
    ("SMTP_PASSWORD", "SMTP-wachtwoord", True),
    ("OUTREACH_FROM_EMAIL", "Afzender (outreach-domein)", False),
    ("OUTREACH_UNSUBSCRIBE_EMAIL", "Adres voor uitschrijvingen", False),
    ("OUTREACH_SIGNATURE", "Handtekening (\\n = nieuwe regel)", False),
    ("OUTREACH_DAILY_MAX", "Max. mails per dag (≤ 30)", False),
    ("CRAWL_USER_AGENT", "User-agent voor websitebezoek", False),
    ("REGIO", "Werkgebied (vlaanderen / belgie / be_nl)", False),
]


@app.get("/api/state")
def state():
    s, con = _ctx()
    settings = []
    for key, label, secret in SETTING_FIELDS:
        val = str(getattr(s, _ATTR.get(key, ""), "") or "") if key in _ATTR else os.getenv(key, "")
        settings.append({"key": key, "label": label, "secret": secret, "set": bool(val),
                         "value": "" if secret else val})
    groups = [{"key": g.key, "label": g.label, "count": discover.accepted_count(con, g.key),
               "quota": g.quota} for g in GROUPS.values()]
    rejected = con.execute("SELECT COUNT(*) FROM companies WHERE excluded=1").fetchone()[0]
    steps = {r["step"]: r["n"] for r in con.execute(
        "SELECT step, COUNT(*) n FROM pipeline WHERE status IN ('ok','overgeslagen') GROUP BY step")}
    active = con.execute("SELECT COUNT(*) FROM companies WHERE excluded=0").fetchone()[0]
    top = []
    for r in con.execute("""SELECT c.id, c.name, c.city, s.rank, s.total, s.priority, s.top50, s.method
                            FROM scores s JOIN companies c ON c.id=s.company_id
                            WHERE s.rank IS NOT NULL ORDER BY s.rank LIMIT 100"""):
        v = lambda f: db.value(con, r["id"], f)  # noqa: E731
        top.append({"rank": r["rank"], "score": r["total"], "priority": r["priority"],
                    "top50": bool(r["top50"]), "name": r["name"], "city": r["city"],
                    "website": v("website"), "instagram": v("instagram"),
                    "email": v("algemeen_email"), "insteek": v("salesinsteek"),
                    "method": r["method"]})
    batches = []
    for b in con.execute("SELECT * FROM outreach_batches ORDER BY id DESC"):
        counts = {r["status"]: r["n"] for r in con.execute(
            "SELECT status, COUNT(*) n FROM outreach_messages WHERE batch_id=? GROUP BY status",
            (b["id"],))}
        first = con.execute("""SELECT COUNT(*) FROM outreach_messages WHERE batch_id=? AND step=0""",
                            (b["id"],)).fetchone()[0]
        batches.append({"id": b["id"], "status": b["status"], "created": b["created_at"][:16].replace("T", " "),
                        "leads": first, "counts": counts})
    ig = [{"handle": r["handle"], "name": r["name"], "url": r["profile_url"], "dm": r["dm"],
           "score": r["score"]} for r in igq.today_list(con, limit=s.ig_daily_max)]
    ig_counts = {r["status"]: r["n"] for r in con.execute(
        "SELECT status, COUNT(*) n FROM ig_queue GROUP BY status")}
    return jsonify({
        "settings": settings, "region": s.region, "groups": groups, "rejected": rejected,
        "active": active, "steps": steps, "top": top, "batches": batches, "ig": ig,
        "ig_counts": ig_counts,
        "can_send": s.can_send, "hosted": hosted(), "login": bool(_password()),
        "suppression": con.execute("SELECT COUNT(*) FROM suppression").fetchone()[0],
        "cities": [c.name for c in CITIES if c.region in REGIONS.get(s.region, {"VL"})],
        "source": "Google Places" if s.google_places_api_key else "OpenStreetMap (gratis)",
        "job": {"name": JOB["name"], "running": JOB["running"], "error": JOB["error"],
                "log": list(JOB["log"])},
    })


def _body() -> dict:
    return request.get_json(silent=True) or {}


def _int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _busy():
    return jsonify({"ok": False, "message": f"Er loopt al een taak: {JOB['name']}"}), 409


@app.post("/api/settings")
def api_settings():
    b = _body()
    allowed = {k for k, _, _ in SETTING_FIELDS}
    updates = {k: str(v) for k, v in b.items() if k in allowed and str(v).strip() != ""}
    if "REGIO" in updates and updates["REGIO"] not in REGIONS:
        return jsonify({"ok": False, "message": "Werkgebied moet vlaanderen, belgie of be_nl zijn."}), 400
    save_settings(updates)
    return jsonify({"ok": True, "message": f"{len(updates)} instelling(en) bewaard."})


@app.post("/api/discover")
def api_discover():
    b = _body()
    group = b.get("group") or ""
    if group and group not in GROUPS:
        abort(400)
    ok = start_job("Leads zoeken", lambda: job_discover(group, b.get("city") or None,
                                                        _int(b.get("limit"))))
    return jsonify({"ok": True}) if ok else _busy()


@app.post("/api/pipeline")
def api_pipeline():
    ok = start_job("Leads verwerken (websites, socials, score)",
                   lambda: job_pipeline(_int(_body().get("limit"))))
    return jsonify({"ok": True}) if ok else _busy()


@app.post("/api/rescore")
def api_rescore():
    ok = start_job("Opnieuw scoren", job_rescore)
    return jsonify({"ok": True}) if ok else _busy()


@app.post("/api/outreach/draft")
def api_draft():
    n = max(1, min(_int(_body().get("n"), 25), 30))
    ok = start_job("Concept-mails schrijven", lambda: job_draft(n))
    return jsonify({"ok": True}) if ok else _busy()


@app.post("/api/outreach/approve")
def api_approve():
    s = Settings.load()
    if not s.can_send:
        return jsonify({"ok": False, "message": "Vul eerst de e-mailinstellingen in (SMTP en afzender)."}), 400
    batch = _int(_body().get("batch"))
    ok = start_job(f"Batch {batch} goedkeuren en versturen", lambda: job_approve(batch))
    return jsonify({"ok": True}) if ok else _busy()


@app.post("/api/outreach/replied")
def api_replied():
    _s, con = _ctx()
    n = outreach.mark_replied(con, (_body().get("email") or "").strip())
    return jsonify({"ok": True, "message": f"Opvolging gestopt voor {n} bedrijf/bedrijven."})


@app.post("/api/outreach/unsubscribe")
def api_unsubscribe():
    _s, con = _ctx()
    v = (_body().get("value") or "").strip()
    if not v or ("@" not in v):
        return jsonify({"ok": False, "message": "Geef een e-mailadres of @domein op."}), 400
    n = outreach.suppress(con, v, "uitgeschreven")
    return jsonify({"ok": True, "message": f"{v} wordt nooit meer gemaild ({n} geplande mail(s) gestopt)."})


@app.post("/api/ig/build")
def api_ig_build():
    ok = start_job("Instagram-wachtrij aanvullen", job_ig_build)
    return jsonify({"ok": True}) if ok else _busy()


@app.post("/api/ig/mark")
def api_ig_mark():
    s, con = _ctx()
    b = _body()
    try:
        res = igq.mark(con, b.get("handle", ""), b.get("status", ""), via="app", out_dir=s.output_dir)
    except (KeyError, ValueError) as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    igq.write_files(con, s.output_dir, limit=s.ig_daily_max)
    return jsonify({"ok": True, "message": f"@{b.get('handle')}: {res}"})


@app.post("/api/ig/sync")
def api_ig_sync():
    s, con = _ctx()
    lines = []
    n = igq.sync(con, s.output_dir, log=lines.append)
    igq.write_files(con, s.output_dir, limit=s.ig_daily_max)
    return jsonify({"ok": True, "message": f"{n} status(sen) overgenomen uit ig_queue_today.csv."})


@app.get("/download/leads.xlsx")
def dl_leads():
    s, con = _ctx()
    paths = export_mod.export(con, s.output_dir)
    return send_file(paths["xlsx"], as_attachment=True,
                     download_name=f"formathouse_leads_{dt.date.today()}.xlsx")


@app.get("/download/batch/<int:batch>.xlsx")
def dl_batch(batch: int):
    s, con = _ctx()
    if not con.execute("SELECT 1 FROM outreach_batches WHERE id=?", (batch,)).fetchone():
        abort(404)
    return send_file(outreach.export_batch(con, batch, s.output_dir), as_attachment=True,
                     download_name=f"outreach_batch_{batch}.xlsx")


@app.get("/download/ig_today.csv")
def dl_ig():
    s, con = _ctx()
    return send_file(igq.write_files(con, s.output_dir, limit=s.ig_daily_max), as_attachment=True,
                     download_name="ig_queue_today.csv")


@app.get("/")
def index():
    return Response(PAGE.replace("__GROUPS__", json.dumps(
        [{"key": g.key, "label": g.label} for g in GROUPS.values()], ensure_ascii=False)),
        mimetype="text/html")


# ── Pagina ───────────────────────────────────────────────────────────

PAGE = r"""<!doctype html>
<html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Format House Leads</title>
<style>
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1b;--muted:#6b6a66;--line:#e4e2dc;--accent:#1d1d1b;
--accent-ink:#fff;--good:#2f7d4f;--warn:#9a6a00;--bad:#b3261e;--soft:#efeee9;--hi:#e8f3ec;--mid:#fdf3d6}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--ink:#ecebe6;--muted:#a3a29c;
--line:#33322e;--accent:#ecebe6;--accent-ink:#141413;--good:#7fc79a;--warn:#e5b94e;--bad:#f2867e;
--soft:#26261f;--hi:#1f3328;--mid:#3a3220}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{padding:20px 24px 8px;max-width:1200px;margin:auto;display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
h1{font-size:22px;margin:0}header .sub{color:var(--muted)}
nav{max-width:1200px;margin:8px auto 0;padding:0 24px;display:flex;gap:4px;flex-wrap:wrap}
nav button{background:none;border:0;padding:8px 14px;border-radius:999px;color:var(--muted);font:inherit;cursor:pointer}
nav button.on{background:var(--accent);color:var(--accent-ink)}
main{max-width:1200px;margin:auto;padding:16px 24px 60px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin-bottom:16px}
.card h2{font-size:16px;margin:0 0 4px}.card p.hint{color:var(--muted);margin:0 0 14px;font-size:14px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:end}
label{display:flex;flex-direction:column;gap:4px;font-size:13px;color:var(--muted)}
input,select,textarea{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:8px;
background:var(--card);color:var(--ink);min-width:0}
.btn{font:inherit;border:0;border-radius:10px;padding:9px 16px;background:var(--accent);color:var(--accent-ink);
cursor:pointer;font-weight:600}.btn.sec{background:var(--soft);color:var(--ink)}
.btn:disabled{opacity:.45;cursor:not-allowed}a.btn{text-decoration:none;display:inline-block}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.stat{background:var(--soft);border-radius:12px;padding:12px 14px}.stat b{font-size:22px;display:block}
.stat span{color:var(--muted);font-size:13px}
.bar{height:6px;background:var(--line);border-radius:3px;margin-top:8px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--good)}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:8px 6px;
border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:500;font-size:13px}
.tbl{overflow-x:auto}.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;background:var(--soft)}
.pill.hoog{background:var(--hi);color:var(--good)}.pill.interessant{background:var(--mid);color:var(--warn)}
#log{background:var(--soft);border-radius:10px;padding:10px 12px;font:12px/1.5 ui-monospace,Menlo,monospace;
max-height:260px;overflow:auto;white-space:pre-wrap;margin-top:10px}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--accent);color:var(--accent-ink);
padding:10px 16px;border-radius:10px;opacity:0;transition:.2s;pointer-events:none;max-width:90vw}.toast.on{opacity:1}
.dm{background:var(--soft);border-radius:10px;padding:10px 12px;margin:8px 0;white-space:pre-wrap}
.ig{border-bottom:1px solid var(--line);padding:12px 0}.ig:last-child{border:0}
.muted{color:var(--muted)}.ok{color:var(--good)}.no{color:var(--warn)}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
section{display:none}section.on{display:block}
@media (max-width:600px){header,nav,main{padding-left:16px;padding-right:16px}}
</style></head><body>
<header><h1>The Format House · Leads</h1><span class="sub" id="sub"></span>
<button class="btn sec" id="logout" style="margin-left:auto;display:none" onclick="post('/logout',{}).then(()=>location.href='/login')">Uitloggen</button></header>
<nav id="nav"></nav>
<main>
<div class="card" id="jobcard" style="display:none"><h2 id="jobname"></h2><div id="log"></div></div>

<section id="s-start">
 <div class="card"><h2>Overzicht</h2><p class="hint">Doel: 300 sterke prospects, waarvan de 50 beste gemarkeerd.</p>
  <div class="stats" id="stats"></div></div>
 <div class="card"><h2>1 · Leads zoeken</h2>
  <p class="hint">Zoekt bedrijven in je werkgebied en filtert ketens, zaken zonder website en zaken buiten Vlaanderen eruit. Bron: <b id="src"></b>.</p>
  <div class="row">
   <label>Sector<select id="d-group"><option value="">alle (volgens quota)</option></select></label>
   <label>Stad<select id="d-city"><option value="">alle steden</option></select></label>
   <label>Max. nieuwe leads<input id="d-limit" type="number" min="1" value="30" style="width:110px"></label>
   <button class="btn" data-job onclick="run('/api/discover',{group:v('d-group'),city:v('d-city'),limit:v('d-limit')})">Zoeken</button>
  </div></div>
 <div class="card"><h2>2 · Leads verwerken</h2>
  <p class="hint">Bezoekt de websites (rustig, volgens robots.txt), kijkt naar Instagram en advertenties en geeft elke lead een score op 100. Alleen nieuwe leads worden verwerkt.</p>
  <div class="row"><button class="btn" data-job onclick="run('/api/pipeline',{})">Verwerken</button>
  <button class="btn sec" data-job onclick="run('/api/rescore',{})">Alles opnieuw scoren</button></div></div>
 <div class="card"><h2>3 · Overzicht downloaden</h2>
  <p class="hint">Excel met de tabbladen Alle leads, Top 50, E-mail outreach, Instagram-wachtrij, Bronnen en Legenda. Groen = feit, geel = inschatting, grijs = onbekend.</p>
  <a class="btn" href="/download/leads.xlsx">Download Excel</a></div>
</section>

<section id="s-leads"><div class="card"><h2>Beste leads</h2>
 <p class="hint">Gerangschikt op score, maandelijks potentieel en contentbehoefte. ★ = top 50.</p>
 <div class="tbl"><table><thead><tr><th>#</th><th>Score</th><th>Bedrijf</th><th>Contact</th><th>Salesinsteek</th></tr></thead>
 <tbody id="top"></tbody></table></div></div></section>

<section id="s-mail">
 <div class="card"><h2>Nieuwe batch concept-mails</h2>
  <p class="hint">Voor leads met een algemeen e-mailadres: een eerste mail en twee opvolgmails (na 4 en 9 dagen). Er wordt niets verstuurd tot je de batch goedkeurt.</p>
  <div class="row"><label>Aantal bedrijven<input id="m-n" type="number" min="1" max="30" value="25" style="width:110px"></label>
  <button class="btn" data-job onclick="run('/api/outreach/draft',{n:v('m-n')})">Mails schrijven</button></div></div>
 <div class="card"><h2>Batches</h2><p class="hint" id="sendhint"></p><div class="tbl"><table>
  <thead><tr><th>Batch</th><th>Gemaakt</th><th>Bedrijven</th><th>Status</th><th></th></tr></thead><tbody id="batches"></tbody></table></div></div>
 <div class="card"><h2>Antwoorden en uitschrijvingen</h2><div class="grid2">
  <div class="row"><label style="flex:1">Antwoord ontvangen van<input id="m-rep" placeholder="info@bedrijf.be"></label>
   <button class="btn sec" onclick="post('/api/outreach/replied',{email:v('m-rep')})">Opvolging stoppen</button></div>
  <div class="row"><label style="flex:1">Uitschrijven (adres of @domein)<input id="m-uns" placeholder="info@bedrijf.be"></label>
   <button class="btn sec" onclick="post('/api/outreach/unsubscribe',{value:v('m-uns')})">Nooit meer mailen</button></div>
 </div><p class="hint" id="supp" style="margin-top:10px"></p></div>
</section>

<section id="s-ig">
 <div class="card"><h2>Instagram-wachtrij van vandaag</h2>
  <p class="hint">Voor leads zonder e-mailadres maar met Instagram. Max. 15 nieuwe per dag. De app verstuurt zelf niets: jij (of Claude via computer use) stuurt de DM en zet hier de status.</p>
  <div class="row"><button class="btn" data-job onclick="run('/api/ig/build',{})">Wachtrij aanvullen</button>
  <a class="btn sec" href="/download/ig_today.csv">Download lijst (CSV)</a>
  <button class="btn sec" onclick="post('/api/ig/sync',{})">Statussen uit CSV overnemen</button></div>
  <p class="hint" id="igc" style="margin-top:10px"></p><div id="ig"></div></div>
</section>

<section id="s-set"><div class="card"><h2>Instellingen</h2>
 <p class="hint">Alles is optioneel. Zonder sleutel gebruikt de app gratis alternatieven of laat ze het veld op 'onbekend'. Sleutels worden nooit opnieuw getoond. Lokaal staan ze in het bestand .env op je computer; in de online versie in je eigen database.</p>
 <div class="grid2" id="settings"></div>
 <div class="row" style="margin-top:14px"><button class="btn" onclick="saveSettings()">Bewaren</button></div></div></section>
</main>
<div class="toast" id="toast"></div>
<script>
const GROUPS=__GROUPS__;const TABS=[["start","Start"],["leads","Leads"],["mail","E-mail"],["ig","Instagram"],["set","Instellingen"]];
let tab=localStorage.getItem("fh-tab")||"start",S=null,wasRunning=false;
const $=id=>document.getElementById(id),v=id=>$(id).value,esc=s=>(s??"").toString().replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function toast(m){const t=$("toast");t.textContent=m;t.classList.add("on");setTimeout(()=>t.classList.remove("on"),3500)}
async function post(url,body){const r=await fetch(url,{method:"POST",headers:{"Content-Type":"application/json","X-FH":"1"},body:JSON.stringify(body)});
 const j=await r.json().catch(()=>({}));if(j.message)toast(j.message);await load();return j}
async function run(url,body){const j=await post(url,body);if(j.ok)toast("Gestart — volg de voortgang bovenaan")}
function setTab(t){tab=t;localStorage.setItem("fh-tab",t);render()}
function render(){
 $("nav").innerHTML=TABS.map(([k,l])=>`<button class="${k==tab?"on":""}" onclick="setTab('${k}')">${l}</button>`).join("");
 document.querySelectorAll("section").forEach(s=>s.classList.toggle("on",s.id=="s-"+tab));
 if(!S)return;
 $("sub").textContent=`werkgebied: ${S.region}`+(S.hosted?" · online":"");$("logout").style.display=S.login?"":"none";$("src").textContent=S.source;
 const total=S.groups.reduce((a,g)=>a+(g.quota?g.count:0),0);
 $("stats").innerHTML=S.groups.filter(g=>g.quota).map(g=>`<div class="stat"><b>${g.count} <span>/ ${g.quota}</span></b><span>${esc(g.label)}</span><div class="bar"><i style="width:${Math.min(100,g.count/g.quota*100)}%"></i></div></div>`).join("")
  +`<div class="stat"><b>${S.steps.score||0}</b><span>gescoord van ${S.active} leads</span></div><div class="stat"><b>${S.rejected}</b><span>afgewezen door voorfilter</span></div>`;
 const j=S.job;$("jobcard").style.display=(j.name?"block":"none");
 $("jobname").innerHTML=j.name?`${j.running?"⏳":(j.error?"✗":"✓")} ${esc(j.name)}${j.running?" — bezig…":(j.error?' — <span class="no">fout</span>':" — klaar")}`:"";
 const L=$("log"),atEnd=L.scrollTop+L.clientHeight>=L.scrollHeight-10;L.textContent=j.log.join("\n");if(atEnd)L.scrollTop=L.scrollHeight;
 document.querySelectorAll("button[data-job]").forEach(b=>b.disabled=j.running);
 $("top").innerHTML=S.top.length?S.top.map(r=>`<tr><td>${r.top50?"★ ":""}${r.rank}</td><td><b>${r.score}</b><br><span class="pill ${r.priority}">${r.priority}</span></td>
  <td><b>${esc(r.name)}</b><br><span class="muted">${esc(r.city||"")}</span>${r.website?`<br><a href="${esc(r.website)}" target="_blank">website</a>`:""}</td>
  <td>${r.email?esc(r.email)+"<br>":""}${r.instagram?`<a href="${esc(r.instagram)}" target="_blank">Instagram</a>`:""}</td><td>${esc(r.insteek||"")}</td></tr>`).join("")
  :`<tr><td colspan="5" class="muted">Nog geen gescoorde leads. Ga naar Start → zoeken en verwerken.</td></tr>`;
 $("sendhint").innerHTML=S.can_send?"Goedkeuren plant de mails in: max. per dag, willekeurig tussen 9u en 17u op werkdagen."+(S.hosted?" Ze vertrekken vanzelf (via de geplande taak) of zodra de app openstaat.":" Laat de app openstaan om ze te laten vertrekken.")
  :'<span class="no">Versturen kan pas als de e-mailinstellingen ingevuld zijn (tab Instellingen). Concepten maken en bekijken kan wel.</span>';
 $("batches").innerHTML=S.batches.length?S.batches.map(b=>{const c=Object.entries(b.counts).map(([k,n])=>`${n} ${k}`).join(", ");
  return `<tr><td>${b.id}</td><td>${b.created}</td><td>${b.leads}</td><td>${esc(c)}</td><td style="white-space:nowrap"><a class="btn sec" href="/download/batch/${b.id}.xlsx">Bekijken</a>
  ${b.status=="concept"?`<button class="btn" ${S.can_send?"":"disabled"} onclick="if(confirm('Batch ${b.id} goedkeuren? De mails worden ingepland en verstuurd.'))run('/api/outreach/approve',{batch:${b.id}})">Goedkeuren</button>`:'<span class="ok">goedgekeurd</span>'}</td></tr>`}).join("")
  :`<tr><td colspan="5" class="muted">Nog geen batches.</td></tr>`;
 $("supp").textContent=`Suppressielijst: ${S.suppression} adres(sen)/domein(en).`;
 $("igc").textContent=Object.keys(S.ig_counts).length?"Totaal: "+Object.entries(S.ig_counts).map(([k,n])=>`${n} ${k.replace("_"," ")}`).join(" · "):"";
 $("ig").innerHTML=S.ig.length?S.ig.map(r=>`<div class="ig"><b>@${esc(r.handle)}</b> — ${esc(r.name)} <span class="muted">(score ${r.score})</span>
  <div class="dm">${esc(r.dm)}</div><div class="row"><a class="btn sec" href="${esc(r.url)}" target="_blank">Profiel openen</a>
  <button class="btn sec" onclick="navigator.clipboard.writeText(${esc(JSON.stringify(r.dm))});toast('DM gekopieerd')">Kopieer DM</button>
  <button class="btn" onclick="post('/api/ig/mark',{handle:'${r.handle}',status:'verstuurd'})">Verstuurd</button>
  <button class="btn sec" onclick="post('/api/ig/mark',{handle:'${r.handle}',status:'geen_interesse'})">Overslaan</button></div></div>`).join("")
  :'<p class="muted">Niets te versturen vandaag. Klik op “Wachtrij aanvullen”.</p>';
}
function renderSettings(){$("settings").innerHTML=S.settings.map(f=>`<label>${esc(f.label)} ${f.set?'<span class="ok">✓ ingesteld</span>':'<span class="muted">niet ingesteld</span>'}
 <input data-key="${f.key}" type="${f.secret?"password":"text"}" placeholder="${f.secret&&f.set?"•••••• (laat leeg om te behouden)":""}" value="${esc(f.value)}"></label>`).join("")}
async function saveSettings(){const b={};document.querySelectorAll("#settings input").forEach(i=>{if(i.value.trim())b[i.dataset.key]=i.value});
 await post("/api/settings",b);renderSettings()}
async function load(){const r=await fetch("/api/state");if(r.status==401){location.href="/login";return}S=await r.json();
 if(!$("d-group").dataset.f){$("d-group").innerHTML+=GROUPS.map(g=>`<option value="${g.key}">${esc(g.label)}</option>`).join("");
  $("d-city").innerHTML+=S.cities.map(c=>`<option>${esc(c)}</option>`).join("");$("d-group").dataset.f=1;renderSettings()}
 if(wasRunning&&!S.job.running)toast(S.job.error?"Taak gestopt met een fout — zie het logboek":"Klaar!");wasRunning=S.job.running;render()}
load();setInterval(load,2000);
</script></body></html>"""


def main(open_browser: bool = True) -> None:
    s, _con = _ctx()
    url = f"http://127.0.0.1:{PORT}/"
    print(f"\n  The Format House · Leads draait op {url}\n  Sluit dit venster om de app te stoppen.\n")
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    import logging
    import flask.cli
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    flask.cli.show_server_banner = lambda *a, **k: None
    try:
        app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False, threaded=True)
    except OSError:
        # draait al (bv. tweede dubbelklik): open gewoon de bestaande app
        print("  De app draait al — ik open hem in je browser.")
        webbrowser.open(url)


if __name__ == "__main__":
    main()
