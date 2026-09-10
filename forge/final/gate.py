"""Final go/no-go gate (A80): the last word.

Go requires the full rollout to pass AND at least one real SUCCEEDED
run recorded in the project — the end-to-end demonstration the master
build demands. Every requirement is listed with its evidence.

Capability classification is truthful about provider state:

- ``ARCHITECTURE_COMPLETE`` — every architecture/security/engineering
  gate passes but no real external provider has been successfully
  capability-VERIFIED (recently). The build is complete and runs on
  deterministic simulators/local providers until an operator verifies
  a real provider.
- ``PRODUCTION_READY`` — all of the above AND at least one real
  external provider is configured, its credentials authenticate, its
  endpoint is reachable, its required capability succeeds in an
  explicit verification, the machine-readable evidence is recent
  enough under the documented TTL policy, and there is no unresolved
  configured-provider failure.

An environment variable alone is NEVER evidence of capability:
``OPENAI_API_KEY`` present is CONFIGURED, nothing more. The gate never
makes network calls; verification is an explicit operator action
(``forge.final.provider_verification`` / ``POST
/api/v1/final/gate/verify``) whose results are persisted with
timestamps and surfaced here.
"""
from __future__ import annotations

import time
from typing import Any

from forge.final.provider_verification import (
    KNOWN_PROVIDERS,
    is_recent,
    provider_config,
    verification_ttl,
)
from forge.final.rollout import rollout_gate
from forge.security.provider_states import ProviderStatus

TERMINAL_OK = ("SUCCEEDED",)

#: Providers whose verification is performed against the shared OpenAI
#: endpoint (auth + connectivity); keyed by the report entry name.
_OPENAI_BACKED = (
    "model-openai", "research-web", "vision-openai", "voice-whisper",
    "voice-tts", "collaboration-openai", "training-openai",
)

def _fabric_base(name: str) -> str:
    """Map a report entry to the model-fabric provider name it shares
    a health signal with (best-effort)."""
    if name == "ollama-local":
        return "ollama"
    if name in _OPENAI_BACKED or name == "model-openai":
        return "openai"
    return name.split("-", 1)[1] if "-" in name else name


