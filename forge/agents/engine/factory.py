"""The agent factory (A82): specification → validated, packaged agent.

The factory is the only way an agent package comes into existence or
changes. Its rules:

* **Validate before promoting.** ``created`` → ``validated`` requires a
  recorded validation report whose checks all passed; ``validated`` →
  ``tested`` requires a recorded benchmark report that met the spec's
  requirements. Neither promotion can be asserted — both are computed.
* **Creating grants nothing.** A new package has an empty grant ledger
  unless an operator explicitly asks for grants, and even then only
  operations the spec already declares can be granted.
* **Editing bumps the version and discards evidence.** Any spec change
  returns the agent to ``created``. A change to capabilities, tools, or
  granted operations is a *major* bump and also revokes every existing
  grant, so widening an agent always requires a fresh operator decision.
* **Retired is final.** A retired package is kept for audit and cannot be
  re-enabled or re-specified.
"""
from __future__ import annotations

import time
from typing import Any

from forge.agents.engine.benchmark import run_agent_benchmark
from forge.agents.engine.errors import (
    AgentLifecycleError,
    AgentNotFoundError,
    AgentPermissionError,
)
from forge.agents.engine.grants import GrantLedger
from forge.agents.engine.lifecycle import AgentState
from forge.agents.engine.package import AgentPackage, PackageStore
from forge.agents.engine.spec import (
    FORBIDDEN_OPERATIONS,
    GRANTABLE_OPERATIONS,
    TOOL_CATALOG,
    AgentSpec,
    validate_spec,
)
from forge.agents.engine.templates import spec_from_template
from forge.agents.engine.versioning import diff_specs, next_version


