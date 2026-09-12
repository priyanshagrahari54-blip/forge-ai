"""Shared test doubles for the Forge Native Model Runtime suite (A81).

Every class here is an explicit **test double**, not a model.  None of them
synthesises an answer: the text they return is supplied by the test, so a
passing test proves the *runtime* behaved (routing, bounds, cancellation,
isolation, reporting) and never that a model produced something.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Iterator, List, Optional, Tuple

from forge.runtime.model_runtime import (BackendKind, ModelBackend,
                                         RuntimeChunk, RuntimeHealth,
                                         RuntimeModel, RuntimeResponse,
                                         RuntimeState)


class ScriptedBackend(ModelBackend):
    """Deterministic backend that replays caller-supplied text.

    ``response`` is the exact text a test asks for; the backend adds nothing.
    ``chunks`` drives ``stream`` the same way.
    """

    def __init__(self, name: str = "scripted", response: str = "scripted-ok",
                 chunks: Optional[List[str]] = None,
                 delay: float = 0.0, chunk_delay: float = 0.0,
                 kind: str = BackendKind.CUSTOM.value) -> None:
        self.name = name
        self.kind = kind
        self.description = "Test double: replays caller-supplied text."
        self.response = response
        self.chunks = list(chunks) if chunks is not None else [response]
        self.delay = delay
        self.chunk_delay = chunk_delay
        self.generate_calls: List[Any] = []
        self.stream_calls: List[Any] = []
        self.cancelled_mid_stream = False

    def available(self) -> Tuple[bool, str]:
        return (True, "test double")

    def generate(self, request, token=None) -> RuntimeResponse:
        self.generate_calls.append(request)
        if self.delay:
            time.sleep(self.delay)
        if token is not None:
            token.raise_if_cancelled()
        return RuntimeResponse(
            text=self.response, model=request.model or self.name,
            backend=self.name, input_tokens=len(request.prompt or ""),
            output_tokens=len(self.response), latency_ms=0.0)

    def stream(self, request, token=None) -> Iterator[Any]:
        self.stream_calls.append(request)
        for index, chunk in enumerate(self.chunks):
            if self.chunk_delay:
                time.sleep(self.chunk_delay)
            if token is not None and token.cancelled:
                self.cancelled_mid_stream = True
                return
            yield RuntimeChunk(text=chunk, output_tokens=index + 1)

    def health(self, probe: bool = True) -> RuntimeHealth:
        return RuntimeHealth(backend=self.name, kind=self.kind,
                             status=RuntimeState.READY.value,
                             checked_at=time.time(), detail="test double")


class HangingBackend(ModelBackend):
    """Ignores its cancellation token: proves the runtime's bounds are real."""

    def __init__(self, name: str = "hang", hold: float = 30.0) -> None:
        self.name = name
        self.kind = BackendKind.CUSTOM.value
        self.description = "Test double: blocks and ignores cancellation."
        self.hold = hold

    def available(self) -> Tuple[bool, str]:
        return (True, "test double")

    def generate(self, request, token=None) -> RuntimeResponse:
        time.sleep(self.hold)
        return RuntimeResponse(text="never", backend=self.name)

    def stream(self, request, token=None) -> Iterator[Any]:
        time.sleep(self.hold)
        yield "never"


class CooperativeBackend(ModelBackend):
    """Observes cancellation promptly and returns partial work."""

    def __init__(self, name: str = "coop", steps: int = 200,
                 step: float = 0.01) -> None:
        self.name = name
        self.kind = BackendKind.CUSTOM.value
        self.description = "Test double: checks the cancellation token."
        self.steps = steps
        self.step = step
        self.observed_cancellation = False

    def available(self) -> Tuple[bool, str]:
        return (True, "test double")

    def generate(self, request, token=None) -> RuntimeResponse:
        for _ in range(self.steps):
            if token is not None and token.cancelled:
                self.observed_cancellation = True
                return RuntimeResponse(text="partial", backend=self.name,
                                       cancelled=True,
                                       finish_reason="cancelled")
            time.sleep(self.step)
        return RuntimeResponse(text="completed", backend=self.name)


