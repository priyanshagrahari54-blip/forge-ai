"""The Project Architect (A83): requirement → plan, editable before execution.

    "Build X" → requirement → specifications → architecture → components
              → dependencies → implementation plan → testing plan
              → benchmark plan → release plan

The Architect is deterministic and evidence-driven:

* classification comes from :mod:`forge.architect.classifier` (terms plus
  repository observations), never from a model's guess;
* components, decisions, and tasks are shaped by the classified project kind
  and enriched by the active **project profiles** — which is how ZEROOS gets a
  boot-and-driver plan without Forge's core knowing ZEROOS exists;
* repository facts (detected build systems, existing tests, languages) come
  from the A83 intelligence layer and are recorded as observations;
* anything the Architect cannot ground in evidence becomes an **open
  question**, not a fabricated decision.

The plan is data. It is stored, edited, re-validated, and must be explicitly
approved before any engine will execute it.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from forge.architect.classifier import (
    Classification,
    acceptance_criteria_for,
    classify,
    constraints_from,
    goals_from,
)
from forge.architect.models import (
    ArchitectureDecision,
    Component,
    Dependency,
    PlanTask,
    ProjectKind,
    ProjectPlan,
    Requirement,
    TestPlan,
    BenchmarkPlan,
    ReleasePlan,
    Specification,
)

MAX_REQUIREMENT_CHARS = 20_000
#: Project kinds that need a boot/validation step rather than just a build.
BOOT_KINDS = (ProjectKind.OPERATING_SYSTEM.value,
              ProjectKind.KERNEL_MODULE.value, ProjectKind.DRIVER.value)


class ArchitectError(ValueError):
    """Raised when a plan cannot be produced honestly."""


def _observations(root: Optional[Path]) -> Dict[str, Any]:
    """Gather real repository facts, tolerating a missing/unreadable tree."""
    facts: Dict[str, Any] = {"root": str(root) if root else ""}
    if root is None or not root.is_dir():
        facts["exists"] = False
        return facts
    facts["exists"] = True
    try:
        from forge.builder.detection import detect
        facts["build_systems"] = [
            {"name": system.name, "tool": system.tool,
             "tool_available": system.available,
             "evidence": list(system.evidence)}
            for system in detect(root)]
    except Exception as exc:  # noqa: BLE001 - recorded, never fatal
        facts["build_systems_error"] = str(exc)
    try:
        from forge.profiles import load_project_config
        config = load_project_config(root, strict=False)
        facts["project_config"] = {
            "name": config.name, "languages": list(config.languages),
            "mode": config.mode, "configured": config.configured,
            "config_error": config.config_error}
    except Exception as exc:  # noqa: BLE001
        facts["project_config_error"] = str(exc)
    try:
        from forge.intelligence.runtime_detection import RuntimeDetector
        facts["detected_languages"] = list(
            RuntimeDetector(root).detect().project_type)
    except Exception as exc:  # noqa: BLE001
        facts["detected_languages_error"] = str(exc)
    return facts


class ProjectArchitect:
    """Turns a requirement into a reviewable, editable project plan."""

    def __init__(self, root: str | Path = ".", *,
                 profiles: Sequence[Any] = ()) -> None:
        self.root = Path(root).resolve()
        self.profiles = list(profiles)

    # -- entry point -----------------------------------------------------

    def plan(self, requirement: str, *,
             plan_id: str = "") -> ProjectPlan:
        """Produce a full project plan for *requirement*."""
        statement = (requirement or "").strip()
        if not statement:
            raise ArchitectError("cannot plan an empty requirement")
        if len(statement) > MAX_REQUIREMENT_CHARS:
            raise ArchitectError(
                "requirement exceeds %d characters" % MAX_REQUIREMENT_CHARS)

        observations = _observations(self.root if self.root.is_dir() else None)
        observation_terms = self._observation_terms(observations)
        classification = classify(statement, observations=observation_terms)

        matched = self._matched_profiles(classification)
        requirement = Requirement(
            statement=statement,
            kind=classification.kind,
            evidence=list(classification.evidence),
            confidence=classification.confidence,
            goals=goals_from(statement),
            constraints=sorted(set(constraints_from(statement))
                               | set(self._profile_constraints(matched))),
            acceptance_criteria=acceptance_criteria_for(classification,
                                                        statement),
            scale=classification.scale,
            observations=observations)

        plan = ProjectPlan(
            id=plan_id or uuid.uuid4().hex[:12],
            requirement=requirement,
            profiles=[item.name for item in matched])

        self._specifications(plan, classification, matched)
        self._architecture(plan, classification, matched, observations)
        self._dependencies(plan, classification, matched, observations)
        self._tasks(plan, classification, matched)
        self._test_plan(plan, classification, matched, observations)
        self._benchmark_plan(plan, classification, matched)
        self._release_plan(plan, classification, matched)
        self._open_questions(plan, classification)
        return plan

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _observation_terms(observations: Dict[str, Any]) -> List[str]:
        terms: List[str] = []
        for system in observations.get("build_systems", []) or []:
            terms.append(str(system.get("name", "")))
        terms.extend(str(item)
                     for item in observations.get("detected_languages", []))
        config = observations.get("project_config") or {}
        terms.extend(str(item) for item in config.get("languages", []))
        return [term for term in terms if term]

    def _matched_profiles(self,
                          classification: Classification) -> List[Any]:
        """Profiles whose project types match the classification."""
        if not self.profiles:
            return []
        kind = classification.kind
        matched = [item for item in self.profiles
                   if kind in item.project_types
                   or any(term in item.project_types
                          for term in classification.evidence)]
        return matched or list(self.profiles)

    @staticmethod
    def _profile_constraints(profiles: Sequence[Any]) -> List[str]:
        out: List[str] = []
        for profile in profiles:
            for item in getattr(profile, "constraints", ()):
                if item not in out:
                    out.append(item)
        return out

    # -- sections --------------------------------------------------------

    def _specifications(self, plan: ProjectPlan,
                        classification: Classification,
                        profiles: Sequence[Any]) -> None:
        index = 1

        def add(area: str, statement: str, verifiable_by: str,
                rationale: str = "", required: bool = True) -> None:
            nonlocal index
            plan.specifications.append(Specification(
                id="S%02d" % index, area=area, statement=statement,
                verifiable_by=verifiable_by, rationale=rationale,
                required=required))
            index += 1

        add("scope", "The project delivers: %s" % (
            plan.requirement.goals[0] if plan.requirement.goals else
            plan.requirement.statement), "review",
            rationale="stated by the requester")
        add("quality", "The artifact builds cleanly with its own declared "
                       "build command.", "build",
            rationale="every project kind needs a build gate")
        add("quality", "Behaviour is covered by automated tests at the levels "
                       "listed in the test plan.", "test-pipeline",
            rationale="compilation is not evidence of correctness")
        add("quality", "Performance-sensitive behaviour is measured against a "
                       "baseline before any optimisation claim.", "benchmark",
            rationale="an unmeasured optimisation is not an optimisation")

        kind = classification.kind
        if kind in BOOT_KINDS:
            add("platform", "The artifact boots in a virtual machine and "
                            "reports readiness on the serial console.",
                "vm-boot-test",
                rationale="system software is validated by booting")
            add("platform", "Hardware capabilities are reported as SUPPORTED, "
                            "PARTIALLY_SUPPORTED, UNSUPPORTED, or UNKNOWN with "
                            "the probe that produced the status.",
                "hardware-probe",
                rationale="an invented capability is worse than an unknown one")
        if kind in (ProjectKind.WEB_APPLICATION.value,
                    ProjectKind.API_SERVICE.value):
            add("interface", "Every declared route or page is exercised by an "
                             "integration test.", "integration-test",
                rationale="the interface is the product")
        if kind == ProjectKind.LIBRARY.value:
            add("interface", "The public API is stable, documented, and "
                             "covered by unit tests.", "unit-test",
                rationale="a library's contract is its API")
        if kind == ProjectKind.AI_APPLICATION.value:
            add("model", "Every model call records the capability requested, "
                         "the model selected, and the outcome.",
                "routing-telemetry",
                rationale="routing quality can only improve if it is measured")
        for profile in profiles:
            for topic in list(getattr(profile, "knowledge", ()))[:6]:
                add("domain", "%s: %s" % (topic.topic, topic.summary[:280]),
                    "review", rationale="profile %s" % profile.name,
                    required=False)

    def _architecture(self, plan: ProjectPlan,
                      classification: Classification,
                      profiles: Sequence[Any],
                      observations: Dict[str, Any]) -> None:
        kind = classification.kind

        def add(name: str, responsibility: str, depends_on=None,
                interfaces=None, kind_name: str = "module",
                path: str = "") -> Component:
            component = Component(
                name=name, responsibility=responsibility,
                depends_on=list(depends_on or ()),
                interfaces=list(interfaces or ()), kind=kind_name, path=path)
            plan.components.append(component)
            return component

        def decide(title: str, decision: str, alternatives, consequences,
                   source: str) -> None:
            plan.decisions.append(ArchitectureDecision(
                id="ADR%02d" % (len(plan.decisions) + 1), title=title,
                decision=decision, alternatives=list(alternatives),
                consequences=list(consequences), source=source))

        add("core", "Domain logic with no dependency on the delivery surface.",
            interfaces=["public API"])
        add("delivery", "The surface the user touches (CLI, HTTP, UI, or "
                        "boot path).", depends_on=["core"],
            kind_name="adapter")
        add("persistence", "State that outlives a single run.",
            depends_on=["core"], kind_name="adapter")

        systems = observations.get("build_systems") or []
        if systems:
            primary = systems[0]
            decide("Build system",
                   "Build with %s (%s)." % (primary["name"],
                                            ", ".join(primary["argv"]
                                                     if "argv" in primary
                                                     else [])),
                   [system["name"] for system in systems[1:]] or
                   ["a build system Forge invents"],
                   ["The plan uses the project's own toolchain, so results "
                    "match what a contributor sees."],
                   source="detection")
        else:
            decide("Build system",
                   "Choose a build system before implementation starts.",
                   ["make", "cmake", "cargo", "npm", "pyproject"],
                   ["Nothing can be built or gated until this is decided."],
                   source="open")

        if kind in BOOT_KINDS:
            add("boot", "Boot path: firmware hand-off, mode switch, kernel "
                        "entry.", depends_on=["core"], kind_name="subsystem")
            add("drivers", "Device drivers behind a common probe/bind "
                           "contract.", depends_on=["core"],
                kind_name="subsystem")
            add("recovery", "Fallback boot path and update rollback.",
                depends_on=["boot", "persistence"], kind_name="subsystem")
            decide("Validation",
                   "Validate by booting under a virtual machine and reading "
                   "the serial console, not by compiling.",
                   ["trust the build", "manual testing"],
                   ["Boot tests need a VM harness in CI, which costs time but "
                    "is the only real evidence the artifact runs."],
                   source="convention")
        if kind == ProjectKind.AI_APPLICATION.value:
            add("model-fabric", "Model routing, telemetry, and fallback.",
                depends_on=["core"], kind_name="subsystem")

        for profile in profiles:
            recipes = list(getattr(profile, "recipes", ()))
            if recipes:
                decide("Project profile",
                       "Follow the %s profile for build, test, and boot "
                       "recipes." % profile.name,
                       ["generic heuristics"],
                       ["Domain recipes come from the profile, so Forge's core "
                        "stays project-agnostic."],
                       source="profile")

    def _dependencies(self, plan: ProjectPlan,
                      classification: Classification,
                      profiles: Sequence[Any],
                      observations: Dict[str, Any]) -> None:
        config = observations.get("project_config") or {}
        for language in list(config.get("languages", []))[:8]:
            plan.dependencies.append(Dependency(
                name=str(language), purpose="declared project language",
                required=True, risk=""))
        if classification.kind == ProjectKind.AI_APPLICATION.value:
            plan.dependencies.append(Dependency(
                name="model provider",
                purpose="serves the capabilities the plan routes to",
                required=True,
                risk="A provider that is unreachable must degrade to a local "
                     "fallback rather than fail the whole task."))
        if classification.kind in BOOT_KINDS:
            plan.dependencies.append(Dependency(
                name="qemu-system-x86_64",
                purpose="boots the artifact for automated validation",
                required=True,
                risk="Absent here it must be reported as unavailable, and the "
                     "boot gate must not be reported as passed."))
            plan.dependencies.append(Dependency(
                name="cross toolchain",
                purpose="compiles freestanding code for the target",
                required=True,
                risk="A host toolchain silently produces an artifact that "
                     "cannot boot."))

    def _tasks(self, plan: ProjectPlan,
               classification: Classification,
               profiles: Sequence[Any]) -> None:
        index = [0]

        def add(title: str, role: str, acceptance: str,
                depends_on=None, component: str = "") -> str:
            index[0] += 1
            task_id = "T%02d" % index[0]
            plan.tasks.append(PlanTask(
                id=task_id, title=title, role=role, acceptance=acceptance,
                depends_on=list(depends_on or ()), component=component))
            return task_id

        t1 = add("Refine the requirement into reviewed specifications.",
                 "architect",
                 "Every specification has an id, an area, and a stated way of "
                 "checking it.", component="core")
        t2 = add("Research prior art and constraints for the chosen approach.",
                 "research",
                 "Findings are recorded with sources, or the absence of "
                 "findings is recorded.", depends_on=[t1])
        t3 = add("Implement the core component.", "coding",
                 "The core builds and its unit tests pass.",
                 depends_on=[t1], component="core")
        t4 = add("Implement the delivery surface.", "coding",
                 "The surface is reachable and behaves as specified.",
                 depends_on=[t3], component="delivery")
        t5 = add("Review the implementation against the specifications.",
                 "review",
                 "Every specification is either satisfied or has a recorded "
                 "reason why not.", depends_on=[t4])
        t6 = add("Run the security review.", "security",
                 "No unresolved high-severity finding remains open.",
                 depends_on=[t4])
        t7 = add("Write the documentation.", "documentation",
                 "Setup, usage, and limitations are documented.",
                 depends_on=[t5])
        t8 = add("Measure the baseline performance.", "performance",
                 "A baseline result exists with at least the plan's minimum "
                 "number of runs.", depends_on=[t4])
        t9 = add("Package the artifact.", "release",
                 "The package builds from a clean checkout.",
                 depends_on=[t6, t7])

        if classification.kind in BOOT_KINDS:
            add("Bring up the boot path.", "coding",
                "The artifact reaches the kernel entry point under a virtual "
                "machine.", depends_on=[t3], component="boot")
            add("Probe and report hardware support.", "hardware",
                "Every probed device has an explicit support status and the "
                "evidence that produced it.", depends_on=[t3],
                component="drivers")
        if classification.kind == ProjectKind.AI_APPLICATION.value:
            add("Wire model routing and telemetry.", "coding",
                "Routed calls record capability, model, latency, and outcome.",
                depends_on=[t3], component="model-fabric")

    def _test_plan(self, plan: ProjectPlan,
                   classification: Classification,
                   profiles: Sequence[Any],
                   observations: Dict[str, Any]) -> None:
        test_plan = plan.test_plan
        test_plan.add_level(
            "static-analysis", "Syntax and lint gates over the whole tree.",
            required=False,
            notes="Advisory: findings are reported, they do not by themselves "
                  "fail the pipeline.")
        test_plan.add_level(
            "unit", "Component-level behaviour in isolation.",
            required=True,
            notes="Must collect at least one test; zero collected is not a "
                  "pass.")
        test_plan.add_level(
            "integration", "Behaviour across component boundaries.",
            required=classification.kind in (
                ProjectKind.WEB_APPLICATION.value,
                ProjectKind.API_SERVICE.value,
                ProjectKind.LIBRARY.value,
                ProjectKind.AI_APPLICATION.value))
        test_plan.add_level(
            "system", "The assembled artifact driven end to end.",
            required=classification.kind in BOOT_KINDS)
        test_plan.add_level(
            "regression", "Previously failing cases, re-run on every change.",
            required=False,
            notes="Targets come from recorded failures, never invented.")
        test_plan.add_level(
            "performance", "Measured timings compared against a baseline.",
            required=False,
            notes="Advisory at plan time; becomes a gate once a baseline "
                  "exists.")
        if classification.kind in BOOT_KINDS:
            test_plan.add_level(
                "vm-boot", "Boot the artifact in a virtual machine and read "
                           "the serial console.", required=True,
                notes="Needs a QEMU harness; when QEMU is unavailable the "
                      "level is reported unavailable, not passed.")
        for profile in profiles:
            for recipe in getattr(profile, "recipes_for",
                                  lambda _kind: ())("test"):
                test_plan.add_level(
                    "profile:%s" % recipe.name,
                    recipe.description or "Profile test recipe",
                    command=" ".join(recipe.argv), required=False,
                    notes="From profile %s." % profile.name)

    def _benchmark_plan(self, plan: ProjectPlan,
                        classification: Classification,
                        profiles: Sequence[Any]) -> None:
        bench = plan.benchmark_plan
        bench.add_metric(
            "wall_ms", "Measure the command's wall time over repeated runs.",
            target="no regression beyond the tolerance")
        bench.add_metric(
            "cpu_seconds", "Kernel-accounted CPU time for the process tree.",
            target="no regression beyond the tolerance")
        bench.add_metric(
            "peak_rss_mib", "Peak resident set size reported by the OS.",
            target="no regression beyond the tolerance")
        if classification.kind in BOOT_KINDS:
            bench.add_metric(
                "boot_ms", "Milliseconds from VM start to the readiness "
                           "marker on the serial console.",
                target="measured on every kernel change")
        if classification.kind in (ProjectKind.WEB_APPLICATION.value,
                                   ProjectKind.API_SERVICE.value):
            bench.add_metric("request_latency_ms",
                             "Latency of a representative request.",
                             target="no regression beyond the tolerance")
            bench.add_metric("throughput_rps",
                             "Requests per second under a fixed load.",
                             target="no regression beyond the tolerance",
                             higher_is_better=True)
        for profile in profiles:
            for recipe in getattr(profile, "recipes_for",
                                  lambda _kind: ())("bench"):
                bench.add_metric("profile:%s" % recipe.name,
                                 " ".join(recipe.argv),
                                 target="measured, never assumed")

    def _release_plan(self, plan: ProjectPlan,
                      classification: Classification,
                      profiles: Sequence[Any]) -> None:
        release = plan.release_plan
        release.add_step("Verify gates",
                         "Build, tests, security review, and benchmarks all "
                         "green.",
                         gate="Any red gate stops the release.")
        release.add_step("Tag and package",
                         "Produce the distributable artifact from a clean "
                         "checkout.")
        if classification.kind in BOOT_KINDS:
            release.add_step("Stage to the inactive slot",
                             "Write the new image beside the current one.")
            release.add_step("Boot-verify the staged image",
                             "Boot it under a virtual machine before making "
                             "it default.",
                             gate="A failed boot leaves the current image "
                                  "active.")
            release.rollback = ("Revert the boot order to the previous slot; "
                                "the previous image is never overwritten by "
                                "the update.")
        else:
            release.add_step("Publish", "Publish the artifact and its "
                                        "checksums.")
            release.rollback = ("Withdraw the published version and restore "
                                "the previous one; both remain retrievable.")

    def _open_questions(self, plan: ProjectPlan,
                        classification: Classification) -> None:
        if classification.kind == ProjectKind.UNKNOWN.value:
            plan.open_questions.append(
                "The requirement did not match any known project shape. Which "
                "of these is it: web application, API, library, CLI, desktop, "
                "mobile, AI application, or system software?")
        if classification.confidence and classification.confidence < 0.5:
            rivals = sorted(
                ((score, kind) for kind, score in classification.scores.items()
                 if kind != classification.kind), reverse=True)[:2]
            if rivals:
                plan.open_questions.append(
                    "The classification is ambiguous (%.2f). Also plausible: "
                    "%s. Confirm before approving." % (
                        classification.confidence,
                        ", ".join(kind for _score, kind in rivals)))
        if not (plan.requirement.observations.get("build_systems") or []):
            plan.open_questions.append(
                "No build system was detected in the repository. Which "
                "toolchain should the project use?")
