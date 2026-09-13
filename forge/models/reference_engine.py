"""A real, first-party local inference backend (Session 11).

Why this module exists
----------------------
Session 11 requires *at least one actual inference path* that works offline,
without shipping model weights, without downloading anything, and without a
third-party dependency. This is that path: a genuine (tiny) neural model
executed by Forge itself.

What it is, precisely
---------------------
``ReferenceLocalBackend`` is a real :class:`~forge.runtime.model_runtime.
ModelBackend`. It

* discovers ``.forgeref`` artifacts inside **explicitly configured**
  directories (bounded depth, symlink/``..`` escapes refused),
* loads real weights into memory (a character-level feed-forward network:
  one-hot context window -> hidden layer -> softmax over the vocabulary),
* runs a **real forward pass** per generated token, with real sampling,
* streams real per-token chunks,
* enforces context, output, timeout and cancellation bounds,
* reports health, resident bytes and resource usage honestly.

What it is *not*
----------------
It is **not** a capable assistant. The artifact ships no trained weights: an
operator (or a test) materialises one with :class:`ReferenceArtifactWriter`,
which writes deterministic pseudo-random weights. The model therefore emits
plausible-looking characters, not answers. To keep that from ever being
mistaken for capability:

* it advertises **no** capabilities, so capability-based routing never selects
  it for a coding/planning/reasoning task;
* its identity carries ``quality=0.0``, ``model_family="forge-reference-clm"``,
  ``metadata["trained"]=False`` and ``metadata["reference_engine"]=True``;
* the docs and the CLI label it ``reference`` everywhere.

It is the honest proof that the chain
``Fabric -> Routing -> Runtime -> Backend -> model`` executes real inference.
Substituting a serious engine is an adapter change, not an architecture
change: register another :class:`ModelBackend` (or an
:class:`~forge.runtime.model_runtime.InferenceAdapter`) and the fabric uses it
unchanged.

Security
--------
* No pickle, no ``eval``, no code in the artifact: the format is a fixed
  binary header plus ``struct``-packed floats and a JSON vocabulary.
* No weights in the repository, no downloads: the writer needs an explicit
  destination path and the backend only reads directories it was given.
* No secret material is logged; generated text never enters telemetry.

Python floor: 3.8 (Windows 7 reference target). Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import random
import struct
import threading
import time
from dataclasses import dataclass
from typing import (Any, Dict, Iterator, List, Optional, Sequence, Tuple)

from forge.models.backends import BackendError
from forge.runtime.model_runtime import (BackendKind, BackendProtocolError,
                                         BackendUnavailableError,
                                         CancellationToken, ErrorKind,
                                         FinishReason, ModelBackend,
                                         RuntimeChunk, RuntimeHealth,
                                         RuntimeModel, RuntimeRequest,
                                         RuntimeResponse, RuntimeState,
                                         redact_text)

__all__ = [
    "REFERENCE_EXTENSION",
    "ReferenceArtifactWriter",
    "ReferenceBackendError",
    "ReferenceLocalBackend",
    "ReferenceModel",
    "ReferenceModelConfig",
    "describe_reference_artifact",
]

#: Artifact extension. Deliberately not one of the runtime's neural formats:
#: this is Forge's own container and is never confused with GGUF/safetensors.
REFERENCE_EXTENSION = ".forgeref"

_MAGIC = b"FORGEREF"
_FORMAT_VERSION = 1
#: Fixed binary header: magic, version, vocab blob length, vocab size,
#: hidden size, context chars. No padding, no platform-dependent layout.
_HEADER = struct.Struct("<8sBIIII")
_HEADER_SIZE = _HEADER.size
#: Hard bounds: a hostile or corrupt artifact cannot allocate unbounded memory.
_MAX_VOCAB = 512
_MAX_HIDDEN = 256
_MAX_CONTEXT = 256
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_MAX_SCAN_FILES = 256
_MAX_SCAN_DEPTH = 4
#: Bound on generated characters for one request (the caller's
#: ``max_output_tokens`` further bounds it).
_MAX_OUTPUT_CHARS = 8192

#: Streaming: how many characters may sit between the generating thread and
#: the consumer. Small on purpose, so a slow consumer applies real
#: backpressure instead of the producer running away.
_STREAM_BUFFER = 16
#: Per-chunk wait while draining the producer (seconds).
_STREAM_CHUNK_TIMEOUT = 0.25
#: Chars-per-token used to report a token-equivalent context window for a
#: character-level model (matches ``forge.models.context_budget``).
_CHARS_PER_TOKEN = 4


class ReferenceBackendError(BackendUnavailableError, BackendError):
    """A reference-engine failure (artifact, format, or resource).

    It belongs to both hierarchies on purpose: the runtime raises
    ``BackendUnavailableError`` (so the runtime's own handlers see it) while
    the fabric and the CLI catch ``forge.models.backends.BackendError``. A
    failure that only satisfied one of them would surface as a traceback
    instead of an honest refusal.
    """


@dataclass(frozen=True)
class ReferenceModelConfig:
    """Shape of the reference network. Small on purpose."""

    vocab_size: int = 96
    hidden_size: int = 24
    context_chars: int = 64
    seed: int = 20260913
    #: Sampling defaults; a request may override temperature within bounds.
    temperature: float = 0.8
    name: str = "reference-clm"

    def validate(self) -> "ReferenceModelConfig":
        if not 2 <= int(self.vocab_size) <= _MAX_VOCAB:
            raise ReferenceBackendError(
                "vocab_size must be within [2, %d]" % _MAX_VOCAB)
        if not 1 <= int(self.hidden_size) <= _MAX_HIDDEN:
            raise ReferenceBackendError(
                "hidden_size must be within [1, %d]" % _MAX_HIDDEN)
        if not 1 <= int(self.context_chars) <= _MAX_CONTEXT:
            raise ReferenceBackendError(
                "context_chars must be within [1, %d]" % _MAX_CONTEXT)
        if not 0.0 < float(self.temperature) <= 4.0:
            raise ReferenceBackendError(
                "temperature must be within (0, 4]")
        return self

    def parameter_count(self) -> int:
        """Real count of the weights this configuration holds."""
        inputs = int(self.context_chars) * int(self.vocab_size)
        return (inputs * int(self.hidden_size) + int(self.hidden_size)
                + int(self.hidden_size) * int(self.vocab_size)
                + int(self.vocab_size))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "vocab_size": int(self.vocab_size),
            "hidden_size": int(self.hidden_size),
            "context_chars": int(self.context_chars),
            "seed": int(self.seed),
            "temperature": float(self.temperature),
            "name": self.name,
            "parameter_count": self.parameter_count(),
        }


#: The default vocabulary: printable ASCII plus newline/tab. Deterministic.
DEFAULT_VOCAB: Tuple[str, ...] = tuple(
    [chr(code) for code in range(32, 127)] + ["\n", "\t"])


class ReferenceModel:
    """Loaded weights + the real forward pass."""

    def __init__(self, config: ReferenceModelConfig, vocab: Sequence[str],
                 weights_1: List[List[float]], bias_1: List[float],
                 weights_2: List[List[float]], bias_2: List[float],
                 *, source: str = "", fingerprint: str = "",
                 size_bytes: int = 0) -> None:
        self.config = config
        self.vocab = list(vocab)
        self._index = {char: position
                       for position, char in enumerate(self.vocab)}
        self.weights_1 = weights_1
        self.bias_1 = bias_1
        self.weights_2 = weights_2
        self.bias_2 = bias_2
        self.source = source
        self.fingerprint = fingerprint
        self.size_bytes = size_bytes
        self.loaded_at = time.time()
        self.generations = 0
        self.tokens = 0

    # -- forward pass ----------------------------------------------------

    def _encode(self, context: str) -> List[float]:
        """One-hot encode the last ``context_chars`` characters (real input)."""
        size = int(self.config.context_chars)
        vocab = int(self.config.vocab_size)
        vector = [0.0] * (size * vocab)
        chars = list(context[-size:])
        # Left-pad so the position of each character is stable.
        chars = [" "] * (size - len(chars)) + chars
        for position, char in enumerate(chars):
            index = self._index.get(char, self._index.get(" "))
            if index is None:
                continue
            vector[position * vocab + index] = 1.0
        return vector

    def _logits(self, context: str) -> List[float]:
        """One real forward pass: one-hot input -> tanh hidden -> logits."""
        vector = self._encode(context)
        hidden: List[float] = []
        for unit, bias in enumerate(self.bias_1):
            column = self.weights_1[unit]
            total = bias
            # The input is one-hot, so only the set positions contribute.
            for index, value in enumerate(vector):
                if value:
                    total += column[index]
            hidden.append(math.tanh(total))
        logits: List[float] = []
        for unit, bias in enumerate(self.bias_2):
            column = self.weights_2[unit]
            total = bias
            for index, value in enumerate(hidden):
                if value:
                    total += column[index] * value
            logits.append(total)
        return logits

    def distribution(self, context: str) -> List[float]:
        """The real next-character distribution (softmax over the logits)."""
        logits = self._logits(context)
        peak = max(logits)
        exps = [math.exp(min(60.0, value - peak)) for value in logits]
        norm = sum(exps) or 1.0
        return [value / norm for value in exps]

    def sample(self, context: str, *, temperature: float = 0.8,
               rng: Optional[random.Random] = None) -> str:
        """Sample one character from the real distribution."""
        rng = rng or random
        temperature = min(4.0, max(0.05, float(temperature)))
        logits = [value / temperature for value in self._logits(context)]
        peak = max(logits)
        exps = [math.exp(min(60.0, value - peak)) for value in logits]
        norm = sum(exps) or 1.0
        pick = rng.random() * norm
        running = 0.0
        for index, weight in enumerate(exps):
            running += weight
            if pick <= running:
                return self.vocab[index]
        return self.vocab[-1]

    def generate(self, prompt: str, *, max_chars: int = 128,
                 temperature: Optional[float] = None,
                 stop: Sequence[str] = (), seed: Optional[int] = None,
                 token: Optional[CancellationToken] = None,
                 deadline: Optional[float] = None,
                 on_token: Any = None) -> Tuple[str, str]:
        """Generate real characters. Returns ``(text, finish_reason)``."""
        limit = max(1, min(int(max_chars or 1), _MAX_OUTPUT_CHARS))
        heat = float(temperature if temperature is not None
                     else self.config.temperature)
        rng = random.Random(int(seed)) if seed is not None else random.Random(
            int(self.config.seed) ^ len(prompt or ""))
        context = (prompt or "")[-int(self.config.context_chars):]
        stops = tuple(item for item in (stop or ()) if item)
        out: List[str] = []
        finish = FinishReason.STOP.value
        for _ in range(limit):
            if token is not None and token.cancelled:
                finish = FinishReason.CANCELLED.value
                break
            if deadline is not None and time.monotonic() > deadline:
                finish = FinishReason.TIMEOUT.value
                break
            char = self.sample(context, temperature=heat, rng=rng)
            out.append(char)
            context = (context + char)[-int(self.config.context_chars):]
            self.tokens += 1
            if on_token is not None:
                if on_token(char) is False:
                    finish = FinishReason.CANCELLED.value
                    break
            if stops and _ends_with_stop("".join(out), stops):
                finish = FinishReason.STOP.value
                break
        else:
            finish = FinishReason.LENGTH.value
        self.generations += 1
        return "".join(out), finish


def _ends_with_stop(text: str, stops: Sequence[str]) -> bool:
    return any(text.endswith(item) for item in stops)


# ---------------------------------------------------------------------------
# Artifact container (no pickle, no code, bounded)
# ---------------------------------------------------------------------------


def _pack_matrix(rows: Sequence[Sequence[float]]) -> bytes:
    flat: List[float] = []
    for row in rows:
        flat.extend(float(value) for value in row)
    return struct.pack("<%df" % len(flat), *flat)


def _unpack_matrix(blob: bytes, rows: int, cols: int) -> List[List[float]]:
    expected = rows * cols
    if len(blob) < expected * 4:
        raise ReferenceBackendError(
            "artifact is truncated: expected %d floats, found %d bytes"
            % (expected, len(blob)))
    flat = struct.unpack("<%df" % expected, blob[:expected * 4])
    return [list(flat[start:start + cols])
            for start in range(0, expected, cols)]


class ReferenceArtifactWriter:
    """Materialise a reference artifact on explicit request.

    The repository ships **no** weights. Nothing is downloaded: the writer
    fills the configured shape with deterministic pseudo-random values from
    ``seed`` and writes the container to ``path``.
    """

    @staticmethod
    def write(path: str, config: Optional[ReferenceModelConfig] = None,
              *, vocab: Sequence[str] = DEFAULT_VOCAB,
              overwrite: bool = False) -> Dict[str, Any]:
        config = (config or ReferenceModelConfig()).validate()
        vocabulary = list(vocab)[:int(config.vocab_size)]
        if len(vocabulary) < 2:
            raise ReferenceBackendError("a vocabulary needs at least 2 tokens")
        config = ReferenceModelConfig(
            vocab_size=len(vocabulary), hidden_size=config.hidden_size,
            context_chars=config.context_chars, seed=config.seed,
            temperature=config.temperature, name=config.name)
        destination = os.path.abspath(str(path))
        if os.path.exists(destination) and not overwrite:
            raise ReferenceBackendError(
                "refusing to overwrite existing artifact %s" % destination)
        parent = os.path.dirname(destination)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        rng = random.Random(int(config.seed))
        inputs = int(config.context_chars) * int(config.vocab_size)
        hidden = int(config.hidden_size)
        scale_1 = 1.0 / math.sqrt(max(1, inputs))
        scale_2 = 1.0 / math.sqrt(max(1, hidden))
        weights_1 = [[rng.gauss(0.0, scale_1) for _ in range(inputs)]
                     for _ in range(hidden)]
        bias_1 = [0.0 for _ in range(hidden)]
        weights_2 = [[rng.gauss(0.0, scale_2) for _ in range(hidden)]
                     for _ in range(int(config.vocab_size))]
        bias_2 = [0.0 for _ in range(int(config.vocab_size))]

        vocab_blob = json.dumps(vocabulary, ensure_ascii=True).encode("ascii")
        payload = b"".join((
            _pack_matrix(weights_1),
            _pack_matrix([bias_1]),
            _pack_matrix(weights_2),
            _pack_matrix([bias_2]),
        ))
        header = _HEADER.pack(_MAGIC, _FORMAT_VERSION, len(vocab_blob),
                              int(config.vocab_size), hidden,
                              int(config.context_chars))
        body = header + vocab_blob + struct.pack("<I", len(payload)) + payload
        tmp = destination + ".part-%d" % os.getpid()
        with open(tmp, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, destination)
        return {
            "path": destination,
            "size_bytes": len(body),
            "fingerprint": hashlib.sha256(body).hexdigest(),
            "config": config.to_dict(),
            "trained": False,
            "note": ("deterministic pseudo-random weights; this artifact is a "
                     "path-verification model, not a capable assistant"),
        }


def describe_reference_artifact(path: str, *,
                                fingerprint: bool = True) -> Dict[str, Any]:
    """Read the header (bounded) and report what the bytes actually say."""
    info: Dict[str, Any] = {"format": "forgeref", "path": path,
                            "header_ok": False}
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        info["detail"] = "unreadable: %s" % (exc,)
        return info
    info["size_bytes"] = size
    if size > _MAX_ARTIFACT_BYTES:
        info["detail"] = "artifact exceeds the %d byte bound" % _MAX_ARTIFACT_BYTES
        return info
    try:
        with open(path, "rb") as handle:
            header = handle.read(_HEADER_SIZE)
            if len(header) < _HEADER_SIZE or header[:8] != _MAGIC:
                info["detail"] = "not a FORGEREF artifact"
                return info
            (_magic, version, vocab_len, vocab_size, hidden,
             context_chars) = _HEADER.unpack(header[:_HEADER_SIZE])
            if version != _FORMAT_VERSION:
                info["detail"] = "unsupported format version %d" % version
                return info
            if not (2 <= vocab_size <= _MAX_VOCAB
                    and 1 <= hidden <= _MAX_HIDDEN
                    and 1 <= context_chars <= _MAX_CONTEXT
                    and 0 < vocab_len <= 8192):
                info["detail"] = "implausible dimensions in header"
                return info
            vocabulary = json.loads(handle.read(vocab_len).decode("ascii"))
            info.update({
                "header_ok": True,
                "version": int(version),
                "vocab_size": int(vocab_size),
                "hidden_size": int(hidden),
                "context_chars": int(context_chars),
                "vocab_tokens": len(vocabulary) if isinstance(vocabulary, list)
                else 0,
            })
            if fingerprint:
                handle.seek(0)
                digest = hashlib.sha256()
                while True:
                    block = handle.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                info["fingerprint"] = digest.hexdigest()
    except (OSError, ValueError, struct.error, UnicodeDecodeError) as exc:
        info["detail"] = "unparsable: %s" % (exc,)
    return info


def _load_reference(path: str) -> Any:
    """Load weights from an artifact. Refuses anything implausible."""
    description = describe_reference_artifact(path)
    if not description.get("header_ok"):
        raise ReferenceBackendError(
            "not a usable reference artifact: %s"
            % description.get("detail", "unknown"))
    with open(path, "rb") as handle:
        header = handle.read(_HEADER_SIZE)
        if len(header) < _HEADER_SIZE:
            raise ReferenceBackendError("artifact header is truncated")
        (_magic, _version, vocab_len, vocab_size, hidden,
         context_chars) = _HEADER.unpack(header)
        vocabulary = json.loads(handle.read(vocab_len).decode("ascii"))
        (payload_len,) = struct.unpack("<I", handle.read(4))
        if payload_len > _MAX_ARTIFACT_BYTES:
            raise ReferenceBackendError("artifact payload is too large")
        payload = handle.read(payload_len)
    if not isinstance(vocabulary, list) or len(vocabulary) != vocab_size:
        raise ReferenceBackendError("vocabulary does not match the header")
    inputs = int(context_chars) * int(vocab_size)
    offset = 0
    size_1 = hidden * inputs * 4
    weights_1 = _unpack_matrix(payload[offset:offset + size_1], hidden, inputs)
    offset += size_1
    size_b1 = hidden * 4
    bias_1 = _unpack_matrix(payload[offset:offset + size_b1], 1, hidden)[0]
    offset += size_b1
    size_2 = vocab_size * hidden * 4
    weights_2 = _unpack_matrix(payload[offset:offset + size_2], vocab_size,
                               hidden)
    offset += size_2
    size_b2 = vocab_size * 4
    bias_2 = _unpack_matrix(payload[offset:offset + size_b2], 1, vocab_size)[0]
    config = ReferenceModelConfig(vocab_size=vocab_size, hidden_size=hidden,
                                  context_chars=context_chars)
    model = ReferenceModel(
        config=config, vocab=vocabulary, weights_1=weights_1, bias_1=bias_1,
        weights_2=weights_2, bias_2=bias_2, source=path,
        fingerprint=str(description.get("fingerprint") or ""),
        size_bytes=int(description.get("size_bytes") or 0))
    return model


class ReferenceLocalBackend(ModelBackend):
    """A real local backend over the reference artifact format.

    Register it with a :class:`~forge.runtime.model_runtime.ModelRuntime`::

        runtime.register_backend(ReferenceLocalBackend(model_dirs=(d,)))

    Every operation is real: discovery reads artifacts, loading parses weights
    into memory, generation runs a forward pass per token. When no artifact is
    present the backend reports itself unavailable and generation fails — it
    never fabricates output.
    """

    kind = BackendKind.CUSTOM.value
    description = ("Forge reference local inference engine: real weights, "
                   "real forward pass, tiny character-level model.")
    local = True
    requires_network = False

    def __init__(self, model_dirs: Sequence[str] = (), *,
                 name: str = "reference",
                 max_resident_bytes: int = 64 * 1024 * 1024,
                 default_max_chars: int = 256,
                 allow_create: bool = False,
                 create_config: Optional[ReferenceModelConfig] = None) -> None:
        self.name = name
        self.model_dirs = tuple(str(item) for item in (model_dirs or ()))
        self.max_resident_bytes = int(max_resident_bytes or 0)
        self.default_max_chars = int(default_max_chars or 256)
        #: Explicit-only artifact creation (never implicit, never a download).
        self.allow_create = bool(allow_create)
        self.create_config = create_config or ReferenceModelConfig()
        self._loaded: Dict[str, Any] = {}
        self._paths: Dict[str, str] = {}
        self._resident = 0
        self._generations = 0
        self._failures = 0
        self._timeouts = 0
        self._cancellations = 0
        self._last_error = ""
        self._lock = threading.RLock()

    # -- availability ----------------------------------------------------

    def available(self) -> Tuple[bool, str]:
        if not self.model_dirs:
            return (False,
                    "no reference model directory is configured; nothing is "
                    "loaded implicitly and no weights ship with Forge")
        found = self._artifacts()
        if not found:
            return (False,
                    "no %s artifact found in %s (create one explicitly with "
                    "`forge models create-reference-artifact`)"
                    % (REFERENCE_EXTENSION, ", ".join(self.model_dirs)))
        return (True, "%d reference artifact(s) in %s"
                % (len(found), ", ".join(self.model_dirs)))

    # -- discovery -------------------------------------------------------

    def _iter_dirs(self) -> Iterator[str]:
        for entry in self.model_dirs:
            if entry and os.path.isdir(entry):
                yield entry

    def _artifacts(self) -> List[str]:
        found: List[str] = []
        for root in self._iter_dirs():
            real_root = os.path.realpath(root)
            for depth, (_dirpath, dirnames, filenames) in enumerate(
                    os.walk(real_root)):
                if depth >= _MAX_SCAN_DEPTH:
                    dirnames[:] = []
                for filename in sorted(filenames):
                    if not filename.lower().endswith(REFERENCE_EXTENSION):
                        continue
                    candidate = os.path.join(_dirpath, filename)
                    if os.path.islink(candidate):
                        continue  # never follow a symlink out of the root
                    if not os.path.realpath(candidate).startswith(
                            real_root + os.sep):
                        continue
                    try:
                        if os.path.getsize(candidate) > _MAX_ARTIFACT_BYTES:
                            continue
                    except OSError:
                        continue
                    found.append(candidate)
                    if len(found) >= _MAX_SCAN_FILES:
                        return found
                dirnames[:] = sorted(dirnames)
        return found

    def list_models(self) -> List[RuntimeModel]:
        models: List[RuntimeModel] = []
        for path in self._artifacts():
            description = describe_reference_artifact(path, fingerprint=True)
            if not description.get("header_ok"):
                continue
            name = os.path.splitext(os.path.basename(path))[0]
            models.append(RuntimeModel(
                model_id=RuntimeModel.make_id(self.name, name),
                name=name, backend=self.name, kind="text",
                capabilities=(),          # honest: no advertised capability
                # A character-level model's window is in characters; report
                # the token-equivalent so a token budget is not misread.
                context_window=max(
                    1, int(description.get("context_chars") or 0)
                    // _CHARS_PER_TOKEN),
                max_output_tokens=_MAX_OUTPUT_CHARS,
                size_bytes=int(description.get("size_bytes") or 0),
                format="forgeref", quantization="f32",
                parameters=str(description.get("parameter_count") or ""),
                local=True, loaded=name in self._loaded or
                RuntimeModel.make_id(self.name, name) in self._loaded,
                path=path, discovered_at=time.time(),
                metadata={
                    "family": "forge-reference-clm",
                    "version": str(description.get("version") or ""),
                    "fingerprint": description.get("fingerprint") or "",
                    "vocab_size": description.get("vocab_size") or 0,
                    "hidden_size": description.get("hidden_size") or 0,
                    "context_chars": description.get("context_chars") or 0,
                    "parameter_count": _parameter_count(description),
                    "trained": False,
                    "reference_engine": True,
                    "source": "reference-artifact",
                    "note": ("tiny character-level reference model; proves "
                             "the inference path, not capability"),
                }))
        models.sort(key=lambda model: model.name)
        return models

    # -- load / unload ---------------------------------------------------

    @property
    def resident_bytes(self) -> int:
        return int(self._resident)

    def load_model(self, model: RuntimeModel,
                   token: Optional[CancellationToken] = None
                   ) -> RuntimeModel:
        model_id = model.model_id or RuntimeModel.make_id(self.name, model.name)
        with self._lock:
            if model_id in self._loaded:
                loaded = self._loaded[model_id]
                entry = _entry_for(model, loaded, self.name)
                entry.loaded = True
                return entry
        path = model.path or self._path_for(model.name)
        if not path or not os.path.isfile(path):
            raise BackendUnavailableError(
                "no reference artifact for model %r" % (model.name or model_id,))
        if token is not None:
            token.raise_if_cancelled()
        network = _load_reference(path)
        size = int(network.size_bytes or os.path.getsize(path))
        with self._lock:
            if self.max_resident_bytes and \
                    self._resident + size > self.max_resident_bytes:
                raise ReferenceBackendError(
                    "loading %s would exceed the %d byte residency budget "
                    "(%d held)" % (model_id, self.max_resident_bytes,
                                  self._resident))
            self._loaded[model_id] = network
            self._paths[model_id] = path
            self._resident += size
        entry = _entry_for(model, network, self.name)
        entry.loaded = True
        entry.path = path
        return entry

    def unload_model(self, model_id: str) -> bool:
        with self._lock:
            network = self._loaded.pop(model_id, None)
            self._paths.pop(model_id, None)
            if network is None:
                bare = model_id.split(":", 1)[-1]
                for key in list(self._loaded):
                    if key.split(":", 1)[-1] == bare:
                        network = self._loaded.pop(key)
                        self._paths.pop(key, None)
                        break
            if network is None:
                return False
            self._resident = max(0, self._resident
                                 - int(network.size_bytes or 0))
            return True

    def loaded_models(self) -> List[RuntimeModel]:
        with self._lock:
            return [RuntimeModel(model_id=key, name=key.split(":", 1)[-1],
                                 backend=self.name, kind="text",
                                 format="forgeref", local=True, loaded=True,
                                 path=self._paths.get(key, ""),
                                 size_bytes=int(
                                     getattr(value, "size_bytes", 0) or 0))
                    for key, value in sorted(self._loaded.items())]

    def _path_for(self, name: str) -> str:
        for path in self._artifacts():
            if os.path.splitext(os.path.basename(path))[0] == name:
                return path
        return ""

    # -- inference -------------------------------------------------------

    def generate(self, request: RuntimeRequest,
                 token: Optional[CancellationToken] = None
                 ) -> RuntimeResponse:
        started = time.perf_counter()
        network, model_id = self._resolve(request)
        if token is not None:
            token.raise_if_cancelled()
        prompt = request.compose_prompt() or (request.prompt or "")
        max_chars = self._output_bound(request)
        timeout = request.timeout
        deadline = (time.monotonic() + float(timeout)) if timeout else None
        # Enforce the model's real context limit: only the last
        # ``context_chars`` characters can influence the next token.
        context_limit = int(network.config.context_chars)
        text, finish = network.generate(
            prompt, max_chars=max_chars, temperature=request.temperature,
            stop=request.stop, seed=request.seed, token=token,
            deadline=deadline)
        latency = (time.perf_counter() - started) * 1000.0
        with self._lock:
            self._generations += 1
            if finish == FinishReason.TIMEOUT.value:
                self._timeouts += 1
            elif finish == FinishReason.CANCELLED.value:
                self._cancellations += 1
        if finish == FinishReason.TIMEOUT.value:
            return RuntimeResponse(
                text=text, model=model_id, backend=self.name, success=False,
                error="reference generation exceeded its %.1fs bound"
                      % float(timeout or 0.0),
                error_kind=ErrorKind.TIMEOUT.value, timed_out=True,
                request_id=request.request_id, trace_id=request.trace_id,
                latency_ms=latency, finish_reason=finish,
                output_tokens=len(text),
                metadata={"context_limit": context_limit})
        if finish == FinishReason.CANCELLED.value:
            return RuntimeResponse(
                text=text, model=model_id, backend=self.name, success=False,
                error="reference generation cancelled",
                error_kind=ErrorKind.CANCELLED.value, cancelled=True,
                request_id=request.request_id, trace_id=request.trace_id,
                latency_ms=latency, finish_reason=finish,
                output_tokens=len(text),
                metadata={"context_limit": context_limit})
        return RuntimeResponse(
            text=text, model=model_id, backend=self.name, success=True,
            request_id=request.request_id, trace_id=request.trace_id,
            input_tokens=len(prompt), output_tokens=len(text),
            latency_ms=latency, finish_reason=finish,
            metadata={
                "engine": "forge-reference",
                "trained": False,
                "context_limit": context_limit,
                "fingerprint": network.fingerprint,
                "parameters": network.config.parameter_count(),
            })

    def stream(self, request: RuntimeRequest,
               token: Optional[CancellationToken] = None) -> Iterator[Any]:
        """Yield characters *as they are generated*.

        The forward pass runs on a worker thread and hands each character to a
        small bounded queue, so the first delta arrives before the last one is
        computed (a real time-to-first-token) and a slow consumer applies real
        backpressure. Cancelling the token, abandoning the generator or passing
        the deadline all stop the producer; the terminal chunk always reports
        how the stream actually ended.
        """
        network, model_id = self._resolve(request)
        prompt = request.compose_prompt() or (request.prompt or "")
        max_chars = self._output_bound(request)
        timeout = request.timeout
        deadline = (time.monotonic() + float(timeout)) if timeout else None
        pending: "queue.Queue" = queue.Queue(maxsize=_STREAM_BUFFER)
        sentinel = object()
        stopped = threading.Event()
        outcome: Dict[str, Any] = {"text": "", "finish": "", "error": ""}

        def _put(item: Any) -> bool:
            while not stopped.is_set():
                try:
                    pending.put(item, timeout=0.05)
                    return True
                except queue.Full:
                    continue
            return False

        def _emit(char: str) -> bool:
            if stopped.is_set():
                return False
            if token is not None and token.cancelled:
                return False
            if deadline is not None and time.monotonic() > deadline:
                return False
            return _put(char)

        def _run() -> None:
            try:
                text, finish = network.generate(
                    prompt, max_chars=max_chars,
                    temperature=request.temperature, stop=request.stop,
                    seed=request.seed, token=token, deadline=deadline,
                    on_token=_emit)
                outcome["text"] = text
                outcome["finish"] = finish
            except Exception as exc:  # reported through the terminal chunk
                outcome["error"] = redact_text(str(exc))[:200]
                outcome["finish"] = FinishReason.ERROR.value \
                    if hasattr(FinishReason, "ERROR") else "failed"
            finally:
                _put(sentinel)

        worker = threading.Thread(target=_run,
                                  name="forge-reference-stream", daemon=True)
        worker.start()
        index = 0
        try:
            while True:
                try:
                    item = pending.get(timeout=_STREAM_CHUNK_TIMEOUT)
                except queue.Empty:
                    if not worker.is_alive() and pending.empty():
                        break
                    if stopped.is_set():
                        break
                    if token is not None and token.cancelled:
                        break
                    if deadline is not None and time.monotonic() > deadline:
                        break
                    continue
                if item is sentinel:
                    break
                index += 1
                yield RuntimeChunk(text=str(item), index=index,
                                   request_id=request.request_id,
                                   output_tokens=index)
        finally:
            #: Abandoning the generator must stop the producer, not leak it.
            stopped.set()
            while not pending.empty():
                try:
                    pending.get_nowait()
                except queue.Empty:
                    break
            worker.join(timeout=1.0)
        finish = str(outcome.get("finish") or "")
        if not finish:
            finish = (FinishReason.CANCELLED.value
                      if (token is not None and token.cancelled)
                      else FinishReason.STOP.value)
        yield RuntimeChunk(text="", index=index + 1, done=True,
                           request_id=request.request_id,
                           output_tokens=len(str(outcome.get("text") or "")),
                           finish_reason=finish,
                           metadata={"model_id": model_id,
                                     "error": str(outcome.get("error") or "")})

    def _resolve(self, request: RuntimeRequest) -> Tuple[Any, str]:
        wanted = (request.model or "").strip()
        if wanted:
            model_id = wanted if ":" in wanted else \
                RuntimeModel.make_id(self.name, wanted)
        else:
            with self._lock:
                if len(self._loaded) == 1:
                    model_id = sorted(self._loaded)[0]
                else:
                    models = self.list_models()
                    if not models:
                        raise self._unavailable()
                    model_id = models[0].model_id
        with self._lock:
            network = self._loaded.get(model_id)
        if network is None:
            # Real engines load on demand; the residency budget still applies.
            try:
                self.load_model(RuntimeModel(model_id=model_id,
                                             name=model_id.split(":", 1)[-1],
                                             backend=self.name))
            except Exception as exc:
                raise BackendUnavailableError(
                    "reference model %r is not loaded and could not be "
                    "loaded: %s" % (model_id, redact_text(str(exc))[:200]))
            with self._lock:
                network = self._loaded.get(model_id)
        if network is None:
            raise self._unavailable()
        return network, model_id

    def _unavailable(self) -> BackendUnavailableError:
        ok, detail = self.available()
        self._last_error = detail
        return BackendUnavailableError(
            "the reference backend cannot run inference: %s" % detail)

    def _output_bound(self, request: RuntimeRequest) -> int:
        requested = request.max_output_tokens
        if requested is None:
            return self.default_max_chars
        return max(1, min(int(requested), _MAX_OUTPUT_CHARS))

    # -- ops -------------------------------------------------------------

    def health(self, probe: bool = True) -> RuntimeHealth:
        ok, detail = self.available()
        health = RuntimeHealth(
            backend=self.name, kind=self.kind,
            status=(RuntimeState.READY.value if ok
                    else RuntimeState.UNAVAILABLE.value),
            checked_at=time.time(), detail=detail,
            error="" if ok else detail, probed=bool(probe),
            requires_network=False, network_allowed=False)
        with self._lock:
            health.generations = self._generations
            health.failures = self._failures
            health.timeouts = self._timeouts
            health.cancellations = self._cancellations
            health.models_loaded = len(self._loaded)
        if ok:
            health.models_available = len(self._artifacts())
        return health

    def resources(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "engine": "forge-reference",
                "resident_bytes": int(self._resident),
                "max_resident_bytes": int(self.max_resident_bytes),
                "models_loaded": len(self._loaded),
                "generations": self._generations,
                "timeouts": self._timeouts,
                "cancellations": self._cancellations,
                "model_dirs": list(self.model_dirs),
                "trained": False,
            }

    # -- explicit artifact creation --------------------------------------

    def create_artifact(self, directory: str, *,
                        name: str = "reference-clm",
                        config: Optional[ReferenceModelConfig] = None,
                        overwrite: bool = False) -> Dict[str, Any]:
        """Create a reference artifact. Explicit only; never a download."""
        if not self.allow_create:
            raise BackendProtocolError(
                "artifact creation is disabled for this backend; pass "
                "allow_create=True (or use the CLI) to request it explicitly")
        if directory not in tuple(self.model_dirs):
            raise BackendProtocolError(
                "refusing to write outside the configured model directories")
        chosen = config or self.create_config
        path = os.path.join(directory, "%s%s" % (name, REFERENCE_EXTENSION))
        return ReferenceArtifactWriter.write(path, chosen,
                                             overwrite=overwrite)


def _parameter_count(description: Dict[str, Any]) -> int:
    try:
        config = ReferenceModelConfig(
            vocab_size=int(description.get("vocab_size") or 0),
            hidden_size=int(description.get("hidden_size") or 0),
            context_chars=int(description.get("context_chars") or 0))
        config.validate()
        return config.parameter_count()
    except (ReferenceBackendError, ValueError):
        return 0


def _entry_for(model: RuntimeModel, network: Any, backend_name: str
               ) -> RuntimeModel:
    return RuntimeModel(
        model_id=model.model_id or RuntimeModel.make_id(backend_name,
                                                        model.name),
        name=model.name or network.config.name, backend=backend_name,
        kind="text", capabilities=(),
        context_window=max(1, int(network.config.context_chars)
                           // _CHARS_PER_TOKEN),
        max_output_tokens=_MAX_OUTPUT_CHARS,
        size_bytes=int(network.size_bytes or 0), format="forgeref",
        quantization="f32",
        parameters=str(network.config.parameter_count()),
        local=True, loaded=True, path=model.path or network.source,
        discovered_at=time.time(),
        metadata={
            "family": "forge-reference-clm",
            "fingerprint": network.fingerprint,
            "vocab_size": int(network.config.vocab_size),
            "hidden_size": int(network.config.hidden_size),
            "context_chars": int(network.config.context_chars),
            "parameter_count": int(network.config.parameter_count()),
            "trained": False,
            "reference_engine": True,
        })
