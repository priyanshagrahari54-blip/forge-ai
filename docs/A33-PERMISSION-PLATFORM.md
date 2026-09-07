# A33 — Forge Permission & Policy Platform

A33 turns Forge's engineering permission system into a **general-purpose,
fine-grained Permission & Policy Platform** covering files, folders, tools,
terminal commands, websites, browser actions, desktop resources, model
providers, network access, and future voice/computer-use capabilities —
without weakening the A32 autonomous engineering loop.

Powerful + autonomous + permission-controlled + auditable + reversible +
fail-closed. No unrestricted access, no sandbox/auth bypasses, no
unauthorized resource use.

## 1. Architecture

A33 **extends** A32's policy core; it does not duplicate it.
`ALLOW` / `DENY` / `REQUIRE_APPROVAL` and the `SAFE` / `ASSISTED` /
`AUTONOMOUS` / `LOCKED` modes stay authoritative. The new policy engine is a
**tighten-only layer**:

```text
Agent
   ↓
Tool Request (actor, task, operation, scope, risk, details)
   ↓
Permission Manager (A32 mode + levels)
   ↓                                    ↘
PolicyGate (A32 verdict)          Policy engine (explicit rules)
   ↓                                    ↘
   └────────── most restrictive wins ────→ ALLOW / DENY / REQUIRE_APPROVAL
   ↓
Tool Runtime (re-checks at execution)
   ↓
side effect
```

For coding the chain is:

```text
Model → ChangeSet → validation → policy evaluation → approval if needed
→ task grant → checkpoint → ChangeApplier → tests → review → security
→ acceptance → explicit Git staging → audit report
```

Rules of combination (structural, tested):

- An explicit engine `DENY` always wins (even against A32 `ALLOW`).
- An explicit engine `REQUIRE_APPROVAL` can tighten an A32 `ALLOW`.
- Engine silence (no matching rule, including default-deny) never changes an
  A32 verdict; engine `ALLOW` never loosens one.
- Resources with no A32 layer (browser, desktop, network, model, voice) use
  the engine verdict directly and **fail closed** (`DENY`) when no rule
  matches.

With no policy/store/audit attached, every A32 path behaves byte-identically
to A32 (proven by the untouched A32 suite staying green).

## 2. Permission model

A permission describes **WHO / WHAT / WHERE / WHEN / HOW / RISK / WHY**:

| Dimension | Field      | Example              |
| --------- | ---------- | -------------------- |
| WHO       | agent      | `Forge/CoderAgent`   |
| WHAT      | operation  | `write`              |
| WHERE     | scope      | `project/src/**`     |
| WHEN      | timestamp + validity window | `valid_until` |
| HOW       | details    | `port=443`, `args`   |
| RISK      | risk       | `LOW`                |
| WHY       | reason     | `Implement CSV export` |

Core types (`forge/security/policy.py`):

- `Resource`: `filesystem | terminal | git | browser | network | model |
  desktop | voice`.
- `PermissionRequest`: one permission question (frozen, serializable).
- `PermissionRule`: constrained dimensions must **ALL** match (frozen,
  validated at load).
- `PermissionPolicy`: indexed, cached, versioned rule set with a fail-closed
  default (`DENY`).
- `PermissionEvaluation`: decision + matched rules + reason + risk + scope.

## 3. Policy precedence (exact algorithm)

```text
EXPLICIT DENY
      ↓
EXPLICIT REQUIRE_APPROVAL
      ↓
EXPLICIT ALLOW
      ↓
DEFAULT POLICY (DENY — fail closed)
```

Refinement — **most-specific wins**:

1. Normalize the request; unsafe scopes match nothing.
2. Collect rules whose resource, operation, scope, agent, task, time window,
   risk ceiling, and detail constraints all match.
3. No match → the policy default (`DENY` unless configured otherwise).
4. Sort matches by **specificity** (static score: exact scope beats pattern
   scope; constrained agent/task/risk/time add weight), then by effect
   (`DENY` → `REQUIRE_APPROVAL` → `ALLOW`), then by rule id.
5. All rules tied at the top specificity form the deciding group; the
   strictest effect in the group wins. Ties are fully deterministic.

Consequences (all tested): a specific `DENY` beats a broad `ALLOW`; an exact
`ALLOW` beats a recursive `DENY`; at equal specificity `DENY` wins; rule
insertion order never changes verdicts.

