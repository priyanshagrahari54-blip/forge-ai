# Forge AI

Forge AI is an autonomous software-engineering foundation: a modular,
provider-independent system that plans work, selects capability-based
agents, builds deterministic repository context, executes durable task
pipelines, recovers from interruptions, measures its own performance,
and audits the tools it runs.

Design goals:

- **Modular and deterministic** — every subsystem is a small, testable
  component with typed inputs and outputs.
- **Provider-independent** — no mandatory paid model service. Agents run
  through local, deterministic executors behind a common contract.
- **Repository-aware** — repository intelligence (symbols, dependencies,
  tests, architecture, runtime detection) feeds bounded context packs
  to every agent.
- **Durable and recoverable** — tasks persist to SQLite; interrupted and
  failed tasks are recovered with bounded retries.
- **Secure by default** — a permission manager gates tool access, tool
  results are scanned for and redact secrets, and a redacted audit trail
  records every operation.

## Installation

Requires Python 3.11+.

```bash
pip install -e .
```

Development dependencies:

```bash
pip install -e .[dev]
```

## Usage

```bash
forge status        # show project state
forge analyze       # print a repository intelligence report
forge plan "<task>" # print a deterministic plan for a task
```

## Test suite

```bash
python -m pytest -q
python -m compileall forge
```

## Architecture

| Area | Modules | Responsibility |
| --- | --- | --- |
| Core | `forge/core/` | Task engine, durable store/queue, recovery, coordinator, supervisor, pipeline, dependency graph |
| Agents | `forge/agents/` | Registry, capability selector, requirement extraction, planner, validator, stage executors, coder/tester/debugger/reviewer/researcher |
| Intelligence | `forge/intelligence/` | Scanner, symbol index, test mapping, architecture, dependency analysis, runtime detection, context query/budget/pack |
| Runtime | `forge/runtime/`, `forge/tools/` | Permission-checked tool execution (filesystem, terminal, search, git) |
| Security | `forge/security/` | Permissions, secret scanning/redaction, audit log |
| Memory | `forge/memory/` | File-backed long-term memory store |
| Performance | `forge/performance/` | Execution metrics recorder |
| Models | `forge/models/` | Provider-independent model routing |

### Core flow

1. A `Task` is added to the durable `PersistentTaskQueue`.
2. `TaskRequirementExtractor` derives required capabilities from the
   task description.
3. `AgentSelector` chooses a registered agent per capability; the
   `CapabilityAgentPlanner` produces a deterministic `AgentPlan`.
4. `AgentPlanValidator` rejects invalid plans before execution.
5. `AgentPipeline` executes the plan; each planned agent is wrapped in a
   `ExecutorStageAgent`, receives an `AgentContext` (query, dependency,
   test, budget-filtered, fingerprinted), and returns a structured
   `AgentResponse`.
6. `TaskExecutionCoordinator` persists every state change, records
   metrics, and leaves failed/interrupted tasks to `TaskRecoveryEngine`.

### Security model

- Every tool maps to a permission level (`safe`, `approval_required`,
  `blocked`); unknown operations are never allowed implicitly.
- Tool outputs are passed through a `SecretRedactor` before returning.
- `AuditLog` records every tool invocation with a redacted message and
  bounded in-memory retention.

## Progress

Implementation stages are tracked in `.forge/autobuild/PROGRESS.md`.

## License

Forge AI is free-first. See the repository license for details.