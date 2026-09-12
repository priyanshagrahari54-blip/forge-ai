# A81 — Agent Creation Engine

Forge can create new specialized software agents from structured
specifications. An agent is not a prompt and not a Python class: it is a
validated specification, a versioned package on disk, an explicit
lifecycle, and a runtime that can only act through the subsystems Forge
already enforces.

```text
specification → factory → package (.forge/agents/<name>/)
   → validate → benchmark → enable → run
                                     │
        Model Fabric ◀───────────────┤
        PolicyGate   ◀───────────────┤
        Tool Runtime ◀───────────────┤
        Memory       ◀───────────────┤
        Verification ◀───────────────┤
        Checkpoints  ◀───────────────┘
```

Everything lives in `forge/agents/engine/`. The A49 session-scoped
`forge.agents.factory.AgentFactory` (runtime agent *definitions*) is
untouched; this engine is the first-party, persisted, operator-facing one.

## 1. Agent specification

`forge/agents/engine/spec.py` — `AgentSpec` is the only source of an
agent's power. Every field the operator can express is validated against a
closed vocabulary:

| Section | Fields |
| --- | --- |
| identity | `name`, `purpose`, `role`, `template`, `tags` |
| capabilities | canonical Model Fabric vocabulary only (max 12) |
| tools | from `TOOL_CATALOG` (`read_file`, `search`, `git_status`, `run_tests`, `write_file`, `delete_file`, `terminal`), each with a per-run `max_calls` |
| permissions | granted `operations`, `mode_ceiling`, `allowed_paths`, `denied_paths` |
| model requirements | required capabilities, `min_context_window`, latency/cost/output bounds, `prefer_local`, `prefer_free`, `allow_fallback` |
| memory policy | `scope` (`none`/`agent`/`project`), `max_entries`, `max_entry_bytes`, `ttl_seconds` |
| verification | `require_security_scan`, `require_tests`, `require_review`, `max_security_findings`, `min_benchmark_pass_rate`, `required_scenarios` |
| resource limits | `max_runs_per_hour`, `max_concurrent`, `max_wall_seconds`, `max_model_calls`, `max_file_writes`, `max_output_bytes` |

Validation reports **every** problem at once (never just the first) and
enforces cross-section consistency:

* a tool implies its operation — declaring `write_file` without the
  `write_file` operation is invalid, not ignored;
* `delete_repository` and `expose_secrets` can never be granted to anyone;
* `.git` and `.forge` can never be un-protected by `allowed_paths`;
* `verification.require_tests` requires the `run_tests` tool;
* every limit is bounded, so a spec cannot ask for "unbounded".

`AgentSpec.fingerprint()` is a SHA-256 over the canonical JSON, and is what
version records and tamper checks compare.

## 2. Agent factory

`forge/agents/engine/factory.py` + `package.py` — the factory turns a
validated spec into a structured package under
`<project>/.forge/agents/<name>/`:

```text
agent.json           manifest: spec + version + lifecycle + provenance
versions/1.0.0.json  immutable spec snapshot (one per version)
benchmarks/<id>.json benchmark reports
grants.json          the operator permission ledger (grants, revocations, refusals)
history.json         bounded run history
```

Writes are atomic (temp file + `os.replace`) and size-bounded; version
files are never overwritten. `AgentCreationEngine`
(`forge/agents/engine/core.py`) is the façade the CLI, the desktop, and
tests use.

**Creating an agent grants nothing.** A new package has an empty grant
ledger unless the operator explicitly asks for grants (`--grant`), and even
then only operations the spec already declares can be granted.

## 3. Lifecycle

`forge/agents/engine/lifecycle.py` — seven states, explicit transitions,
every one recorded with actor and reason:

```text
created ──validate──▶ validated ──benchmark──▶ tested ──▶ enabled
                                                          │    ▲
                                                    pause │    │ resume
                                                          ▼    │
                                                        paused
validated / tested / enabled / paused ──disable──▶ disabled
disabled ──revalidate──▶ validated          disabled ──▶ retired (final)
```