## 4. Resource scopes

- **filesystem**: exact `path/to/file`; `dir/*` direct children; `dir/**`
  or trailing-slash `dir/` recursive; `**` everything. Must be
  repo-relative POSIX; absolute paths, `..`, and backslashes never match.
- **terminal**: executable scope plus pinned `args`. `ALLOW` rules MUST pin
  a concrete executable and exact args (rejected otherwise); `**` means any
  executable and is only valid with `DENY` / `REQUIRE_APPROVAL`.
- **git**: operation (`status | diff | commit | push`) plus optional path
  scope.
- **browser**: `example.com` exact host; `*.example.com` subdomains (apex
  excluded). Only `http(s)` URLs without userinfo match. Wildcards on bare
  TLDs are rejected as ambiguous. Suffix confusion (`evil-example.com`) does
  not match.
- **network**: host pattern plus optional `port` (1–65535) and `protocol`
  (`tcp | udp | http | https`).
- **model**: optional `provider` / `model` / `capability` constraints.
- **desktop**: operation (`read_screen | mouse_move | mouse_click |
  keyboard | launch | window | clipboard | file_access`) plus target scope.
- **voice**: `command` operation plus intent scope.

## 5. Agent identity

Every request carries its actor (`Supervisor`, `CoderAgent`,
`DebuggerAgent`, `ReviewerAgent`, `ResearchAgent`, `BrowserAgent`,
`DesktopAgent`, `VoiceInterface`, ...). Rules may constrain `agent`, and
agent-constrained rules outrank generic ones at equal scope. Approval tokens
are bound to the requesting agent and are non-transferable.

## 6. Task-scoped permissions

`ChangeApplier` mints a `TaskGrant` over exactly the validated paths of a
proposal, bound to the proposal fingerprint, when a task id and approval
store are configured. Each write is checked against the grant; grants are
revoked on apply failure, on rollback, and when the supervisor run ends.
Temporary authority never accumulates into permanent privilege.

## 7. Approval model

`REQUIRE_APPROVAL` stops Forge and produces a structured `ApprovalRequest`
(operation, resource, scope, files, agent, risk, model/provider, reason,
consequences). A **distinct approver** (never the requesting agent) decides;
an approved request mints a scoped `ApprovalToken`:

- bound to agent, task, resource, operation, scopes, files, detail bindings
  (ports, args, providers), and optionally a proposal fingerprint;
- single-use by default (`max_uses` explicit);
- time-bounded; expired tokens are invalid automatically, never extended;
- idempotent within one enforcement chain (gate + runtime share a
  `request_id`), consuming anew per chain;
- previewable without consumption (`check` / dry-run `preview`).

Escalation (`request_escalation`) files a request that still needs another
approver; agents cannot self-grant.

## 8. Expiration

Rules (`valid_from` / `valid_until`), tokens (`ttl_seconds`), task grants,
and requests (store `request_ttl`) all expire automatically. Expiry is
evaluated against an injectable clock; time-bound rules bypass the decision
cache so boundary behavior is always freshly computed. Nothing silently
extends anything (`prune` only drops).

## 9. Browser / network / desktop / voice foundations

A33 ships **permission abstractions plus safe mocks**, not real control:

- `forge/tools/browser.py` — `MockBrowser`: gated navigate/read/click/
  type/submit/upload/download serving deterministic fixtures. No automation
  libraries; unauthorized domains fail closed.
- `forge/tools/network.py` — `MockNetwork`: gated host/port/protocol
  requests with a `check()` seam future real code must call. No sockets.
- `forge/tools/desktop.py` — `DesktopResource`, `DesktopAction`,
  `DesktopPermission`, `DesktopActionRequest`, `MockDesktop`: records
  intentions, controls nothing. No input/screen APIs.
- `forge/voice.py` — `VoiceCommand`, `VoiceIntent`, `VoicePermission`,
  `VoiceCommandResult`, `VoiceInterface`: deterministic template parsing
  routed through permission evaluation. Unknown intents are denied. No
  speech recognition.
- `forge/cockpit.py` — backend interfaces for task submission/status,
  approval queue/decisions/token minting, event streams, logs, and
  model/agent summaries. No frontend.

## 10. Data classification & model policy

Content classifies as `PUBLIC | INTERNAL | CONFIDENTIAL | SECRET`
(`forge/security/classification.py`). Project content defaults to
`INTERNAL`; secret patterns, `.env` files, and credential filenames raise
the level, and detection always wins over a declared level.

