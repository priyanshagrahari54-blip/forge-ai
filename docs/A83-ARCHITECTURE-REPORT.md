# A83 — Forge AI Architecture Report (pre-implementation)

Status: **inspection complete, written before any code changed.**
Baseline measured on branch `arena/01a0a292-forge-ai` @ `96e9161`.

Every claim below was produced by reading the tree or running a command in
it. Where a claim is a measurement, the command is named.

---

## 1. What Forge AI actually is today

| Layer | Where it lives | Verdict |
|---|---|---|
| CLI | `forge/cli.py` (2 831 lines, 61 subcommands) | **Works.** Real commands, real gates. |
| Backend API | `forge/api/` (33 route modules) + `forge/api/app.py` | **Works.** FastAPI, bounded bodies, CSP, rate limiting. |
| Cockpit UI | `forge/cockpit/web/{index.html,app.js,handsfree.js,styles.css}` | **Works.** Voice + palette + nav. |
| Control plane | `forge/control/control_plane.py` (6 993 lines) | **Works but monolithic.** Single file owns projects, runs, events, approvals, orchestration. |
| Forge Server | `forge/server/` (19 modules, SQLite) | **Works.** Auth, scopes, queue, workers, recovery. |
| Model layer | `forge/models/` (20 modules) + `forge/runtime/model_runtime.py` (3 994 lines) | **Works.** Registry, providers, fabric, telemetry, native GGUF runtime. |
| Agents | `forge/agents/` (26 modules) | **Works.** Registry, capability planner, coder/debugger/tester/reviewer, teams, skills, governance. |
| Repo intelligence | `forge/intelligence/` (21 modules) | **Works, Python-only.** Symbols, dependency graph, test mapping, runtime detection, budgeted context packs. |
| Memory | `forge/memory/` (8 modules) | **Works.** Typed records, retention, redaction, SQLite-backed. |
| Security | `forge/security/` (11 modules) | **Strong.** Policy, approvals, audit, SSRF, classification, policy gate. |
| Tools | `forge/tools/` (12 modules) | **Works.** filesystem, terminal, git, change applier, checkpoint, browser, network, search. |
| Tests | `tests/` — 246 files, 47 278 lines | **Green: `2582 passed, 3 skipped in 330.97s`** (`python -m pytest -q`). |

### Verified measurements

```
$ find forge -name '*.py' | xargs wc -l | tail -1
  69957 total
$ find tests -name '*.py' | xargs wc -l | tail -1
  47278 total
$ python -m pytest -q -p no:cacheprovider
2582 passed, 3 skipped in 330.97s (0:05:30)
```

---

## 2. What already works and must be preserved

These are load-bearing. A83 adds to them; it does not replace them.

1. **Permission & approval platform (A33).** `PermissionPolicy` /
   `PermissionRequest` / `ApprovalStore.enforce_with_token`. Every mutation
   boundary in the repo routes through it. Any new engine that writes files or
   runs commands must pass the same gate.
2. **Supervisor transaction (A32).** `forge/core/supervisor.py` — plan → code
   → test → review with real gates and rollback. `forge/final/` gates sit on
   top.
3. **Model Fabric (A46).** `ModelRegistry` + `FabricRouter` + `RoutingPolicy`
   + `Telemetry` + `RouterFeedback`, with a capability vocabulary in
   `forge/models/capabilities.py` and a documented fallback ladder in
   `forge/models/policy.py`.
4. **Repository intelligence (A32).** `RepositoryIntelligence.build()` composes
   symbol index, dependency graph, architecture map, test mapping, runtime
   detection. `AgentContextBuilder` produces budgeted, fingerprinted context.
5. **Long-term memory (A81).** `forge/memory/engine.py` with `MemoryType`
   (session/task/project/failure/decision/agent/model_performance), retention,
   dedupe by fingerprint, redaction.
6. **Honesty discipline.** The repo's house style: real subprocesses, real exit
   codes, explicit `simulation=True` flags (`forge/collaboration/connectors.py`),
   "no tests collected" reported as such (`forge/agents/tester.py`). A83 keeps
   this contract — no fabricated capability claims.
7. **Portability floor.** Python 3.8 is a hard target, enforced in CI by
   `vermin -t=3.8-`. **Every new module must stay 3.8-clean** (no `X | Y` at
   runtime outside annotations, no `match`, no `tomllib`, no `str.removeprefix`
   reliance without a guard).

---

## 3. Gaps found (each verified, not assumed)

