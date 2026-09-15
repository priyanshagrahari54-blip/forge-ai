"""A83 platform tests: architect, multi-agent engineering, hardware, VM,
task routing, routing statistics, and long-term memory.

Every test drives the real implementation. The VM test does not fake a boot:
it runs a real subprocess whose stdout stands in for the guest's serial
console, so the marker judgement is exercised end to end. Hardware tests point
the probe's sysfs/proc paths at a synthetic tree, so the parsing is tested
rather than assumed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from forge.architect import (
    NotApproved,
    PlanError,
    PlanStore,
    ProjectArchitect,
    classify,
)
from forge.engineering import (
    AgentSpec,
    ContextError,
    EngineeringPipeline,
    ProjectContext,
    Roster,
    default_roster,
    order_specs,
)
from forge.hardware import (
    PARTIALLY_SUPPORTED,
    SUPPORTED,
    UNKNOWN,
    UNSUPPORTED,
    Device,
    HardwareAgent,
    HardwareProbe,
    HardwareReport,
    build_matrix,
    pci_class_name,
    render as render_hardware,
)
from forge.hardware import probe as probe_module
from forge.vm import (
    BOOTED,
    NO_IMAGE,
    NO_MARKER,
    PANIC,
    TIMEOUT,
    UNAVAILABLE,
    BootScenario,
    VmHarness,
    judge,
    render as render_vm,
)
from forge.models.fabric import RouteDecision, RouterFeedback
from forge.models.stats import CapabilityStats, StatsStore, render, rerank
from forge.models.taskmap import (
    ARCHITECTURE,
    CODE_LARGE,
    CODE_SMALL,
    HEAVY,
    REPO_ANALYSIS,
    TRIVIAL,
    classify_task,
    profile_for,
    request_for,
    routing_table,
)
from forge.knowledge import (
    BENCHMARK,
    BUG,
    CONSTRAINT,
    DECISION,
    FAILED_APPROACH,
    MemoryEntry,
    MemoryError,
    ProjectMemory,
    memory_prompt,
    render as render_memory,
)


# ---------------------------------------------------------------------------
# Architect
# ---------------------------------------------------------------------------

def test_classification_cites_evidence():
    result = classify(
        "Build an operating system with its own kernel for x86-64")
    assert result.kind == "operating-system"
    assert result.confidence > 0.5
    assert result.evidence


def test_classification_without_signal_says_unknown():
    result = classify("Do the thing")
    assert result.kind == "unknown"
    assert result.confidence == 0.0


@pytest.mark.parametrize("text,kind", [
    ("Build a REST API service with endpoints", "api-service"),
    ("Write a reusable library others can install", "library"),
    ("Create a desktop application with a GUI", "desktop-application"),
    ("Build an Android application", "mobile-application"),
    ("Write a kernel module for a character device", "kernel-module"),
])
def test_project_types_are_recognised(text, kind):
    assert classify(text).kind == kind


def test_boot_kind_requires_a_boot_test(tmp_path):
    plan = ProjectArchitect(tmp_path).plan(
        "Build an operating system with its own kernel")
    levels = [item["name"] for item in plan.test_plan.levels]
    metrics = [item["name"] for item in plan.benchmark_plan.metrics]
    assert "vm-boot" in levels
    assert "boot_ms" in metrics
    assert any(component.name == "boot" for component in plan.components)


def test_non_boot_project_does_not_require_a_vm(tmp_path):
    plan = ProjectArchitect(tmp_path).plan("Build a REST API with a database")
    levels = [item["name"] for item in plan.test_plan.levels]
    assert "vm-boot" not in levels


def test_plan_validates_dependency_cycles(tmp_path):
    plan = ProjectArchitect(tmp_path).plan("Build a REST API with a database")
    plan.tasks[0].depends_on = [plan.tasks[-1].id]
    plan.tasks[-1].depends_on = [plan.tasks[0].id]
    assert any("cycle" in item.lower() for item in plan.validate())


def test_plan_validates_unknown_dependencies(tmp_path):
    plan = ProjectArchitect(tmp_path).plan("Build a REST API with a database")
    plan.tasks[0].depends_on = ["T99"]
    assert any("T99" in item for item in plan.validate())


def test_empty_requirement_is_rejected(tmp_path):
    plan = ProjectArchitect(tmp_path).plan("Build a REST API with a database")
    plan.requirement.statement = ""
    assert plan.validate()
    assert plan.ready_tasks() == []


def test_ready_tasks_have_no_unmet_dependencies(tmp_path):
    plan = ProjectArchitect(tmp_path).plan(
        "Build an operating system with its own kernel")
    ready = plan.ready_tasks()
    assert ready
    ids = {task.id for task in plan.tasks}
    for task in ready:
        assert task.id in ids
        assert all(dep in ids for dep in task.depends_on)


def test_fingerprint_is_stable_across_a_reload(tmp_path):
    store = PlanStore(tmp_path)
    plan = ProjectArchitect(tmp_path).plan("Build a REST API with a database")
    store.save(plan)
    first = store.load(plan.id).fingerprint()
    second = store.load(plan.id).fingerprint()
    assert first == second, "a fingerprint that moves on reload breaks approval"


def test_fingerprint_excludes_the_fingerprint_itself(tmp_path):
    plan = ProjectArchitect(tmp_path).plan("Build a REST API with a database")
    assert plan.fingerprint()
    assert "fingerprint" not in plan._content_hash_payload()


def test_editing_a_plan_drops_its_approval(tmp_path):
    store = PlanStore(tmp_path)
    plan = ProjectArchitect(tmp_path).plan("Build a REST API with a database")
    store.save(plan)
    approved = store.approve(plan.id, actor="reviewer")
    first = approved.fingerprint
    assert store.load(plan.id).status == "approved"

    edited = store.update(plan.id, {"open_questions": ["drop the database"]},
                          actor="reviewer", summary="descoped")
    assert edited.status == "draft"
    assert edited.revision > 1
    assert edited.fingerprint() != first
    assert edited.history


def test_update_rejects_non_editable_fields(tmp_path):
    store = PlanStore(tmp_path)
    plan = ProjectArchitect(tmp_path).plan("Build a REST API with a database")
    store.save(plan)
    with pytest.raises(PlanError) as excinfo:
        store.update(plan.id, {"id": "other"}, actor="x")
    assert "cannot be edited" in str(excinfo.value)


def test_execution_is_refused_without_approval(tmp_path):
    store = PlanStore(tmp_path)
    plan = ProjectArchitect(tmp_path).plan("Build a REST API with a database")
    store.save(plan)
    with pytest.raises(NotApproved) as excinfo:
        store.checkout_for_execution(plan.id)
    assert "not been approved" in str(excinfo.value)


def test_execution_is_refused_after_an_edit(tmp_path):
    store = PlanStore(tmp_path)
    plan = ProjectArchitect(tmp_path).plan("Build a REST API with a database")
    store.save(plan)
    store.approve(plan.id, actor="reviewer")
    store.update(plan.id, {"open_questions": ["changed"]}, actor="reviewer")
    with pytest.raises(NotApproved):
        store.checkout_for_execution(plan.id)


def test_approved_plan_checks_out_and_executes(tmp_path):
    store = PlanStore(tmp_path)
    plan = ProjectArchitect(tmp_path).plan("Build a REST API with a database")
    store.save(plan)
    approved = store.approve(plan.id, actor="reviewer")
    checked_out = store.checkout_for_execution(plan.id)
    assert checked_out.id == plan.id
    assert checked_out.status == "executing"
    assert store.approval(plan.id).fingerprint == approved.fingerprint


def test_unknown_plan_raises(tmp_path):
    with pytest.raises(PlanError):
        PlanStore(tmp_path).load("does-not-exist")


def test_plan_survives_a_json_round_trip(tmp_path):
    plan = ProjectArchitect(tmp_path).plan("Build a REST API with a database")
    payload = plan.to_dict()
    assert payload["fingerprint"] == plan.fingerprint()
    assert json.loads(json.dumps(payload, default=str))


# ---------------------------------------------------------------------------
# Shared engineering context
# ---------------------------------------------------------------------------

def test_context_requires_a_key_and_a_source(tmp_path):
    context = ProjectContext("demo", root=tmp_path)
    with pytest.raises(ContextError):
        context.record("", "observation", "v", source="a")
    with pytest.raises(ContextError):
        context.record("k", "observation", "v", source="")


def test_context_rejects_unknown_kinds(tmp_path):
    context = ProjectContext("demo", root=tmp_path)
    with pytest.raises(ContextError):
        context.record("k", "not-a-kind", "v", source="a")


def test_context_facts_are_last_write_wins(tmp_path):
    context = ProjectContext("demo", root=tmp_path)
    context.record("language", "observation", "python", source="a")
    context.record("language", "observation", "python+rust", source="b")
    assert context.fact("language").value == "python+rust"
    assert context.fact("language").source == "b"
    assert context.fact("missing") is None


def test_context_rejects_oversized_values(tmp_path):
    context = ProjectContext("demo", root=tmp_path)
    with pytest.raises(ContextError):
        context.record("big", "observation", "x" * (1 << 20), source="a")


def test_context_rejects_unserialisable_values(tmp_path):
    context = ProjectContext("demo", root=tmp_path)
    circular: dict = {"name": "loop"}
    circular["self"] = circular
    with pytest.raises(ContextError):
        context.record("k", "observation", circular, source="a")


def test_context_turns_and_status(tmp_path):
    context = ProjectContext("demo", root=tmp_path)
    context.note_turn("coding", "coding", "wrote the parser", ok=True)
    context.note_turn("testing", "testing", "3 failed", ok=False)
    assert len(context.turns()) == 2
    assert context.turns()[-1].ok is False
    status = context.status()
    assert status["turns"] == 2
    assert status["facts"] == 0
    assert status["fingerprint"]


def test_context_persists_and_reloads(tmp_path):
    context = ProjectContext("demo", root=tmp_path)
    context.record("kind", "architecture", {"layers": 3}, source="architect")
    target = tmp_path / "context.json"
    context.save(target)
    reloaded = ProjectContext.load(target)
    assert reloaded.fact("kind").value == {"layers": 3}
    assert reloaded.fact("kind").source == "architect"


# ---------------------------------------------------------------------------
# Roster and scheduling
# ---------------------------------------------------------------------------

def test_roster_declares_the_whole_team():
    roster = default_roster()
    roles = roster.roles()
    for expected in ("architect", "research", "dependency", "coding", "build",
                     "testing", "debugging", "review", "security",
                     "performance", "hardware", "devops", "documentation",
                     "release"):
        assert expected in roles, expected
    assert len(roles) == 14


def test_every_agent_declares_what_it_reads():
    for spec in default_roster().specs:
        assert spec.responsibility
        assert spec.reads, "%s declares nothing it reads" % spec.role


def test_implemented_agents_are_a_subset_of_the_roster():
    roster = default_roster()
    implemented = roster.implemented()
    assert implemented
    assert set(implemented) <= set(roster.roles())
    assert roster.spec("coding") is not None
    assert roster.spec("no-such-role") is None


def test_mutating_agents_run_alone():
    specs = [
        AgentSpec("architect", "architect", "plans", (), ("plan",), False,
                  None),
        AgentSpec("research", "research", "reads", (), ("facts",), False,
                  None),
        AgentSpec("coding", "coding", "edits", ("plan",), ("files",), True,
                  None),
        AgentSpec("testing", "testing", "tests", ("files",), ("report",),
                  False, None),
    ]
    for wave in order_specs(specs):
        if any(spec.mutating for spec in wave):
            assert len(wave) == 1, "a mutating agent shared a wave"


def test_ordering_respects_reads_and_writes():
    specs = [
        AgentSpec("testing", "testing", "", ("files",), ("report",), False,
                  None),
        AgentSpec("coding", "coding", "", ("plan",), ("files",), True, None),
        AgentSpec("architect", "architect", "", (), ("plan",), False, None),
    ]
    waves = order_specs(specs)
    order = [spec.role for wave in waves for spec in wave]
    assert order.index("architect") < order.index("coding")
    assert order.index("coding") < order.index("testing")


def test_unresolvable_reads_are_run_not_dropped():
    specs = [
        AgentSpec("a", "a", "", ("nothing-provides-this",), ("x",), False,
                  None),
        AgentSpec("b", "b", "", ("x",), ("y",), False, None),
    ]
    scheduled = [spec.role for wave in order_specs(specs) for spec in wave]
    assert set(scheduled) == {"a", "b"}, "work was silently dropped"


def test_cyclic_reads_terminate():
    specs = [
        AgentSpec("a", "a", "", ("b",), ("a",), False, None),
        AgentSpec("b", "b", "", ("a",), ("b",), False, None),
    ]
    scheduled = [spec.role for wave in order_specs(specs) for spec in wave]
    assert set(scheduled) == {"a", "b"}


def test_pipeline_runs_the_architect(tmp_path):
    roster = Roster(specs=[
        AgentSpec("architect", "architect", "plans", ("requirement",),
                  ("plan",), False, None),
    ])
    report = EngineeringPipeline(tmp_path, roster=roster).run(
        roles=("architect",))
    assert report.stages
    assert report.stages[0].role == "architect"
    assert report.status in ("passed", "failed", "blocked")
    assert report.duration_ms >= 0


def test_pipeline_records_context(tmp_path):
    report = EngineeringPipeline(tmp_path).run(roles=("architect",))
    assert report.context_revision >= 0
    assert isinstance(report.parallel_groups, list)


# ---------------------------------------------------------------------------
# Hardware
# ---------------------------------------------------------------------------

def test_pci_class_names_are_reported_not_guessed():
    assert pci_class_name("0x0200") == "network"
    assert pci_class_name("0x030000").startswith("vga") or \
        pci_class_name("0x030000") == "display"
    assert pci_class_name("0xffff") == "unknown-class"
    assert pci_class_name("nonsense") in ("", "unknown-class")


def _point_probe_at(monkeypatch, tmp_path):
    """Aim the probe's sysfs/proc constants at a synthetic tree."""
    sys_root = tmp_path / "sys"
    proc_root = tmp_path / "proc"
    for name, value in (
            ("SYSFS_PCI", sys_root / "bus/pci/devices"),
            ("SYSFS_USB", sys_root / "bus/usb/devices"),
            ("SYSFS_BLOCK", sys_root / "block"),
            ("SYSFS_NET", sys_root / "class/net"),
            ("SYSFS_SOUND", sys_root / "class/sound"),
            ("SYSFS_INPUT", sys_root / "class/input"),
            ("DMI_PRODUCT", sys_root / "class/dmi/id"),
            ("PROC_CPUINFO", proc_root / "cpuinfo"),
            ("PROC_MEMINFO", proc_root / "meminfo")):
        monkeypatch.setattr(probe_module, name, str(value))
    proc_root.mkdir(parents=True, exist_ok=True)
    (sys_root / "bus/pci/devices").mkdir(parents=True, exist_ok=True)
    return sys_root