`ModelDataPolicy` governs what classified content may reach a provider:

- `SECRET` → never to an external model unless explicitly authorized;
- `CONFIDENTIAL` → policy-controlled (`allow | approval | deny`);
- `INTERNAL` → configurable for remote models;
- `PUBLIC` → normal; local models always permitted.

The Model Fabric enforces the policy per candidate model in `generate` and
`stream` (request-level override supported); denied models are skipped with
feedback and never called. This is a filter, not competing model
infrastructure (§35).

## 11. Configuration

Policies load from strict mappings (`PermissionPolicy.from_dict`, round-trip
via `to_dict`), JSON/YAML compatible:

```yaml
permissions:
  mode: assisted
default: deny
rules:
  - id: code-read
    resource: filesystem
    operation: read
    scope: project/**
    effect: allow
  - id: src-write
    resource: filesystem
    operation: write
    scope: project/src/**
    effect: require_approval
  - id: env-deny
    resource: filesystem
    operation: write
    scope: project/.env
    effect: deny
```

Validation rejects unknown keys, enum values, scope syntax, wildcard TLDs,
unpinned terminal `ALLOW`s, misplaced dimensions, duplicate ids, and
inverted windows — atomically (never a partial policy). Fail closed.

## 12. Simulation, audit, observability

- `policy.simulate(request)` → `{decision, matched_rules, reason, risk,
  scope}`, side-effect free (powers future cockpit previews).
- `AuditLog` records every decision with `timestamp, request_id, task_id,
  trace_id, agent, resource, operation, scope, risk, decision,
  matched_rule, approval_required` (+ `approval_id`, `policy_rule`,
  `reason`). Secrets are redacted with the shared report redactor. The
  in-memory log is authoritative; the JSONL sink is best-effort with an
  error counter.
- Enforcement layers share `request_id` chains, so gate + runtime + engine
  events correlate per action.

## 13. Performance

Rules index by `(resource, operation)`; specificity is precomputed;
decisions cache by canonical request + policy version, with invalidation on
any mutation. Time-bound rules skip the cache. No repository scans on the
enforcement path.

## 14. Security invariants

1. `DENY` can never become `ALLOW` through agent logic.
2. `REQUIRE_APPROVAL` cannot execute without approval.
3. An approval cannot expand its original scope.
4. An expired approval cannot execute.
5. An agent cannot grant itself permission.
6. A broad `ALLOW` cannot override a more-specific `DENY`.
7. Unknown permissions fail closed.
8. Protected resources remain protected.
9. Permission checks happen before side effects.
10. Every decision is auditable without exposing secrets.

Proven by `tests/test_a33_invariants.py`; abuse coverage in
`tests/test_a33_attack.py` (traversal, absolute paths, symlink escape,
confusion, precedence determinism, replay, expiry, scope expansion,
self-escalation, tool-bypass boundaries, domain confusion, data smuggling,
audit secrecy).

## 15. Threat model

- **Untrusted model output**: never trusted; validated, gated, granted,
  checkpointed. Model text cannot set authorization flags.
- **Confused deputy**: every layer re-evaluates (gate + runtime + engine);
  tokens bind agent/task/operation/scope/bindings/fingerprint.
- **Privilege creep**: task grants expire; tokens are single-use and
  time-bounded; no permanent accumulation.
- **Scope smuggling**: unsafe paths match nothing; domain matching rejects
  suffix/userinfo/scheme tricks; terminal `ALLOW` pins exact argv.
- **Audit evasion**: decisions recorded at every layer with correlation
  ids; secrecy preserved by redaction.
- **Bypass by direct call**: raw tool classes are runtime internals; agents
  receive only the enforcing `ToolRuntime`; `ChangeApplier` is the single
  writer path; tests pin these boundaries.

Out of scope by design (see STOP condition): real browser automation,
unrestricted desktop control, speech recognition, the full web UI, and any
sandbox/auth/security-control bypass.

## 16. Status

Implemented and tested: policy engine, profiles, approvals, task grants,
expiration, audit, classification, model data policy, browser/network/
desktop/voice foundations, cockpit interfaces, A32 integration, invariants,
attack tests, CSV E2E, mock E2E. Full suite green; A32 suite untouched and
passing. No A34 work started.
