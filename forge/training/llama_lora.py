"""A minimal, honest llama-family forward pass with LoRA, in torch.

Only the LoRA matrices train; the base weights stay frozen (they came from a
quantized GGUF, and re-quantizing noise into them would be worse than leaving
them alone). Every other part of the network — RMSNorm, rotary embeddings,
grouped-query attention, SwiGLU MLP — is implemented here so an adapter can be
trained against the exact weights the runtime serves.

Correctness is checked the only way that means anything: the base model's loss
on real Forge text must be low (a broken forward pass produces ~log(vocab)).
:func:`evaluate_loss` is that check, and the training job records it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forge.training.gguf_model import GgufLlama


@dataclass
class LoRAConfig:
    #: Which projections get an adapter. q/v is the small, effective default.
    targets: tuple[str, ...] = ("attn_q", "attn_v")
    rank: int = 8
    alpha: int = 16
    dropout: float = 0.0

    @property
    def scale(self) -> float:
        return float(self.alpha) / float(max(1, self.rank))


def _lora_parameters(torch, base_weight, config: LoRAConfig, generator):
    """A and B for one projection; B starts at zero so training starts neutral."""
    out_features, in_features = base_weight.shape
    a = torch.zeros((config.rank, in_features), dtype=torch.float32)
    b = torch.zeros((out_features, config.rank), dtype=torch.float32)
    a.normal_(mean=0.0, std=0.02, generator=generator)
    return a.requires_grad_(True), b.requires_grad_(True)


class LlamaLoraModel:
    """The GGUF's weights plus trainable LoRA deltas."""

    def __init__(self, gguf: GgufLlama, config: LoRAConfig | None = None,
                 *, seed: int = 0) -> None:
        import torch

        self.torch = torch
        self.gguf = gguf
        self.config = config or LoRAConfig()
        self.cfg = gguf.config
        generator = torch.Generator().manual_seed(int(seed))
        self.params: dict[str, Any] = {}
        for layer in range(self.cfg.layers):
            for target in self.config.targets:
                name = f"blk.{layer}.{target}.weight"
                if not gguf.has(name):
                    continue
                a, b = _lora_parameters(torch, gguf.weight(name), self.config,
                                        generator)
                self.params[f"{name}.lora_a"] = a
                self.params[f"{name}.lora_b"] = b
        self._rope_cache: dict[int, tuple[Any, Any]] = {}

    def trainable(self) -> list[Any]:
        return list(self.params.values())

    def _rope(self, length: int):
        cached = self._rope_cache.get(length)
        if cached is not None:
            return cached
        torch = self.torch
        head_dim = self.cfg.head_dim
        position = torch.arange(length, dtype=torch.float32)
        frequency = 1.0 / (self.cfg.rope_base ** (
            torch.arange(0, head_dim, 2, dtype=torch.float32) / head_dim))
        angles = torch.outer(position, frequency)
        self._rope_cache[length] = (torch.cos(angles), torch.sin(angles))
        return self._rope_cache[length]

    def _rms_norm(self, x, weight):
        torch = self.torch
        variance = x.pow(2).mean(-1, keepdim=True)
        return x * torch.rsqrt(variance + self.cfg.rms_eps) * weight

    def _rotate(self, x, cos, sin):
        torch = self.torch
        x1, x2 = x[..., ::2], x[..., 1::2]
        return torch.stack((x1 * cos - x2 * sin, x1 * sin + x2 * cos),
                           dim=-1).flatten(-2)

    def _linear(self, x, name):
        torch = self.torch
        base = self.gguf.weight(name)
        output = x @ base.t()
        a = self.params.get(f"{name}.lora_a")
        b = self.params.get(f"{name}.lora_b")
        if a is not None and b is not None:
            delta = (x @ a.t()) @ b.t()
            output = output + delta * self.config.scale
        return output

    def forward(self, tokens):
        """Logits for a batch of token ids, shape (batch, seq, vocab)."""
        torch = self.torch
        batch, length = tokens.shape
        cfg = self.cfg

        hidden = self.gguf.weight("token_embd.weight")[tokens]
        cos, sin = self._rope(length)
        cos = cos[None, None, :, :]
        sin = sin[None, None, :, :]
        mask = torch.triu(torch.full((length, length), float("-inf")),
                          diagonal=1)[None, None, :, :]
        group = cfg.heads // max(1, cfg.kv_heads)

        for layer in range(cfg.layers):
            prefix = f"blk.{layer}."
            normed = self._rms_norm(hidden, self.gguf.weight(prefix + "attn_norm.weight"))

            q = self._linear(normed, prefix + "attn_q.weight")
            k = self._linear(normed, prefix + "attn_k.weight")
            v = self._linear(normed, prefix + "attn_v.weight")

            q = q.view(batch, length, cfg.heads, cfg.head_dim).transpose(1, 2)
            k = k.view(batch, length, cfg.kv_heads, cfg.head_dim).transpose(1, 2)
            v = v.view(batch, length, cfg.kv_heads, cfg.head_dim).transpose(1, 2)

            q = self._rotate(q, cos, sin)
            k = self._rotate(k, cos, sin)

            if group > 1:
                k = k.repeat_interleave(group, dim=1)
                v = v.repeat_interleave(group, dim=1)

            scores = (q @ k.transpose(-2, -1)) / (cfg.head_dim ** 0.5)
            scores = scores + mask
            attention = torch.softmax(scores, dim=-1)
            context = (attention @ v).transpose(1, 2).reshape(batch, length, cfg.hidden)

            hidden = hidden + self._linear(context, prefix + "attn_output.weight")

            normed = self._rms_norm(hidden, self.gguf.weight(prefix + "ffn_norm.weight"))
            gate = torch.nn.functional.silu(self._linear(normed, prefix + "ffn_gate.weight"))
            up = self._linear(normed, prefix + "ffn_up.weight")
            hidden = hidden + self._linear(gate * up, prefix + "ffn_down.weight")

        hidden = self._rms_norm(hidden, self.gguf.weight("output_norm.weight"))
        return hidden @ self.gguf.weight("token_embd.weight").t()