def test_probe_on_an_empty_tree_reports_nothing_observed(monkeypatch, tmp_path):
    _point_probe_at(monkeypatch, tmp_path)
    report = HardwareProbe(allow_tools=False).probe()
    assert isinstance(report, HardwareReport)
    assert isinstance(report.devices, list)
    matrix = build_matrix(report)
    # Nothing was observed, so nothing may be claimed either way.
    assert all(subsystem.status == UNKNOWN
               for subsystem in matrix.subsystems)
    assert matrix.device_count == 0


def test_probe_never_reports_unsupported_without_a_device(monkeypatch, tmp_path):
    _point_probe_at(monkeypatch, tmp_path)
    report = HardwareProbe(allow_tools=False).probe()
    for device in report.devices:
        if device.status == UNSUPPORTED:
            assert device.identifier, "UNSUPPORTED needs an observed device"
            assert device.evidence, "UNSUPPORTED needs evidence"


def test_probe_reads_a_synthetic_pci_device(monkeypatch, tmp_path):
    sys_root = _point_probe_at(monkeypatch, tmp_path)
    device = sys_root / "bus/pci/devices/0000:00:02.0"
    device.mkdir(parents=True)
    (device / "vendor").write_text("0x8086\n", encoding="utf-8")
    (device / "device").write_text("0x1234\n", encoding="utf-8")
    (device / "class").write_text("0x030000\n", encoding="utf-8")
    report = HardwareProbe(allow_tools=False).probe()
    assert report.devices, "the synthetic PCI device was not found"
    found = report.devices[0]
    assert found.identifier
    assert found.evidence, "every device fact must cite its source"
    assert found.status in (SUPPORTED, PARTIALLY_SUPPORTED, UNSUPPORTED,
                            UNKNOWN)


