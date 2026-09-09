"""Final go/no-go gate (A80): the last word.

Go requires the full rollout to pass AND at least one real SUCCEEDED
run recorded in the project — the end-to-end demonstration the master
build demands. Every requirement is listed with its evidence.

The gate also validates provider state and capability:

- ``decision`` — ``GO`` / ``NO_GO`` with machine-readable reasons.
- ``capability_status`` —
  ``PRODUCTION_READY``: go criteria met AND at least one real
  external provider is configured and believed available;
  ``ARCHITECTURE_COMPLETE``: go criteria met but every real external
  provider is unconfigured (the build is complete; it runs on
  deterministic simulators until an operator configures providers);
  never reported as production-ready in that state.
- ``security_invariants`` — posture and secret evidence taken from the
  live security gate inside the rollout.

A80 does not ask only "did the tests pass": it demands A71-A79 gates
plus a real successful run plus explicit real-capability validation,
and it never reports PRODUCTION_READY while every provider is a
simulator.
"""
from __future__ import annotations

import os
import time
from typing import Any

from forge.final.rollout import rollout_gate

TERMINAL_OK = ("SUCCEEDED",)


def external_provider_report() -> dict[str, dict[str, Any]]:
    """Deterministic, env-only report of real external providers.

    Every integration that can talk to the outside world is listed
    with its configuration state. No network probing happens here (the
    gate must stay deterministic); a provider is AVAILABLE when its
    configuration is present and complete, MISCONFIGURED when present
    but incomplete/refused by policy (e.g. SSH without an allowlist),
    and UNAVAILABLE when not configured at all.
    """
    def env(*names: str) -> str:
        return next((os.environ.get(name, "").strip()
                     for name in names if os.environ.get(name, "").strip()),
                    "")

    openai_key = env("OPENAI_API_KEY")
    report: dict[str, dict[str, Any]] = {}

    report["model-openai"] = {
        "kind": "model",
        "status": "AVAILABLE" if openai_key else "UNAVAILABLE",
        "configured": bool(openai_key),
        "note": "" if openai_key else "set OPENAI_API_KEY for real model calls",
    }
    report["ollama-local"] = {
        "kind": "model",
        "status": "AVAILABLE" if env("OLLAMA_BASE_URL", "OLLAMA_URL")
        else "AVAILABLE",  # local-first default endpoint is implicit
        "configured": True,
        "note": "Local endpoint; reachability is probed per call.",
    }
    report["research-web"] = {
        "kind": "research",
        "status": "AVAILABLE" if (openai_key or env("FORGE_SEARXNG_URL"))
        else "UNAVAILABLE",
        "configured": bool(openai_key or env("FORGE_SEARXNG_URL")),
        "note": "providers: openai web search / searxng",
    }
    report["vision-openai"] = {
        "kind": "vision",
        "status": "AVAILABLE" if openai_key else "UNAVAILABLE",
        "configured": bool(openai_key),
        "note": "FORGE_VISION_PROVIDER=openai-vision + OPENAI_API_KEY",
    }
    voice_stt = env("FORGE_VOICE_STT_PROVIDER")
    report["voice-whisper"] = {
        "kind": "voice",
        "status": "AVAILABLE" if (voice_stt == "openai-whisper"
                                  and openai_key) else (
            "MISCONFIGURED" if voice_stt == "openai-whisper"
            else "UNAVAILABLE"),
        "configured": voice_stt == "openai-whisper",
        "note": "FORGE_VOICE_STT_PROVIDER=openai-whisper + OPENAI_API_KEY",
    }
    voice_tts = env("FORGE_VOICE_TTS_PROVIDER")
    report["voice-tts"] = {
        "kind": "voice",
        "status": "AVAILABLE" if (voice_tts == "openai-tts"
                                  and openai_key) else (
            "MISCONFIGURED" if voice_tts == "openai-tts"
            else "UNAVAILABLE"),
        "configured": voice_tts == "openai-tts",
        "note": "FORGE_VOICE_TTS_PROVIDER=openai-tts + OPENAI_API_KEY",
    }
    colab = env("FORGE_COLAB_URL")
    ssh_host = env("FORGE_COMPUTE_SSH_HOST", "FORGE_COMPUTE_SSH_USER")
    modal = env("MODAL_TOKEN_ID")
    ssh_allowlist = env("FORGE_COMPUTE_SSH_ALLOWLIST")
    if ssh_host and not ssh_allowlist:
        report["compute-ssh"] = {
            "kind": "compute",
            "status": "MISCONFIGURED",
            "configured": True,
            "note": "FORGE_COMPUTE_SSH_ALLOWLIST missing; destination "
                    "refused (fail-closed)",
        }
    else:
        report["compute-ssh"] = {
            "kind": "compute",
            "status": "AVAILABLE" if ssh_host else "UNAVAILABLE",
            "configured": bool(ssh_host),
            "note": "FORGE_COMPUTE_SSH_* configuration",
        }
    report["compute-colab"] = {
        "kind": "compute",
        "status": "AVAILABLE" if colab else "UNAVAILABLE",
        "configured": bool(colab),
        "note": "FORGE_COLAB_URL",
    }
    report["compute-modal"] = {
        "kind": "compute",
        "status": "AVAILABLE" if modal else "UNAVAILABLE",
        "configured": bool(modal),
        "note": "MODAL_TOKEN_ID",
    }
    deploy_host = env("FORGE_DEPLOY_SSH_HOST")
    deploy_allowlist = env("FORGE_DEPLOY_SSH_ALLOWLIST")
    report["deploy-ssh-rsync"] = {
        "kind": "deployment",
        "status": "AVAILABLE" if (deploy_host and deploy_allowlist)
        else ("MISCONFIGURED" if deploy_host and not deploy_allowlist
              else "UNAVAILABLE"),
        "configured": bool(deploy_host),
        "note": "FORGE_DEPLOY_SSH_HOST/PATH + allowlist",
    }
    report["collaboration-openai"] = {
        "kind": "collaboration",
        "status": "AVAILABLE" if openai_key else "UNAVAILABLE",
        "configured": bool(openai_key),
        "note": "OPENAI_API_KEY",
    }
    report["training-openai"] = {
        "kind": "training",
        "status": "AVAILABLE" if openai_key else "UNAVAILABLE",
        "configured": bool(openai_key),
        "note": ("OPENAI_API_KEY + FORGE_TRAINING_EXTERNAL_UPLOAD mode "
                 "(default deny)"),
    }
    return report


