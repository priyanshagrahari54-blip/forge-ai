"""Candidate system (A81): isolated build, apply, test, compare.

A candidate is a throwaway copy of the repository under a temporary
directory. The proposed change is applied *there*, never to the live
checkout. Tests, build, security and architecture checks run inside the
copy; results are compared with a baseline measured on an identical,
unmodified copy. Any regression rejects the candidate.

Changes reach a candidate through a *change producer*: a callable that
receives the candidate root and proposal and returns ``{path: content}``.
In production the producer asks the routed model (via ``CoderAgent`` in
the isolated copy); tests use deterministic producers. Producers can never
write outside the candidate, and every produced change passes the
guardrails before being written.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from forge.security.verification import is_excluded
from forge.self_improvement.evidence import parse_pytest_output
from forge.self_improvement.guardrails import (
    GuardrailViolation,
    Guardrails,
    is_protected_path,
    normalize_path,
)
from forge.self_improvement.proposals import ImprovementProposal

ChangeProducer = Callable[[Path, ImprovementProposal], "dict[str, str]"]

MAX_CHANGE_BYTES = 200_000
MAX_CHANGED_FILES = 12
DEFAULT_TEST_TIMEOUT = 900


@dataclass
class TestOutcome:
    command: list[str]
    returncode: int | None
    passed: int
    failed: int
    errors: int
    duration_seconds: float
    output_tail: str
    failing_tests: list[str] = field(default_factory=list)
    skipped: int = 0
    #: True when pytest printed a final summary line (a crash/timeout does not).
    summary_found: bool = True
    #: True when the process was killed by the timeout.
    timed_out: bool = False

    @property
    def collected(self) -> int:
        return self.passed + self.failed + self.errors + self.skipped

    @property
    def ok(self) -> bool:
        """Green run: exit 0 with a real summary. Exit 5 (nothing collected)
        is *not* success — a change that breaks collection must not pass."""
        return self.returncode == 0 and self.summary_found and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["ok"] = self.ok
        return data


@dataclass
class Candidate:
    id: str
    proposal_id: str
    root: str
    baseline_root: str
    created_at: float = field(default_factory=time.time)
    changes: dict[str, str] = field(default_factory=dict)
    originals: dict[str, str | None] = field(default_factory=dict)
    change_fingerprint: str = ""
    status: str = "created"
    error: str = ""
    tests: TestOutcome | None = None
    baseline_tests: TestOutcome | None = None
    build_ok: bool | None = None
    security: dict[str, Any] = field(default_factory=dict)
    architecture: dict[str, Any] = field(default_factory=dict)
    guardrail_violations: list[dict[str, str]] = field(default_factory=list)
    comparison: "CandidateComparison | None" = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "proposal_id": self.proposal_id, "root": self.root,
            "baseline_root": self.baseline_root, "created_at": self.created_at,
            "changed_files": sorted(self.changes), "change_fingerprint": self.change_fingerprint,
            "status": self.status, "error": self.error,
            "tests": self.tests.to_dict() if self.tests else None,
            "baseline_tests": self.baseline_tests.to_dict() if self.baseline_tests else None,
            "build_ok": self.build_ok, "security": dict(self.security),
            "architecture": dict(self.architecture),
            "guardrail_violations": list(self.guardrail_violations),
            "comparison": self.comparison.to_dict() if self.comparison else None,
        }


@dataclass
class CandidateComparison:
    baseline_passed: int
    candidate_passed: int
    baseline_failed: int
    candidate_failed: int
    baseline_duration: float
    candidate_duration: float
    metric: str = ""
    baseline_value: float | None = None
    candidate_value: float | None = None
    improvement: float = 0.0
    improvement_pct: float | None = None
    regressions: list[str] = field(default_factory=list)
    improvements: list[str] = field(default_factory=list)

    @property
    def regressed(self) -> bool:
        return bool(self.regressions)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["regressed"] = self.regressed
        return data


def _tree_digest(root: Path) -> dict[str, str]:
    """Hash every application file under ``root`` (used to detect writes a
    change producer made outside the declared set)."""
    digests: dict[str, str] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if is_excluded(rel.parts):
            continue
        try:
            digests[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return digests


def _copy_repo(source: Path, destination: Path) -> None:
    """Copy application content only (no runtime state, caches, venvs)."""

    def ignore(directory: str, names: list[str]) -> set[str]:
        rel = Path(directory).relative_to(source).parts if Path(directory) != source else ()
        skipped: set[str] = set()
        for name in names:
            parts = rel + (name,)
            if is_excluded(parts) or name in (".git", ".forge"):
                skipped.add(name)
        return skipped

    if destination.exists():
        shutil.rmtree(destination, ignore_errors=True)
    shutil.copytree(source, destination, ignore=ignore, symlinks=False)


class CandidateRunner:
    """Create and evaluate isolated candidates for one repository root."""

    def __init__(self, root: str | Path = ".", *, guardrails: Guardrails | None = None,
                 workspace: Path | None = None, test_timeout: int = DEFAULT_TEST_TIMEOUT,
                 test_args: Iterable[str] | None = None) -> None:
        self.root = Path(root).resolve()
        self.guardrails = guardrails or Guardrails()
        self.workspace = workspace
        self.test_timeout = int(test_timeout)
        self.test_args = list(test_args) if test_args is not None else ["-q", "-p", "no:cacheprovider"]
        #: Baseline test outcomes keyed by (tree digest, targets); the
        #: baseline copy is identical for every candidate of one source tree,
        #: so measuring it once per tree state is exact, not approximate.
        self._baseline_cache: dict[tuple[str, tuple[str, ...]], TestOutcome] = {}
        self._source_digest: str | None = None

    # -- isolation ------------------------------------------------------------

    def _new_dir(self, prefix: str) -> Path:
        if self.workspace is not None:
            self.workspace.mkdir(parents=True, exist_ok=True)
            return Path(tempfile.mkdtemp(prefix=prefix, dir=str(self.workspace)))
        return Path(tempfile.mkdtemp(prefix=prefix))

    def create(self, proposal: ImprovementProposal) -> Candidate:
        baseline_root = self._new_dir("forge-baseline-")
        candidate_root = self._new_dir("forge-candidate-")
        _copy_repo(self.root, baseline_root)
        _copy_repo(self.root, candidate_root)
        self._source_digest = hashlib.sha256(
            json.dumps(_tree_digest(baseline_root), sort_keys=True).encode("utf-8")).hexdigest()
        ident = "CAND-" + hashlib.sha256(
            f"{proposal.id}|{time.time_ns()}".encode("utf-8")).hexdigest()[:12]
        return Candidate(id=ident, proposal_id=proposal.id, root=str(candidate_root),
                         baseline_root=str(baseline_root))

    def cleanup(self, candidate: Candidate) -> None:
        for path in (candidate.root, candidate.baseline_root):
            shutil.rmtree(path, ignore_errors=True)

    # -- apply ----------------------------------------------------------------

    def apply(self, candidate: Candidate, proposal: ImprovementProposal,
              producer: ChangeProducer) -> Candidate:
        """Ask ``producer`` for changes and write them *inside the candidate*."""
        root = Path(candidate.root)
        before_tree = _tree_digest(root)
        try:
            changes = producer(root, proposal)
        except GuardrailViolation as exc:
            candidate.status = "rejected"
            candidate.guardrail_violations = exc.violations
            candidate.error = str(exc)
            return candidate
        except Exception as exc:  # producer failure is a candidate failure
            candidate.status = "failed"
            candidate.error = f"change producer failed: {exc}"
            return candidate
        # A producer may write into the candidate directly (the model path
        # does, through CoderAgent). Every such write must be declared and
        # returned; silent side-writes are a rejection, and the candidate is
        # restored to its pre-producer state so nothing hidden is tested.
        after_tree = _tree_digest(root)
        side_writes = sorted(
            path for path in set(before_tree) | set(after_tree)
            if before_tree.get(path) != after_tree.get(path)
            and normalize_path(path) not in {normalize_path(str(k)) for k in (changes or {})})
        if side_writes:
            candidate.status = "rejected"
            candidate.error = ("change producer wrote undeclared files: "
                               + ", ".join(side_writes[:5]))
            candidate.guardrail_violations = [
                {"rule": "undeclared write", "path": p} for p in side_writes[:20]]
            return candidate
        if not isinstance(changes, dict) or not changes:
            candidate.status = "failed"
            candidate.error = "change producer returned no changes"
            return candidate
        normalized: dict[str, str] = {}
        for raw_path, content in changes.items():
            path = normalize_path(str(raw_path))
            if not isinstance(content, str):
                candidate.status = "failed"
                candidate.error = f"non-text content for {path}"
                return candidate
            normalized[path] = content
        if len(normalized) > MAX_CHANGED_FILES:
            candidate.status = "rejected"
            candidate.error = f"candidate touches {len(normalized)} files (max {MAX_CHANGED_FILES})"
            return candidate
        total = sum(len(c.encode("utf-8")) for c in normalized.values())
        if total > MAX_CHANGE_BYTES:
            candidate.status = "rejected"
            candidate.error = f"candidate change is {total} bytes (max {MAX_CHANGE_BYTES})"
            return candidate
        declared = {normalize_path(p) for p in proposal.affected_files}
        undeclared = sorted(p for p in normalized if p not in declared)
        if undeclared:
            candidate.status = "rejected"
            candidate.error = "candidate touched undeclared files: " + ", ".join(undeclared[:5])
            return candidate
        originals: dict[str, str | None] = {}
        base_root = Path(candidate.baseline_root)
        for path in normalized:
            target = base_root / path  # pristine copy, untouched by the producer
            if target.is_file():
                try:
                    originals[path] = target.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    originals[path] = None
            else:
                originals[path] = None
        if all(p.startswith("tests/") or "/tests/" in p for p in normalized):
            candidate.status = "rejected"
            candidate.error = "candidate only changes tests; improvements must change behavior"
            return candidate
        violations = self.guardrails.check_changes(normalized, originals)
        if violations:
            candidate.status = "rejected"
            candidate.guardrail_violations = violations
            candidate.error = str(GuardrailViolation(violations))
            return candidate
        for path, content in normalized.items():
            if path.endswith(".py"):
                try:
                    ast.parse(content, filename=path)
                except SyntaxError as exc:
                    candidate.status = "rejected"
                    candidate.error = f"syntax error in {path}: {exc.msg} (line {exc.lineno})"
                    return candidate
        for path, content in normalized.items():
            target = root / path
            try:
                target.resolve().relative_to(root.resolve())
            except ValueError:
                candidate.status = "rejected"
                candidate.error = f"path escapes candidate: {path}"
                return candidate
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        candidate.changes = normalized
        candidate.originals = originals
        candidate.change_fingerprint = hashlib.sha256(
            json.dumps(normalized, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        candidate.status = "applied"
        return candidate

    # -- measurement ----------------------------------------------------------

    def _run(self, command: list[str], cwd: Path, timeout: int) -> tuple[int | None, str, float]:
        started = time.perf_counter()
        env = dict(os.environ)
        # Keep candidate runs hermetic: no user site-packages surprises, no
        # bytecode written into the copy, and never inherit a live Forge root.
        env.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                    "PYTHONPATH": str(cwd)})
        env.pop("FORGE_DB_PATH", None)
        try:
            process = subprocess.run(command, cwd=cwd, text=True, capture_output=True,
                                     timeout=timeout, check=False, env=env)
        except subprocess.TimeoutExpired as exc:
            return None, f"TIMEOUT after {timeout}s: {exc}", time.perf_counter() - started
        except OSError as exc:
            return None, str(exc), time.perf_counter() - started
        return process.returncode, process.stdout + process.stderr, time.perf_counter() - started

    def run_tests(self, root: Path, targets: Iterable[str] | None = None) -> TestOutcome:
        command = [sys.executable, "-B", "-m", "pytest", *self.test_args, "-rfE"]
        if targets:
            command.extend(t for t in targets if not is_protected_path(t))
        code, output, duration = self._run(command, root, self.test_timeout)
        parsed = parse_pytest_output(output)
        return TestOutcome(command=command, returncode=code, passed=parsed["passed"],
                           failed=parsed["failed"], errors=parsed["errors"],
                           duration_seconds=round(duration, 3), output_tail=output[-4000:],
                           failing_tests=parsed["failing_tests"], skipped=parsed["skipped"],
                           summary_found=parsed["summary_found"],
                           timed_out=code is None and output.startswith("TIMEOUT"))

    def baseline_tests(self, root: Path, targets: Iterable[str] | None = None) -> TestOutcome:
        """Baseline outcome, measured once per source-tree state."""
        key = (self._source_digest or "", tuple(sorted(targets or [])))
        cached = self._baseline_cache.get(key)
        if cached is not None:
            return cached
        outcome = self.run_tests(root, targets)
        if self._source_digest:
            self._baseline_cache[key] = outcome
        return outcome

    def run_build(self, root: Path) -> bool:
        code, _output, _duration = self._run(
            [sys.executable, "-m", "compileall", "-q", "."], root, 300)
        return code == 0

    def run_security(self, root: Path, changed: Iterable[str]) -> dict[str, Any]:
        from forge.security.verification import VerificationPipeline

        gate = VerificationPipeline(root).security(list(changed))
        return {"passed": gate.passed, "details": gate.details,
                "findings": list(gate.evidence.get("findings", []))}

    def run_architecture(self, baseline_root: Path, candidate_root: Path,
                         changed: Iterable[str]) -> dict[str, Any]:
        """Architecture checks: no new import cycles, no protected files,
        no new dangerous imports, packages remain importable."""
        from forge.intelligence.dependencies import DependencyIndexer
        from forge.intelligence.dependency_analysis import DependencyAnalyzer

        issues: list[str] = []
        changed_list = list(changed)
        for path in changed_list:
            if is_protected_path(path):
                issues.append(f"protected path changed: {path}")
        try:
            before = DependencyAnalyzer(DependencyIndexer(baseline_root).build()).cycles()
            after = DependencyAnalyzer(DependencyIndexer(candidate_root).build()).cycles()
            before_set = {tuple(sorted(c)) for c in before}
            new_cycles = [c for c in after if tuple(sorted(c)) not in before_set]
            for cycle in new_cycles[:5]:
                issues.append("new dependency cycle: " + " -> ".join(cycle))
        except Exception as exc:  # fail closed: an unverifiable architecture is not clean
            issues.append(f"dependency analysis unavailable (fail-closed): {exc}")
        dangerous = re.compile(r"^\s*(?:import|from)\s+(?:ctypes|pickle|marshal|socket)\b", re.M)
        for path in changed_list:
            target = candidate_root / path
            if not target.is_file() or not path.endswith(".py"):
                continue
            try:
                text = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            base = baseline_root / path
            base_text = base.read_text(encoding="utf-8") if base.is_file() else ""
            if dangerous.search(text) and not dangerous.search(base_text):
                issues.append(f"new low-level import in {path}")
            if path.startswith("forge/") and not path.startswith("forge/security/") \
                    and re.search(r"^\s*from\s+forge\.security\.(?:policy|permissions)\s+import", text, re.M) \
                    and not re.search(r"^\s*from\s+forge\.security\.(?:policy|permissions)\s+import", base_text, re.M):
                issues.append(f"new direct dependency on security policy internals in {path}")
        return {"passed": not issues, "issues": issues}

    def baseline(self, candidate: Candidate, targets: Iterable[str] | None = None) -> TestOutcome:
        outcome = self.baseline_tests(Path(candidate.baseline_root), targets)
        candidate.baseline_tests = outcome
        return outcome

    def evaluate(self, candidate: Candidate, proposal: ImprovementProposal,
                 *, targets: Iterable[str] | None = None,
                 metric_probe: Callable[[Path], float | None] | None = None) -> Candidate:
        """Test the candidate, measure the baseline, and compare."""
        if candidate.status != "applied":
            return candidate
        root = Path(candidate.root)
        base_root = Path(candidate.baseline_root)
        target_list = list(targets or [])
        candidate.status = "testing"
        candidate.baseline_tests = candidate.baseline_tests or self.baseline_tests(base_root, target_list or None)
        candidate.tests = self.run_tests(root, target_list or None)
        candidate.build_ok = self.run_build(root)
        candidate.security = self.run_security(root, candidate.changes)
        candidate.architecture = self.run_architecture(base_root, root, candidate.changes)
        base_value = cand_value = None
        if metric_probe is not None:
            try:
                base_value = metric_probe(base_root)
                cand_value = metric_probe(root)
            except Exception as exc:
                candidate.error = f"metric probe failed: {exc}"
        candidate.comparison = self.compare(candidate, proposal, base_value, cand_value)
        candidate.status = "regressed" if candidate.comparison.regressed else "evaluated"
        return candidate

    def compare(self, candidate: Candidate, proposal: ImprovementProposal,
                baseline_value: float | None = None,
                candidate_value: float | None = None) -> CandidateComparison:
        base = candidate.baseline_tests
        cand = candidate.tests
        assert base is not None and cand is not None
        regressions: list[str] = []
        improvements: list[str] = []
        if cand.timed_out:
            regressions.append("tests: candidate run timed out")
        if not cand.summary_found:
            regressions.append("tests: candidate run produced no pytest summary (crash?)")
        if cand.returncode in (2, 3, 4):
            regressions.append(f"tests: pytest exited {cand.returncode} (interrupted/internal/usage error)")
        if not cand.ok and base.ok:
            regressions.append(f"tests: candidate failed ({cand.failed} failed, "
                               f"{cand.errors} errors) while baseline passed")
        if base.collected and cand.collected < base.collected:
            regressions.append(f"tests: collected count fell {base.collected} -> {cand.collected} "
                               "(tests lost or collection broken)")
        if cand.passed < base.passed:
            regressions.append(f"tests: passed count fell {base.passed} -> {cand.passed}")
        if cand.skipped > base.skipped:
            regressions.append(f"tests: skips rose {base.skipped} -> {cand.skipped}")
        new_failures = sorted(set(cand.failing_tests) - set(base.failing_tests))
        if new_failures:
            regressions.append("tests: new failures " + ", ".join(new_failures[:5]))
        base_bad = base.failed + base.errors
        cand_bad = cand.failed + cand.errors
        fixed = sorted(set(base.failing_tests) - set(cand.failing_tests))
        if cand_bad < base_bad and cand.collected >= base.collected:
            improvements.append(f"tests: failures fell {base_bad} -> {cand_bad}"
                                + (" (" + ", ".join(fixed[:3]) + ")" if fixed else ""))
        # A higher pass count alone is *not* an improvement: a candidate could
        # add trivial tests. Only fewer failures or a metric probe count.
        if candidate.build_ok is False:
            regressions.append("build: compileall failed")
        if candidate.security and not candidate.security.get("passed", False):
            regressions.append("security: findings in changed files")
        if candidate.architecture and not candidate.architecture.get("passed", False):
            regressions.append("architecture: " + "; ".join(candidate.architecture.get("issues", [])[:3]))
        if base.duration_seconds > 0 and cand.duration_seconds > base.duration_seconds * 1.5 + 5:
            regressions.append(f"latency: tests slowed {base.duration_seconds:.1f}s -> "
                               f"{cand.duration_seconds:.1f}s")
        improvement = 0.0
        pct: float | None = None
        metric = proposal.metric
        if baseline_value is not None and candidate_value is not None:
            # Every self-analysis metric is "lower is better".
            improvement = float(baseline_value) - float(candidate_value)
            if baseline_value:
                pct = round(improvement / abs(float(baseline_value)) * 100.0, 2)
            if improvement < 0:
                regressions.append(f"{metric}: worsened {baseline_value} -> {candidate_value}")
            elif improvement > 0:
                improvements.append(f"{metric}: improved {baseline_value} -> {candidate_value}")
        else:
            failure_delta = base_bad - cand_bad
            improvement = float(failure_delta) if cand.collected >= base.collected else 0.0
            if improvement > 0:
                pct = round(failure_delta / max(1, base_bad) * 100.0, 2)
        return CandidateComparison(
            baseline_passed=base.passed, candidate_passed=cand.passed,
            baseline_failed=base.failed + base.errors, candidate_failed=cand.failed + cand.errors,
            baseline_duration=base.duration_seconds, candidate_duration=cand.duration_seconds,
            metric=metric, baseline_value=baseline_value, candidate_value=candidate_value,
            improvement=round(improvement, 4), improvement_pct=pct,
            regressions=regressions, improvements=improvements)


# -- change producers ------------------------------------------------------------

def model_change_producer(fabric: Any = None, router: Any = None) -> ChangeProducer:
    """Producer that asks the routed model through ``CoderAgent`` in the
    candidate copy. Writes happen through the coder's permissioned runtime
    against the *candidate* root, so the live checkout is never touched."""

    def produce(candidate_root: Path, proposal: ImprovementProposal) -> dict[str, str]:
        from forge.agents.coder import CoderAgent
        from forge.agents.execution import AgentRequest
        from forge.core.task_engine import TaskEngine, TaskStatus
        from forge.intelligence.repository import RepositoryIntelligence

        coder = CoderAgent(root=str(candidate_root), router=router, fabric=fabric)
        intelligence = RepositoryIntelligence.build(candidate_root)
        context = coder.build_context(intelligence, proposal.instructions,
                                      tuple(proposal.affected_files))
        task = TaskEngine().add(f"self-improve-{proposal.id}", proposal.title)
        task.status = TaskStatus.CODING
        before: dict[str, str | None] = {}
        for path in proposal.affected_files:
            target = candidate_root / path
            before[path] = target.read_text(encoding="utf-8") if target.is_file() else None
        instructions = (
            f"{proposal.instructions}\n\nHypothesis: {proposal.hypothesis}\n"
            f"Only modify these files: {', '.join(proposal.affected_files)}.\n"
            "Do not touch security, permissions, policy, approvals, or credentials.")
        response = coder.execute(AgentRequest(
            task=task, stage=TaskStatus.CODING, context=context,
            instructions=instructions, metadata={"approved": True}))
        if not response.success:
            raise RuntimeError(response.error or "model coding failed")
        changes: dict[str, str] = {}
        for path in sorted(set(response.metadata.get("files", [])) | set(before)):
            path = normalize_path(str(path))
            target = candidate_root / path
            if target.is_file():
                text = target.read_text(encoding="utf-8")
                if before.get(path) != text:
                    changes[path] = text
            elif before.get(path) is not None:
                raise RuntimeError(f"model deleted {path}; deletions are not permitted")
        return changes

    return produce