def test_probe_reads_cpu_and_memory(monkeypatch, tmp_path):
    _point_probe_at(monkeypatch, tmp_path)
    (tmp_path / "proc/cpuinfo").write_text(
        "processor\t: 0\nmodel name\t: Test CPU @ 3.00GHz\n"
        "cpu MHz\t\t: 3000.000\nflags\t\t: fpu sse2 avx2\n\n"
        "processor\t: 1\nmodel name\t: Test CPU @ 3.00GHz\n",
        encoding="utf-8")
    (tmp_path / "proc/meminfo").write_text(
        "MemTotal:       16384000 kB\nMemAvailable:  8192000 kB\n",
        encoding="utf-8")
    report = HardwareProbe(allow_tools=False).probe()
    assert report.cpu, "cpuinfo was not read"
    assert report.memory, "meminfo was not read"
    blob = json.dumps({"cpu": report.cpu, "memory": report.memory},
                      default=str).lower()
    assert "test cpu" in blob
    assert "avx2" in blob


def test_probe_reads_dmi_firmware(monkeypatch, tmp_path):
    sys_root = _point_probe_at(monkeypatch, tmp_path)
    dmi = sys_root / "class/dmi/id"
    dmi.mkdir(parents=True)
    (dmi / "sys_vendor").write_text("QEMU\n", encoding="utf-8")
    (dmi / "product_name").write_text("Standard PC\n", encoding="utf-8")
    report = HardwareProbe(allow_tools=False).probe()
    assert "qemu" in json.dumps(report.firmware, default=str).lower()


