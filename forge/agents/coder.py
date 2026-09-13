from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from forge.models.fabric import ModelFabric

from forge.agents.execution import AgentExecutor, AgentRequest, AgentResponse
from forge.intelligence.agent_context import AgentContext, AgentContextBuilder
from forge.intelligence.repository import RepositoryIntelligence
from forge.models.readiness import (
    check_fabric_readiness,
    describe_no_model_error,
    fabric_has_real_model,
    is_fallback_response,
)
from forge.models.router import ModelRouter
from forge.runtime.defaults import create_default_runtime
from forge.runtime.runtime import ToolResult, ToolRuntime
from forge.core.run_control import TaskCancelled
from forge.security.classification import classify_text
from forge.security.permissions import PermissionManager
from forge.tools.change_applier import (
    ApprovalCallback,
    ChangeApplier,
    CodeChange,
)


_SECRET_PATTERNS = (
    re.compile(r"(?:api[_-]?key|secret|password|token)\s*[:=]\s*['\"][^'\"]{8,}", re.I),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
_MAX_FILE_BYTES = 2 * 1024 * 1024

#: Response-level risk labels the coder schema accepts (A32.3). Anything else
#: is malformed model output and rejects the whole response.
RISK_LEVELS = frozenset({"NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL"})

#: Marker fragments identifying the offline placeholder's refusal payload.
_FALLBACK_EXPLANATION_MARKERS = (
    "no safe local synthesis engine is configured",
)


def _strip_code_fences(text: str) -> str:
    """Remove Markdown code fences while preserving the inner payload."""
    cleaned = text.strip()
    if "```" in cleaned:
        cleaned = re.sub(r"```(?:json)?", "", cleaned).replace("```", "").strip()
    return cleaned


def _balanced_json_candidates(text: str) -> list[str]:
    """Yield candidate JSON objects from text with balanced braces.

    Small/local models often wrap the required JSON object in explanatory
    prose ("Here is the change: {...} hope this helps"). Candidates are the
    balanced ``{...}`` spans, longest first, so extraction prefers the most
    complete object. String literals and escapes are honored while scanning.
    """
    spans: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append(text[start:index + 1])
                start = -1
    # Longest first: prefer the most complete object.
    spans.sort(key=len, reverse=True)
    return spans


def _remove_trailing_commas(payload: str) -> str:
    """Drop ``,`` before ``}``/``]`` outside string literals (one repair pass)."""
    out: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(payload):
        char = payload[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == ",":
            rest = payload[index + 1:].lstrip()
            if rest.startswith(("}", "]")):
                index += 1
                continue
        out.append(char)
        index += 1
    return "".join(out)


def loads_model_json(text: str) -> tuple[dict, bool]:
    """Parse model output into a JSON object, tolerating common wrapping.

    Returns ``(data, extracted)`` where ``extracted`` is True when the payload
    was recovered from surrounding prose or repaired (trailing commas),
    rather than parsing verbatim. Raises ``ValueError`` naming the failure
    when nothing parses; callers surface the message to operators.
    """
    cleaned = _strip_code_fences(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        data = None
    else:
        if isinstance(data, dict):
            return data, False
        raise ValueError("Model response must be a JSON object")
    for candidate in _balanced_json_candidates(cleaned):
        for attempt in (candidate, _remove_trailing_commas(candidate)):
            try:
                data = json.loads(attempt)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data, True
    # Last resort: a single trailing-comma repair over the whole payload.
    try:
        data = json.loads(_remove_trailing_commas(cleaned))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Model returned invalid JSON: {exc}. "
            "The model must return ONLY the JSON change object."
        ) from exc
    if isinstance(data, dict):
        return data, True
    raise ValueError("Model response must be a JSON object")


class CoderAgent(AgentExecutor):
    """Implement software changes through the Model Fabric.

    PRIMARY (production): ``CoderAgent → Model Fabric → provider``. The
    fabric is the authoritative routing layer (capability, policy, health,
    telemetry, failover).

    LEGACY COMPATIBILITY: ``CoderAgent → legacy ModelRouter → provider``.
    The ``router=`` argument is preserved verbatim for pre-existing callers
    and tests; it is compatibility infrastructure, not a second production
    routing algorithm. New callers must use ``fabric=`` (the default when
    neither is supplied).
    """

    name = "coder"

    def __init__(self, runtime: ToolRuntime | None = None, root: str = ".", router: ModelRouter | None = None,
                 fabric: "ModelFabric | None" = None, approval_store=None,
                 model_policy=None,
                 approval_callback: ApprovalCallback | None = None,
                 commit_guard: Callable[[], str] | None = None):
        #: Execution-fence guard for this coder's writes (Session 10).
        #: A per-request ``request.guard`` wins over this instance-level
        #: default; both default to ``None`` (unfenced, unchanged
        #: behavior). When set, a fenced attempt's writes are refused
        #: before they touch the filesystem.
        self.commit_guard = commit_guard
        # Routing input priority: explicit fabric > explicit legacy router >
        # default fabric. The fabric is canonical; the legacy router argument
        # is a compatibility adapter preserved verbatim so pre-existing
        # integrations keep their exact behavior.
        if fabric is not None:
            self.fabric = fabric
            self.router = None
        elif router is not None:
            self.fabric = None
            self.router = router
        else:
            from forge.models.fabric import ModelFabric
            self.fabric = ModelFabric.from_defaults()
            self.router = None
        self.root = str(Path(root).resolve())
        self.runtime = runtime or create_default_runtime(PermissionManager(), self.root)
        # Every model-produced write goes through the controlled change-application
        # layer (path/content/secret validation + permissioned ToolRuntime).
        # The repository root enables old-state guard verification.
        self.applier = ChangeApplier(self.runtime, root=self.root,
                                     approval_store=approval_store,
                                     approval_callback=approval_callback)
        #: Interactive approval hook (A34); ``None`` keeps A32 behavior.
        self.approval_callback = approval_callback
        #: Optional model data policy, enforced by the fabric per request.
        self.model_policy = model_policy
        #: Policy decisions from the most recent apply (observability).
        self.last_decisions: list = []
        #: Task grant from the most recent apply, when task-scoped.
        self.last_task_grant: dict | None = None

    def describe(self) -> str:
        return "Responsible for implementing software changes using a routed model."

    def build_context(self, intelligence: RepositoryIntelligence, task: str,
                      target_files: tuple[str, ...] = (), target_symbols: tuple[str, ...] = (),
                      max_tokens: int = 4000) -> AgentContext:
        return AgentContextBuilder(intelligence, max_tokens).build(task, target_files, target_symbols)

    def write_file(self, path: str, content: str, approved: bool = False) -> ToolResult:
        return self.runtime.execute("write_file", approved=approved, path=path, content=content)

    def _prompt(self, request: AgentRequest) -> str:
        context_parts = []
        for item in (request.context.items if request.context else []):
            # Defense in depth: context files must stay inside the repository.
            if not self._is_repo_relative(item.path):
                continue
            try:
                content = (Path(self.root) / item.path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                content = "<unavailable>"
            context_parts.append(f"FILE: {item.path}\n{content}")
        context = "\n\n".join(context_parts)
        return (
            "You are an autonomous coding agent. Implement the task while preserving architecture. "
            "Return ONLY JSON with either "
            "{changes:{relative/path:str file contents}, explanation:str} or "
            "{summary:str, changes:[{path:str, action:create|modify, content:str, "
            "risk:NONE|LOW|MEDIUM|HIGH|CRITICAL, old_hash:str, old_content:str}], "
            "tests_to_run:[relative/path], reasoning_summary:str, "
            "risk_level:NONE|LOW|MEDIUM|HIGH|CRITICAL, risks:[str]}. "
            "Per-change risk/old_hash/old_content are optional guards. "
            "Never edit outside the repository, never emit secrets, and never include hidden reasoning.\n"
            f"TASK: {request.task.description}\nCONTEXT:\n{context}\nTOOLS/INSTRUCTIONS:{request.instructions}"
        )

    @staticmethod
    def _is_repo_relative(path: str) -> bool:
        candidate = PurePosixPath(path)
        return bool(path) and not candidate.is_absolute() and ".." not in candidate.parts

    def _parse_changes(self, text: str) -> tuple[dict[str, str], dict]:
        """Parse a model response into validated changes plus structured summary.

        Accepts the legacy mapping schema ``{"changes": {path: content}}`` and
        the richer list schema ``{"changes": [{path, action, content, ...}],
        ...}``. List entries may carry optional ``risk``, ``old_hash``, and
        ``old_content`` guards, threaded into the ChangeSet engine. Deletions
        are rejected: autonomous runs never delete files implicitly.
        """
        data, extracted = loads_model_json(text)
        if extracted:
            # Recovery is observable: operators can see the model needed help.
            pass

        summary = data.get("summary", "")
        reasoning = data.get("reasoning_summary", "")
        risks = data.get("risks", [])
        # ``tests_to_run`` is the canonical A32 field; ``tests`` stays as a
        # backward-compatible alias carrying the same list.
        tests = data.get("tests_to_run", data.get("tests", []))
        risk_level = data.get("risk_level", "NONE")
        if not isinstance(risk_level, str) or risk_level.upper() not in RISK_LEVELS:
            raise ValueError(
                f"Model risk_level must be one of {sorted(RISK_LEVELS)}")
        risk_level = risk_level.upper()

        changes_raw = data.get("changes")
        validated: dict[str, str] = {}
        change_meta: dict[str, dict[str, str | None]] = {}
        if isinstance(changes_raw, dict):
            if not all(isinstance(path, str) and isinstance(content, str)
                       for path, content in changes_raw.items()):
                raise ValueError("Every model change must map a path to string file contents")
            for path, content in changes_raw.items():
                self._validate_change(path, content)
                validated[path] = content
                change_meta[path] = {
                    "risk": risk_level, "expected_old_hash": None,
                    "expected_old_content": None,
                }
        elif isinstance(changes_raw, list):
            for item in changes_raw:
                if not isinstance(item, dict):
                    raise ValueError("Every model change entry must be an object")
                path = item.get("path")
                content = item.get("content")
                action = item.get("action", "modify")
                if not isinstance(path, str) or not isinstance(content, str):
                    raise ValueError("Every model change entry must have string path and content")
                if action not in ("create", "modify"):
                    raise ValueError(f"Unsupported change action for {path!r}: {action!r}")
                self._validate_change(path, content)
                item_risk = item.get("risk", risk_level)
                if not isinstance(item_risk, str) or item_risk.upper() not in RISK_LEVELS:
                    raise ValueError(
                        f"Change risk for {path!r} must be one of {sorted(RISK_LEVELS)}")
                old_hash = item.get("old_hash")
                old_content = item.get("old_content")
                if old_hash is not None and not isinstance(old_hash, str):
                    raise ValueError(f"Change old_hash for {path!r} must be a string")
                if old_content is not None and not isinstance(old_content, str):
                    raise ValueError(f"Change old_content for {path!r} must be a string")
                validated[path] = content
                change_meta[path] = {
                    "risk": item_risk.upper(), "expected_old_hash": old_hash,
                    "expected_old_content": old_content,
                }
        else:
            raise ValueError("Model response must contain a changes mapping or list")

        tests_list = [str(t) for t in tests] if isinstance(tests, list) else []
        extra = {
            "summary": summary if isinstance(summary, str) else "",
            "reasoning_summary": reasoning if isinstance(reasoning, str) else "",
            "risks": [str(r) for r in risks] if isinstance(risks, list) else [],
            "tests": tests_list,
            "tests_to_run": list(tests_list),
            "risk_level": risk_level,
            "change_meta": change_meta,
            # True when the JSON was recovered from surrounding prose or a
            # trailing-comma repair instead of parsing verbatim. Operators
            # can see the model needed help; validation after recovery is
            # identical to the verbatim path.
            "extracted_from_prose": extracted,
        }
        return validated, extra

    def _validate_change(self, path: str, content: str) -> None:
        self._validate_path(path)
        self._validate_content(path, content)
        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_FILE_BYTES:
            raise ValueError(f"Model file exceeds {_MAX_FILE_BYTES} bytes: {path}")
        if any(pattern.search(content) for pattern in _SECRET_PATTERNS):
            raise ValueError(f"Model response appears to contain a secret: {path}")

    def _changes(self, text: str) -> dict[str, str]:
        validated, _extra = self._parse_changes(text)
        return validated

    def _apply_changes(self, changes: dict[str, str], approved: bool,
                       extra: dict | None = None, task_id: str = "",
                       approval_token_id: str = "",
                       commit_guard: Callable[[], str] | None = None) -> tuple[list[str], list[str]]:
        """Apply validated changes through the controlled change-application layer.

        Per-change risk and old-state guards parsed from the model response
        travel with each entry so the ChangeSet engine and the policy gate see
        exactly what the model proposed. ``extra`` also carries
        ``change_meta`` for observability; it is never a caller-supplied
        change shortcut (``request.metadata["changes"]`` is ignored).
        """
        meta = (extra or {}).get("change_meta", {})
        # Per-request execution fence wins over the instance-level guard
        # (Session 10): a fenced attempt cannot write, even if the coder
        # was constructed with a live guard.
        effective_guard = commit_guard if commit_guard is not None \
            else self.commit_guard
        result = self.applier.apply(
            [
                CodeChange(
                    path=path,
                    content=content,
                    risk=meta.get(path, {}).get("risk", "NONE"),
                    expected_old_hash=meta.get(path, {}).get("expected_old_hash"),
                    expected_old_content=meta.get(path, {}).get("expected_old_content"),
                )
                for path, content in changes.items()
            ],
            approved=approved,
            label="coder",
            capability="coding",
            actor=self.name,
            task_id=task_id,
            approval_token_id=approval_token_id,
            commit_guard=effective_guard,
        )
        self.last_decisions = list(result.decisions)
        self.last_task_grant = result.task_grant
        return result.changed_paths, result.errors

    @staticmethod
    def _validate_content(path: str, content: str) -> None:
        """Reject syntactically invalid Python before it can be written.

        ``compile`` parses without executing, so untrusted model output is
        syntax-checked safely. Non-Python files are validated structurally only.
        """
        if not path.endswith(".py"):
            return
        try:
            compile(content, path, "exec")
        except SyntaxError as exc:
            raise ValueError(f"Model produced invalid Python for {path}: {exc}") from exc

    @staticmethod
    def _validate_path(path: str) -> None:
        candidate = PurePosixPath(path)
        if not path or candidate.is_absolute() or re.match(r"^[A-Za-z]:[\\/]", path):
            raise ValueError(f"Model path must be relative: {path!r}")
        if ".." in candidate.parts or ".git" in candidate.parts or ".forge" in candidate.parts:
            raise ValueError(f"Model path is outside the permitted source tree: {path!r}")
        if "\\" in path:
            raise ValueError(f"Model path must use repository-relative POSIX separators: {path!r}")

    def _fabric_failure_error(self, response) -> str:
        """Explain a fabric-level failure, diagnosing a missing model first."""
        try:
            if self.fabric is not None and not fabric_has_real_model(self.fabric):
                return describe_no_model_error(fabric=self.fabric)
        except Exception:
            pass
        return (response.error
                or "No available coding model provider; configure Ollama or another provider")

    def _fallback_refusal_error(self, response) -> str | None:
        """Return an actionable error when the placeholder refused synthesis.

        Returns ``None`` when the empty change set came from a *real* model
        (which keeps the legacy ``"Model proposed no changes"`` message).
        Detection is twofold: the response is attributed to a fallback
        model/provider, or the payload carries the placeholder's refusal
        marker. A short live probe distinguishes "Ollama down" from "model
        not pulled" so the remediation names the actual broken link.
        """
        provider = str(getattr(response, "provider", "") or "")
        model = str(getattr(response, "model", "") or "")
        text = str(getattr(response, "text", "") or "").lower()
        try:
            attributed = bool(self.fabric is not None and is_fallback_response(
                self.fabric, model, provider))
        except Exception:
            attributed = False
        marked = any(marker in text for marker in _FALLBACK_EXPLANATION_MARKERS)
        if not (attributed or marked):
            return None
        try:
            report = check_fabric_readiness(
                self.fabric, probe_network=True, timeout=3.0)
        except Exception:
            report = None
        return describe_no_model_error(report)

    def _repair_parse_once(self, model_request, raw_text: str,
                           parse_error: ValueError) -> tuple[dict[str, str], dict] | None:
        """Re-prompt once after unparseable model output; None when hopeless."""
        from forge.models.request import ModelRequest

        if self.fabric is None:
            return None
        snippet = raw_text.strip().replace("\n", " ")[:500]
        repair = ModelRequest(
            prompt=(
                "Your previous response could not be parsed as JSON "
                f"({parse_error}). Return ONLY the JSON change object, with no "
                "prose before or after it, no Markdown fences, and no "
                "trailing commas. Previous output for reference: " + snippet
            ),
            capability="coding",
            required_capabilities=("coding",),
            context=model_request.context,
            task=model_request.task,
            prefer_local=True,
            prefer_free=True,
            max_output_tokens=4000,
        )
        try:
            response = self.fabric.generate(repair)
        except Exception:
            return None
        if not response.success:
            return None
        try:
            return self._parse_changes(response.text)
        except ValueError:
            return None

    def _execute_via_fabric(self, request: AgentRequest) -> AgentResponse:
        """Code through the centralized Model Fabric.

        The fabric routes, calls the provider, records telemetry/feedback, and
        returns a structured response. Every write still goes through the
        permissioned ToolRuntime and the same structural/path/secret validation
        as the legacy path; model output is never trusted or written directly.
        """
        from forge.models.request import ModelRequest

        prompt_text = self._prompt(request)
        model_request = ModelRequest(
            prompt=prompt_text,
            capability="coding",
            required_capabilities=("coding",),
            context=str(request.context) if request.context else "",
            task=request.task.description,
            min_context_window=request.context.estimated_tokens if request.context else 0,
            prefer_local=True,
            prefer_free=True,
            metadata={"model_data_policy": self.model_policy}
            if self.model_policy is not None else {},
        )
        response = self.fabric.generate(model_request)
        if not response.success:
            return AgentResponse(
                False,
                error=self._fabric_failure_error(response),
                agent=self.name,
                stage=request.stage,
            )
        try:
            try:
                changes, extra = self._parse_changes(response.text)
            except ValueError as parse_error:
                # Bounded recovery: small/local models sometimes emit
                # prose-wrapped or slightly malformed JSON. One repair
                # re-prompt is attempted; a second failure is reported
                # honestly with the original parse error intact.
                repaired = self._repair_parse_once(
                    model_request, response.text, parse_error)
                if repaired is None:
                    raise
                changes, extra = repaired
                extra["repair_attempted"] = True
            if not changes:
                refusal = self._fallback_refusal_error(response)
                if refusal is not None:
                    return AgentResponse(
                        False, error=refusal, agent=self.name,
                        stage=request.stage,
                        metadata={"files": [], "fallback_refusal": True,
                                  "model": response.model,
                                  "provider": response.provider})
                raise ValueError("Model proposed no changes")
            approved = bool(request.metadata.get("approved", False))
            token_id = str(request.metadata.get("approval_token_id", "") or "")
            extra["classification"] = classify_text(prompt_text).value
            applied, errors = self._apply_changes(
                changes, approved, extra, task_id=request.task.id,
                approval_token_id=token_id,
                commit_guard=getattr(request, "guard", None))
            if errors:
                return AgentResponse(False, error=errors[0], agent=self.name,
                                     stage=request.stage, metadata={"files": applied})
            return AgentResponse(True, output=response.text, agent=self.name, stage=request.stage,
                                 metadata={"files": applied, "model": response.model,
                                           "provider": response.provider, "routing": "fabric",
                                           **extra})
        except TaskCancelled:
            raise
        except Exception as exc:
            return AgentResponse(False, error=str(exc), agent=self.name, stage=request.stage,
                                 metadata={"files": []})

    def execute(self, request: AgentRequest) -> AgentResponse:
        if self.fabric is not None:
            return self._execute_via_fabric(request)
        model = None
        try:
            model = self.router.select("coding", context_size=request.context.estimated_tokens if request.context else 0)
            if not model or not model.provider:
                return AgentResponse(False, error="No available coding model provider; configure Ollama or another provider", agent=self.name, stage=request.stage)
            try:
                prompt_text = self._prompt(request)
                result = model.provider.generate(prompt_text, context=str(request.context), task=request.task.description)
            except Exception:
                self.router.record(model.name, False, None, capability="coding", task_complexity=1.0)
                raise
            changes, extra = self._parse_changes(result.text)
            if not changes:
                provider_name = ""
                try:
                    provider_name = str(getattr(getattr(model, "provider", None), "name", "") or "")
                except Exception:
                    provider_name = ""
                if provider_name.strip().lower() == "local":
                    return AgentResponse(
                        False, error=describe_no_model_error(),
                        agent=self.name, stage=request.stage,
                        metadata={"files": [], "fallback_refusal": True})
                raise ValueError("Model proposed no changes")
            approved = bool(request.metadata.get("approved", False))
            token_id = str(request.metadata.get("approval_token_id", "") or "")
            extra["classification"] = classify_text(prompt_text).value
            applied, errors = self._apply_changes(
                changes, approved, extra, task_id=request.task.id,
                approval_token_id=token_id,
                commit_guard=getattr(request, "guard", None))
            if errors:
                return AgentResponse(False, error=errors[0], agent=self.name,
                                     stage=request.stage, metadata={"files": applied})
            self.router.record(model.name, True, result.latency, capability="coding", task_complexity=1.0)
            return AgentResponse(True, output=result.text, agent=self.name, stage=request.stage,
                                 metadata={"files": applied, "model": model.name,
                                           "routing": "legacy-router", **extra})
        except TaskCancelled:
            raise
        except Exception as exc:
            return AgentResponse(False, error=str(exc), agent=self.name, stage=request.stage,
                                 metadata={"files": []})