def final_gate(plane: Any, session: Any) -> dict[str, Any]:
    rollout = rollout_gate(plane, session)
    succeeded_runs = 0
    rows, _total = plane.runs.list_for_project(session.project_id)
    for run in rows:
        raw_status = getattr(run.status, "value", str(run.status))
        if str(raw_status) in TERMINAL_OK:
            succeeded_runs += 1

    # Security invariants from the live security gate inside the rollout.
    security_details = rollout.get("details", {}).get("security", {})
    security_checks = security_details.get("checks", []) or []

    def _check_passed(name: str) -> bool | None:
        return next((bool(entry.get("passed"))
                     for entry in security_checks
                     if entry.get("check") == name), None)

    security_invariants = {
        "security_gate_passed": bool(security_details.get("passed")),
        "no_secret_hits": _check_passed("no_secret_hits") is not False,
        "policy_constrained": _check_passed("posture") is not False,
    }

    # Real-provider capability validation (env-derived, deterministic).
    provider_report = external_provider_report()
    # Fold in live fabric health when the fabric knows these providers
    # (router feedback degrades unhealthy models after real failures).
    try:
        fabric_health = plane.fabric.provider_health()
        for name in list(provider_report):
            base = name.split("-", 1)[1] if "-" in name else name
            for provider_name, state in fabric_health.items():
                if provider_name != base:
                    continue
                status = str(state.get("status", "")).lower()
                if status == "unhealthy" and \
                        provider_report[name]["status"] == "AVAILABLE":
                    provider_report[name]["status"] = "DEGRADED"
                    provider_report[name]["note"] = (
                        provider_report[name].get("note", "") +
                        "; degraded by router feedback")
    except Exception:
        pass  # gate stays deterministic when the fabric is unavailable
    real_capable = [
        name for name, state in provider_report.items()
        if state.get("status") == "AVAILABLE" and
        name not in ("ollama-local",)]
    misconfigured = [
        name for name, state in provider_report.items()
        if state.get("status") == "MISCONFIGURED"]

    requirements = {
        "rollout_passed": rollout["passed"],
        "real_successful_run": succeeded_runs >= 1,
        "security_invariants": all(security_invariants.values()),
    }
    go = all(requirements.values())
    decision = "GO" if go else "NO_GO"

    if go and real_capable:
        capability_status = "PRODUCTION_READY"
    elif go:
        capability_status = "ARCHITECTURE_COMPLETE"
    else:
        capability_status = "NOT_READY"

    reasons: list[str] = []
    for key, passed in requirements.items():
        if not passed:
            reasons.append(f"requirement not met: {key}")
    if go and not real_capable:
        reasons.append(
            "no real external provider is configured; the build runs on "
            "deterministic simulators (ARCHITECTURE_COMPLETE, not "
            "PRODUCTION_READY)")
    for name in misconfigured:
        reasons.append(
            f"provider {name} is present but misconfigured "
            f"({provider_report[name].get('note', '')})")

    return {
        "go": bool(go),
        "decision": decision,
        "capability_status": capability_status,
        "requirements": requirements,
        "reasons": reasons,
        "evidence": {
            "rollout": rollout,
            "succeeded_runs": succeeded_runs,
            "security_invariants": security_invariants,
            "real_providers": real_capable,
            "misconfigured_providers": misconfigured,
            "provider_report": provider_report,
        },
        "checked_at": time.time(),
        "note": ("Go requires every rollout gate to pass, at least one "
                 "genuinely SUCCEEDED run on record, and live security "
                 "invariants. PRODUCTION_READY additionally requires a "
                 "configured real external provider; a GO on simulators "
                 "alone is classified ARCHITECTURE_COMPLETE."),
    }
