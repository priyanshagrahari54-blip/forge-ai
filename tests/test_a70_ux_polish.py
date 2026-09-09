"""UX polish (A70): dashboard panels fed by real endpoints."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402


def test_dashboard_feed_endpoints_are_compatible(tmp_path):
    """The three endpoints the dashboard consumes return the exact
    shapes the UI renders — one integration check per panel."""
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        autonomy = client.get("/api/v1/autonomy", headers=headers)
        assert autonomy.status_code == 200
        assert "level" in autonomy.json()
        assert "summary" in autonomy.json()

        metrics = client.get("/api/v1/observability/metrics",
                             headers=headers)
        assert metrics.status_code == 200
        body = metrics.json()
        assert "counters" in body and "gauges" in body
        assert "agents_defined" in body["gauges"]
        assert "active_runs" in body["gauges"]

        hardening = client.get("/api/v1/hardening/report",
                               headers=headers)
        assert hardening.status_code == 200
        report = hardening.json()
        assert report["overall"] in ("ok", "attention")
        assert "rule_count" in report["policy"]
        assert "active_sessions" in report["sessions"]
        assert isinstance(report["secrets"], list)


def test_dashboard_template_has_polish_containers():
    html = Path(__file__).parent.parent / "forge" / "cockpit" / "web" \
        / "index.html"
    source = html.read_text(encoding="utf-8")
    assert 'id="d-autonomy"' in source
    assert 'id="d-audit"' in source
    assert "chip-row" in source


def test_dashboard_js_wires_panels_with_graceful_failure():
    js = Path(__file__).parent.parent / "forge" / "cockpit" / "web" \
        / "app.js"
    source = js.read_text(encoding="utf-8")
    assert 'api("/api/v1/autonomy")' in source
    assert 'api("/api/v1/observability/metrics")' in source
    assert 'api("/api/v1/hardening/report")' in source
    assert "fmtCount" in source
    # Each panel degrades honestly instead of faking data.
    assert "backend offline" in source
    assert "unavailable (backend offline)" in source
    # Cockpit invariant: class toggling only, never inline styles.
    assert ".style" not in source


def test_dashboard_styles_cover_the_new_panels():
    css = Path(__file__).parent.parent / "forge" / "cockpit" / "web" \
        / "styles.css"
    source = css.read_text(encoding="utf-8")
    assert ".chip-row" in source
    assert ".chip.ok" in source
    assert ".chip.warn" in source
    assert ".chip.bad" in source
    assert ".audit-line" in source


def test_panels_reflect_live_state(tmp_path):
    """The autonomy panel must track real state changes."""
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        before = client.get("/api/v1/autonomy", headers=headers).json()
        assert before["level"] == "assisted"
        plane.autonomy_set_level(session, "autonomous")
        after = client.get("/api/v1/autonomy", headers=headers).json()
        assert after["level"] == "autonomous"
        # Counters advance after real work.
        task = plane.submit_task(session, "add a csv export")
        metrics = client.get("/api/v1/observability/metrics",
                             headers=headers).json()
        assert metrics["counters"].get("tasks.submitted", 0) >= 1
        assert task.mode == "autonomous"
