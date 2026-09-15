"""Optional Hugging Face/PEFT inference adapter for Forge Native Runtime.

No heavyweight ML dependency is imported at module import time. The host must
explicitly construct this adapter and pass it to ``NativeBackend`` through the
runtime's ``InferenceAdapter`` seam. This keeps model execution deterministic,
inspectable and subject to Forge's existing runtime controls.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Iterator, Optional, Tuple

from forge.runtime.model_runtime import (
    BackendUnavailableError,
    CancellationToken,
    InferenceAdapter,
    RuntimeChunk,
    RuntimeRequest,
    RuntimeResponse,
    RuntimeTimeoutError,
    redact_text,
)


class HuggingFaceInferenceAdapter(InferenceAdapter):
    """Run a local Hugging Face causal LM, optionally with a PEFT adapter.

    ``base_model`` may be a local Transformers directory or a Hub identifier.
    Network access is disabled by default. ``adapter_path`` is optional and
    should point at a LoRA/QLoRA adapter produced by Model Studio.
    """

    name = "huggingface"

    def __init__(self, base_model: str, *, adapter_path: str = "",
                 allow_network: bool = False,
                 max_cache_entries: int = 2) -> None:
        self.base_model = str(base_model)
        self.adapter_path = str(adapter_path or "")
        self.allow_network = bool(allow_network)
        self.max_cache_entries = max(1, int(max_cache_entries))
        self._lock = threading.RLock()
        self._cached: list[Tuple[str, Any, Any]] = []

    def _imports(self):
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
            try:
                from transformers import TextIteratorStreamer
            except ImportError:
                TextIteratorStreamer = None
            try:
                from peft import PeftModel
            except ImportError:
                PeftModel = None
        except ImportError as exc:
            raise BackendUnavailableError(
                "Hugging Face inference requires torch and transformers") from exc
        return torch, AutoModelForCausalLM, AutoTokenizer, TextIteratorStreamer, PeftModel

    def available(self) -> Tuple[bool, str]:
        try:
            self._imports()
        except BackendUnavailableError as exc:
            return False, redact_text(str(exc))
        if not self.base_model:
            return False, "No Hugging Face base model is configured."
        return True, "Hugging Face inference adapter is available."

    def _load(self):
        torch, AutoModelForCausalLM, AutoTokenizer, _, PeftModel = self._imports()
        key = self.base_model + "\0" + self.adapter_path
        with self._lock:
            for cached_key, model, tokenizer in self._cached:
                if cached_key == key:
                    return torch, model, tokenizer

        local_only = not self.allow_network
        tokenizer = AutoTokenizer.from_pretrained(
            self.base_model,
            local_files_only=local_only,
            trust_remote_code=False,
        )
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token
        model = AutoModelForCausalLM.from_pretrained(
            self.base_model,
            local_files_only=local_only,
            trust_remote_code=False,
        )
        if self.adapter_path:
            if PeftModel is None:
                raise BackendUnavailableError(
                    "PEFT is required to load the configured adapter.")
            model = PeftModel.from_pretrained(
                model,
                self.adapter_path,
                is_trainable=False,
                local_files_only=local_only,
            )
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model.to(device)
        model.eval()

        with self._lock:
            self._cached.insert(0, (key, model, tokenizer))
            self._cached = self._cached[:self.max_cache_entries]
        return torch, model, tokenizer

    @staticmethod
    def _prompt(request: RuntimeRequest) -> str:
        return request.compose_prompt()

    def generate(self, request: RuntimeRequest,
                 token: Optional[CancellationToken] = None) -> RuntimeResponse:
        started = time.time()
        try:
            if token is not None:
                token.raise_if_cancelled()
            torch, model, tokenizer = self._load()
            prompt = self._prompt(request)
            encoded = tokenizer(prompt, return_tensors="pt")
            device = next(model.parameters()).device
            encoded = {key: value.to(device) for key, value in encoded.items()}
            max_new = int(request.max_output_tokens or 256)
            if max_new < 1:
                max_new = 1
            with torch.no_grad():
                output = model.generate(
                    **encoded,
                    max_new_tokens=max_new,
                    do_sample=bool(request.temperature and request.temperature > 0),
                    temperature=float(request.temperature or 1.0),
                    eos_token_id=tokenizer.eos_token_id,
                    pad_token_id=tokenizer.pad_token_id,
                )
            generated = output[0][encoded["input_ids"].shape[-1]:]
            text = tokenizer.decode(generated, skip_special_tokens=True)
            for stop in tuple(request.stop or ()):
                if stop and stop in text:
                    text = text.split(stop, 1)[0]
            if token is not None:
                token.raise_if_cancelled()
            return RuntimeResponse(
                text=text,
                model=request.model,
                backend=request.backend,
                success=True,
                request_id=request.request_id,
                trace_id=request.trace_id,
                latency_ms=(time.time() - started) * 1000.0,
                finish_reason="stop",
                input_tokens=0,
                output_tokens=0,
                metadata={"adapter": self.name, "adapter_path": bool(self.adapter_path)},
            )
        except (RuntimeTimeoutError, BackendUnavailableError):
            raise
        except Exception as exc:
            return RuntimeResponse.failure(
                redact_text(str(exc))[:500],
                backend=request.backend,
                model=request.model,
                request_id=request.request_id,
                trace_id=request.trace_id,
                latency_ms=(time.time() - started) * 1000.0,
            )

    def stream(self, request: RuntimeRequest,
               token: Optional[CancellationToken] = None) -> Iterator[RuntimeChunk]:
        """Stream text through Transformers' TextIteratorStreamer when available."""
        torch, model, tokenizer, TextIteratorStreamer, _ = self._imports()
        if TextIteratorStreamer is None:
            result = self.generate(request, token=token)
            if result.text:
                yield RuntimeChunk(text=result.text, index=0, done=False,
                                   request_id=request.request_id)
            yield RuntimeChunk(text="", index=1, done=True,
                               request_id=request.request_id,
                               finish_reason=result.finish_reason)
            return

        prompt = self._prompt(request)
        encoded = tokenizer(prompt, return_tensors="pt")
        device = next(model.parameters()).device
        encoded = {key: value.to(device) for key, value in encoded.items()}
        streamer = TextIteratorStreamer(
            tokenizer,
            skip_prompt=True,
            skip_special_tokens=True,
        )
        max_new = max(1, int(request.max_output_tokens or 256))
        errors: list[BaseException] = []

        def _generate() -> None:
            try:
                with torch.no_grad():
                    model.generate(
                        **encoded,
                        streamer=streamer,
                        max_new_tokens=max_new,
                        do_sample=bool(request.temperature and request.temperature > 0),
                        temperature=float(request.temperature or 1.0),
                        eos_token_id=tokenizer.eos_token_id,
                        pad_token_id=tokenizer.pad_token_id,
                    )
            except BaseException as exc:
                errors.append(exc)
                try:
                    streamer.end()
                except Exception:
                    pass

        thread = threading.Thread(target=_generate, name="forge-hf-generate", daemon=True)
        thread.start()
        index = 0
        try:
            for chunk in streamer:
                if token is not None and token.cancelled:
                    raise RuntimeTimeoutError("Runtime request cancelled while streaming.") \
                        if token.reason == "timeout" else BackendUnavailableError("Runtime request cancelled.")
                if chunk:
                    yield RuntimeChunk(
                        text=chunk,
                        index=index,
                        done=False,
                        request_id=request.request_id,
                    )
                    index += 1
        finally:
            if token is not None and token.cancelled:
                try:
                    token.raise_if_cancelled()
                except Exception:
                    pass
        if errors:
            raise BackendUnavailableError("Hugging Face generation failed.") from errors[0]
        yield RuntimeChunk(
            text="",
            index=index,
            done=True,
            request_id=request.request_id,
            finish_reason="stop",
        )


__all__ = ["HuggingFaceInferenceAdapter"]
