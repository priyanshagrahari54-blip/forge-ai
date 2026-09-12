"""Forge research package.

- :mod:`forge.research.engine` — A47 evidence-based repository Q&A.
- :mod:`forge.research.web` — legacy provider search (SearXNG/OpenAI).
- :mod:`forge.research.secure_engine` — secure multi-source research
  engine with provenance, planning, ranking, dedup, summarization,
  cache, and SSRF-guarded web access.
"""
from forge.research.provenance import (  # noqa: F401
    Citation, Provenance, ResearchResult, SourceOutcome,
)
from forge.research.secure_engine import SecureResearchEngine  # noqa: F401

__all__ = ["Citation", "Provenance", "ResearchResult", "SourceOutcome",
           "SecureResearchEngine"]
