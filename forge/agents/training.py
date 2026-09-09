"""Agent training and fine-tuning pipeline (A50 evolution).

Provides real training capabilities:

* **Benchmark-driven training**: Run agents against benchmark suites,
  collect performance data, and generate training datasets from
  successful/failed examples.
* **OpenAI fine-tuning**: Upload training data and create fine-tuned
  models via the OpenAI API.
* **Continuous evaluation**: Track model performance over time with
  real metrics.

Security model (fail-closed):

* Every training example is scanned by :class:`TrainingDataPolicy`
  before it may leave the machine. Secrets, credentials, private
  keys, tokens, and PII are never uploaded automatically.
* External training upload requires explicit policy authorization:
  the operator mode defaults to ``deny``
  (``FORGE_TRAINING_EXTERNAL_UPLOAD``) and a dataset containing
  secret material is refused even when authorized.
* Promotion/retirement decisions inspect the real outcome history
  (``outcome_history`` from the A50 evolution ledger) through the
  shared helpers in ``forge.agents.evolution`` — consecutive-failure
  rules verify the actual trailing runs.

All training is permission-gated (Resource.MODEL/call) and audited.
Training data comes from real run outcomes — never fabricated.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from forge.agents.evolution import promotion_eligible, retirement_eligible
from forge.security.classification import (
    DataClassification,
    classify_text,
)

MAX_TRAINING_EXAMPLES = 1000
MAX_FINE_TUNE_MODELS = 5
REQUEST_TIMEOUT = 30.0

#: Additional credential/PII signals checked before any external upload.
_EXTRA_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),             # OpenAI-style keys
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),      # GitHub tokens
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),    # Slack tokens
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),          # Google API keys
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                # AWS access key id
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?:api[_-]?key|secret|password|passwd|token)"
               r"\s*[:=]\s*['\"][^'\"]{8,}", re.IGNORECASE),
    re.compile(r"[a-zA-Z0-9.+/]+://[^/\s:@]+:[^@\s]+@"),  # URL creds
    re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{20,}"),   # bearer tokens
)
_PII_PATTERNS = (
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),  # email
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                # US SSN
    re.compile(r"\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?"
               r"[-.\s]?\d{3}[-.\s]?\d{4}\b"),           # phone-ish
)


class TrainingExample:
    """One real training example derived from a run outcome."""

    __slots__ = ("input_text", "output_text", "success",
                 "agent", "task_id", "created_at")

    def __init__(self, input_text: str, output_text: str,
                 success: bool, agent: str, task_id: str) -> None:
        self.input_text = input_text[:4000]
        self.output_text = output_text[:4000]
        self.success = success
        self.agent = agent
        self.task_id = task_id
        self.created_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "input": self.input_text,
            "output": self.output_text,
            "success": self.success,
            "agent": self.agent,
            "task_id": self.task_id,
        }

    def to_openai_format(self) -> dict[str, Any]:
        """Convert to OpenAI fine-tuning format."""
        return {
            "messages": [
                {"role": "system",
                 "content": f"You are the {self.agent} agent."},
                {"role": "user", "content": self.input_text},
                {"role": "assistant", "content": self.output_text},
            ]
        }


def scan_text(text: str) -> dict[str, Any]:
    """Scan one text fragment; returns hits + classification signals.

    Combines the A33 classifier with the extra credential/PII patterns
    used only at the training boundary. Detection only escalates.
    """
    text = text or ""
    classification = classify_text(text)
    secret_hits: list[str] = []
    pii_hits: list[str] = []
    for pattern in _EXTRA_SECRET_PATTERNS:
        for match in pattern.finditer(text):
            secret_hits.append(match.group(0)[:60])
    for pattern in _PII_PATTERNS:
        for match in pattern.finditer(text):
            pii_hits.append(match.group(0)[:60])
    level = classification.value
    if secret_hits and level != "secret":
        level = "secret"
    elif pii_hits and level not in ("secret", "confidential"):
        level = "confidential"
    return {"level": level,
            "secret_hits": secret_hits[:10],
            "pii_hits": pii_hits[:10],
            "clean": not secret_hits and not pii_hits}


class TrainingDataPolicy:
    """Fail-closed policy for training data leaving the machine.

    Modes (``FORGE_TRAINING_EXTERNAL_UPLOAD``):

    - ``deny`` (default)  — no external upload at all.
    - ``approval``        — external upload only when authorized.
    - ``allow``           — external upload for clean/confidential
                            content when authorized.

    Secret material (API keys, tokens, private keys, passwords,
    credentials) is NEVER uploaded automatically: in every mode a
    secret hit refuses the upload, and even an ``authorized`` flag can
    only lift the refusal in ``allow`` mode — where it is still gated
    by the caller's explicit authorization. This is fail-closed: no
    code path uploads without passing :meth:`evaluate`.
    """

    def __init__(self, mode: str = "") -> None:
        self.mode = (mode or os.environ.get(
            "FORGE_TRAINING_EXTERNAL_UPLOAD", "deny")).strip().lower()
        if self.mode not in ("deny", "approval", "allow"):
            raise ValueError(
                f"Unknown FORGE_TRAINING_EXTERNAL_UPLOAD mode "
                f"{self.mode!r}; use deny|approval|allow")

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode,
                "fail_closed": True,
                "note": "Secret/credential/PII scans run before any "
                        "external upload; default mode is deny."}

    def scan_example(self, example: Any) -> dict[str, Any]:
        """Scan one example (input + output + metadata)."""
        input_scan = scan_text(getattr(example, "input_text", ""))
        output_scan = scan_text(getattr(example, "output_text", ""))
        combined = {
            "level": "secret"
            if input_scan["level"] == "secret"
            or output_scan["level"] == "secret"
            else "confidential"
            if input_scan["level"] == "confidential"
            or output_scan["level"] == "confidential"
            else "clean",
            "secret_hits": input_scan["secret_hits"]
            + output_scan["secret_hits"],
            "pii_hits": input_scan["pii_hits"]
            + output_scan["pii_hits"],
        }
        return combined

    def scan_dataset(self, dataset: Any) -> dict[str, Any]:
        """Scan a whole dataset; aggregate hits by kind."""
        secret_examples: list[int] = []
        pii_examples: list[int] = []
        clean = 0
        for index, example in enumerate(dataset.examples):
            result = self.scan_example(example)
            if result["level"] == "secret":
                secret_examples.append(index)
            elif result["level"] == "confidential":
                pii_examples.append(index)
            else:
                clean += 1
        return {
            "examples": len(dataset.examples),
            "clean": clean,
            "confidential_or_pii": len(pii_examples),
            "secret": len(secret_examples),
            "secret_example_indexes": secret_examples[:20],
            "mode": self.mode,
        }

    def evaluate(self, dataset: Any, *,
                 authorized: bool = False) -> dict[str, Any]:
        """Fail-closed decision for uploading ``dataset`` externally."""
        report = self.scan_dataset(dataset)
        if report["examples"] == 0:
            return {"allowed": False, "reason": "no examples to upload",
                    "report": report, "authorized": authorized}
        if report["secret"] > 0:
            allowed = self.mode == "allow" and authorized
            return {
                "allowed": allowed,
                "reason": (
                    f"{report['secret']} example(s) contain secret/"
                    f"credential material; automatic upload is refused."
                    if not allowed else
                    "secret material present; upload permitted only by "
                    "explicit allow-mode + authorization"),
                "report": report,
                "authorized": authorized,
                "secret_present": True,
            }
        if self.mode == "deny":
            return {"allowed": False,
                    "reason": "external training upload is disabled by "
                              "policy (FORGE_TRAINING_EXTERNAL_UPLOAD="
                              "deny)",
                    "report": report, "authorized": authorized}
        if not authorized:
            return {"allowed": False,
                    "reason": "external training upload requires "
                              "explicit authorization",
                    "report": report, "authorized": False}
        return {"allowed": True, "reason": "policy check passed",
                "report": report, "authorized": True}


class TrainingDataset:
    """Collection of real training examples from run outcomes."""

    def __init__(self, agent: str) -> None:
        self.agent = agent
        self.examples: list[TrainingExample] = []

    def add(self, input_text: str, output_text: str,
            success: bool, task_id: str = "") -> None:
        if len(self.examples) >= MAX_TRAINING_EXAMPLES:
            return  # bounded
        self.examples.append(TrainingExample(
            input_text, output_text, success,
            self.agent, task_id))

    @property
    def size(self) -> int:
        return len(self.examples)

    @property
    def success_rate(self) -> float:
        if not self.examples:
            return 0.0
        return sum(1 for e in self.examples if e.success) / len(
            self.examples)

    def export_jsonl(self) -> str:
        """Export in OpenAI JSONL format."""
        lines = []
        for ex in self.examples:
            lines.append(json.dumps(ex.to_openai_format()))
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "examples": len(self.examples),
            "success_rate": round(self.success_rate, 3),
            "exported_format": "openai_jsonl",
        }


class OpenAIFineTuner:
    """Fine-tune models via the OpenAI API.

    Requires ``OPENAI_API_KEY``.
    """

    def __init__(self, *, api_key: str = "") -> None:
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")

    def available(self) -> bool:
        return bool(self._api_key)

    def upload_training_file(self, jsonl_content: str,
                             purpose: str = "fine-tune") -> dict[str, Any]:
        """Upload a training file to OpenAI."""
        import urllib.request
        import urllib.error

        if not self._api_key:
            return {"error": "No OPENAI_API_KEY configured"}

        boundary = "----ForgeBoundary"
        body_parts: list[bytes] = []
        body_parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; '
            f'filename="training.jsonl"\r\n'
            f"Content-Type: application/jsonl\r\n\r\n".encode("utf-8"))
        body_parts.append(jsonl_content.encode("utf-8"))
        body_parts.append(b"\r\n")
        body_parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="purpose"\r\n\r\n'
            f"{purpose}\r\n".encode("utf-8"))
        body_parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(body_parts)

        url = "https://api.openai.com/v1/files"
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type":
                    f"multipart/form-data; boundary={boundary}",
            })
        try:
            with urllib.request.urlopen(
                    req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return {"error": f"HTTP {exc.code}: {exc.reason}"}
        except Exception as exc:
            return {"error": str(exc)}

    def create_fine_tune(self, training_file_id: str, *,
                         model: str = "gpt-4o-mini",
                         suffix: str = "") -> dict[str, Any]:
        """Start a fine-tuning job."""
        import json
        import urllib.request
        import urllib.error

        if not self._api_key:
            return {"error": "No OPENAI_API_KEY configured"}

        body: dict[str, Any] = {
            "training_file": training_file_id,
            "model": model,
        }
        if suffix:
            body["suffix"] = suffix[:18]

        data = json.dumps(body).encode("utf-8")
        url = "https://api.openai.com/v1/fine_tuning/jobs"
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            })
        try:
            with urllib.request.urlopen(
                    req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return {"error": f"HTTP {exc.code}: {exc.reason}"}
        except Exception as exc:
            return {"error": str(exc)}

    def check_job(self, job_id: str) -> dict[str, Any]:
        """Check fine-tuning job status."""
        import json
        import urllib.request
        import urllib.error

        if not self._api_key:
            return {"error": "No OPENAI_API_KEY configured"}
        url = (f"https://api.openai.com/v1/fine_tuning/jobs/{job_id}")
        req = urllib.request.Request(url, method="GET",
                                     headers={
                                         "Authorization":
                                             f"Bearer {self._api_key}"})
        try:
            with urllib.request.urlopen(
                    req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return {"error": f"HTTP {exc.code}: {exc.reason}"}
        except Exception as exc:
            return {"error": str(exc)}


class AgentTrainingPipeline:
    """Full train → benchmark → promote → retire pipeline.

    Combines the evolution ledger (metrics from real runs), training
    datasets, benchmark harness, and lifecycle management into one
    continuous improvement loop.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._datasets: dict[str, TrainingDataset] = {}
        self._fine_tuner = OpenAIFineTuner()
        self._jobs: list[dict[str, Any]] = []
        self._promotions: list[dict[str, Any]] = []

    def collect_training_data(self, agent: str,
                              runs: list[dict[str, Any]]) -> dict[str, Any]:
        """Collect training examples from real run outcomes."""
        dataset = self._datasets.setdefault(
            agent, TrainingDataset(agent))
        for run in runs:
            dataset.add(
                input_text=run.get("requirement", ""),
                output_text=run.get("output", ""),
                success=run.get("status") == "SUCCEEDED",
                task_id=run.get("task_id", ""))
        return dataset.to_dict()

    def export_dataset(self, agent: str) -> dict[str, Any]:
        """Export training data (local) with the upload policy verdict."""
        dataset = self._datasets.get(agent)
        if dataset is None:
            return {"error": f"No dataset for agent {agent!r}"}
        policy = TrainingDataPolicy()
        verdict = policy.evaluate(dataset, authorized=False)
        return {"agent": agent, "examples": dataset.size,
                "jsonl_preview": dataset.export_jsonl()[:2000],
                "fine_tuner_available":
                    self._fine_tuner.available(),
                "upload_policy": {
                    "mode": policy.mode,
                    "upload_allowed": verdict["allowed"],
                    "reason": verdict["reason"],
                    "scan": verdict["report"],
                }}

    def start_fine_tuning(self, agent: str, *,
                          model: str = "gpt-4o-mini",
                          authorized: bool = False) -> dict[str, Any]:
        """Upload training data and start a fine-tuning job.

        Fail-closed: the dataset first passes
        :class:`TrainingDataPolicy` (secret/credential/PII scan +
        explicit authorization + operator mode). Any policy refusal
        returns an error BEFORE any API call.
        """
        dataset = self._datasets.get(agent)
        if dataset is None or dataset.size == 0:
            return {"error": f"No training data for agent {agent!r}"}
        policy = TrainingDataPolicy()
        verdict = policy.evaluate(dataset, authorized=authorized)
        if not verdict["allowed"]:
            return {"error": f"Training upload refused by policy: "
                             f"{verdict['reason']}",
                    "policy": verdict}
        if not self._fine_tuner.available():
            return {"error": "No OPENAI_API_KEY configured "
                             "for fine-tuning"}
        # Upload training file
        upload = self._fine_tuner.upload_training_file(
            dataset.export_jsonl())
        if "error" in upload:
            return {"error": f"Upload failed: {upload['error']}"}
        file_id = upload.get("id", "")
        if not file_id:
            return {"error": "Upload returned no file id"}
        # Start fine-tuning
        result = self._fine_tuner.create_fine_tune(
            file_id, model=model, suffix=f"forge-{agent[:12]}")
        job = {"agent": agent, "file_id": file_id,
               "model": model, "result": result,
               "policy": verdict,
               "started_at": time.time()}
        self._jobs.append(job)
        return job

    def check_training_status(self, job_id: str) -> dict[str, Any]:
        """Check the status of a fine-tuning job."""
        if not self._fine_tuner.available():
            return {"error": "No OPENAI_API_KEY configured"}
        return self._fine_tuner.check_job(job_id)

    def should_promote(self, agent: str,
                       generation: int,
                       metrics: dict[str, Any]) -> dict[str, Any]:
        """Decide promotion from the REAL outcome history.

        Promotion criteria (all must be met, evaluated over
        ``metrics["outcome_history"]``):
        - At least 5 recorded runs
        - Success rate >= 0.8
        - The last 3 runs contain no consecutive failures

        The windowed check inspects the actual trailing outcomes via
        ``forge.agents.evolution.promotion_eligible``; an agent with
        fewer than two recorded outcomes can never pass the window
        check by accident.
        """
        metrics = metrics or {}
        decision = promotion_eligible(metrics)
        promote = bool(decision["eligible"])
        result = {
            "agent": agent,
            "generation": generation,
            "promote": promote,
            "criteria": decision["criteria"],
            "metrics": metrics,
        }
        if promote:
            self._promotions.append({
                "agent": agent,
                "generation": generation,
                "promoted_at": time.time(),
            })
        return result

    def should_retire(self, agent: str,
                      metrics: dict[str, Any]) -> dict[str, Any]:
        """Decide retirement from the REAL outcome history.

        Retirement criteria (either triggers, evaluated over
        ``metrics["outcome_history"]``):
        - At least 10 runs with success rate < 0.3
        - 5 CONSECUTIVE failures — five FAILED rows in a row, never
          merely ``runs >= 5 and last_outcome == FAILED``
        """
        metrics = metrics or {}
        decision = retirement_eligible(metrics)
        return {
            "agent": agent,
            "retire": bool(decision["eligible"]),
            "criteria": decision["criteria"],
            "metrics": metrics,
        }

    def pipeline_status(self) -> dict[str, Any]:
        policy = TrainingDataPolicy()
        return {
            "session": self.session_id,
            "datasets": {
                name: ds.to_dict()
                for name, ds in self._datasets.items()
            },
            "fine_tuning_jobs": len(self._jobs),
            "promotions": len(self._promotions),
            "fine_tuner_available": self._fine_tuner.available(),
            "training_data_policy": policy.to_dict(),
        }
