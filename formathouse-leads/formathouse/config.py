"""Instellingen uit .env (nooit sleutels in code)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _data_dir() -> Path:
    """Vaste map voor je data (leads, instellingen, exports), los van de app-code,
    zodat een nieuwe versie van de app er nooit aan komt."""
    custom = os.getenv("FH_DATA_DIR", "").strip()
    if custom:
        return Path(custom).expanduser()
    docs = Path.home() / "Documents"
    return (docs if docs.exists() else Path.home()) / "Format House Leads"


DATA_DIR = _data_dir()
ENV_PATH = DATA_DIR / ".env"


def migrate_old_data() -> list[str]:
    """Eenmalig: .env / leads.db / output uit de app-map naar de datamap verplaatsen."""
    import shutil
    moved = []
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return moved
    for name in (".env", "leads.db", "output"):
        old, new = PROJECT_ROOT / name, DATA_DIR / name
        if old.exists() and not new.exists() and not (name == "output" and not any(
                p.name != ".gitkeep" for p in old.iterdir())):
            shutil.copytree(old, new) if old.is_dir() else shutil.copy2(old, new)
            moved.append(name)
    return moved


load_dotenv(PROJECT_ROOT / ".env")   # oude plek (achterwaarts compatibel)
load_dotenv(ENV_PATH, override=True)


def save_env(updates: dict[str, str], path: Path | None = None) -> None:
    """Werk sleutels in .env bij (bestaande regels en commentaar blijven staan)."""
    path = path or ENV_PATH
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    if not lines and (PROJECT_ROOT / ".env.example").exists():
        lines = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    todo = {k: v.replace("\n", " ").strip() for k, v in updates.items()}
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in todo:
            lines[i] = f"{key}={todo.pop(key)}"
    lines += [f"{k}={v}" for k, v in todo.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    load_dotenv(path, override=True)


def hosted() -> bool:
    """Online-modus: instellingen in de database i.p.v. in .env."""
    return bool(os.getenv("DATABASE_URL", "").strip())


def _db_settings() -> dict[str, str]:
    if not hosted():
        return {}
    try:
        from . import db
        con = db.connect(None)
        return {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM app_settings")}
    except Exception:
        return {}


def save_settings(updates: dict[str, str]) -> None:
    """Lokaal: naar .env. Online: naar de tabel app_settings."""
    if not hosted():
        save_env(updates)
        return
    from . import db
    con = db.connect(None)
    for k, v in updates.items():
        con.execute("INSERT OR REPLACE INTO app_settings(key, value) VALUES (?,?)", (k, v.strip()))


@dataclass(frozen=True)
class Settings:
    google_places_api_key: str
    db_path: Path
    output_dir: Path
    region: str
    crawl_user_agent: str
    crawl_delay: float
    meta_access_token: str
    meta_ig_user_id: str
    meta_graph_version: str
    anthropic_api_key: str
    claude_model: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    from_name: str
    from_email: str
    daily_max: int
    signature: str
    unsubscribe_email: str
    brevo_api_key: str
    ig_daily_max: int

    @property
    def can_send(self) -> bool:
        return bool((self.smtp_host or self.brevo_api_key) and self.from_email)

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(ENV_PATH, override=True)
        overrides = _db_settings()

        def get(name: str, default=None):
            v = overrides.get(name)
            return v if v not in (None, "") else os.getenv(name, default)
        def p(name: str, default: str) -> Path:  # relatieve paden: t.o.v. de datamap
            v = Path(get(name) or default)
            return v if v.is_absolute() else DATA_DIR / v

        return cls(
            google_places_api_key=get("GOOGLE_PLACES_API_KEY", "").strip(),
            db_path=p("LEADS_DB", "leads.db"),
            output_dir=p("OUTPUT_DIR", "output"),
            region=(get("REGIO") or "vlaanderen").strip().lower(),
            crawl_user_agent=get("CRAWL_USER_AGENT")
                or "FormatHouseResearch/0.1 (bedrijfsonderzoek; respecteert robots.txt)",
            crawl_delay=float(get("CRAWL_DELAY_SECONDS") or 3),
            meta_access_token=get("META_ACCESS_TOKEN", "").strip(),
            meta_ig_user_id=get("META_IG_BUSINESS_ACCOUNT_ID", "").strip(),
            meta_graph_version=(get("META_GRAPH_VERSION") or "v23.0").strip(),
            anthropic_api_key=get("ANTHROPIC_API_KEY", "").strip(),
            claude_model=(get("CLAUDE_MODEL") or "claude-opus-5-5").strip(),
            smtp_host=get("SMTP_HOST", "").strip(),
            smtp_port=int(get("SMTP_PORT") or 587),
            smtp_user=get("SMTP_USER", "").strip(),
            smtp_password=get("SMTP_PASSWORD", ""),
            from_name=get("OUTREACH_FROM_NAME") or "The Format House",
            from_email=get("OUTREACH_FROM_EMAIL", "").strip(),
            daily_max=min(int(get("OUTREACH_DAILY_MAX") or 25), 30),
            signature=(get("OUTREACH_SIGNATURE") or "Team The Format House")
                .replace("\\n", "\n"),
            unsubscribe_email=get("OUTREACH_UNSUBSCRIBE_EMAIL", "").strip()
                or get("OUTREACH_FROM_EMAIL", "").strip(),
            brevo_api_key=(get("BREVO_API_KEY") or "").strip(),
            ig_daily_max=min(int(get("IG_DAILY_MAX") or 15), 15),
        )