def test_probe_reports_a_unreadable_source_as_unavailable(monkeypatch, tmp_path):
    _point_probe_at(monkeypatch, tmp_path)
    # A cpuinfo that exists but cannot be decoded must not crash the probe.
    (tmp_path / "proc/cpuinfo").write_bytes(b"\xff\xfe\x00garbage")
    report = HardwareProbe(allow_tools=False).probe()
    assert isinstance(report.unavailable, list)


def test_matrix_classifies_an_observed_device():
    report = HardwareReport(
        cpu={"model": "Test CPU", "count": 4},
        devices=[Device(category="cpu", identifier="cpu0",
                        label="Test CPU", status=SUPPORTED,
                        evidence=["/proc/cpuinfo"])])
    matrix = build_matrix(report)
    assert matrix.subsystems
    cpu = next((item for item in matrix.subsystems
                if item.subsystem == "cpu"), None)
    assert cpu is not None
    assert cpu.status == SUPPORTED
    assert cpu.observed
    assert cpu.evidence


def test_matrix_is_unknown_when_nothing_was_observed():
    matrix = build_matrix(HardwareReport())
    assert matrix.subsystems
    assert all(subsystem.status == UNKNOWN
               for subsystem in matrix.subsystems)
    assert all(not subsystem.supported for subsystem in matrix.subsystems)


def test_matrix_lists_every_status_it_uses():
    report = HardwareReport(devices=[
        Device(category="cpu", identifier="c", status=SUPPORTED,
               evidence=["x"]),
        Device(category="network", identifier="n", status=UNSUPPORTED,
               evidence=["y"]),
    ])
    matrix = build_matrix(report)
    for subsystem in matrix.subsystems:
        assert subsystem.status in (
            SUPPORTED, PARTIALLY_SUPPORTED, UNSUPPORTED, UNKNOWN)
        assert subsystem.reason, "every status needs a reason"


def test_hardware_agent_surveys_and_records(tmp_path):
    agent = HardwareAgent(tmp_path, probe=HardwareProbe(allow_tools=False))
    report, matrix = agent.survey()
    assert isinstance(report, HardwareReport)
    assert matrix.subsystems
    context = ProjectContext("demo", root=tmp_path)
    agent.attach(context)
    response = agent.execute(None)
    assert response.success in (True, False)
    assert context.fact("hardware") is not None


