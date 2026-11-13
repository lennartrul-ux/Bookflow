"""Automatisch bijwerken van de lokale app vanaf GitHub.

- Vergelijkt de laatste commit van de branch met de geïnstalleerde versie (.fh_version).
- Haalt bij een nieuwe versie de code op en vervangt alleen app-bestanden:
  je data (Documenten/Format House Leads) en .venv blijven onaangeroerd.
- Privé-repository: zet een GitHub-token met leestoegang in Instellingen (GITHUB_TOKEN).
Exitcodes (python -m formathouse.updater): 0 = niets te doen of bijgewerkt,
3 = bijgewerkt én afhankelijkheden gewijzigd (opnieuw installeren), 1 = mislukt.
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests

from .config import PROJECT_ROOT

REPO = os.getenv("FH_UPDATE_REPO", "lennartrul-ux/Bookflow")
BRANCH = os.getenv("FH_UPDATE_BRANCH", "formathouse-leads")
SUBDIR = "formathouse-leads"              # map binnen de repository
VERSION_FILE = PROJECT_ROOT / ".fh_version"
KEEP = {".env", ".venv", "leads.db", "output", ".fh_version", ".git"}
DEP_FILES = {"pyproject.toml", "requirements.txt"}
API = "https://api.github.com"


@dataclass
class Result:
    status: str              # actueel / bijgewerkt / fout / geen_toegang
    message: str
    deps_changed: bool = False
    version: str | None = None


def _headers(token: str) -> dict:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "FormatHouseLeads-updater"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def installed_version() -> str | None:
    try:
        return VERSION_FILE.read_text().strip() or None
    except OSError:
        return None


def latest_version(token: str, session=requests) -> tuple[str | None, str]:
    r = session.get(f"{API}/repos/{REPO}/commits/{BRANCH}", headers=_headers(token), timeout=10)
    if r.status_code in (401, 403, 404):
        return None, ("Geen toegang tot de privé-repository. Vul in Instellingen een GitHub-token "
                      "met leestoegang in om automatisch bij te werken.")
    r.raise_for_status()
    return r.json()["sha"], ""


def apply_zip(data: bytes, root: Path = PROJECT_ROOT) -> bool:
    """Pak de app-bestanden uit de zipball uit over `root`. Geeft True als
    pyproject.toml/requirements.txt gewijzigd zijn."""
    deps_changed = False
    with zipfile.ZipFile(io.BytesIO(data)) as z, tempfile.TemporaryDirectory() as tmp:
        names = z.namelist()
        top = names[0].split("/")[0]
        prefix = f"{top}/{SUBDIR}/"
        members = [n for n in names if n.startswith(prefix) and not n.endswith("/")]
        if not any(n.endswith("formathouse/__init__.py") for n in members):
            raise RuntimeError("download bevat de app niet")
        staging = Path(tmp) / "app"
        for n in members:
            rel = n[len(prefix):]
            if not rel or rel.split("/")[0] in KEEP:
                continue
            dest = (staging / rel).resolve()
            if staging.resolve() not in dest.parents:
                continue  # geen paden buiten de app-map
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read(n))
            info = z.getinfo(n)
            if (info.external_attr >> 16) & 0o111 or rel.endswith(".command"):
                dest.chmod(0o755)
        for f in DEP_FILES:
            new, old = staging / f, root / f
            if new.exists() and (not old.exists() or new.read_bytes() != old.read_bytes()):
                deps_changed = True
        # de codemap volledig vervangen (verwijderde bestanden verdwijnen ook)
        pkg_new, pkg_old = staging / "formathouse", root / "formathouse"
        backup = root / ".formathouse_oud"
        if backup.exists():
            shutil.rmtree(backup)
        if pkg_old.exists():
            pkg_old.rename(backup)
        try:
            shutil.copytree(pkg_new, pkg_old)
        except Exception:
            if backup.exists():
                backup.rename(pkg_old)
            raise
        shutil.rmtree(backup, ignore_errors=True)
        for item in staging.iterdir():
            if item.name == "formathouse":
                continue
            dest = root / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)
    return deps_changed


def update(token: str | None = None, session=requests, root: Path = PROJECT_ROOT) -> Result:
    if (root / ".git").exists() or (root.parent / ".git").exists():
        return Result("actueel", "Ontwikkelversie (git): bijwerken met git pull.")
    token = token if token is not None else os.getenv("GITHUB_TOKEN", "").strip()
    try:
        sha, err = latest_version(token, session)
    except requests.RequestException as e:
        return Result("fout", f"Kon niet controleren op updates ({e.__class__.__name__}).")
    if not sha:
        return Result("geen_toegang", err)
    current = installed_version() if root == PROJECT_ROOT else (
        (root / ".fh_version").read_text().strip() if (root / ".fh_version").exists() else None)
    if current == sha:
        return Result("actueel", "Je hebt de nieuwste versie.", version=sha)
    try:
        r = session.get(f"{API}/repos/{REPO}/zipball/{sha}", headers=_headers(token), timeout=60)
        r.raise_for_status()
        deps = apply_zip(r.content, root)
    except Exception as e:
        return Result("fout", f"Bijwerken mislukt: {e}")
    (root / ".fh_version").write_text(sha)
    return Result("bijgewerkt", "Nieuwe versie geïnstalleerd.", deps_changed=deps, version=sha)


def main() -> int:
    from .config import migrate_old_data
    moved = migrate_old_data()
    if moved:
        print(f"  Data verplaatst naar je datamap: {', '.join(moved)}")
    res = update()
    if res.status != "actueel":
        print(f"  Update: {res.message}")
    if res.status == "fout":
        return 1
    return 3 if res.deps_changed else 0


if __name__ == "__main__":
    sys.exit(main())