### G1 — No build engine at all
`grep -rni "cargo\|cmake\|makefile\|gradle\|meson\|bazel" forge/ --include=*.py`
returns **zero hits**. The only build-ish surface is `forge/agents/tester.py`,
which hard-codes `python -m pytest`, and `forge/compute/engine.py`, which runs
`sys.executable -I -c` cells. Nothing can build C/C++, Rust, Java, Android,
kernel, or system software.

### G2 — Testing is Python-only and single-stage
`forge/agents/tester.py` runs one command and reports exit code + tail. There is
no static-analysis stage, no unit/integration/system/regression/performance
separation, and no structured per-test results (only `exit_code`, `verdict`,
`no_tests`).

### G3 — No repository intelligence beyond Python, no call graph, no semantic search
* `forge/intelligence/symbols.py` uses `forge/intelligence/python_parser.py`
  (AST) — Python only.
* `grep -rni "call.graph\|callgraph" forge/` → **zero hits.**
* `forge/memory/relevance.py` states outright: *"Retrieval is deterministic and
  dependency-free: no embeddings and no model"*. There is no semantic code
  search anywhere.
* No configuration discovery, no API/route discovery.
* `RepositoryIntelligence` is rebuilt from scratch on every call — no persisted
  index, no invalidation.

### G4 — Project memory is generic, not engineering knowledge
`MemoryType` has 7 kinds. None of them model architecture decisions, constraints,
known bugs, failed approaches, benchmarks, project conventions, or testing
requirements as first-class, queryable structures.

### G5 — `.forge/project.yaml` is dead configuration
```
$ grep -rn "project\.yaml\|project\.yml\|forge/project" --include=*.py .
(no matches)
```
The file exists in the repo and is referenced by `.forge/audit/FRESH_AUDIT.md`
as documentation, but **no Python code reads it**. Project type, testing
enabled, git enabled, and `require_approval_for_writes` are all ignored.

### G6 — No project profiles / plugins with real behavior
`forge/plugins/` (A66) is deliberately declarative-only: `manifest.py` says
*"strict validation, no code loading"* and `bind_capabilities` only reports
which declared capabilities have a real provider. There is no mechanism for a
project (like ZEROOS) to contribute domain knowledge, build recipes, test
recipes, or boot procedures. Nothing in the repo mentions `qemu`, `hardware`,
or OS/kernel development.

### G7 — Planning is a fixed template
`forge/core/planner.py` `_PLAN_TEMPLATE` is five hard-coded strings
("Understand the requirements", "Inspect the existing project", …).
`forge/agents/specs.py` and `forge/agents/requirements.py` extract capabilities
but produce no architecture, no component list, no dependency set, no test
plan, no benchmark plan, no release plan, and nothing is editable or approved
before execution.

### G8 — Agent roster is not an engineering organisation
Registered roles are `planning/coding/testing/debugging/review/security/
documentation` (`forge/core/agent_pipeline.py` `STAGE_ROLES`). There is no
architect, research, performance, build, release, dependency, hardware, or
devops agent. Agents do not share a structured project context object: each
request rebuilds `AgentContext` from `RepositoryIntelligence`, i.e. the repo is
rediscovered per agent.

### G9 — Model routing does not route by engineering task, and does not learn from measurements
* Routing is by **capability** (`coding`, `reasoning`, …) + hard bounds. There
  is no mapping from an engineering task ("analyze this 400-file repo",
  "generate a 600-line module", "diagnose this backtrace") to a model class
  (lightweight / coding / reasoning / long-context / heavy-remote).
* `RouterFeedback` carries `latency_ms`, `input_tokens`, `output_tokens`,
  `success`, `complexity` — but there is **no aggregate store** that turns that
  history into routing signal. `ModelRouter.record()` (legacy) does
  exponential smoothing on the model object; `FabricRouter` does not consume
  feedback at all. Cost is a static `cost_per_token`, never measured. Task
  quality is never recorded.
* Two routers coexist (`ModelRouter` legacy + `FabricRouter`) — duplication
  that must not grow.

### G10 — No performance lab
`forge/performance/profiler.py` derives timings from run-record timestamps
only. `forge/benchmark/harness.py` benchmarks **models**, not code. Nothing
measures CPU, RAM, disk I/O, network, startup time, throughput, or frame time,
and nothing compares before/after.

### G11 — No hardware awareness, no VM testing
Zero hits for hardware probing or QEMU. `forge/desktop/` is desktop
automation (input, screenshots), not hardware engineering.

### G12 — Debug loop exists but is bound to pytest + the Coder agent
`forge/agents/debugger.py` has real `FailureReport`/`DebugAttempt`/`DebugLoopResult`
structures and bounded retries, but it only handles Python test failures and
cannot consume build errors, static-analysis findings, or VM boot failures.

