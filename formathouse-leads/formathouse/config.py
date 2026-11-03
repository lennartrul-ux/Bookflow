"""Instellingen uit .env (nooit sleutels in code)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    google_places_api_key: str
    db_path: Path
    output_dir: Path
    region: str

    @classmethod
    def load(cls) -> "Settings":
        def p(name: str, default: str) -> Path:
            v = Path(os.getenv(name) or default)
            return v if v.is_absolute() else PROJECT_ROOT / v

        return cls(
            google_places_api_key=os.getenv("GOOGLE_PLACES_API_KEY", "").strip(),
            db_path=p("LEADS_DB", "leads.db"),
            output_dir=p("OUTPUT_DIR", "output"),
            region=(os.getenv("REGIO") or "vlaanderen").strip().lower(),
        )