class AgentFactory:
    """Creates, validates, tests, and versions agent packages."""

    def __init__(self, root: str = ".", *, store: PackageStore | None = None,
                 runtime: Any = None) -> None:
        self.root = root
        self.store = store or PackageStore(root)
        self.runtime = runtime

    # -- creation --------------------------------------------------------

    def create(self, spec: AgentSpec, *, actor: str,
               grant: bool = False) -> AgentPackage:
        """Create a package from a validated spec, in state ``created``."""
        if not actor or not actor.strip():
            raise AgentPermissionError(
                "Creating an agent requires a named operator")
        spec = validate_spec(spec.to_dict())
        package = self.store.create(spec, actor=actor.strip())
        if grant:
            ledger = self._ledger(package)
            ledger.grant_spec_operations(actor=actor,
                                         reason="granted at creation")
            self.store.write_grants(package.name, ledger.to_dict())
        return package

    def create_from_template(self, template_id: str, name: str, *,
                             purpose: str = "", actor: str = "operator",
                             overrides: Any = None,
                             grant: bool = False) -> AgentPackage:
        spec = spec_from_template(template_id, name=name, purpose=purpose,
                                  overrides=overrides)
        return self.create(spec, actor=actor, grant=grant)

    def create_from_dict(self, payload: Any, *, actor: str,
                         grant: bool = False) -> AgentPackage:
        return self.create(validate_spec(payload), actor=actor, grant=grant)

    # -- reads -----------------------------------------------------------

    def get(self, name: str) -> AgentPackage:
        return self.store.load(name)

    def list(self, state: str = "") -> list:
        summaries = [self.store.load(name).summary()
                     for name in self.store.names()]
        if state:
            summaries = [item for item in summaries if item["state"] == state]
        return summaries

    def history(self, name: str, limit: int = 20) -> list:
        return self.store.history(name, limit=limit)

    def benchmarks(self, name: str, limit: int = 5) -> list:
        return self.store.benchmarks(name, limit=limit)

    def versions(self, name: str) -> list:
        return self.store.list_versions(name)

    def version(self, name: str, version: str) -> dict:
        return self.store.read_version(name, version)

    def grants(self, name: str) -> dict:
        package = self.store.load(name)
        return self._ledger(package).to_dict()

    # -- validation ------------------------------------------------------

    def validate(self, name: str, *, actor: str) -> dict:
        """Run every structural check and promote to ``validated`` on success."""
        package = self.store.load(name)
        if package.state == AgentState.RETIRED:
            raise AgentLifecycleError(
                "Agent %r is retired and cannot be re-validated" % name)
        report = self._validation_report(package)
        package.lifecycle.validation = report
        if report["passed"]:
            if package.state in (AgentState.CREATED, AgentState.DISABLED):
                package.lifecycle.transition(
                    AgentState.VALIDATED, actor=actor,
                    reason="validation passed (%d checks)"
                           % len(report["checks"]))
        self.store.write_manifest(package)
        return report

    def _validation_report(self, package: AgentPackage) -> dict:
        spec = package.spec
        checks: list = []

        findings = list(spec.findings())
        checks.append({
            "name": "spec", "passed": not findings,
            "detail": "; ".join(findings) if findings
            else "spec re-validates against the closed vocabulary"})

        unknown = [tool for tool in spec.tool_names()
                   if tool not in TOOL_CATALOG]
        unregistered = self._unregistered_tools(spec)
        checks.append({
            "name": "tools",
            "passed": not unknown and not unregistered,
            "detail": ("; ".join(
                (["unknown tool(s): %s" % ", ".join(unknown)] if unknown
                 else [])
                + (["not registered in this runtime: %s"
                    % ", ".join(unregistered)] if unregistered else []))
                or "all %d declared tool(s) exist" % len(spec.tools))})

        required = set(spec.required_operations())
        declared = set(spec.permissions.operations)
        missing = sorted(required - declared)
        forbidden = sorted(declared & set(FORBIDDEN_OPERATIONS))
        unknown_ops = sorted(declared - set(GRANTABLE_OPERATIONS)
                             - set(FORBIDDEN_OPERATIONS))
        checks.append({
            "name": "permissions",
            "passed": not (missing or forbidden or unknown_ops),
            "detail": ("; ".join(
                (["tools need ungranted operation(s): %s"
                  % ", ".join(missing)] if missing else [])
                + (["blocked operation(s) requested: %s"
                    % ", ".join(forbidden)] if forbidden else [])
                + (["unknown operation(s): %s" % ", ".join(unknown_ops)]
                   if unknown_ops else []))
                or "ceiling covers every declared tool (%s)"
                % (", ".join(sorted(declared)) or "read-only"))})

        # The model check judges the *specification*: are the required
        # capabilities real? Whether a model happens to be reachable right
        # now is environment state, reported as a note (and enforced at
        # run time, where a run without a real model fails honestly) —
        # otherwise validation would pass with no fabric attached and fail
        # with a fallback-only one, which is not a property of the spec.
        model_note = "capabilities are in the canonical vocabulary"
        if self.runtime is not None and self.runtime.fabric is not None:
            from forge.models.readiness import fabric_has_real_model

            if fabric_has_real_model(self.runtime.fabric):
                model_note += "; a real model is registered in the fabric"
            else:
                model_note += ("; no real model is reachable, so runs will "
                               "fail honestly unless model.allow_fallback "
                               "is set")
        checks.append({"name": "model", "passed": True,
                       "detail": model_note})

        checks.append({
            "name": "verification",
            "passed": not (spec.verification.require_tests
                           and "run_tests" not in spec.tool_names()),
            "detail": ("require_tests needs the run_tests tool"
                       if spec.verification.require_tests
                       and "run_tests" not in spec.tool_names()
                       else "verification requirements are consistent")})

        versions = self.store.list_versions(package.name)
        fingerprint = spec.fingerprint()
        recorded = any(item["fingerprint"] == fingerprint
                       for item in versions)
        checks.append({
            "name": "package",
            "passed": recorded,
            "detail": ("version %s recorded for fingerprint %s"
                       % (package.version, fingerprint[:12]) if recorded
                       else "no version record matches the current spec")})

        passed = all(check["passed"] for check in checks)
        return {"passed": passed, "at": time.time(), "checks": checks,
                "findings": [check["detail"] for check in checks
                             if not check["passed"]],
                "fingerprint": fingerprint, "version": package.version}

    def _unregistered_tools(self, spec: AgentSpec) -> list:
        if self.runtime is None:
            return []
        try:
            registered = set(self.runtime.tool_runtime().tools)
        except Exception:
            return []
        return [tool for tool in spec.tool_names() if tool not in registered]

    # -- testing ---------------------------------------------------------

    def test(self, name: str, *, actor: str, fabric: Any = None,
             include_model_checks: bool = True) -> dict:
        """Benchmark the agent and promote to ``tested`` when it passes."""
        package = self.store.load(name)
        if package.state == AgentState.RETIRED:
            raise AgentLifecycleError(
                "Agent %r is retired and cannot be tested" % name)
        if package.state not in (AgentState.VALIDATED, AgentState.TESTED):
            raise AgentLifecycleError(
                "Agent %r must be validated before it can be tested "
                "(current state: %s)" % (name, package.state))
        runtime = self._require_runtime()
        report = run_agent_benchmark(
            runtime, package, fabric=fabric,
            include_model_checks=include_model_checks)
        self.store.record_benchmark(name, report.to_dict())
        package.lifecycle.benchmark = report.to_dict()
        if report.passed:
            if package.state == AgentState.VALIDATED:
                package.lifecycle.transition(
                    AgentState.TESTED, actor=actor,
                    reason="benchmark passed (%d/%d executed scenarios)"
                           % (report.passed_count, report.executed))
        self.store.write_manifest(package)
        return report.to_dict()

    def _require_runtime(self):
        if self.runtime is None:
            raise AgentLifecycleError(
                "A runtime is required to benchmark an agent")
        return self.runtime

    # -- lifecycle -------------------------------------------------------

    def enable(self, name: str, *, actor: str, reason: str = "") -> dict:
        return self._transition(name, AgentState.ENABLED, actor,
                                reason or "operator enabled the agent")

    def pause(self, name: str, *, actor: str, reason: str = "") -> dict:
        return self._transition(name, AgentState.PAUSED, actor,
                                reason or "operator paused the agent")

    def resume(self, name: str, *, actor: str, reason: str = "") -> dict:
        return self._transition(name, AgentState.ENABLED, actor,
                                reason or "operator resumed the agent")

    def disable(self, name: str, *, actor: str, reason: str = "") -> dict:
        return self._transition(name, AgentState.DISABLED, actor,
                                reason or "operator disabled the agent")

    def retire(self, name: str, *, actor: str, reason: str = "") -> dict:
        return self._transition(name, AgentState.RETIRED, actor,
                                reason or "operator retired the agent")

    def _transition(self, name: str, target: str, actor: str,
                    reason: str) -> dict:
        package = self.store.load(name)
        record = package.lifecycle.transition(target, actor=actor,
                                              reason=reason)
        self.store.write_manifest(package)
        return {"agent": name, "state": package.state,
                "transition": record.to_dict()}

    # -- versioning ------------------------------------------------------

    def update_spec(self, name: str, spec: AgentSpec, *, actor: str,
                    notes: str = "") -> dict:
        """Apply a new spec: bump the version, invalidate evidence."""
        package = self.store.load(name)
        if package.state == AgentState.RETIRED:
            raise AgentLifecycleError(
                "Agent %r is retired; create a new agent instead" % name)
        spec = validate_spec(spec.renamed(name).to_dict())
        if spec.fingerprint() == package.spec.fingerprint():
            return {"agent": name, "version": package.version,
                    "change": "none",
                    "detail": "spec is unchanged; nothing to do"}
        old_spec = package.spec
        version, kind = next_version(package.version, old_spec, spec)
        package.spec = spec
        package.version = version
        revoked: list = []
        if kind == "major":
            ledger = self._ledger(package, spec=old_spec)
            for operation in ledger.active_operations():
                try:
                    entry = ledger.revoke(
                        operation, actor=actor,
                        reason="spec v%s changed the granted surface"
                               % version)
                    revoked.append(entry["operation"])
                except AgentPermissionError:
                    continue
            self.store.write_grants(name, ledger.to_dict())
        package.lifecycle.respec(
            actor=actor,
            reason=notes or "spec changed (%s bump to v%s)" % (kind, version))
        self.store.write_manifest(package)
        self.store.record_version(package, notes=notes or "%s bump" % kind,
                                  actor=actor)
        return {"agent": name, "version": version, "change": kind,
                "revoked_operations": revoked,
                "state": package.state,
                "diff": diff_specs(old_spec, spec)}

    def revert_to_version(self, name: str, version: str, *,
                          actor: str) -> dict:
        """Restore a recorded spec as a new version (records are immutable)."""
        record = self.store.read_version(name, version)
        payload = dict(record.get("spec") or {})
        spec = validate_spec(payload)
        return self.update_spec(name, spec, actor=actor,
                                notes="reverted to v%s" % version)

    # -- grants ----------------------------------------------------------

    def grant(self, name: str, operation: str, *, actor: str,
              reason: str = "", ttl_seconds: float = 0.0) -> dict:
        package = self.store.load(name)
        ledger = self._ledger(package)
        grant = ledger.grant(operation, actor=actor, reason=reason,
                             ttl_seconds=ttl_seconds)
        self.store.write_grants(name, ledger.to_dict())
        return grant.to_dict()

    def grant_spec(self, name: str, *, actor: str,
                   reason: str = "") -> list:
        package = self.store.load(name)
        ledger = self._ledger(package)
        created = ledger.grant_spec_operations(
            actor=actor, reason=reason or "granted from specification")
        self.store.write_grants(name, ledger.to_dict())
        return [grant.to_dict() for grant in created]

    def revoke(self, name: str, operation: str, *, actor: str,
               reason: str = "") -> dict:
        package = self.store.load(name)
        ledger = self._ledger(package)
        entry = ledger.revoke(operation, actor=actor, reason=reason)
        self.store.write_grants(name, ledger.to_dict())
        return entry

    def effective_permissions(self, name: str) -> dict:
        """The permission view a run would actually get."""
        package = self.store.load(name)
        ledger = self._ledger(package)
        return {
            "agent": name,
            "ceiling": list(package.spec.permissions.operations),
            "granted": list(ledger.active_operations()),
            "not_granted": sorted(
                set(package.spec.permissions.operations)
                - set(ledger.active_operations())),
            "mode_ceiling": package.spec.permissions.mode_ceiling,
            "allowed_paths": list(package.spec.permissions.allowed_paths),
            "denied_paths": list(package.spec.permissions.denied_paths),
            "blocked_always": list(FORBIDDEN_OPERATIONS),
            "state": package.state,
        }

    def _ledger(self, package: AgentPackage,
                spec: AgentSpec | None = None) -> GrantLedger:
        return GrantLedger(package.name, spec or package.spec,
                           self.store.read_grants(package.name))

    # -- cleanup ---------------------------------------------------------

    def delete(self, name: str, *, actor: str) -> dict:
        """Remove a package. Retired agents are kept unless forced."""
        if not self.store.exists(name):
            raise AgentNotFoundError("No such agent: %s" % name)
        package = self.store.load(name)
        if package.state != AgentState.RETIRED:
            raise AgentLifecycleError(
                "Retire agent %r before deleting it (state: %s)"
                % (name, package.state))
        self.store.remove(name)
        return {"agent": name, "deleted": True, "actor": actor}
