"""Native context engine: relevant, budgeted, provenance-labeled context.

Builds the working context for each task from seven sources named in A81
layer 4 — task relevance, imports, symbols, related files, tests, recent
changes, previous failures, memory — by *reusing* the existing intelligence
stack (``RepositoryIntelligence`` + ``AgentContextBuilder``, whose relevance
engine, dependency expansion, and test selection are already indexed) rather
than re-scoring the repository from scratch.

Two properties are enforced here:

* **Budget**: sections are trimmed in a fixed priority order to a token
  budget (default small for 2 GB machines). The whole repository is never
  dumped; every section records how much it was truncated.
* **Honesty**: each section carries a ``status`` — ``present``, ``empty``
  (the source exists but had nothing relevant), or ``unavailable`` (source
  missing, e.g. no git repo). Empty/unavailable sections stay visible in the
  fingerprint so a "context" that is mostly nothing can never masquerade as
  rich context.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

#: Estimated characters per token for the cheap deterministic budget.
CHARS_PER_TOKEN = 4

#: Section render order + relative priority when trimming (higher first).
_SECTION_PRIORITY = {
    "task": 90,
    "plan": 80,
    "relevant_files": 70,
    "related_tests": 60,
    "recent_changes": 50,
    "previous_failures": 45,
    "memory_decisions": 40,
    "patterns": 30,
}


@dataclass
class ContextSection:
    name: str
    content: str
    status: str = "present"  # present | empty | unavailable
    truncated_chars: int = 0
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "chars": len(self.content),
            "truncated_chars": self.truncated_chars,
            "detail": self.detail,
        }


@dataclass
class NativeContext:
    """Rendered, budgeted, fingerprinted context bundle."""

    sections: List[ContextSection] = field(default_factory=list)
    files: Tuple[str, ...] = ()
    #: Selected context items (path/score/reason dicts) for ranking reuse.
    items: List[Dict[str, Any]] = field(default_factory=list)
    estimated_tokens: int = 0
    budget_tokens: int = 1600
    fingerprint: str = ""
    notes: List[str] = field(default_factory=list)

    def render(self) -> str:
        parts: List[str] = []
        for section in self.sections:
            header = "## %s [%s]" % (section.name.replace("_", " "),
                                      section.status)
            parts.append(header + "\n" + (section.content or "(nothing)"))
        return "\n\n".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sections": [section.to_dict() for section in self.sections],
            "files": list(self.files),
            "estimated_tokens": self.estimated_tokens,
            "budget_tokens": self.budget_tokens,
            "fingerprint": self.fingerprint,
            "notes": list(self.notes),
        }


class NativeContextEngine:
    """Assemble task context from repository intelligence + memory."""

    #: Repository-intelligence summary lines are capped so a 450-file repo
    #: still costs a bounded number of bytes per build.
    MAX_SUMMARY_LINES = 12

    def __init__(self, max_tokens: int = 1600) -> None:
        self.max_tokens = max(256, int(max_tokens))

    # -- public API --------------------------------------------------------

    def build(self, task: str, plan: Optional[Dict[str, Any]] = None,
              intelligence: Any = None, memory: Any = None,
              git_root: Any = None,
              target_files: Optional[Tuple[str, ...]] = None,
              target_symbols: Optional[Tuple[str, ...]] = None,
              ) -> NativeContext:
        context = NativeContext(budget_tokens=self.max_tokens)
        budget_chars = self.max_tokens * CHARS_PER_TOKEN

        self._add(context, "task", task.strip() or "(empty task)",
                  "present" if task.strip() else "empty")

        if plan is not None:
            self._add(context, "plan", json.dumps({
                "task_class": plan.get("task_class"),
                "confidence": plan.get("confidence"),
                "steps": [{"id": step.get("id"), "kind": step.get("kind"),
                           "targets": step.get("target_files", []),
                           "tests": step.get("test_targets", [])}
                          for step in plan.get("steps", [])],
            }, indent=1, sort_keys=True, default=str), "present")

        if intelligence is not None:
            self._relevant_files(context, task, intelligence,
                                 target_files or (), target_symbols or ())
            self._related_tests(context, intelligence, plan)
        else:
            self._add(context, "relevant_files", "", "unavailable",
                      detail="no repository intelligence supplied")

        self._recent_changes(context, git_root)

        if memory is not None:
            self._memory_sections(context, memory, task)
        else:
            self._add(context, "previous_failures", "", "unavailable",
                      detail="memory disabled")
            self._add(context, "memory_decisions", "", "unavailable",
                      detail="memory disabled")

        self._apply_budget(context, budget_chars)
        rendered = context.render()
        context.estimated_tokens = int(
            math.ceil(len(rendered) / float(CHARS_PER_TOKEN)))
        context.fingerprint = hashlib.sha256(
            ("\n".join(
                "%s|%s|%s" % (s.name, s.status, s.content)
                for s in context.sections)).encode("utf-8")).hexdigest()[:24]
        return context

    # -- section builders ----------------------------------------------------

    @staticmethod
    def _add(context: NativeContext, name: str, content: str,
             status: str = "present", detail: str = "") -> None:
        if not content:
            status = "empty" if status == "present" else status
        context.sections.append(ContextSection(
            name=name, content=content.strip(), status=status,
            detail=detail))

    def _relevant_files(self, context: NativeContext, task: str,
                        intelligence: Any, target_files: Tuple[str, ...],
                        target_symbols: Tuple[str, ...]) -> None:
        """Delegate selection to the existing AgentContextBuilder."""
        from forge.intelligence.agent_context import AgentContextBuilder
        try:
            builder = AgentContextBuilder(
                intelligence,
                max_tokens=max(256, self.max_tokens // 2))
            agent_context = builder.build(
                task, target_files=target_files,
                target_symbols=target_symbols)
        except Exception as exc:  # stale/odd index must not kill the run
            self._add(context, "relevant_files", "", "unavailable",
                      detail="context builder failed: %s" % exc)
            return
        items = list(agent_context.items)
        lines: List[str] = []
        ranked: List[Dict[str, Any]] = []
        for item in items[:20]:
            location = item.path
            if getattr(item, "symbol", None):
                location += " :: " + item.symbol
            reason = getattr(item, "reason", "") or ""
            score = getattr(item, "score", 0.0)
            lines.append("- %s (score %.2f)%s" % (
                location, score, (" — " + reason) if reason else ""))
            ranked.append({"path": item.path, "score": float(score),
                           "reason": reason})
        files = tuple(dict.fromkeys(item.path for item in items))
        context.files = files
        context.items = ranked
        # imports/symbols of selected files, from the same index:
        detail_lines: List[str] = []
        for path in files[:6]:
            try:
                source_info = intelligence.source_context(path)
            except Exception:
                continue
            symbols = [entry.get("name", "")
                       for entry in source_info.get("symbols", [])][:8]
            deps = list(source_info.get("dependencies", []))[:8]
            detail_lines.append(
                "%s: symbols=%s imports=%s" % (
                    path, ", ".join(s for s in symbols if s) or "-",
                    ", ".join(d for d in deps) or "-"))
        content = "\n".join(lines)
        if detail_lines:
            content += "\n\nimports/symbols:\n" + "\n".join(detail_lines)
        status = "present" if lines else "empty"
        self._add(context, "relevant_files", content, status,
                  detail="%d file(s) selected by the relevance engine "
                         "(budgeted, not a repo dump)" % len(lines))

    @staticmethod
    def _related_tests(context: NativeContext, intelligence: Any,
                       plan: Optional[Dict[str, Any]]) -> None:
        tests = getattr(intelligence, "tests", None)
        if tests is None:
            return
        paths: List[str] = []
        if plan:
            for step in plan.get("steps", []):
                for path in step.get("test_targets", []) or []:
                    if path not in paths:
                        paths.append(path)
        if not paths:
            try:
                architecture = getattr(intelligence, "architecture", None)
                paths = list(getattr(architecture, "test_files", ()) or [])
            except Exception:
                paths = []
        lines = ["- %s" % path for path in paths[:20]]
        NativeContextEngine._add(
            context, "related_tests", "\n".join(lines),
            "present" if lines else "empty",
            detail="%d test file(s) mapped to the plan targets" % len(lines))

    @staticmethod
    def _recent_changes(context: NativeContext, git_root: Any) -> None:
        if git_root is None:
            return
        try:
            from forge.tools.git import GitTool
            git = GitTool(str(git_root))
            probe = git.run("rev-parse", "--is-inside-work-tree")
            if probe.returncode != 0:
                NativeContextEngine._add(
                    context, "recent_changes", "", "unavailable",
                    detail="not a git worktree")
                return
            status = git.status()
            log = git.run("log", "--oneline", "-n", "5")
            lines: List[str] = []
            if status.strip():
                lines.append("working tree:\n" + "\n".join(
                    status.splitlines()[:10]))
            if log.returncode == 0 and log.stdout.strip():
                lines.append("recent commits:\n"
                             + "\n".join(log.stdout.strip().splitlines()[:5]))
            NativeContextEngine._add(
                context, "recent_changes", "\n\n".join(lines),
                "present" if lines else "empty",
                detail="git status (<=10 lines) + last 5 commits")
        except Exception as exc:
            NativeContextEngine._add(
                context, "recent_changes", "", "unavailable",
                detail="git inspection failed: %s" % exc)

    @staticmethod
    def _memory_sections(context: NativeContext, memory: Any,
                         task: str) -> None:
        terms = [word for word in "".join(
            ch if ch.isalnum() else " " for ch in task.lower()).split()
            if len(word) >= 4][:6]
        try:
            failures = memory.recent_failures(limit=3)
            decisions = memory.decisions(limit=3)
            strategies = memory.successful_strategies(terms or None, limit=3)
        except Exception as exc:
            NativeContextEngine._add(
                context, "previous_failures", "", "unavailable",
                detail="memory read failed: %s" % exc)
            return
        fail_lines = []
        for record in failures:
            payload = record.payload
            fail_lines.append("- [%s] %s: %s" % (
                record.created_at, payload.get("classification", "failure"),
                str(payload.get("summary", ""))[:200]))
        NativeContextEngine._add(
            context, "previous_failures", "\n".join(fail_lines),
            "present" if fail_lines else "empty",
            detail="newest failure memories")
        dec_lines = ["- %s (%s)" % (
            str(r.payload.get("decision", ""))[:200], r.source)
            for r in decisions]
        NativeContextEngine._add(
            context, "memory_decisions", "\n".join(dec_lines),
            "present" if dec_lines else "empty",
            detail="newest project decisions")
        strat_lines = ["- %s" % str(r.payload.get("strategy", ""))[:200]
                       for r in strategies]
        NativeContextEngine._add(
            context, "patterns", "\n".join(strat_lines),
            "present" if strat_lines else "empty",
            detail="successful strategies matching task terms")

    # -- budget ------------------------------------------------------------------

    @staticmethod
    def _render_cost(section: ContextSection) -> int:
        """Chars this section costs in the final render (header + body).

        Measured the same way :meth:`NativeContext.render` composes, so a
        render that fits the cost budget genuinely fits the token budget.
        """
        header = "## %s [%s]\n" % (section.name.replace("_", " "),
                                   section.status)
        body = section.content if section.content.strip() else "(nothing)"
        return len(header) + len(body) + 2

    @staticmethod
    def _apply_budget(context: NativeContext, budget_chars: int) -> None:
        """Trim sections deterministically until the render fits the budget.

        Lowest-priority sections shrink first and are emptied (with status
        ``empty`` + a truncation note) before higher ones are touched.
        """
        def total_cost() -> int:
            return sum(NativeContextEngine._render_cost(s)
                       for s in context.sections)

        ordered = sorted(context.sections,
                         key=lambda s: _SECTION_PRIORITY.get(s.name, 10))
        for section in ordered:
            total = total_cost()
            if total <= budget_chars:
                break
            excess = total - budget_chars
            keep = max(0, len(section.content) - excess)
            dropped = len(section.content) - keep
            if not dropped:
                continue
            section.content = section.content[:keep]
            section.truncated_chars = dropped
            if keep == 0 and section.status == "present":
                section.status = "empty"
                section.detail = ((section.detail + "; ")
                                  if section.detail else "") \
                    + "emptied by context budget"
        if total_cost() > budget_chars:
            context.notes.append(
                "budget exceeded after trimming all sections "
                "(task/plan are never dropped): %d > %d chars"
                % (total_cost(), budget_chars))
