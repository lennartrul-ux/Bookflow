"""Instellingen uit .env (nooit sleutels in code)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
load_dotenv(ENV_PATH)


def save_env(updates: dict[str, str], path: Path = ENV_PATH) -> None:
    """Werk sleutels in .env bij (bestaande regels en commentaar blijven staan)."""
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
    ig_daily_max: int

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(ENV_PATH, override=True)
        def p(name: str, default: str) -> Path:
            v = Path(os.getenv(name) or default)
            return v if v.is_absolute() else PROJECT_ROOT / v

        return cls(
            google_places_api_key=os.getenv("GOOGLE_PLACES_API_KEY", "").strip(),
            db_path=p("LEADS_DB", "leads.db"),
            output_dir=p("OUTPUT_DIR", "output"),
            region=(os.getenv("REGIO") or "vlaanderen").strip().lower(),
            crawl_user_agent=os.getenv("CRAWL_USER_AGENT")
                or "FormatHouseResearch/0.1 (bedrijfsonderzoek; respecteert robots.txt)",
            crawl_delay=float(os.getenv("CRAWL_DELAY_SECONDS") or 3),
            meta_access_token=os.getenv("META_ACCESS_TOKEN", "").strip(),
            meta_ig_user_id=os.getenv("META_IG_BUSINESS_ACCOUNT_ID", "").strip(),
            meta_graph_version=(os.getenv("META_GRAPH_VERSION") or "v23.0").strip(),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
            claude_model=(os.getenv("CLAUDE_MODEL") or "claude-opus-5-5").strip(),
            smtp_host=os.getenv("SMTP_HOST", "").strip(),
            smtp_port=int(os.getenv("SMTP_PORT") or 587),
            smtp_user=os.getenv("SMTP_USER", "").strip(),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            from_name=os.getenv("OUTREACH_FROM_NAME") or "The Format House",
            from_email=os.getenv("OUTREACH_FROM_EMAIL", "").strip(),
            daily_max=min(int(os.getenv("OUTREACH_DAILY_MAX") or 25), 30),
            signature=(os.getenv("OUTREACH_SIGNATURE") or "Team The Format House")
                .replace("\\n", "\n"),
            unsubscribe_email=os.getenv("OUTREACH_UNSUBSCRIBE_EMAIL", "").strip()
                or os.getenv("OUTREACH_FROM_EMAIL", "").strip(),
            ig_daily_max=min(int(os.getenv("IG_DAILY_MAX") or 15), 15),
        )
