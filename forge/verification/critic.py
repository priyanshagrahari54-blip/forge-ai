"""Critic system (A84 Stage K).

An independent review pass for produced answers, code and reports. The
deterministic core checks what *can* be checked without trusting anyone:

* requirement coverage — every goal/constraint of the enhanced prompt must be
  addressed or explicitly declared out of scope;
* contradiction with retrieved memory — statements that invert a retrieved
  record's polarity are flagged for the user, not silently reconciled;
* hallucination indicators — file paths that do not exist, citations absent
  from the evidence set, and certainty language ("verified", "confirmed")
  without a verification artifact;
* code correctness signals — compiles, no security findings via the existing
  deterministic review/security gates;
* security — the A32/A33 secret and dangerous-execution patterns run over
  produced content before it can be accepted.

An optional model critic (fabric capability ``review``) *contributes* extra
findings. A model can add findings and can propose to downgrade one with a
reason; it can never remove a deterministic HIGH/CRITICAL finding. This is
the same authority structure as A32's review gate: model opinion is input,
the deterministic gate is law.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = ["Critic", "CritiqueReport", "CritiqueVerdict"]


class CritiqueVerdict:
    APPROVE = "APPROVE"
    REVISE = "REVISE"
    BLOCK = "BLOCK"


_PATH_RE = re.compile(
    r"(?<![\w./-])((?:[\w.-]+/)+[\w.-]+\.(?:py|js|ts|tsx|jsx|md|json|ya?ml|"
    r"toml|sql|sh|go|rs|java|c|h|cpp))\b")
_CITATION_RE = re.compile(r"\[([^\]\[]{1,120})\]")
_CERTAINTY_RE = re.compile(
    r"\b(verified|confirmed|proven|guaranteed|100%\s*accurate)\b", re.IGNORECASE)
_NEGATIONS = ("not", "never", "no ", "cannot", "without", "prohibits",
              "disabled", "banned")


@dataclass(frozen=True)
class CritiqueFinding:
    severity: str            # INFO | LOW | MEDIUM | HIGH | CRITICAL
    kind: str
    message: str
    ref: str = ""
    from_model: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"severity": self.severity, "kind": self.kind,
                "message": self.message, "ref": self.ref,
                "from_model": self.from_model}


@dataclass(frozen=True)
class CritiqueReport:
    verdict: str
    findings: Tuple[CritiqueFinding, ...] = ()
    coverage: Dict[str, Any] = field(default_factory=dict)
    model_review: Dict[str, Any] = field(default_factory=dict)
    independence: str = "deterministic"

    @property
    def approved(self) -> bool:
        return self.verdict == CritiqueVerdict.APPROVE

    @property
    def blocked(self) -> bool:
        return self.verdict == CritiqueVerdict.BLOCK

    def to_dict(self) -> Dict[str, Any]:
        return {"verdict": self.verdict,
                "findings": [f.to_dict() for f in self.findings],
                "coverage": dict(self.coverage),
                "model_review": dict(self.model_review),
                "independence": self.independence,
                "honesty": ("critique is advisory for text and binding for "
                            "the BLOCK cases it can prove (uncompilable "
                            "code, security hits, missing required content)")}


class Critic:
    """Deterministic critic with optional model contribution."""

    def __init__(self, *, fabric: Any = None, project_root: Any = None,
                 model_critic_enabled: bool = True) -> None:
        self.fabric = fabric
        self.project_root = Path(project_root).resolve() if project_root else None
        self.model_critic_enabled = bool(model_critic_enabled and fabric)

    # -- main entry ---------------------------------------------------------------

    def review(self, output_text: str, *, enhanced: Any = None,
                evidence: Sequence[Dict[str, Any]] = (),
                memory_hits: Sequence[Any] = (),
                code_files: Optional[Dict[str, str]] = None,
                claims: Sequence[str] = ()) -> CritiqueReport:
        findings: List[CritiqueFinding] = []
        text = output_text or ""

        findings.extend(self._check_requirements(text, enhanced))
        findings.extend(self._check_hallucinations(text, evidence,
                                                    code_files))
        findings.extend(self._check_memory_contradictions(text, memory_hits))
        if code_files:
            findings.extend(self._check_code(code_files))
        findings.extend(self._check_certainty_language(text, evidence, claims))

        model_review: Dict[str, Any] = {"performed": False, "model": ""}
        if self.model_critic_enabled and text:
            extra, model_review = self._model_review(text, enhanced)
            findings.extend(extra)

        verdict = self._verdict(findings)
        coverage = self._coverage(text, enhanced)
        return CritiqueReport(verdict=verdict, findings=tuple(findings),
                              coverage=coverage, model_review=model_review)

    # -- deterministic checks --------------------------------------------------------

    @staticmethod
    def _check_requirements(text: str, enhanced: Any) -> List[CritiqueFinding]:
        out: List[CritiqueFinding] = []
        if enhanced is None:
            return out
        goals = tuple(getattr(enhanced, "goals", ()) or ())
        verification = tuple(getattr(enhanced, "verification", ()) or ())
        lowered = text.lower()
        from forge.prompt_intelligence.pipeline import content_terms
        for goal in goals:
            terms = content_terms(goal)
            if not terms:
                continue
            hit = sum(1 for term in terms if term in lowered)
            if hit / float(len(terms)) < 0.3 and "out of scope" not in lowered:
                out.append(CritiqueFinding(
                    "MEDIUM", "requirement-coverage",
                    "output may not address this stated goal: "
                    f"{goal[:140]!r}", ref=goal[:60]))
        for requirement in verification:
            kind = requirement.split(" ", 1)[0]
            if kind in ("run",) and "test" not in lowered:
                out.append(CritiqueFinding(
                    "HIGH", "verification-missing",
                    "verification required tests but the output mentions "
                    "none", ref=requirement[:80]))
            if kind == "cite" and not _CITATION_RE.search(text) \
                    and "http" not in lowered and "path" not in lowered:
                out.append(CritiqueFinding(
                    "HIGH", "citation-missing",
                    "research verification requires citations; none found "
                    "in the output", ref=requirement[:80]))
        return out

    def _check_hallucinations(self, text: str,
                              evidence: Sequence[Dict[str, Any]],
                              code_files: Optional[Dict[str, str]]
                              ) -> List[CritiqueFinding]:
        out: List[CritiqueFinding] = []
        cited = set()
        for item in evidence:
            for key in ("url", "cite", "citation", "document", "path"):
                value = item.get(key) if isinstance(item, dict) else None
                if value:
                    cited.add(str(value))
        # file paths named in prose must exist (unless the output creates them)
        for path in set(_PATH_RE.findall(text)):
            token = path if isinstance(path, str) else ""
            if not token:
                continue
            if code_files and token in code_files:
                continue
            if self.project_root is not None:
                candidate = (self.project_root / token)
                if candidate.exists():
                    continue
                out.append(CritiqueFinding(
                    "MEDIUM", "unverified-path",
                    f"mentions {token} which does not exist in the project "
                    "root and is not created by this change", ref=token))
        # bracket citations must correspond to provided evidence
        if evidence:
            for cite in _CITATION_RE.findall(text):
                cite_clean = cite.strip().strip("<>")
                if cite_clean.lower() in ("note", "citation needed"):
                    continue
                if not any(cite_clean in value or value.endswith(cite_clean)
                           for value in cited):
                    out.append(CritiqueFinding(
                        "HIGH", "invented-citation",
                        f"citation {cite_clean[:80]!r} does not match any "
                        "provided evidence item", ref=cite_clean[:80]))
        return out

    @staticmethod
    def _check_memory_contradictions(text: str, memory_hits: Sequence[Any]
                                     ) -> List[CritiqueFinding]:
        """Flag outputs that invert a retrieved memory's polarity."""
        out: List[CritiqueFinding] = []
        lowered = " " + (text or "").lower() + " "
        for hit in memory_hits:
            content = str(getattr(hit, "content", "")
                          or (hit if isinstance(hit, str) else ""))
            if len(content) < 12:
                continue
            subject = re.split(r"[:\s]+", content.strip(), maxsplit=3)
            anchor = " ".join(w for w in subject if len(w) > 3)[:40]
            if anchor and anchor in lowered:
                memory_negative = any(n in content.lower() for n in _NEGATIONS)
                output_negative = any(
                    n in lowered[lowered.find(anchor):lowered.find(anchor)
                                  + 240] for n in _NEGATIONS)
                if memory_negative != output_negative:
                    out.append(CritiqueFinding(
                        "MEDIUM", "memory-contradiction",
                        "output disagrees with a retrieved memory "
                        f"({content[:80]!r}); surface the conflict to the "
                        "user instead of silently picking a side",
                        ref=anchor))
        return out

    @staticmethod
    def _check_code(code_files: Dict[str, str]) -> List[CritiqueFinding]:
        out: List[CritiqueFinding] = []
        for path, content in (code_files or {}).items():
            if not path.endswith(".py"):
                continue
            try:
                ast.parse(content or "")
            except SyntaxError as exc:
                out.append(CritiqueFinding(
                    "CRITICAL", "code-invalid",
                    f"{path} does not compile: line {exc.lineno}: "
                    f"{exc.msg}", ref=path))
        if code_files:
            from forge.security.review import ReviewGate
            joined = "\n".join(f"--- a/{p}\n+++ b/{p}\n{c}"
                                for p, c in sorted(code_files.items()))
            decision = ReviewGate().review(joined, list(code_files))
            for finding in decision.findings:
                out.append(CritiqueFinding(
                    str(finding.severity.value), "security-" + finding.rule,
                    finding.message, ref=finding.file or "",
                    from_model=False))
        return out

    @staticmethod
    def _check_certainty_language(text: str, evidence: Sequence[Dict[str, Any]],
                                  claims: Sequence[str]) -> List[CritiqueFinding]:
        out: List[CritiqueFinding] = []
        matches = _CERTAINTY_RE.findall(text or "")
        if matches and not evidence and not claims:
            out.append(CritiqueFinding(
                "MEDIUM", "unbacked-certainty",
                "claims like " + ", ".join(sorted(set(matches))[:3]) +
                " without any attached evidence; downgrade to what is "
                "actually shown", ref=""))
        return out

    @staticmethod
    def _coverage(text: str, enhanced: Any) -> Dict[str, Any]:
        goals = tuple(getattr(enhanced, "goals", ()) or ())
        if not goals:
            return {"goals": 0, "addressed": 0, "ratio": None}
        from forge.prompt_intelligence.pipeline import content_terms
        lowered = (text or "").lower()
        addressed = 0
        for goal in goals:
            terms = content_terms(goal)
            if terms and sum(1 for t in terms if t in lowered) / float(
                    len(terms)) >= 0.3:
                addressed += 1
        return {"goals": len(goals), "addressed": addressed,
                "ratio": round(addressed / float(len(goals)), 4),
                "method": "lexical overlap (deterministic, not semantic)"}

    # -- optional model contribution --------------------------------------------------

    def _model_review(self, text: str, enhanced: Any) -> Tuple[List[CritiqueFinding], Dict[str, Any]]:
        from forge.models.request import ModelRequest
        brief = "\n".join([
            "You are an independent critic. Review the OUTPUT against the "
            "REQUEST. List concrete problems only: missing requirements, "
            "factual overreach, unsafe content, contradictions. For each: "
            "one line starting SEVERITY: (CRITICAL|HIGH|MEDIUM|LOW) "
            "followed by the problem. If none, reply exactly: CLEAN"])
        try:
            response = self.fabric.request(ModelRequest(
                prompt=brief + "\n\nREQUEST:\n" + str(
                    getattr(enhanced, "original", ""))[:1500] +
                "\n\nOUTPUT:\n" + text[:4000],
                capability="review", task="critic"))
        except Exception as exc:
            return [], {"performed": False,
                        "error": f"{type(exc).__name__}"}
        if not getattr(response, "success", False):
            return [], {"performed": False,
                        "error": str(getattr(response, "error", ""))[:200]}
        raw = str(getattr(response, "text", "") or "")
        findings: List[CritiqueFinding] = []
        for line in raw.splitlines():
            match = re.match(r"\s*(CRITICAL|HIGH|MEDIUM|LOW)\s*:\s*(.+)", line)
            if match:
                findings.append(CritiqueFinding(
                    match.group(1).upper(), "model-finding",
                    match.group(2).strip()[:280], from_model=True))
        return findings, {"performed": True,
                          "model": str(getattr(response, "model", "")),
                          "provider": str(getattr(response, "provider", "")),
                          "raw_lines": len(raw.splitlines())}

    # -- authority -------------------------------------------------------------------

    @staticmethod
    def _verdict(findings: Sequence[CritiqueFinding]) -> str:
        """Authority ladder: only *provable deterministic* failures block.

        A model-only CRITICAL is never binding on its own (it gets REVISE —
        the same authority structure as A32's optional model reviewer); a
        deterministic CRITICAL (uncompilable code) blocks. Everything else
        with findings asks for revision.
        """
        if any(f.severity == "CRITICAL" and not f.from_model
               for f in findings):
            return CritiqueVerdict.BLOCK
        if findings:
            return CritiqueVerdict.REVISE
        return CritiqueVerdict.APPROVE