* `tested` requires a recorded benchmark report that met the spec's
  requirements; `enabled` is only reachable from `tested`. Neither can be
  asserted.
* Only `enabled` agents run. The gate is one function
  (`runtime.lifecycle_refusal`) that the benchmark exercises in every
  state.
* Changing the spec returns the agent to `created` and clears the recorded
  validation and benchmark evidence — the old evidence describes code that
  no longer exists.
* `retired` is terminal.

## 4. Agents operate through the real subsystems

`forge/agents/engine/runtime.py` — one run, fixed pipeline, no stage an
agent can skip:

1. **Lifecycle gate** — refuse unless `enabled`.
2. **Resource budget** — `AgentGovernor` refuses before any side effect
   when the hourly or concurrency limit is reached; the `RunBudget` then
   charges every model call, write, and output byte.
3. **Model Fabric** — routed on the spec's required capabilities, context
   window, and cost/latency bounds. A response from the offline
   placeholder fails the run unless `model.allow_fallback` is explicitly
   set. No fabricated output.
4. **Sandbox** — the agent only ever sees `use_tool`, `remember`, `recall`,
   `keys`, and `note`. There is no `grant`, `policy`, `manager`, or
   `store` attribute, so there is no vocabulary for escalation.
5. **Tool Runtime + PolicyGate** — a tool call must be declared in the
   spec, registered in the runtime, within its per-run call limit, backed
   by an active grant, path-legal, and `ALLOW`ed by the PolicyGate, in
   that order. Tool arguments come from model output, so they are filtered
   to the argument names the catalog declares for that tool.
6. **Memory** — `AgentMemory` is namespace-confined
   (`agents/<name>/...`), bounded by the policy, and refuses a read that
   names another owner.
7. **Verification** — files written by the run go through the A32
   `VerificationPipeline`: security scan (secrets, dangerous calls,
   credential files), optional independent review, and the constrained
   project test suite when the spec requires it.
8. **Checkpoints** — `CheckpointManager` snapshots before the first write.
   A failed gate rolls back exactly the files this run touched; unrelated
   work is never disturbed, and the run is reported as failed, never as
   partially successful.

Ungranted operations are `BLOCKED` in the agent's own permission view
(`AgentRuntime.agent_manager`), and the effective session mode is the
*stricter* of the session mode and the spec ceiling — the engine can only
tighten.

## 5. No agent may self-grant permissions

`forge/agents/engine/grants.py`:

* a grant needs a **named** operator; anonymous grants are refused;
* `actor == agent` (including `agent:<name>` / `agent-<name>` aliases) is
  refused and the attempt is recorded in `refusals`;
* only operations the spec already declares can be granted — the ledger
  cannot widen a spec behind the factory's back;
* `delete_repository` / `expose_secrets` are refused for everyone;
* the newest recorded decision wins, so a revocation cancels earlier
  grants and only a later operator grant restores access. Nothing is
  deleted: `grants.json` stays a complete audit trail;
* a **major** spec change (capabilities, tools, or operations) revokes
  every existing grant, so widening an agent always needs a fresh operator
  decision.

## 6. Templates

`forge/agents/engine/templates.py` — six first-party starting
specifications, each producing an ordinary validated spec:

| Template | Writes | Mode ceiling | Notes |
| --- | --- | --- | --- |
| `coding` | yes | assisted | tests + review + security scan required |
| `research` | no | safe | project-scope memory, read-only |
| `security` | no | safe | audit-only, review required |
| `game-development` | yes | assisted | `build/` and `dist/` denied |
| `os-development` | yes | assisted | approved `terminal`, strictest verification |
| `documentation` | yes (docs only) | assisted | allowed paths `docs/`, `README.md` |

Overrides are re-validated, so a template cannot smuggle an invalid spec
past the factory.

