"""Planner and context-engine integration for the secure research engine.

Two opt-in hooks; nothing changes for callers that do not use them:

* :class:`ResearchAwarePlanner` wraps :class:`forge.core.planner.Planner`.
  It runs a *local-only* (or web-enabled when asked) research pass over
  the requirement and inserts a "Research" step carrying the top cited
  findings, keeping the canonical five-step shape intact afterwards.
* :func:`research_context_items` / :func:`enrich_context_with_research`
  turn ``LOCAL_SOURCE`` results (repo-relative paths + lines) into
  :class:`ContextItem`s the context engine already understands. Only
  local, verified evidence is ever added to an agent's file context;
  web/model/user results are exposed separately as ``external_notes``
  so a prompt can include them as clearly labeled untrusted text.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from forge.core.planner import Planner, PlanStep
from forge.intelligence.context import ContextItem, ContextPack
from forge.research.provenance import Provenance
from forge.research.secure_engine import SecureResearchEngine


def research_context_items(report: Dict[str, Any], root: str | Path,
                           *, limit: int = 8) -> List[ContextItem]:
    """Convert local research results into context items (repo files only)."""
    root_path = Path(root).resolve()
    items: List[ContextItem] = []
    seen: set = set()
    for result in report.get("results", []):
        if result.get("provenance") != Provenance.LOCAL_SOURCE.value:
            continue
        locator = str(result.get("citation", {}).get("locator", ""))
        if not locator or locator.startswith((".git/", "/", "\\")) or ".." in locator:
            continue
        candidate = root_path / locator
        try:
            candidate.resolve().relative_to(root_path)
        except ValueError:
            continue
        if not candidate.is_file() or locator in seen:
            continue
        seen.add(locator)
        line = result.get("citation", {}).get("line")
        items.append(ContextItem(
            path=locator,
            kind="research",
            symbol=result.get("title") if result.get("kind") == "symbol" else None,
            reason=f"research:{result.get('source')} "
                   f"({', '.join(result.get('matched_terms', [])[:3])})",
            score=float(result.get("metadata", {}).get("rank_score", result.get("score", 0.0))),
            start_line=line, end_line=line,
        ))
        if len(items) >= limit:
            break
    return items


def external_notes(report: Dict[str, Any], *, limit: int = 6) -> List[Dict[str, Any]]:
    """Non-local results as labeled, untrusted notes (never file context)."""
    notes: List[Dict[str, Any]] = []
    for result in report.get("results", []):
        if result.get("provenance") == Provenance.LOCAL_SOURCE.value:
            continue
        notes.append({
            "provenance": result.get("provenance"),
            "untrusted": True,
            "cite": result.get("cite"),
            "title": result.get("title"),
            "snippet": result.get("snippet", "")[:500],
        })
        if len(notes) >= limit:
            break
    return notes


def enrich_context_with_research(pack: ContextPack, report: Dict[str, Any],
                                 root: str | Path, *, limit: int = 8) -> int:
    """Add local research evidence to an existing pack. Returns items added."""
    before = len(pack.items)
    for item in research_context_items(report, root, limit=limit):
        pack.add(item)
    return len(pack.items) - before


class ResearchAwarePlanner(Planner):
    """Planner that grounds the plan in cited research findings."""

    def __init__(self, engine: Optional[SecureResearchEngine] = None, *,
                 root: str | Path = ".", allow_web: bool = False,
                 max_findings: int = 4) -> None:
        super().__init__()
        self._engine = engine
        self.root = Path(root).resolve()
        self.allow_web = allow_web
        self.max_findings = max_findings
        self.last_report: Optional[Dict[str, Any]] = None

    @property
    def engine(self) -> SecureResearchEngine:
        if self._engine is None:
            self._engine = SecureResearchEngine(self.root)
        return self._engine

    def create_plan(self, request: str) -> list[PlanStep]:
        base = super().create_plan(request)
        try:
            report = self.engine.research(request, allow_web=self.allow_web)
        except Exception as exc:  # research must never break planning
            report = {"results": [], "web_failed": False,
                      "answer": f"research unavailable: {type(exc).__name__}"}
        self.last_report = report
        findings = report.get("results", [])[: self.max_findings]
        if findings:
            cited = "; ".join(
                f"{r['cite']} ({r['provenance']})" for r in findings)
            description = f"Research the requirement using cited evidence: {cited}"
        else:
            description = ("Research the requirement: no supporting evidence was "
                           "found in the consulted sources.")
        if report.get("web_failed"):
            description += (" Web research failed and was not replaced by model "
                            "knowledge.")
        research_step = PlanStep(id="1a", description=description,
                                 depends_on=("1",))
        steps: list[PlanStep] = []
        for step in base:
            steps.append(step)
            if step.id == "1":
                steps.append(research_step)
            elif step.id == "2":
                steps[-1] = PlanStep(id=step.id, description=step.description,
                                     depends_on=("1a",))
        return steps

    def context_items(self, limit: int = 8) -> List[ContextItem]:
        if not self.last_report:
            return []
        return research_context_items(self.last_report, self.root, limit=limit)
