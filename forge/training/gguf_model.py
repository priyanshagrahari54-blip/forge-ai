"""Load a llama.cpp GGUF (the exact file the runtime serves) into torch.

Fine-tuning the *deployed* model matters: an adapter trained against a
different copy of the weights is not an adapter for what answers requests. So
this reader takes the GGUF llama-server is serving, dequantizes its weights with
``gguf.quants``, and builds the tokenizer from the GGUF's own vocabulary and
merges — no download, no second copy of the model, no Hugging Face Hub.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Tensor-name prefixes that identify a llama.cpp llama-family GGUF.
_REQUIRED_TENSORS = (
    "token_embd.weight", "output_norm.weight", "blk.0.attn_q.weight",
    "blk.0.ffn_gate.weight",
)


@dataclass(frozen=True)
class LlamaConfig:
    vocab_size: int
    hidden: int
    layers: int
    heads: int
    kv_heads: int
    ffn: int
    rope_base: float
    context: int
    rms_eps: float = 1e-5
    tie_embeddings: bool = False

    @property
    def head_dim(self) -> int:
        return self.hidden // self.heads


class GgufLlama:
    """Weights, tokenizer and config of one GGUF file."""

    def __init__(self, path: str | Path) -> None:
        import torch
        from gguf import GGUFReader

        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"no GGUF at {self.path}")
        self.reader = GGUFReader(str(self.path))
        fields = self.reader.fields
        names = [tensor.name for tensor in self.reader.tensors]
        missing = [name for name in _REQUIRED_TENSORS if name not in names]
        if missing:
            raise ValueError(
                f"{self.path.name} is not a llama-family GGUF: missing {missing}")
        self._names = set(names)

        def field(key: str, default: Any = None) -> Any:
            entry = fields.get(key)
            if entry is None:
                return default
            try:
                return entry.contents()
            except TypeError:
                return default

        blocks = [name.split(".")[1] for name in names
                  if name.startswith("blk.")]
        layers = len({int(index) for index in blocks})
        self.config = LlamaConfig(
            vocab_size=len(field("tokenizer.ggml.tokens", []) or []),
            hidden=int(field("llama.embedding_length", 0) or 0),
            layers=layers,
            heads=int(field("llama.attention.head_count", 0) or 0),
            kv_heads=int(field("llama.attention.head_count_kv", 0) or 0),
            ffn=int(field("llama.feed_forward_length", 0) or 0),
            rope_base=float(field("llama.rope.freq_base", 10000.0) or 10000.0),
            context=int(field("llama.context_length", 2048) or 2048),
            rms_eps=float(field("llama.attention.layer_norm_rms_epsilon", 1e-5)
                          or 1e-5),
        )
        if not all((self.config.vocab_size, self.config.hidden,
                    self.config.heads, self.config.ffn)):
            raise ValueError(f"{self.path.name} is missing required gguf metadata")
        if self.config.kv_heads == 0:
            self.config = LlamaConfig(**{**self.config.__dict__,
                                         "kv_heads": self.config.heads})
        self._torch = torch
        self._weights: dict[str, Any] = {}

    # -- weights -------------------------------------------------------------

    def has(self, name: str) -> bool:
        return name in self._names

    def weight(self, name: str):
        """Dequantized, float32 weight tensor (cached per name)."""
        cached = self._weights.get(name)
        if cached is not None:
            return cached
        import numpy as np
        from gguf.quants import dequantize

        tensor = next(t for t in self.reader.tensors if t.name == name)
        raw = dequantize(tensor.data, tensor.tensor_type)
        array = np.asarray(raw, dtype=np.float32)
        expected = tuple(reversed([int(dim) for dim in tensor.shape]))
        if array.size != int(np.prod(expected)):
            raise ValueError(f"{name}: dequantized size does not match shape")
        tensor_out = self._torch.from_numpy(array.reshape(expected).copy())
        self._weights[name] = tensor_out
        return tensor_out

    # -- tokenizer -----------------------------------------------------------

    def tokenizer(self):
        """A GPT-2-style BPE built from the GGUF's own vocabulary and merges."""
        cached = getattr(self, "_tokenizer", None)
        if cached is not None:
            return cached
        from tokenizers import Tokenizer, decoders, models, pre_tokenizers

        vocab_entries = self.reader.fields["tokenizer.ggml.tokens"].contents()
        vocab = {str(token): index for index, token in enumerate(vocab_entries)}
        merges: list[tuple[str, str]] = []
        entry = self.reader.fields.get("tokenizer.ggml.merges")
        for merge in (entry.contents() if entry is not None else []):
            parts = str(merge).split(" ")
            if len(parts) == 2:
                merges.append((parts[0], parts[1]))
        tokenizer = Tokenizer(models.BPE(vocab=vocab, merges=merges))
        tokenizer.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
        tokenizer.decoder = decoders.ByteLevel()
        self._tokenizer = tokenizer
        return tokenizer

    def encode(self, text: str) -> list[int]:
        return list(self.tokenizer().encode(text).ids)

    def decode(self, ids: list[int]) -> str:
        #: ``skip_special_tokens=False`` on purpose: when inspecting training
        #: data the ChatML markers are part of what the model was trained on,
        #: and hiding them would misrepresent the sequence.
        return self.tokenizer().decode([int(i) for i in ids],
                                       skip_special_tokens=False)