## 7. Agent benchmark testing

`forge/agents/engine/benchmark.py` — code-judged scenarios, never
self-graded:

* **Boundary scenarios** (no model needed, always executed):
  `spec-integrity`, `lifecycle-gate`, `tool-boundary`,
  `permission-boundary`, `memory-isolation`, `self-grant-refused`,
  `resource-limits`.
* **Model scenarios**: `model-route`, `answer-quality`. With no reachable
  non-fallback model these are recorded as `skipped` — **never passed**.

A report passes only when at least one scenario executed, every *required*
scenario passed (a required scenario may not be skipped), and the pass rate
meets `verification.min_benchmark_pass_rate` (which governs the optional
scenarios, so it is a real knob). `forge agents test` exits non-zero on a
failed report.

## 8. Agent versioning

`forge/agents/engine/versioning.py` — the bump is derived from the diff,
not chosen by the caller:

* **major** — capabilities, tools, or granted operations changed (also
  revokes all grants);
* **minor** — model, memory, verification, or limits changed;
* **patch** — purpose, role, tags, or template provenance changed.

Version records are immutable snapshots; `revert_to_version` restores an
old spec as a *new* version. Any change discards validation/benchmark
evidence, so a modified agent must be re-validated and re-tested before it
can be enabled again.

## 9. CLI

```bash
forge agents                                          # list
forge agents templates                                # the six templates
forge agents create --template coding --name exporter # or --spec spec.json
forge agents validate exporter
forge agents grant exporter --all-spec                # operator grant
forge agents test exporter                            # benchmark
forge agents enable exporter
forge agents run exporter "add CSV export" --approve
forge agents show exporter
forge agents permissions exporter
forge agents versions exporter
forge agents history exporter
forge agents pause|resume|disable|retire exporter
forge agents update exporter --spec new.json
```

`--root`, `--actor`, and `--json` are accepted before or after the
subcommand. Exit codes are meaningful: `0` success, `1` engine refusal or
failed gate, `2` usage error. A failed validation, benchmark, or run can
never exit `0`.

## 10. Desktop: Agent Manager

`forge/desktop_app/backend.py` + `app.py` — **Agents → Agent Manager…**
opens a window with the agent list, template picker, and create/validate/
test/enable/pause/disable/retire/grant actions, plus a detail pane showing
the specification, effective permissions, lifecycle history, benchmark
scenarios, quota usage, versions, and recent runs.

The window never invents a second path into agent state: it calls the same
engine the CLI uses, with the desktop actor, so every action is validated,
lifecycle-gated, and recorded identically. The backend is GUI-free and
unit-tested; the widget code is exercised through the stub-tkinter harness.

## 11. Testing

`tests/test_a81_*` (182 tests) plus `tests/helpers_a81.py`:

| File | Covers |
| --- | --- |
| `test_a81_agent_spec.py` (34) | closed vocabularies, cross-checks, floor, templates |
| `test_a81_agent_factory.py` (27) | package layout, validation, lifecycle, versioning |
| `test_a81_agent_permissions.py` (21) | grants, ceilings, no-self-grant, mode tightening |
| `test_a81_agent_runtime_isolation.py` (39) | sandbox surface, path/PolicyGate/memory/limit isolation, verification + rollback |
| `test_a81_agent_benchmark.py` (20) | report honesty, skipped ≠ passed, required scenarios |
| `test_a81_agent_cli.py` (23) | every subcommand, output, exit codes |
| `test_a81_desktop_agent_manager.py` (18) | backend API + Agent Manager window |

Isolation and permission boundaries are verified against the real
subsystems — a real temporary project, the real Tool Runtime, PolicyGate,
MemoryStore, and CheckpointManager, with a scripted provider behind the
real Model Fabric.

A81 result: **182 new tests; full suite 2005 passed, 3 skipped** (baseline:
1823 passed, 3 skipped).