def external_provider_report(
        records: dict[str, dict[str, Any]] | None = None,
        fabric_health: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Deterministic report of real external providers.

    Pure configuration + persisted verification records: this function
    NEVER touches the network (the gate must stay deterministic and
    offline). Status vocabulary is the ``PROVIDER_STATUS_LADDER``:
    NOT_CONFIGURED / CONFIGURED / MISCONFIGURED / VERIFIED /
    AUTH_ERROR / TIMEOUT / RATE_LIMITED / UNAVAILABLE / PROVIDER_ERROR
    / POLICY_DENIED / DEGRADED.

    ``CONFIGURED`` only means configuration exists; a provider reaches
    ``VERIFIED`` solely through a fresh, successful explicit
    capability verification (see ``forge.final.provider_verification``).
    """
    records = records or {}
    ttl = verification_ttl()
    report: dict[str, dict[str, Any]] = {}

    def verification_block(name: str) -> dict[str, Any] | None:
        """Decorate one persisted verification record."""
        record = records.get(name)
        if record is None:
            return None
        fresh = is_recent(record, ttl)
        verified_at = record.get("verified_at")
        return {
            "state": record.get("status", ""),
            "capability": record.get("capability", ""),
            "checked_at": record.get("checked_at"),
            "verified_at": verified_at,
            "expires_at": (float(verified_at) + ttl)
            if verified_at else None,
            "fresh": bool(fresh),
            "evidence": record.get("evidence") or {},
            "error": record.get("error", ""),
            "note": record.get("note", ""),
        }

    for name in KNOWN_PROVIDERS:
        cfg = provider_config(name)
        kind = cfg.get("kind", "external")
        entry: dict[str, Any] = {
            "kind": kind,
            "configured": bool(cfg["configured"]),
            "config_state": cfg["config_state"],
            "note": cfg["note"],
            "verification": None,
        }
        verification = verification_block(name)
        status = cfg["config_state"]  # NOT_CONFIGURED | MISCONFIGURED
        if cfg["config_state"] == "CONFIGURED":
            if verification is None:
                status = ProviderStatus.CONFIGURED.value
                entry["note"] = (
                    "configured but NOT capability-verified; run the "
                    "explicit provider verification "
                    "(POST /api/v1/final/gate/verify)")
            elif verification["fresh"]:
                status = verification["state"]
            else:
                # Stale evidence: neither verified nor a fresh failure.
                status = ProviderStatus.CONFIGURED.value
                if verification["state"] == ProviderStatus.VERIFIED.value:
                    entry["note"] = (
                        "verified earlier but evidence is stale "
                        "(older than the documented TTL); re-run "
                        "provider verification")
                else:
                    entry["note"] = (
                        "previous verification attempt is stale "
                        f"({verification['state']}); re-run provider "
                        "verification")
        entry["status"] = status
        if verification is not None:
            entry["verification"] = verification
        report[name] = entry

    # Fabric-health fold-in: router feedback (real failed calls) can
    # only downgrade a standing status, never upgrade it.
    for name, entry in report.items():
        if entry["config_state"] != "CONFIGURED":
            continue
        if entry.get("status") not in (
                ProviderStatus.CONFIGURED.value,
                ProviderStatus.VERIFIED.value):
            continue
        if not fabric_health:
            continue
        for provider_name, state in fabric_health.items():
            if provider_name != _fabric_base(name):
                continue
            raw = str(state.get("status", "")).lower()
            if raw == "unhealthy":
                entry["status"] = ProviderStatus.DEGRADED.value
                entry["note"] = entry.get("note", "") + \
                    "; degraded by fabric router feedback"
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

    # Provider state: env-derived configuration + PERSISTED explicit
    # verification records. The gate itself never calls the network.
    try:
        records = dict(plane.provider_verifications.all())
    except Exception:
        records = {}
    try:
        fabric_health = plane.fabric.provider_health()
    except Exception:
        fabric_health = None
    provider_report = external_provider_report(
        records=records, fabric_health=fabric_health)

    external = {name: state for name, state in provider_report.items()
                if state.get("kind") in ("external", "voice", "vision",
                                         "compute", "deployment",
                                         "research", "collaboration",
                                         "training")
                and state.get("config_state") == "CONFIGURED"}
    verified = [name for name, state in external.items()
                if state.get("status") == ProviderStatus.VERIFIED.value
                and state.get("verification", {}).get("fresh")]
    configured_not_verified = [
        name for name, state in external.items()
        if state.get("status") in (
            ProviderStatus.CONFIGURED.value,
            ProviderStatus.DEGRADED.value)]
    unresolved = [
        name for name, state in external.items()
        if state.get("status") in (
            ProviderStatus.AUTH_ERROR.value, ProviderStatus.TIMEOUT.value,
            ProviderStatus.RATE_LIMITED.value,
            ProviderStatus.UNAVAILABLE.value,
            ProviderStatus.PROVIDER_ERROR.value,
            ProviderStatus.POLICY_DENIED.value)]
    misconfigured = [
        name for name, state in provider_report.items()
        if state.get("config_state") == "MISCONFIGURED"]

    requirements = {
        "rollout_passed": rollout["passed"],
        "real_successful_run": succeeded_runs >= 1,
        "security_invariants": all(security_invariants.values()),
    }
    go = all(requirements.values())

    ttl = verification_ttl()
    readiness = {
        # 1-2. rollout + security gates (the requirements above).
        "rollout_and_security_gates_pass": bool(go),
        # 3. at least one real external provider is configured.
        "real_provider_configured": len(external) > 0,
        # 4-6. authentication + connectivity + required capability
        #      succeeded in an explicit verification.
        "provider_verified": len(verified) > 0,
        # 7. the result is machine-verifiable (structured evidence).
        "machine_verifiable_evidence": bool(provider_report),
        # 8. evidence is recent enough under the documented TTL policy.
        "evidence_recent": all(
            state.get("verification", {}).get("fresh", False)
            for state in (provider_report[name] for name in verified))
        if verified else False,
        # 9. no critical security failure (live security gate clean).
        "no_critical_security_failure": all(security_invariants.values()),
        # 10. no unresolved provider failure.
        "no_unresolved_provider_failure": not unresolved
        and not misconfigured,
    }
    production_ready = go and all(readiness.values())
    decision = "GO" if go else "NO_GO"
    if production_ready:
        capability_status = "PRODUCTION_READY"
    elif go:
        capability_status = "ARCHITECTURE_COMPLETE"
    else:
        capability_status = "NOT_READY"

    reasons: list[str] = []
    for key, passed in requirements.items():
        if not passed:
            reasons.append(f"requirement not met: {key}")
    if go and not production_ready:
        for key, passed in readiness.items():
            if not passed:
                if key == "provider_verified":
                    reasons.append(
                        "no real external provider has been capability-"
                        "VERIFIED; configured-but-unverified providers: "
                        + (", ".join(sorted(configured_not_verified))
                           or "none"))
                elif key == "real_provider_configured":
                    reasons.append(
                        "no real external provider is configured "
                        "(ARCHITECTURE_COMPLETE, not PRODUCTION_READY)")
                elif key == "no_unresolved_provider_failure":
                    reasons.append(
                        "unresolved configured-provider failure(s): "
                        + ", ".join(sorted(unresolved)))
                else:
                    reasons.append(f"production-ready criterion not met: "
                                   f"{key}")
    for name in misconfigured:
        reasons.append(
            f"provider {name} is present but misconfigured "
            f"({provider_report[name].get('note', '')})")

    return {
        "go": bool(go),
        "decision": decision,
        "capability_status": capability_status,
        "requirements": requirements,
        "production_readiness": readiness,
        "reasons": reasons,
        "evidence": {
            "rollout": rollout,
            "succeeded_runs": succeeded_runs,
            "security_invariants": security_invariants,
            "configured_providers": sorted(external),
            "verified_providers": sorted(verified),
            "configured_not_verified": sorted(configured_not_verified),
            "unresolved_provider_failures": sorted(unresolved),
            "misconfigured_providers": sorted(misconfigured),
            "provider_report": provider_report,
            "verification_policy": {
                "ttl_seconds": ttl,
                "mechanism": "POST /api/v1/final/gate/verify "
                             "(explicit; the gate itself never calls "
                             "the network)",
                "freshness": ("evidence is usable while newer than "
                              "ttl_seconds; stale evidence falls back "
                              "to CONFIGURED"),
            },
        },
        "checked_at": time.time(),
        "note": (
            "GO requires every rollout gate to pass, at least one "
            "genuinely SUCCEEDED run on record, and live security "
            "invariants. PRODUCTION_READY additionally requires an "
            "explicit, recent, successful capability verification of "
            "a configured real external provider — configuration "
            "alone (e.g. an OPENAI_API_KEY environment variable) is "
            "CONFIGURED, never VERIFIED. A GO without any verified "
            "provider is classified ARCHITECTURE_COMPLETE."),
    }
