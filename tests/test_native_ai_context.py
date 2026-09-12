"""Context engine: relevant, budgeted, memory-aware — never a repo dump."""
from __future__ import annotations

from helpers_native_ai import write_repo

from forge.intelligence.repository import RepositoryIntelligence
from forge.native.context import NativeContextEngine
from forge.native.engine import NativeAIEngine
from forge.native.memory import NativeMemory


def build(tmp_path, task="fix calc.py add", memory=None, engine=None,
          max_tokens=1600):
    write_repo(tmp_path)
    intelligence = RepositoryIntelligence.build(tmp_path)
    context = NativeContextEngine(max_tokens=max_tokens).build(
        task, None, intelligence, memory, git_root=tmp_path)
    return context


def test_sections_cover_the_required_sources(tmp_path):
    context = build(tmp_path)
    names = {section.name for section in context.sections}
    assert {"task", "relevant_files", "related_tests", "recent_changes",
            "previous_failures", "memory_decisions"} <= names


def test_relevant_files_use_the_existing_relevance_engine(tmp_path):
    context = build(tmp_path, task="fix the add function in calc.py")
    section = [s for s in context.sections
               if s.name == "relevant_files"][0]
    assert section.status == "present"
    assert "calc.py" in section.content
    assert "repo dump" not in section.content.lower()
    assert len(context.files) >= 1


def test_symbols_and_imports_are_surfaced(tmp_path):
    context = build(tmp_path, task="explain calc.py add")
    section = [s for s in context.sections
               if s.name == "relevant_files"][0]
    assert "symbols=" in section.content  # from the existing index


def test_budget_is_enforced_and_truncation_is_labeled(tmp_path):
    huge = NativeContextEngine(max_tokens=256)
    write_repo(tmp_path, extra={
        "big_%02d.py" % i: "x%d = [%s]\n" % (i, ",".join(
            str(j) for j in range(400))) for i in range(12)})
    intelligence = RepositoryIntelligence.build(tmp_path)
    fat_plan = {
        "task_class": "refactor",
        "confidence": "high",
        "steps": [{"id": "s%d" % i, "kind": "edit",
                   "targets": ["big_%02d.py" % i], "tests": []}
                  for i in range(40)],
    }
    context = huge.build("refactor big files across the repository",
                         fat_plan, intelligence, None, git_root=tmp_path)
    assert context.estimated_tokens <= 256
    truncated = [s for s in context.sections if s.truncated_chars > 0]
    assert truncated, "budget must actually bite, with recorded truncation"
    assert all(s.truncated_chars for s in truncated)
    assert context.estimated_tokens <= 256


def test_recent_changes_section_handles_non_git(tmp_path):
    # tmp_path is not a git repo: the section must say "unavailable",
    # never silently pretend there is nothing to report.
    write_repo(tmp_path)
    context = NativeContextEngine().build("t", None,
                                         RepositoryIntelligence.build(
                                             tmp_path),
                                         None, git_root=tmp_path)
    section = [s for s in context.sections if s.name == "recent_changes"][0]
    assert section.status == "unavailable"
    assert "git" in section.detail


def test_memory_and_previous_failures_flow_into_context(tmp_path):
    write_repo(tmp_path)
    memory = NativeMemory(tmp_path)
    memory.record_failure("fix calc.py add", "assertion",
                          "assert 1 == 2 failed in test_calc.py")
    context = build(tmp_path, memory=memory)
    section = [s for s in context.sections
               if s.name == "previous_failures"][0]
    assert section.status == "present"
    assert "assertion" in section.content


def test_fingerprint_is_stable_and_content_sensitive(tmp_path):
    first = build(tmp_path)
    second = build(tmp_path)
    assert first.fingerprint == second.fingerprint
    other = build(tmp_path, task="refactor calc.py add")
    assert other.fingerprint != first.fingerprint


def test_missing_intelligence_is_reported_as_unavailable(tmp_path):
    context = NativeContextEngine().build("some task", None, None, None)
    section = [s for s in context.sections
               if s.name == "relevant_files"][0]
    assert section.status == "unavailable"


def test_engine_run_attaches_context_to_report(tmp_path):
    write_repo(tmp_path)
    engine = NativeAIEngine(tmp_path, persist_status=False)
    result = engine.run("analyze the repository")
    context = result.report.context
    assert context["fingerprint"]
    assert context["estimated_tokens"] <= engine.context_engine.max_tokens
    statuses = {s["status"] for s in context["sections"]}
    assert statuses <= {"present", "empty", "unavailable"}
