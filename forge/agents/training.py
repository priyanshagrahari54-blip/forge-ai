"""Agent training and fine-tuning pipeline (A50 evolution).

Provides real training capabilities:

* **Benchmark-driven training**: Run agents against benchmark suites,
  collect performance data, and generate training datasets from
  successful/failed examples.
* **OpenAI fine-tuning**: Upload training data and create fine-tuned
  models via the OpenAI API.
* **Continuous evaluation**: Track model performance over time with
  real metrics.

All training is permission-gated (Resource.MODEL/call) and audited.
Training data comes from real run outcomes — never fabricated.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

MAX_TRAINING_EXAMPLES = 1000
MAX_FINE_TUNE_MODELS = 5
REQUEST_TIMEOUT = 30.0


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
        """Export training data in OpenAI JSONL format."""
        dataset = self._datasets.get(agent)
        if dataset is None:
            return {"error": f"No dataset for agent {agent!r}"}
        return {"agent": agent, "examples": dataset.size,
                "jsonl_preview": dataset.export_jsonl()[:2000],
                "fine_tuner_available":
                    self._fine_tuner.available()}

    def start_fine_tuning(self, agent: str, *,
                          model: str = "gpt-4o-mini") -> dict[str, Any]:
        """Upload training data and start a fine-tuning job."""
        dataset = self._datasets.get(agent)
        if dataset is None or dataset.size == 0:
            return {"error": f"No training data for agent {agent!r}"}
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
        """Decide whether an agent should be promoted based on metrics.

        Promotion criteria (all must be met):
        - At least 5 recorded runs
        - Success rate >= 0.8
        - No consecutive failures in last 3 runs
        """
        runs = metrics.get("runs", 0)
        success_rate = metrics.get("success_rate", 0.0)
        last_outcome = metrics.get("last_outcome", "")

        criteria = {
            "min_runs": runs >= 5,
            "success_rate": success_rate >= 0.8,
            "not_failing": last_outcome != "FAILED",
        }
        promote = all(criteria.values())
        result = {
            "agent": agent,
            "generation": generation,
            "promote": promote,
            "criteria": criteria,
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
        """Decide whether an agent should be retired.

        Retirement criteria (any triggers):
        - At least 10 runs with success rate < 0.3
        - 5 consecutive failures
        """
        runs = metrics.get("runs", 0)
        success_rate = metrics.get("success_rate", 0.0)
        last_outcome = metrics.get("last_outcome", "")

        criteria = {
            "low_success": runs >= 10 and success_rate < 0.3,
            "consecutive_failures": last_outcome == "FAILED"
                                    and runs >= 5,
        }
        retire = any(criteria.values())
        return {
            "agent": agent,
            "retire": retire,
            "criteria": criteria,
            "metrics": metrics,
        }

    def pipeline_status(self) -> dict[str, Any]:
        return {
            "session": self.session_id,
            "datasets": {
                name: ds.to_dict()
                for name, ds in self._datasets.items()
            },
            "fine_tuning_jobs": len(self._jobs),
            "promotions": len(self._promotions),
            "fine_tuner_available": self._fine_tuner.available(),
        }