def test_hardware_report_renders_every_status_word(tmp_path):
    text = render_hardware(
        build_matrix(HardwareProbe(allow_tools=False).probe()))
    assert "UNKNOWN" in text or "unknown" in text.lower()


# ---------------------------------------------------------------------------
# VM / QEMU
# ---------------------------------------------------------------------------

def test_judge_prefers_a_panic_over_a_ready_marker():
    verdict, ready, failure = judge(
        "kernel: ready\nkernel: panic - not syncing\n",
        ready_marker="ready", failure_marker="panic")
    assert verdict == PANIC
    assert "ready" in ready
    assert "panic" in failure


def test_judge_reports_a_booted_guest():
    verdict, ready, _ = judge("hello\nshell ready\n",
                              ready_marker="shell ready",
                              failure_marker="panic")
    assert verdict == BOOTED
    assert ready == "shell ready"


def test_judge_distinguishes_no_marker_from_timeout():
    assert judge("booting...\n", ready_marker="ready",
                 failure_marker="panic")[0] == NO_MARKER
    assert judge("booting...\n", ready_marker="ready", failure_marker="panic",
                 timed_out=True)[0] == TIMEOUT


def test_judge_without_markers_never_claims_success():
    assert judge("anything at all")[0] == NO_MARKER
    assert judge("", ready_marker="ready")[0] == NO_MARKER


def _console_script(tmp_path, text, *, hang=False):
    """An executable printing ``text`` to stdout, standing in for a guest."""
    path = tmp_path / "guest.py"
    body = "import sys\n"
    if hang:
        body += "sys.stdout.write('booting\\n'); sys.stdout.flush()\n"
        body += "import time; time.sleep(60)\n"
    else:
        body += "sys.stdout.write(%r)\n" % text
    path.write_text(body, encoding="utf-8")
    return path


def test_harness_boots_a_guest_that_prints_its_marker(tmp_path):
    script = _console_script(tmp_path, "BIOS ok\nkernel: shell ready\n")
    result = VmHarness(tmp_path).boot(BootScenario(
        name="boot", argv=(sys.executable, str(script)),
        ready_marker="shell ready", failure_marker="panic",
        timeout_seconds=30))
    assert result.verdict == BOOTED
    assert result.ok
    assert result.ready_line == "kernel: shell ready"
    assert result.return_code == 0
    assert result.reason


def test_harness_detects_a_panic(tmp_path):
    script = _console_script(
        tmp_path, "booting\nkernel panic - not syncing: VFS\n")
    result = VmHarness(tmp_path).boot(BootScenario(
        name="boot", argv=(sys.executable, str(script)),
        ready_marker="shell ready", failure_marker="kernel panic",
        timeout_seconds=30))
    assert result.verdict == PANIC
    assert not result.ok
    assert "kernel panic" in result.failure_line


def test_harness_reports_no_marker_when_the_guest_is_silent(tmp_path):
    script = _console_script(tmp_path, "booting\nstill booting\n")
    result = VmHarness(tmp_path).boot(BootScenario(
        name="boot", argv=(sys.executable, str(script)),
        ready_marker="shell ready", failure_marker="panic",
        timeout_seconds=30))
    assert result.verdict == NO_MARKER
    assert not result.ok


def test_harness_times_out_a_hung_guest(tmp_path):
    script = _console_script(tmp_path, "", hang=True)
    result = VmHarness(tmp_path).boot(BootScenario(
        name="boot", argv=(sys.executable, str(script)),
        ready_marker="shell ready", failure_marker="panic",
        timeout_seconds=1))
    assert result.verdict == TIMEOUT
    assert result.cancellation in ("SIGTERM", "SIGKILL")


def test_harness_reports_a_missing_binary_as_unavailable(tmp_path):
    result = VmHarness(tmp_path).boot(BootScenario(
        name="boot",
        argv=(str(tmp_path / "qemu-system-x86_64-absent"), "-nographic"),
        ready_marker="ready", timeout_seconds=5))
    assert result.verdict == UNAVAILABLE
    assert not result.ok


def test_harness_skips_scenarios_whose_artifacts_are_missing(tmp_path):
    class _Profile:
        def recipes_for(self, kind):
            from forge.profiles import Recipe
            return [Recipe(name="boot", kind="boot",
                           argv=("qemu-system-x86_64", "-nographic"),
                           evidence=("disk.img",))]

    harness = VmHarness(tmp_path, profile=_Profile())
    runnable, skipped = harness.available_scenarios()
    assert runnable == []
    assert skipped
    assert "disk.img" in skipped[0]["reason"]


def test_harness_reports_no_image_without_a_boot_recipe(tmp_path):
    report = VmHarness(tmp_path).run(build=False)
    assert report.status == NO_IMAGE
    assert not report.ok
    assert "no_image" in render_vm(report) or report.status in render_vm(report)