class ExplodingBackend(ModelBackend):
    """Raises, with a secret-looking value in the message.

    Used to prove the runtime redacts backend errors before they reach a
    response, a log line, or the caller.
    """

    SECRET = "api_key=SUPERSECRETVALUE123"

    def __init__(self, name: str = "boom") -> None:
        self.name = name
        self.kind = BackendKind.CUSTOM.value
        self.description = "Test double: always fails."

    def available(self) -> Tuple[bool, str]:
        return (True, "test double")

    def generate(self, request, token=None) -> RuntimeResponse:
        raise RuntimeError(f"backend exploded with {self.SECRET}")

    def stream(self, request, token=None) -> Iterator[Any]:
        raise RuntimeError(f"stream exploded with {self.SECRET}")
        yield  # pragma: no cover - makes this a generator

    def list_models(self):
        raise RuntimeError(f"discovery exploded with {self.SECRET}")

    def health(self, probe: bool = True):
        # A backend that cannot generate cannot report itself healthy; its
        # probe fails with the same secret-bearing message, which the runtime
        # must redact before it reaches any report.
        raise RuntimeError(f"health probe exploded with {self.SECRET}")


class BrokenBackend(ModelBackend):
    """Violates the backend contract, to prove protocol errors are caught."""

    def __init__(self, name: str = "broken") -> None:
        self.name = name
        self.kind = BackendKind.CUSTOM.value
        self.description = "Test double: returns the wrong types."

    def available(self) -> Tuple[bool, str]:
        return (True, "test double")

    def generate(self, request, token=None):
        return "not a RuntimeResponse"

    def stream(self, request, token=None) -> Iterator[Any]:
        yield 12345

    def health(self, probe: bool = True):
        return "not a RuntimeHealth"


class ModelServingBackend(ScriptedBackend):
    """A scripted backend that also reports a model list (discovery tests)."""

    def __init__(self, name: str = "serving", models: Optional[List[str]] = None,
                 fail_discovery: bool = False, **kwargs: Any) -> None:
        super().__init__(name=name, **kwargs)
        self.model_names = list(models) if models is not None else ["alpha",
                                                                    "beta"]
        self.fail_discovery = fail_discovery
        self.loaded: List[str] = []

    def list_models(self) -> List[RuntimeModel]:
        if self.fail_discovery:
            raise RuntimeError("discovery unavailable")
        return [RuntimeModel(
            model_id=RuntimeModel.make_id(self.name, item), name=item,
            backend=self.name, kind="text", capabilities=("coding",),
            context_window=4096, format="gguf", local=True,
            discovered_at=time.time()) for item in self.model_names]

    def load_model(self, model, token=None) -> RuntimeModel:
        if token is not None:
            token.raise_if_cancelled()
        self.loaded.append(model.model_id)
        model.loaded = True
        return model

    def unload_model(self, model_id: str) -> bool:
        if model_id in self.loaded:
            self.loaded.remove(model_id)
            return True
        return False


class StubClient:
    """Client double for ``ExternalClientBackend`` (llama.cpp / Forge)."""

    def __init__(self, name: str = "stub-client", response: str = "client-ok",
                 models: Optional[List[str]] = None) -> None:
        self.name = name
        self.response = response
        self.models = list(models or ["client-model"])
        self.generated = 0

    def available(self) -> Tuple[bool, str]:
        return (True, "stub client present")

    def list_models(self) -> List[str]:
        return list(self.models)

    def load_model(self, model, token=None):
        return model

    def unload_model(self, model_id: str) -> bool:
        return True

    def generate(self, request, token=None) -> RuntimeResponse:
        self.generated += 1
        return RuntimeResponse(text=self.response,
                               model=request.model or self.models[0],
                               backend="")

    def stream(self, request, token=None) -> Iterator[str]:
        for chunk in ("a", "b", "c"):
            yield chunk


def make_runtime(*backends: ModelBackend, **config: Any):
    """Build a runtime with the given backends and a bounded configuration."""
    from forge.runtime.model_runtime import ModelRuntime, RuntimeConfig

    defaults = {
        "default_backend": backends[0].name if backends else "native",
        "backends": ("native",),
        "timeout_seconds": 1.0,
        "max_timeout_seconds": 5.0,
        "chunk_timeout_seconds": 0.4,
    }
    defaults.update(config)
    runtime = ModelRuntime(RuntimeConfig(**defaults))
    for backend in backends:
        runtime.register_backend(backend)
    return runtime


def wait_until(predicate, timeout: float = 5.0,
               interval: float = 0.01) -> bool:
    """Poll a predicate; keeps timing assertions deterministic."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def run_in_thread(target, *args, **kwargs):
    """Run ``target`` in a daemon thread and return ``(thread, results)``."""
    results: List[Any] = []

    def _wrapper() -> None:
        results.append(target(*args, **kwargs))

    thread = threading.Thread(target=_wrapper, daemon=True)
    thread.start()
    return thread, results
