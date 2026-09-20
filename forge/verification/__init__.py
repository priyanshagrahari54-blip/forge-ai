"""Independent verification plane (A84 Stages J/K cross-checks).

Important outputs should pass through verification that is *not* the producer:

* :class:`forge.verification.critic.Critic` — reviews a produced answer for
  requirement coverage, contradictions with retrieved memory, hallucination
  indicators (unverifiable file paths, invented citations, "verified"-style
  language without evidence) and, for code, correctness signals collected
  from the existing deterministic gates (compile + security scan + review).
  An optional model critic (fabric ``review`` capability) *contributes*
  findings; the deterministic gate remains mandatory — a model can add a
  finding but never remove one.
* :class:`forge.verification.verifier.EvidenceVerifier` — traces claims back
  to evidence: citations must exist in the provided evidence set, local paths
  must exist on disk, and single-source claims stay labeled single-source.
* :class:`forge.verification.loop.CritiqueLoop` — the bounded
  ``generate -> critique -> revise -> verify`` cycle for high-impact tasks,
  with the existing acceptance-gate vocabulary (APPROVE / REVISE / BLOCK).

Verification never *rewrites* the artifact and never grants authority: it
produces findings and verdicts the caller must honour. A CRITICAL finding
cannot be voted away.
"""
from forge.verification.critic import Critic, CritiqueReport, CritiqueVerdict
from forge.verification.verifier import EvidenceVerifier, VerificationFinding
from forge.verification.loop import CritiqueLoop, LoopOutcome

__all__ = ["Critic", "CritiqueLoop", "CritiqueReport", "CritiqueVerdict",
           "EvidenceVerifier", "LoopOutcome", "VerificationFinding"]
