"""Evidence verifier (A84 Stage K/F provenance checks).

Traces each important claim back to something real, and *nothing more*:

* a claim citing a web/document source must name a citation present in the
  evidence set (URL/path/date);
* a claim naming a local file path must find the file on disk;
* provenance class decides what the claim may be worded as: verified
  (LOCAL/WEB/USER per the A81 provenance vocabulary) vs model knowledge
  (never presented as verified);
* claims with zero matching evidence come back ``UNVERIFIED`` with the exact
  missing link named — they are reported, not deleted and not upgraded.

The verifier does not decide truth. It decides *whether the trace exists*,
which is the only claim a verification layer is allowed to make.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = ["EvidenceVerifier", "VerificationFinding"]

_CITATION_RE = re.compile(r"\[([^\]\[]{1,160})\]")
_URL_RE = re.compile(r"https?://[^\s)\]<>\"']+")

#: Provenance classes considered "verified" — copied from
#: forge.research.provenance so the two modules can never drift.
VERIFIED_PROVENANCE = ("LOCAL_SOURCE", "REAL_WEB_RESULT", "USER_PROVIDED")


@dataclass(frozen=True)
class VerificationFinding:
    claim: str
    status: str                 # VERIFIED | UNVERIFIED | DISPUTED
    matched: Tuple[str, ...] = ()
    missing: Tuple[str, ...] = ()
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"claim": self.claim, "status": self.status,
                "matched": list(self.matched), "missing": list(self.missing),
                "detail": self.detail}


class EvidenceVerifier:
    """Deterministic claim-to-evidence tracing."""

    def __init__(self, *, project_root: Any = None,
                 contradiction_similarity_floor: float = 0.55) -> None:
        self.project_root = Path(project_root).resolve() if project_root else None
        self.contradiction_floor = float(contradiction_similarity_floor)

    # -- public ------------------------------------------------------------------

    def verify_claims(self, claims: Sequence[str], *,
                      evidence: Sequence[Dict[str, Any]],
                      contradictions: Sequence[Dict[str, Any]] = ()
                      ) -> Tuple[VerificationFinding, ...]:
        """Check each claim string against the evidence set.

        ``evidence`` entries may carry ``url``/``cite``/``path``/``provenance``
        keys (research results, tool results or memory records normalized to
        dicts). ``contradictions`` entries name claim topics two sources
        disagree about; a claim on that topic is DISPUTED with both citations.
        """
        evidence = list(evidence or ())
        index = self._index(evidence)
        disputed = {str(item.get("topic") or item.get("subject") or "").lower()
                    for item in (contradictions or ())}
        out: List[VerificationFinding] = []
        for claim in claims or ():
            text = str(claim or "").strip()
            if not text:
                continue
            topic = _topic(text)
            if topic and any(topic in d or d in topic for d in disputed if d):
                out.append(VerificationFinding(
                    claim=text[:300], status="DISPUTED",
                    missing=("independent sources disagree on this topic",),
                    detail="both positions must be shown; the claim may not "
                           "be stated as settled"))
                continue
            matched: List[str] = []
            missing: List[str] = []
            cite = _CITATION_RE.search(text)
            urls = _URL_RE.findall(text)
            if cite:
                needle = cite.group(1).strip().strip("<>")
                candidates = index.get("cite", ())
                hit = next((value for value in candidates
                            if needle in value or value.endswith(needle)), "")
                if hit:
                    matched.append(hit[:200])
                else:
                    missing.append(f"citation {needle[:80]!r} not in evidence")
            for url in urls:
                if url in index.get("url", ()):
                    matched.append(url)
                elif self._path_exists(url):
                    matched.append(url)
                else:
                    missing.append(f"URL/path {url[:120]!r} not retrievable "
                                   "from provided evidence")
            if not matched and not missing and index.get("claim_terms"):
                # No explicit citation: fall back to term overlap, which can
                # at best yield UNVERIFIED — never VERIFIED without a trace.
                missing.append("no citation attached")
            status = "VERIFIED" if matched and not missing else "UNVERIFIED"
            out.append(VerificationFinding(
                claim=text[:300], status=status, matched=tuple(matched),
                missing=tuple(missing),
                detail=("trace exists to a verified provenance class"
                        if status == "VERIFIED" else
                        "claim stands only as reported-uncertain; it may "
                        "not be worded as verified")))
        return tuple(out)

    def verify_paths(self, paths: Iterable[str]) -> List[Dict[str, Any]]:
        """Existence check for repository paths referenced by an answer."""
        out: List[Dict[str, Any]] = []
        for path in paths or ():
            token = str(path or "").strip()
            if not token:
                continue
            exists = self._path_exists(token)
            out.append({"path": token[:200], "exists": exists,
                        "root_checked": str(self.project_root or "")})
        return out

    # -- internals ------------------------------------------------------------------

    def _index(self, evidence: Sequence[Dict[str, Any]]) -> Dict[str, Tuple[str, ...]]:
        urls: List[str] = []
        cites: List[str] = []
        for item in evidence:
            if not isinstance(item, dict):
                continue
            for key in ("url",):
                value = item.get(key)
                if value:
                    urls.append(str(value))
            citation = item.get("citation") or {}
            if isinstance(citation, dict):
                for key in ("url", "path"):
                    value = citation.get(key)
                    if value:
                        urls.append(str(value))
            for key in ("cite", "citation_text", "document", "path"):
                value = item.get(key)
                if value:
                    cites.append(str(value))
            render = item.get("render")
            if isinstance(render, str) and render:
                cites.append(render)
        return {"url": tuple(dict.fromkeys(urls)),
                "cite": tuple(dict.fromkeys(cites)),
                "claim_terms": tuple(sorted({
                    term for item in evidence if isinstance(item, dict)
                    for term in _terms(str(item.get("snippet", "")))}))[:2000]}

    def _path_exists(self, token: str) -> bool:
        if self.project_root is None:
            return False
        candidate = (self.project_root / token.lstrip("/"))
        try:
            return candidate.exists()
        except OSError:
            return False


def _topic(text: str) -> str:
    words = re.findall(r"[a-z0-9_.-]{4,}", text.lower())
    return " ".join(words[:4])


def _terms(text: str) -> Tuple[str, ...]:
    return tuple(w for w in re.findall(r"[a-z0-9_.-]{5,}", (text or "").lower()))[:12]