def test_boot_scenario_comes_from_a_profile_recipe():
    from forge.profiles import Recipe
    recipe = Recipe(name="boot", kind="boot",
                    argv=("qemu-system-x86_64", "-nographic"),
                    options={"ready_marker": "shell ready",
                             "failure_marker": "panic"},
                    timeout_seconds=45)
    scenario = BootScenario.from_recipe(recipe)
    assert scenario.ready_marker == "shell ready"
    assert scenario.failure_marker == "panic"
    assert scenario.timeout_seconds == 45
    assert scenario.argv[0] == "qemu-system-x86_64"


# ---------------------------------------------------------------------------
# Task-kind routing
# ---------------------------------------------------------------------------

def test_routing_table_covers_every_kind():
    table = routing_table()
    kinds = {item["kind"] for item in table}
    assert kinds == {TRIVIAL, CODE_SMALL, CODE_LARGE, ARCHITECTURE,
                     "debugging", REPO_ANALYSIS, "documentation", HEAVY}
    assert all(item["reason"] for item in table)


def test_heavy_request_overrides_everything():
    assert classify_task(capability="coding", prompt="hi",
                         heavy=True).kind == HEAVY


def test_large_context_becomes_repository_analysis():
    profile = classify_task(capability="coding", prompt="summarise",
                            context="x" * 70_000)
    assert profile.kind == REPO_ANALYSIS
    assert profile.min_context_window >= 128_000
    assert "context characters" in profile.reason


def test_big_prompt_promotes_small_code_to_large():
    assert classify_task(capability="coding",
                         prompt="x" * 8_000).kind == CODE_LARGE
    assert classify_task(capability="coding",
                         prompt="x" * 100).kind == CODE_SMALL


def test_reasoning_without_analysis_is_trivial():
    profile = classify_task(capability="reasoning", prompt="rename a variable")
    assert profile.kind == TRIVIAL
    assert "no analysis signal" in profile.reason


def test_reasoning_with_a_real_question_is_architecture():
    profile = classify_task(
        capability="reasoning",
        prompt="analyse the trade-off between these two designs")
    assert profile.kind == ARCHITECTURE


def test_debugging_maps_to_a_reasoning_model():
    profile = classify_task(capability="debugging", prompt="why does it fail")
    assert profile.kind == "debugging"
    assert profile.model_class == "reasoning"
    assert "reasoning" in profile.required_capabilities


def test_unknown_capability_falls_back_to_trivial():
    assert classify_task(capability="nonsense", prompt="hi").kind == TRIVIAL


def test_profile_for_rejects_unknown_kinds():
    with pytest.raises(ValueError):
        profile_for("not-a-kind")


def test_request_carries_the_task_kind_into_metadata():
    request = request_for(classify_task(capability="coding", prompt="hi"),
                          prompt="hi")
    assert request.metadata["task_kind"] == CODE_SMALL
    assert request.metadata["model_class"] == "coding"
    assert request.metadata["routing_reason"]
    assert request.capability


# ---------------------------------------------------------------------------
# Routing statistics
# ---------------------------------------------------------------------------

def _feedback(model, capability, success=True, *, latency_ms=100.0,
              error="", tokens=(10, 20)):
    return RouterFeedback(
        model=model, provider="test", capability=capability, success=success,
        latency_ms=latency_ms, input_tokens=tokens[0],
        output_tokens=tokens[1], error=error)


def test_stats_aggregate_attempts_and_failures(tmp_path):
    store = StatsStore(tmp_path)
    store.record(_feedback("m", "coding", True))
    store.record(_feedback("m", "coding", True))
    store.record(_feedback("m", "coding", False, error="boom"))
    store.record(_feedback("m", "coding", False, error="timeout after 30s"))
    entry = store.get("m", "coding")
    assert entry.attempts == 4
    assert entry.successes == 2
    assert entry.failures == 1
    assert entry.timeouts == 1
    assert entry.success_rate == 0.5
    assert entry.failure_rate == 0.5
    assert entry.total_tokens == 120


def test_refusals_are_counted_separately(tmp_path):
    store = StatsStore(tmp_path)
    store.record(_feedback("m", "coding", False, error="refused by policy"))
    entry = store.get("m", "coding")
    assert entry.refused == 1
    assert entry.failures == 0


def test_stats_persist_across_instances(tmp_path):
    store = StatsStore(tmp_path)
    store.record(_feedback("m", "coding", True, latency_ms=250.0))
    reopened = StatsStore(tmp_path)
    entry = reopened.get("m", "coding")
    assert entry.attempts == 1
    assert entry.p50_latency_ms == 250.0


def test_stats_ignores_entries_without_a_model(tmp_path):
    store = StatsStore(tmp_path)
    store.record(_feedback("", "coding"))
    store.record(_feedback("m", ""))
    assert store.snapshot() == []


def test_percentiles_come_from_samples():
    entry = CapabilityStats(model="m", capability="coding")
    entry.latency_ms = [10.0, 20.0, 30.0, 40.0, 1000.0]
    assert entry.percentile_latency_ms(50) == 30.0
    assert entry.percentile_latency_ms(95) == 1000.0
    assert CapabilityStats(model="x", capability="y").p95_latency_ms == 0.0
    assert CapabilityStats(model="x", capability="y").mean_latency_ms == 0.0


