# Forge AI Architecture

This document describes the concrete modules, contracts, and invariants
of Forge AI as implemented in `forge/`.

## Core (`forge/core/`)

- `task_engine.py` — `Task`, `TaskStatus`, `TaskEngine`. Lifecycle:
  `pending -> running -> completed | failed`, plus `planning`,
  `researching`, `coding`, `testing`, `debugging`, `reviewing`,
  `recovery`. Enforces dependency completion before start.
- `task_store.py` — SQLite persistence (`tasks` table) with `save`,
  `load`, `load_all`, `delete`, `clear`.
- `task_queue.py` — `PersistentTaskQueue`: priority-ordered ready-task
  selection backed by `TaskStore`.
- `task_recovery.py` — `RecoveryPolicy` (max attempts, retry failed) and
  `TaskRecoveryEngine` moving interrupted `running` tasks to `recovery`
  and retryable `failed` tasks back to `pending`.
- `task_coordinator.py` — `TaskExecutionCoordinator` orchestrates queue,
  recovery, agent execution, pipeline execution, and metrics recording.
- `agent_pipeline.py` — `AgentPipeline` with a standard four-stage
  `execute()` and a capability-driven `execute_plan()`; validates plans
  before execution and records per-stage metrics.
- `dependency_graph.py` / `planner.py` / `state.py` / `supervisor.py` /
  `pipeline_agents.py` / `agent_executor.py` — supporting structures.

## Agents (`forge/agents/`)

- `registry.py` — `AgentRegistration` (name, role, executor,
  capabilities) and `AgentRegistry` (register/replace/get/by-role/
  by-capability/capabilities).
- `requirements.py` — deterministic `TaskRequirementExtractor` mapping
  task text to capabilities and roles.
- `selector.py` — `AgentSelector` scoring candidates by capability and
  role; deterministic order.
- `planner.py` — `CapabilityAgentPlanner` producing `AgentPlan` of
  `PlannedAgent`s (capability + registration + order).
- `validator.py` — `AgentPlanValidator` rejecting unknown agents,
  capability/role mismatch, duplicate agents, and unmapped stages.
  Empty plans are valid no-ops.
- `execution.py` — `AgentRequest`/`AgentResponse` and the common
  `AgentExecutor` contract plus `CallableAgentExecutor`.
- `stage_executor.py` — `ExecutorStageAgent` adapting any
  `AgentExecutor` to a pipeline `StageAgent`.
- Domain agents: `coder.py`, `tester.py`, `debugger.py`,
  `reviewer.py`, `researcher.py` — metadata + `build_context`, with
  deterministic executors where applicable.

## Intelligence (`forge/intelligence/`)

- Repository scanning, Python parsing, symbol indexing, source-to-test
  mapping, architecture analysis, dependency analysis, runtime
  detection.
- Context selection: relevance scoring, query engine, dependency
  expansion, test selection, symbol selection, token budgeting, and
  `DeterministicContextPack` fingerprinting.
- `agent_context.py` — `AgentContext` (pack + estimated tokens +
  fingerprint) and `AgentContextBuilder` assembling the full chain.

## Runtime and tools (`forge/runtime/`, `forge/tools/`)

- `ToolRuntime.execute` checks permissions, runs the handler, redacts
  secrets from output/error, and records an audit entry.
- Tools: `FileSystemTool` (path-traversal safe), `TerminalTool`
  (bounded subprocess), `SearchTool`, `GitTool` (status/diff helpers).
- `defaults.py` — `create_default_runtime` registers the standard tool
  set plus `scan_secrets`.

## Security (`forge/security/`)

- `permissions.py` — operation -> `PermissionLevel`
  (`safe`/`approval_required`/`blocked`).
- `secrets.py` — `SecretScanner`/`SecretRedactor` with deterministic
  pattern matching and guaranteed redaction for AWS keys, GitHub/Slack
  tokens, Stripe keys, JWTs, private keys, API keys, passwords, and
  connection strings.
- `audit.py` — bounded `AuditLog` with injectable clock and mandatory
  redaction.

## Performance (`forge/performance/`)

- `metrics.py` — `MetricsRecorder` with `MetricRecord` (task ID, stage,
  agent, status, duration, attempts, retries, affected files, model),
  query helpers, bounded retention, and `summary()`.

## Invariants

- Existing tests are regression requirements; changes are additive and
  backward-compatible.
- No secrets are ever written into audit entries or finding snippets;
  redacted tool results are the only outputs an agent can observe.
- Agent plans are deterministic and validated before any agent runs.
- Metrics and audit logs are bounded to keep memory usage predictable.