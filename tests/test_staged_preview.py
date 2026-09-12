"""Staged-build live preview (A82): website rendering + what-was-made.

Covers path safety (traversal, protected dirs, sensitive names, symlink
escape), candidate discovery, entry management, the bounded content
viewer, raw-byte serving rules, the HTTP API, and an end-to-end run in
which a real Supervisor transaction builds a website that the preview
then serves.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import (  # noqa: E402
    drive_to_terminal,
    login,
    make_client,
    make_plane,
    make_repo,
)

from forge.control.control_plane import (  # noqa: E402
    InvalidRequest,
    NotFound,
    RunStatus,
)
from forge.control.db import Database  # noqa: E402
from forge.staged.preview import (  # noqa: E402
    PreviewError,
    normalize_preview_path,
    resolve_under_root,
    scan_candidates,
)
from forge.staged.service import StagedBuilds  # noqa: E402

PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


# -- fakes ---------------------------------------------------------------


class FakeRuns:
    def __init__(self) -> None:
        self._runs: dict = {}

    def get(self, run_id: str):
        return self._runs.get(run_id)


class FakePlane:
    def __init__(self, db: Database, root: str) -> None:
        self._db = db
        self.runs = FakeRuns()
        self._root = root

    def get_project(self, project_id: str):
        return SimpleNamespace(id=project_id, root=self._root)


def _session(project_id: str = "demo", actor: str = "tester"):
    return SimpleNamespace(project_id=project_id, actor=actor)


@pytest.fixture()
def root(tmp_path):
    proj = tmp_path / "proj"
    (proj / "public").mkdir(parents=True)
    (proj / "node_modules" / "pkg").mkdir(parents=True)
    (proj / ".git").mkdir(parents=True)
    (proj / "index.html").write_text("<h1>Hello Forge</h1>\n")
    (proj / "public" / "about.html").write_text("about\n")
    (proj / "styles.css").write_text("body { color: red; }\n")
    (proj / "app.py").write_text("print('hi')\n")
    (proj / "logo.png").write_bytes(PNG_1PX)
    (proj / "blob.bin").write_bytes(bytes(range(256)))
    (proj / "big.txt").write_text("x" * 300_000)
    (proj / ".env").write_text("TOKEN=abc\n")
    (proj / "id_rsa").write_text("private\n")
    (proj / "node_modules" / "pkg" / "skip.html").write_text("skip\n")
    (proj / ".git" / "hidden.html").write_text("skip\n")
    return proj


@pytest.fixture()
def svc(tmp_path, root):
    plane = FakePlane(Database(str(tmp_path / "p.db")), str(root))
    return StagedBuilds(plane)


@pytest.fixture()
def build(svc):
    return svc.create_build(_session(), "Web")


# -- path safety ---------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("index.html", "index.html"),
    ("./public/about.html", "public/about.html"),
    ("a\\b.html", "a/b.html"),
    ("  styles.css  ", "styles.css"),
])
def test_normalize_accepts_benign_paths(raw, expected):
    assert normalize_preview_path(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", "/etc/passwd", "../x", "a/../../x", "..", "~/.bashrc",
    "a\x00b", ".git/config", "x/.Git/y", ".forge/memory/z", ".env",
    "sub/.env", "id_rsa", "key.pem", "tls.key", "db.p12",
    "my_secret.txt", "credentials.json", "x" * 513, None, 123,
])
def test_normalize_rejects_escape_protected_and_sensitive(raw):
    with pytest.raises(PreviewError):
        normalize_preview_path(raw)


def test_resolve_defeats_symlink_escape(tmp_path, root):
    outside = tmp_path / "outside.txt"
    outside.write_text("nope\n")
    link = root / "link.html"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(PreviewError):
        resolve_under_root(str(root), "link.html")


def test_scan_candidates_prefers_conventional_and_skips(root):
    candidates = scan_candidates(str(root))
    assert candidates[0] == "index.html"
    assert "public/about.html" in candidates
    assert not any("node_modules" in item for item in candidates)
    assert not any(".git" in item for item in candidates)


# -- entries -------------------------------------------------------------

def test_preview_entry_lifecycle(svc, build):
    session = _session()
    assert svc.get_preview(session, build.id)["entry"] == ""
    svc.set_preview_entry(session, build.id, "public/about.html")
    meta = svc.get_preview(session, build.id)
    assert meta["entry"] == "public/about.html"
    assert meta["entry_exists"] is True
    assert meta["candidates"][0] == "index.html"
    svc.set_preview_entry(session, build.id, "")
    assert svc.get_preview(session, build.id)["entry"] == ""


@pytest.mark.parametrize("entry", [
    "missing.html", "app.py", "styles.css", "../index.html",
    "/index.html", ".env", "logo.png",
])
def test_preview_entry_rejects_bad_values(svc, build, entry):
    with pytest.raises(InvalidRequest):
        svc.set_preview_entry(_session(), build.id, entry)


def test_preview_is_project_scoped(svc, build):
    with pytest.raises(NotFound):
        svc.get_preview(_session("other"), build.id)
    with pytest.raises(NotFound):
        svc.read_preview_file(_session("other"), build.id, "index.html")


# -- content viewer + raw serving ----------------------------------------

def test_read_preview_file_kinds(svc, build):
    session = _session()
    text = svc.read_preview_file(session, build.id, "app.py")
    assert text["kind"] == "text"
    assert "print" in text["content"]
    image = svc.read_preview_file(session, build.id, "logo.png")
    assert image["kind"] == "image"
    assert image["content"] is None
    binary = svc.read_preview_file(session, build.id, "blob.bin")
    assert binary["kind"] == "binary"
    big = svc.read_preview_file(session, build.id, "big.txt")
    assert big["truncated"] is True
    assert len(big["content"]) == 200_000


def test_read_preview_file_denials(svc, build):
    session = _session()
    with pytest.raises(NotFound):
        svc.read_preview_file(session, build.id, "missing.txt")
    with pytest.raises(InvalidRequest):
        svc.read_preview_file(session, build.id, "../app.py")
    with pytest.raises(InvalidRequest):
        svc.read_preview_file(session, build.id, ".env")


def test_resolve_raw_serves_allowlist_only(svc, build):
    session = _session()
    path, media = svc.resolve_preview_raw(session, build.id, "index.html")
    assert media == "text/html"
    assert path.endswith("index.html")
    assert svc.resolve_preview_raw(
        session, build.id, "styles.css")[1] == "text/css"
    assert svc.resolve_preview_raw(
        session, build.id, "logo.png")[1] == "image/png"
    with pytest.raises(NotFound):  # source is viewable, not servable
        svc.resolve_preview_raw(session, build.id, "app.py")
    with pytest.raises(InvalidRequest):
        svc.resolve_preview_raw(session, build.id, ".env")
    with pytest.raises(NotFound):
        svc.resolve_preview_raw(session, build.id, "missing.html")


def test_resolve_raw_rejects_oversize(svc, build, root):
    (root / "huge.html").write_bytes(b"x" * 6_000_000)
    with pytest.raises(InvalidRequest):
        svc.resolve_preview_raw(_session(), build.id, "huge.html")


# -- HTTP API ------------------------------------------------------------

def _seed_demo_files(root: Path) -> None:
    (root / "index.html").write_text(
        "<!doctype html><html><body><h1>Demo site</h1></body></html>\n")
    (root / "styles.css").write_text("body { color: red; }\n")
    (root / "app.py").write_text("print('hi')\n")
    (root / ".env").write_text("TOKEN=abc\n")


def test_api_preview_flow(tmp_path):
    plane = make_plane(tmp_path, start=False)
    try:
        _seed_demo_files(tmp_path / "demo")
        client = make_client(plane)
        with client:
            _session, _token, headers = login(client)
            created = client.post("/api/v1/builds", json={"name": "Site"},
                                  headers=headers)
            build_id = created.json()["build"]["build_id"]

            meta = client.get(f"/api/v1/builds/{build_id}/preview",
                              headers=headers)
            assert meta.status_code == 200, meta.text
            assert "index.html" in meta.json()["candidates"]

            chosen = client.patch(
                f"/api/v1/builds/{build_id}/preview",
                json={"entry": "index.html"}, headers=headers)
            assert chosen.status_code == 200, chosen.text
            assert chosen.json()["entry"] == "index.html"

            raw = client.get(
                f"/api/v1/builds/{build_id}/preview/raw",
                params={"path": "index.html"}, headers=headers)
            assert raw.status_code == 200, raw.text
            assert "Demo site" in raw.text
            assert "text/html" in raw.headers["content-type"]
            assert raw.headers["Content-Security-Policy"] == (
                "sandbox allow-scripts")
            assert raw.headers["X-Content-Type-Options"] == "nosniff"

            css = client.get(
                f"/api/v1/builds/{build_id}/preview/raw",
                params={"path": "styles.css"}, headers=headers)
            assert css.status_code == 200
            assert "text/css" in css.headers["content-type"]

            denied = client.get(
                f"/api/v1/builds/{build_id}/preview/raw",
                params={"path": "app.py"}, headers=headers)
            assert denied.status_code == 404
            secret = client.get(
                f"/api/v1/builds/{build_id}/preview/raw",
                params={"path": "../app.py"}, headers=headers)
            assert secret.status_code in (400, 404)
            env = client.get(
                f"/api/v1/builds/{build_id}/preview/raw",
                params={"path": ".env"}, headers=headers)
            assert env.status_code == 400

            viewed = client.get(
                f"/api/v1/builds/{build_id}/preview/file",
                params={"path": "app.py"}, headers=headers)
            assert viewed.status_code == 200, viewed.text
            assert viewed.json()["kind"] == "text"

            # The login cookie persists on the client; drop it (and the
            # bearer header) to prove anonymous preview is refused.
            client.cookies.clear()
            anon = client.get(f"/api/v1/builds/{build_id}/preview/raw",
                              params={"path": "index.html"})
            assert anon.status_code == 401

            foreign = client.get("/api/v1/builds/b-0000000000000000/preview",
                                 headers=headers)
            assert foreign.status_code == 404
    finally:
        plane.stop()


# -- end to end: a real run builds a site, preview serves it --------------

HTML_PAYLOAD = json.dumps({
    "summary": "Landing page",
    "changes": [
        {"path": "index.html", "action": "create",
         "content": "<!doctype html>\n<html>\n<head>\n"
                    "<meta charset=\"utf-8\">\n<title>Demo</title>\n"
                    "<link rel=\"stylesheet\" href=\"styles.css\">\n"
                    "</head>\n<body>\n<h1>Hello Forge</h1>\n"
                    "<script src=\"site.js\"></script>\n</body>\n</html>\n"},
        {"path": "styles.css", "action": "create",
         "content": "body { font-family: sans-serif; }\n"},
        {"path": "site.js", "action": "create",
         "content": "document.title = 'Demo ready';\n"},
        {"path": "tests/test_site.py", "action": "create",
         "content": "from pathlib import Path\n"
                    "def test_index_exists():\n"
                    "    assert 'Hello Forge' in "
                    "Path('index.html').read_text()\n"},
    ],
    "tests_to_run": ["tests/test_site.py"],
    "reasoning_summary": "landing page",
    "risk_level": "low",
})


class HtmlProvider:
    """Scripted model that always answers with the landing-page payload."""

    name = "html-scripted"

    def __init__(self) -> None:
        from helpers_a34 import ScriptedProvider

        self._base = ScriptedProvider(coder_payload=HTML_PAYLOAD)
        self.prompts: list = []

    def generate(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return self._base.generate(prompt, **kwargs)


def test_e2e_preview_serves_what_the_run_built(tmp_path):
    plane = make_plane(tmp_path, provider=HtmlProvider())
    try:
        make_repo(tmp_path / "demo")
        client = make_client(plane)
        with client:
            _session, _token, headers = login(client)
            created = client.post("/api/v1/builds", json={
                "name": "Landing",
                "roadmap": "Stage 1: landing page.",
                "blueprint": "Static HTML + CSS + JS."},
                headers=headers)
            assert created.status_code == 200, created.text
            build_id = created.json()["build"]["build_id"]
            added = client.post(
                f"/api/v1/builds/{build_id}/stages",
                json={"stages": [
                    {"title": "Landing page",
                     "prompt": "Build index.html with tests."}]},
                headers=headers)
            assert added.status_code == 200, added.text

            started = client.post(f"/api/v1/builds/{build_id}/run-next",
                                  json={}, headers=headers)
            assert started.status_code == 200, started.text
            finished = drive_to_terminal(
                client, headers, started.json()["run"]["task_id"],
                timeout=240.0)
            assert finished["status"] == "SUCCEEDED", finished

            board = client.get(f"/api/v1/builds/{build_id}",
                               headers=headers).json()
            assert board["stages"][0]["status"] == "completed"
            assert "index.html" in board["stages"][0][
                "evidence"]["files_changed"]

            meta = client.get(f"/api/v1/builds/{build_id}/preview",
                              headers=headers).json()
            assert "index.html" in meta["candidates"]
            made = meta["files_made"][0]
            assert made["status"] == "completed"
            assert "index.html" in made["files"]

            chosen = client.patch(
                f"/api/v1/builds/{build_id}/preview",
                json={"entry": "index.html"}, headers=headers)
            assert chosen.json()["entry_exists"] is True

            raw = client.get(
                f"/api/v1/builds/{build_id}/preview/raw",
                params={"path": "index.html"}, headers=headers)
            assert raw.status_code == 200, raw.text
            assert "Hello Forge" in raw.text
            assert raw.headers["Content-Security-Policy"] == (
                "sandbox allow-scripts")
    finally:
        plane.stop()
