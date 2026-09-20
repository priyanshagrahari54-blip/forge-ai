"""Prompt Intelligence Layer (A84 Stage D).

Forge users should not need to write perfect prompts. This package inserts a
deterministic enhancement plane between the raw user request and model
execution:

```
raw request -> intent extraction -> ambiguity detection -> goal id
-> constraint extraction -> (context retrieval hook) -> task decomposition
-> required capability detection -> output-format inference
-> enhanced prompt -> model-specific adaptation -> execution
```

Guarantees, enforced in code:

* **Intent is preserved, never rewritten** (D1): the enhancement output
  carries the extracted goal + constraint tokens and an
  ``intent_preserved`` check — the enhanced prompt must retain every
  preserved token of the original objective. An enhancement that drops a
  user requirement is refused, not silently applied.
* **Adaptation is structure, not objective** (D2): per-model prompt shaping
  changes ordering, explicitness and verification scaffolding only. The
  goal text is copied verbatim into every adaptation.
* **Important ambiguity asks, never guesses** (D3): the quality evaluator
  returns ``ASK_USER`` with concrete questions when a blocking ambiguity is
  detected; low-severity gaps are annotated, not interrogated.
* **Versioning without leakage** (D4): the ledger stores original, enhanced
  and model-targeted prompts plus outcome metrics under bounded size — and
  explicitly does not store system instructions or private reasoning traces.

Everything here is stdlib-only, deterministic and Python 3.8 compatible. No
component of this layer calls a model on its own; execution belongs to the
Model Fabric behind the existing permission gates.
"""
from forge.prompt_intelligence.pipeline import (
    EnhancedPrompt,
    PromptIntelligence,
    decompose_task,
    detect_output_format,
    extract_constraints,
    extract_goals,
    extract_intent,
    required_capabilities,
)
from forge.prompt_intelligence.quality import (
    PromptQualityEvaluator,
    QualityFlags,
    QualityVerdict,
)
from forge.prompt_intelligence.adaptation import ModelPromptAdapter
from forge.prompt_intelligence.versions import PromptLedger, PromptVersion
from forge.prompt_intelligence.strategies import PromptStrategyLedger

__all__ = [
    "EnhancedPrompt",
    "ModelPromptAdapter",
    "PromptIntelligence",
    "PromptLedger",
    "PromptQualityEvaluator",
    "PromptStrategyLedger",
    "PromptVersion",
    "QualityFlags",
    "QualityVerdict",
    "decompose_task",
    "detect_output_format",
    "extract_constraints",
    "extract_goals",
    "extract_intent",
    "required_capabilities",
]
