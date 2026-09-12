"""Shared fixtures for the A81 Native AI Engine test suites.

Everything here is deterministic: temp-repo fixtures written by the tests,
scripted providers returning fixed payloads, and fabrics whose only "model"
is an explicit test double (never a real network provider).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from forge.models.fabric import ModelFabric
from forge.models.provider import ModelResult, ProviderInfo, ProviderRegistry
from forge.models.registry import Model, ModelRegistry

CALC_GOOD = "def add(a, b):\n    return a + b\n"
CALC_BROKEN = "def add(a, b):\n    return a - b\n"
TEST_CALC = (
    "import calc\n\n\n"
    "def test_add():\n"
    "    assert calc.add(1, 2) == 3\n"
)


def write_repo(root: Path, calc: str = CALC_GOOD,
               test_body: Optional[str] = TEST_CALC,
               extra: Optional[Dict[str, str]] = None) -> Path:
    """Create a tiny Python package fixture inside ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "calc.py").write_text(calc, encoding="utf-8")
    if test_body is not None:
        (root / "test_calc.py").write_text(test_body, encoding="utf-8")
    for name, text in (extra or {}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


class ScriptedProvider:
    """A fake provider that answers with pre-scripted model output.

    ``responder(prompt) -> text`` may be a callable; a plain dict is treated
    as the changes payload. Every response records ``calls`` so tests can
    assert exactly how often a model was asked (never more than allowed).
    """

    name = "scripted"

    def __init__(self, responder: Any) -> None:
        self.responder = responder
        self.calls: list[str] = []

    def generate(self, prompt: str, *, context: str = "", task: str = "",
                 **kwargs: Any) -> ModelResult:
        self.calls.append(prompt)
        if callable(self.responder):
            text = self.responder(prompt)
        elif isinstance(self.responder, dict):
            text = json.dumps({
                "explanation": "scripted",
                "changes": {str(k): str(v)
                            for k, v in self.responder.items()},
            })
        else:
            text = str(self.responder)
        return ModelResult(text, "scripted-model", 12, 34, 0.01)


def scripted_fabric(responder: Any,
                    provider_name: str = "scripted") -> ModelFabric:
    """A fabric whose only model is the scripted double (never fallback)."""
    fabric = ModelFabric(registry=ModelRegistry(),
                         providers=ProviderRegistry())
    provider = responder if isinstance(responder, ScriptedProvider) \
        else ScriptedProvider(responder)
    fabric.providers.register(
        provider_name, provider,
        ProviderInfo(name=provider_name, kind="local", local=True,
                     free=True,
                     capabilities=("coding", "debugging", "review")))
    fabric.registry.register(Model(
        name="scripted-model", provider=provider_name,
        capabilities=("coding", "debugging", "review"),
        context_window=4096))
    return fabric


def changes_text(changes: Dict[str, str], explanation: str = "scripted fix"
                 ) -> str:
    return json.dumps({"explanation": explanation, "changes": changes})


def read(root: Path, name: str) -> str:
    return (root / name).read_text(encoding="utf-8")
