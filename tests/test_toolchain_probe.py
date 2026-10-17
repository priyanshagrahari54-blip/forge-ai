from __future__ import annotations

import subprocess

from forge.intelligence.toolchain_probe import (
    DEFAULT_PROBES, ToolProbe, ToolStatus, probe_tool, probe_toolchains,
)


def test_missing_tool_is_explicit(monkeypatch):
    monkeypatch.setattr("forge.intelligence.toolchain_probe.shutil.which", lambda _: None)
    result = probe_tool(ToolProbe("Example", "not-installed"))
    assert result.available is False
    assert result.version_checked is False
    assert result.error == "not found on PATH"


def test_available_tool_uses_fixed_argv_without_shell(monkeypatch):
    monkeypatch.setattr("forge.intelligence.toolchain_probe.shutil.which", lambda _: "/tools/demo")
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="Demo compiler 1.2\nextra\n")

    monkeypatch.setattr("forge.intelligence.toolchain_probe.subprocess.run", fake_run)
    result = probe_tool(ToolProbe("Demo", "demo", ("--version",)))
    assert result.available and result.version_checked
    assert result.version == "Demo compiler 1.2"
    assert seen["argv"] == ["/tools/demo", "--version"]
    assert seen["shell"] is False


def test_nonzero_version_exit_is_reported(monkeypatch):
    monkeypatch.setattr("forge.intelligence.toolchain_probe.shutil.which", lambda _: "/tools/demo")
    monkeypatch.setattr(
        "forge.intelligence.toolchain_probe.subprocess.run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 2, stdout="version failed"),
    )
    result = probe_tool(ToolProbe("Demo", "demo"))
    assert result.available and result.version_checked
    assert result.error == "version command exited 2"


def test_timeout_separates_presence_from_version_check(monkeypatch):
    monkeypatch.setattr("forge.intelligence.toolchain_probe.shutil.which", lambda _: "/tools/demo")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr("forge.intelligence.toolchain_probe.subprocess.run", timeout)
    result = probe_tool(ToolProbe("Demo", "demo"), timeout=30)
    assert result.available and not result.version_checked
    assert result.error == "version probe timed out"


def test_inventory_counts_only_observed_status(monkeypatch):
    probes = (ToolProbe("One", "one"), ToolProbe("Two", "two"))
    def fake_probe(probe, timeout=2.0):
        available = probe.executable == "one"
        return ToolStatus(probe.name, probe.executable, available,
                          version="One 1.0" if available else "",
                          version_checked=available)
    monkeypatch.setattr("forge.intelligence.toolchain_probe.probe_tool", fake_probe)
    report = probe_toolchains(probes)
    assert report["tool_count"] == 2
    assert report["available_count"] == 1
    assert report["version_checked_count"] == 1
    assert "does not prove" in report["note"]


def test_default_inventory_covers_multiple_language_families():
    names = {probe.executable for probe in DEFAULT_PROBES}
    assert {"rustc", "go", "javac", "dotnet", "php", "ruby", "swift", "dart"} <= names