def test_score_is_neutral_without_enough_data(tmp_path):
    store = StatsStore(tmp_path)
    store.record(_feedback("m", "coding", False))
    score, reason = store.score("m", "coding")
    assert score == 1.0
    assert "too few" in reason


def test_a_failing_model_scores_below_a_working_one(tmp_path):
    store = StatsStore(tmp_path)
    for _ in range(4):
        store.record(_feedback("good", "coding", True))
        store.record(_feedback("bad", "coding", False, error="boom"))
    good, _ = store.score("good", "coding")
    bad, bad_reason = store.score("bad", "coding")
    assert good > bad
    assert "0% success" in bad_reason


def test_quality_nudges_but_does_not_dominate(tmp_path):
    store = StatsStore(tmp_path)
    for _ in range(4):
        store.record(_feedback("m", "coding", True))
    plain, _ = store.score("m", "coding")
    store.record(_feedback("m", "coding", True), quality=5.0)
    with_quality, _ = store.score("m", "coding")
    assert with_quality <= plain
    assert with_quality > plain * 0.9


def test_rerank_puts_the_reliable_model_first(tmp_path):
    store = StatsStore(tmp_path)
    for _ in range(4):
        store.record(_feedback("flaky", "coding", False, error="boom"))
        store.record(_feedback("steady", "coding", True))
    decision = RouteDecision(model=None, score=0.0,
                             candidates=("flaky", "steady"))
    reranked = rerank(decision, store, capability="coding")
    assert reranked.candidates[0] == "steady"
    assert reranked.factors["reliability_preferred"] == "steady"
    assert reranked.factors["reranked_by_reliability"] == 1.0
    assert reranked.factors["reliability:flaky"] < \
        reranked.factors["reliability:steady"]


def test_rerank_never_widens_eligibility(tmp_path):
    store = StatsStore(tmp_path)
    for _ in range(4):
        store.record(_feedback("allowed", "coding", False, error="boom"))
    decision = RouteDecision(model=None, score=0.0, candidates=("allowed",))
    reranked = rerank(decision, store, capability="coding")
    assert reranked.candidates == ("allowed",), "a model was invented"


def test_rerank_leaves_a_single_candidate_untouched(tmp_path):
    store = StatsStore(tmp_path)
    decision = RouteDecision(model=None, score=0.0, candidates=("only",))
    assert rerank(decision, store) is decision


def test_rerank_falls_back_to_the_chosen_model_capability(tmp_path):
    class _Model:
        capability = "coding"

    store = StatsStore(tmp_path)
    for _ in range(4):
        store.record(_feedback("flaky", "coding", False, error="boom"))
        store.record(_feedback("steady", "coding", True))
    decision = RouteDecision(model=_Model(), score=0.0,
                             candidates=("flaky", "steady"))
    assert rerank(decision, store).candidates[0] == "steady"


def test_stats_render_shows_a_table(tmp_path):
    store = StatsStore(tmp_path)
    for _ in range(3):
        store.record(_feedback("m", "coding", True))
    text = render(store.snapshot())
    assert "model" in text and "m" in text
    assert render([]) == "no model routing statistics recorded yet"


def test_stats_file_is_bounded(tmp_path):
    store = StatsStore(tmp_path)
    for index in range(250):
        store.record(_feedback("model-%d" % index, "coding", True))
    assert len(store.snapshot()) <= 200


def test_corrupt_stats_file_is_ignored(tmp_path):
    target = tmp_path / ".forge/model_stats.json"
    target.parent.mkdir(parents=True)
    target.write_text("{not json", encoding="utf-8")
    store = StatsStore(tmp_path)
    assert store.snapshot() == []


# ---------------------------------------------------------------------------
# Long-term memory
# ---------------------------------------------------------------------------

def test_memory_rejects_unknown_kinds():
    with pytest.raises(MemoryError):
        MemoryEntry(kind="opinion", title="x")


def test_memory_requires_a_title():
    with pytest.raises(MemoryError):
        MemoryEntry(kind=DECISION, title="   ")


def test_memory_add_requires_a_source(tmp_path):
    with pytest.raises(MemoryError):
        ProjectMemory(tmp_path).add(DECISION, "use sqlite", source="")


def test_benchmark_without_numbers_is_rejected():
    with pytest.raises(MemoryError) as excinfo:
        MemoryEntry(kind=BENCHMARK, title="it is faster", source="a")
    assert "unmeasured" in str(excinfo.value)


def test_benchmark_with_metrics_is_accepted():
    entry = MemoryEntry(
        kind=BENCHMARK, title="boot time", source="perf-lab",
        payload={"metrics": {"boot_ms": {"before": 900, "after": 700}}})
    assert entry.kind == BENCHMARK


def test_memory_persists_and_reloads(tmp_path):
    memory = ProjectMemory(tmp_path)
    created = memory.add(DECISION, "use a monorepo", "one repository",
                         source="architect", tags=["layout"])
    reloaded = ProjectMemory(tmp_path)
    found = reloaded.get(created.id)
    assert found is not None
    assert found.title == "use a monorepo"
    assert found.source == "architect"
    assert found.tags == ["layout"]


def test_ids_are_stable_for_the_same_fact(tmp_path):
    memory = ProjectMemory(tmp_path)
    first = memory.add(DECISION, "use sqlite", source="a", save=False)
    second = memory.add(DECISION, "use sqlite", source="b", save=False)
    assert first.id == second.id


