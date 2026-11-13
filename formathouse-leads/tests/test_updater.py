"""Updater: vervangt alleen app-code, nooit data; meldt geen toegang netjes."""
import io
import zipfile
from types import SimpleNamespace

from formathouse import updater


def zipball(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for path, text in files.items():
            z.writestr(f"lennartrul-ux-Bookflow-abc123/{path}", text)
    return buf.getvalue()


class GH:
    def __init__(self, sha="abc123", zip_bytes=b"", code=200):
        self.sha, self.zip, self.code = sha, zip_bytes, code
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers.get("Authorization")))
        if "/commits/" in url:
            return SimpleNamespace(status_code=self.code, json=lambda: {"sha": self.sha},
                                   raise_for_status=lambda: None)
        return SimpleNamespace(status_code=200, content=self.zip, raise_for_status=lambda: None)


def make_app(root):
    (root / "formathouse").mkdir(parents=True)
    (root / "formathouse" / "__init__.py").write_text("oud")
    (root / "formathouse" / "weg.py").write_text("verdwijnt")
    (root / "pyproject.toml").write_text("deps v1")
    (root / ".env").write_text("GEHEIM=1")
    (root / "leads.db").write_text("data")


def test_update_replaces_code_keeps_data(tmp_path):
    root = tmp_path / "app"
    make_app(root)
    z = zipball({"formathouse-leads/formathouse/__init__.py": "nieuw",
                 "formathouse-leads/formathouse/webapp.py": "app",
                 "formathouse-leads/pyproject.toml": "deps v2",
                 "formathouse-leads/.env": "OVERSCHREVEN",
                 "formathouse-leads/Start Format House.command": "#!/bin/bash",
                 "README.md": "andere map in de repo"})
    gh = GH(zip_bytes=z)
    res = updater.update(token="tok", session=gh, root=root)
    assert res.status == "bijgewerkt" and res.deps_changed
    assert (root / "formathouse" / "__init__.py").read_text() == "nieuw"
    assert not (root / "formathouse" / "weg.py").exists()
    assert (root / ".env").read_text() == "GEHEIM=1"          # data blijft
    assert (root / "leads.db").read_text() == "data"
    assert not (root / "README.md").exists()                   # alleen de app-map
    assert (root / "Start Format House.command").stat().st_mode & 0o111
    assert gh.calls[0][1] == "Bearer tok"
    # tweede keer: al actueel, geen download
    gh2 = GH(zip_bytes=z)
    assert updater.update(token="tok", session=gh2, root=root).status == "actueel"
    assert len(gh2.calls) == 1


def test_private_repo_without_token(tmp_path):
    root = tmp_path / "app"
    make_app(root)
    res = updater.update(token="", session=GH(code=404), root=root)
    assert res.status == "geen_toegang" and "token" in res.message
    assert (root / "formathouse" / "__init__.py").read_text() == "oud"


def test_bad_zip_does_not_break_app(tmp_path):
    root = tmp_path / "app"
    make_app(root)
    res = updater.update(token="t", session=GH(zip_bytes=zipball({"iets/anders.txt": "x"})), root=root)
    assert res.status == "fout"
    assert (root / "formathouse" / "__init__.py").read_text() == "oud"
