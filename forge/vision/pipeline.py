"""Vision pipeline (A39): image → understanding → safe proposals.

The screenshot-to-action chain is strictly proposal-only here:

```text
screenshot
→ VisionProvider.analyze (bounded, untrusted input)
→ structured understanding (elements, text, errors, dangers)
→ proposed actions (each policy-evaluated, NONE executed)
```

Execution of any proposed action happens only through the existing
browser/desktop bridges under their own A33 gates — vision can never
grant permissions, and an image saying "delete everything" is reported
as an untrusted dangerous instruction, never as an authorization.
"""
from __future__ import annotations

from typing import Any

from forge.vision.base import VisionProvider, VisionResult
from forge.vision.simulated import SimulatedVisionProvider
from forge.vision.base import UnconfiguredVisionProvider

#: Provider names the control plane accepts in A39. Real providers
#: register behind the same VisionProvider protocol as plugins.
AVAILABLE_PROVIDERS = ("simulated",)


def build_vision_provider(name: str) -> VisionProvider:
    """Resolve a provider by name; unknown names fail closed."""
    if name == "simulated":
        return SimulatedVisionProvider()
    if name == "unconfigured":
        return UnconfiguredVisionProvider()
    raise ValueError(
        f"Unknown vision provider {name!r}; available: "
        f"{', '.join(AVAILABLE_PROVIDERS)}")


def propose_actions(result: VisionResult) -> list[dict[str, Any]]:
    """Derive proposed actions from one structured understanding.

    Pure and bounded: UI-element regions become click proposals;
    dangerous instructions become explicitly blocked proposals. Nothing
    is executed here — the control plane evaluates each proposal
    against the A33 policy before anything may act on it.
    """
    proposals: list[dict[str, Any]] = []
    for danger in result.dangerous_instructions:
        proposals.append({
            "action": "blocked_untrusted_instruction",
            "target": "",
            "reason": ("The image contains text that reads like an "
                       "instruction to act. Image content is untrusted "
                       "input and can never authorize actions."),
            "source_text": danger[:200],
            "status": "blocked",
        })
    for finding in result.findings:
        if finding.kind != "ui_element":
            continue
        proposals.append({
            "action": "click",
            "target": finding.content,
            "region": list(finding.region) if finding.region else None,
            "reason": "Simulated layout region from screenshot "
                      "(no OCR/model in A39).",
            "status": "proposed",
        })
    if not proposals:
        proposals.append({
            "action": "observe",
            "target": "",
            "reason": "No actionable regions detected.",
            "status": "proposed",
        })
    return proposals[:40]