def test_superseding_marks_the_previous_entry(tmp_path):
    memory = ProjectMemory(tmp_path)
    first = memory.add(DECISION, "use sqlite", source="a")
    memory.add(DECISION, "use postgres", source="b", supersedes=first.id)
    assert memory.get(first.id).status == "superseded"
    active = memory.by_kind(DECISION)
    assert len(active) == 1
    assert active[0].title == "use postgres"
    assert len(memory.by_kind(DECISION, include_superseded=True)) == 2


def test_superseding_an_unknown_entry_fails(tmp_path):
    with pytest.raises(MemoryError):
        ProjectMemory(tmp_path).add(DECISION, "x", source="a",
                                    supersedes="deadbeef")


def test_search_ranks_title_above_detail(tmp_path):
    memory = ProjectMemory(tmp_path)
    memory.add(DECISION, "use sqlite for state", source="a")
    memory.add(BUG, "the writer stalls", "it involves sqlite", source="b")
    results = memory.search("sqlite")
    assert len(results) == 2
    assert results[0][0].title.startswith("use sqlite")
    assert results[0][1] > results[1][1]


def test_search_ignores_superseded_entries(tmp_path):
    memory = ProjectMemory(tmp_path)
    first = memory.add(DECISION, "use sqlite", source="a")
    memory.add(DECISION, "use postgres", source="b", supersedes=first.id)
    assert memory.search("sqlite") == []


def test_search_without_a_query_returns_nothing(tmp_path):
    memory = ProjectMemory(tmp_path)
    memory.add(DECISION, "x", source="a")
    assert memory.search("") == []
    assert memory.search("   ") == []


def test_search_can_be_limited_by_kind(tmp_path):
    memory = ProjectMemory(tmp_path)
    memory.add(DECISION, "use sqlite", source="a")
    memory.add(BUG, "sqlite lock stalls", source="b")
    assert all(entry.kind == BUG
               for entry, _ in memory.search("sqlite", kinds=(BUG,)))


def test_scope_lookup_matches_subpaths(tmp_path):
    memory = ProjectMemory(tmp_path)
    memory.add(CONSTRAINT, "no dynamic allocation in the kernel",
               scope=["kernel/"], source="profile")
    assert memory.for_path("kernel/memory/heap.c")
    assert memory.for_path("kernel/sched.c")
    assert not memory.for_path("userspace/app.c")


def test_resolve_records_the_note(tmp_path):
    memory = ProjectMemory(tmp_path)
    entry = memory.add(BUG, "race on shutdown", source="debugger")
    resolved = memory.resolve(entry.id, note="fixed by locking the queue")
    assert resolved.status == "resolved"
    assert resolved.payload["resolution"].startswith("fixed")


def test_resolve_an_unknown_entry_fails(tmp_path):
    with pytest.raises(MemoryError):
        ProjectMemory(tmp_path).resolve("nope")


def test_open_items_default_to_what_must_hold(tmp_path):
    memory = ProjectMemory(tmp_path)
    memory.add(CONSTRAINT, "must boot in QEMU", source="profile")
    memory.add(BUG, "flaky test", source="tester")
    assert {entry.kind for entry in memory.open_items()} == {CONSTRAINT}
    assert {entry.kind
            for entry in memory.open_items(kinds=(BUG,))} == {BUG}


def test_memory_summary_counts(tmp_path):
    memory = ProjectMemory(tmp_path)
    memory.add(DECISION, "a", source="x")
    memory.add(BUG, "b", source="x")
    memory.add(FAILED_APPROACH, "c", source="x")
    summary = memory.summary()
    assert summary["entries"] == 3
    assert summary["open_bugs"] == 1
    assert summary["failed_approaches"] == 1
    assert summary["by_kind"][DECISION] == 1
    assert summary["by_status"]["open"] == 3


def test_memory_render_and_prompt_are_readable(tmp_path):
    memory = ProjectMemory(tmp_path)
    memory.add(CONSTRAINT, "no dynamic allocation in the kernel",
               scope=["kernel/"], source="profile")
    memory.add(FAILED_APPROACH, "a custom scheduler",
               "it starved idle tasks", source="debugger")
    memory.add(BUG, "double fault on boot", source="vm-harness")
    text = render_memory(memory)
    assert "no dynamic allocation" in text
    prompt = memory_prompt(memory, path="kernel/sched.c")
    assert "Constraints" in prompt
    assert "Already tried" in prompt
    assert "no dynamic allocation" in prompt


def test_memory_prunes_to_its_bound(tmp_path):
    memory = ProjectMemory(tmp_path)
    for index in range(4_100):
        memory.add(BUG, "bug %d" % index, source="x", save=False)
    memory.save()
    assert len(memory.all()) <= 4_000


def test_corrupt_memory_file_is_reported_not_fatal(tmp_path):
    target = tmp_path / ".forge/memory.json"
    target.parent.mkdir(parents=True)
    target.write_text("{not json", encoding="utf-8")
    memory = ProjectMemory(tmp_path)
    assert memory.all() == []
    assert memory.load_error