def loss_on_batch(model: LlamaLoraModel, batch: list[list[int]]):
    """Mean next-token cross-entropy over a batch of token sequences."""
    torch = model.torch
    length = min(len(sequence) for sequence in batch)
    ids = torch.tensor([sequence[:length] for sequence in batch], dtype=torch.long)
    logits = model.forward(ids[:, :-1])
    targets = ids[:, 1:]
    return torch.nn.functional.cross_entropy(
        logits.reshape(-1, logits.shape[-1]), targets.reshape(-1))


def evaluate_loss(model: LlamaLoraModel, sequences: list[list[int]],
                  *, batches: int = 4) -> float:
    """Average loss over real text. A sane forward pass lands far below log(vocab)."""
    torch = model.torch
    usable = [sequence for sequence in sequences if len(sequence) > 2]
    if not usable:
        return float("nan")
    total, count = 0.0, 0
    with torch.no_grad():
        for index in range(0, min(len(usable), batches), 2):
            group = usable[index:index + 2]
            total += float(loss_on_batch(model, group))
            count += 1
    return total / max(1, count)


@dataclass
class TrainingRecord:
    step: int
    loss: float


@dataclass
class LoRAResult:
    steps: int
    initial_loss: float
    final_loss: float
    seconds: float
    history: list[TrainingRecord] = field(default_factory=list)
    trainable_parameters: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "initial_loss": round(self.initial_loss, 4),
            "final_loss": round(self.final_loss, 4),
            "seconds": round(self.seconds, 2),
            "trainable_parameters": self.trainable_parameters,
            "history": [{"step": r.step, "loss": round(r.loss, 4)}
                        for r in self.history],
        }


def train_lora(model: LlamaLoraModel, sequences: list[list[int]], *,
               steps: int = 30, learning_rate: float = 3e-3,
               batch_size: int = 2, log_every: int = 5,
               seed: int = 0) -> LoRAResult:
    """Train the adapters on real token sequences and report the loss curve."""
    import time

    torch = model.torch
    torch.manual_seed(int(seed))
    optimizer = torch.optim.AdamW(model.trainable(), lr=learning_rate)
    usable = [sequence for sequence in sequences if len(sequence) > 2]
    if not usable:
        raise ValueError("no usable training sequences")

    initial = evaluate_loss(model, usable)
    started = time.perf_counter()
    history: list[TrainingRecord] = []
    generator = torch.Generator().manual_seed(int(seed))
    for step in range(1, max(1, steps) + 1):
        index = int(torch.randint(0, len(usable), (1,), generator=generator))
        group = usable[index:index + batch_size] or usable[:batch_size]
        optimizer.zero_grad(set_to_none=True)
        loss = loss_on_batch(model, group)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.trainable(), 1.0)
        optimizer.step()
        if step % log_every == 0 or step == 1:
            history.append(TrainingRecord(step=step, loss=float(loss)))
    final = evaluate_loss(model, usable)
    return LoRAResult(
        steps=max(1, steps),
        initial_loss=initial,
        final_loss=final,
        seconds=time.perf_counter() - started,
        history=history,
        trainable_parameters=sum(int(p.numel()) for p in model.trainable()),
    )