---

## 4. Architectural weaknesses

| # | Weakness | Evidence | Consequence |
|---|---|---|---|
| W1 | `control_plane.py` is 6 993 lines | `wc -l` | Every new capability lands in the same file; high regression risk. |
| W2 | Two control planes (`forge/control/` for cockpit, `forge/server/` for the server) | `forge/server/server.py` docstring: *"Mirrors the A34 ControlPlane semantics"* | Behaviour drift between the two entry points. |
| W3 | Two routers | `forge/models/router.py` holds both `ModelRouter` and `FabricRouter` | Feedback applied in one, ignored by the other. |
| W4 | Intelligence is Python-specific but not named as such | `python_parser.py` is the only parser | Silent blindness on any non-Python project. |
| W5 | No shared project-context object across agents | `AgentPipeline` passes `AgentContext` per request | Repeated repo discovery, no cumulative knowledge. |
| W6 | Dead project config | G5 | Users configure Forge via a file Forge ignores. |
| W7 | Full suite is 5m30s | measured above | Slow feedback for the platform's own development. |

## 5. Performance bottlenecks

1. **`RepositoryIntelligence.build()` re-walks and re-parses the whole tree**
   on every construction (symbol index, dependency index, architecture, test
   mapping, runtime detection). No cache, no invalidation by mtime/hash.
2. **`MemoryStore` default root is a relative path** (`.forge/memory`) —
   resolution depends on cwd.
3. **Telemetry is an unbounded in-memory list** until `flush()`.
4. **`ModelRouter.record()`** appends to `self.history` without a bound.

## 6. Security risks (existing, worth noting; A83 must not add to them)

* The repo is already strong (SSRF guard, classification, audit, approvals,
  fail-closed policy gate). The new risk surface A83 introduces is **command
  execution in build/test/VM engines** — mitigated by an allowlisted argv
  builder, no shell, cwd confinement, timeouts, and the existing approval gate
  for anything mutating.
* `CSP` on the cockpit deliberately omits `frame-ancestors` (documented in
  `forge/api/app.py`) for proxied previews.

## 7. Duplicated functionality

| Duplicate | Files | Decision |
|---|---|---|
| Routers | `models/router.py` (`ModelRouter`, `FabricRouter`) | Keep both; new stats feed `FabricRouter` only. Document legacy. |
| Control planes | `control/control_plane.py`, `server/*` | Do not merge in A83 (too risky). New engines stay plane-agnostic. |
| Memory stores | `memory/store.py` (files) + `memory/engine.py` (SQLite) | Keep; new knowledge layer sits on the engine. |
| Test running | `agents/tester.py` vs new test engine | Tester stays as the agent; the new engine becomes the thing it can call. |
| Context building | `intelligence/agent_context.py` vs new project context | New context *wraps* `AgentContext`, does not replace it. |

---

## 8. Decision: what A83 does

**Preserve** — everything in §2. No existing module is rewritten; new
capabilities are additive packages with new tests. The only edits to existing
files are: registering new API routes, adding CLI subcommands, and adding
`README` docs.

**Refactor** — only W6 (make `.forge/project.yaml` actually load, as the seed
for project profiles) and the unbounded-growth issues in §5 for the new code.

**Add** — the A83 packages in `docs/A83-PLATFORM-UPGRADE.md`:

```
forge/profiles/      project profiles as plugins (+ ZEROOS profile)   §12
forge/architect/     requirement → spec → architecture → plans        §2
forge/engineering/   14-agent roster + shared project context         §3
forge/models/taskmap.py + stats.py   task-kind routing from measurements §4
forge/intelligence/{call_graph,semantic,discovery,index_store}.py     §5
forge/knowledge/     structured, searchable project knowledge         §6
forge/build/         universal build orchestration                    §7
forge/testing/       staged test pipeline, structured results         §8
forge/debug/         bounded automated debugging loop + fix ledger    §9
forge/perf/          performance lab: measure, compare, gate          §10
forge/hardware/      real hardware probing + support matrix           §11
forge/vm/            QEMU boot + automated VM testing                 §13
```

**Honesty contract for all of it** (mirrors the repo's existing discipline):

* Nothing is reported as working that was not executed.
* Detection reports *evidence* (the file/line or sysfs path it found).
* Absence is reported as `UNKNOWN`, never as `UNSUPPORTED`, and never as
  `SUPPORTED`.
* Every engine that shells out: no shell interpolation, allowlisted argv,
  real timeouts, real exit codes, bounded captured output.
* "Faster" is only ever emitted by `forge/perf` from a measured before/after.
