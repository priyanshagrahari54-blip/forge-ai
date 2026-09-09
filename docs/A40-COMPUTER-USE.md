# A40 — Computer Use

A40 turns A39's image understanding into controlled computer use: the
vision-driven loop `screen → understand → element tree → propose →
PolicyGate → execute`, running exclusively over the A35 desktop
execution pipeline (A33 policy → hard risk invariants → profile →
approvals).

```text
SCREENSHOT (bounded, untrusted)
 ↓ vision analyze (Resource.VISION / analyze)
 ↓ versioned snapshot + element tree (bounded, honest confidence labels)
 ↓ proposals (pure dry run: authorize-only, nothing executes)
 ↓ ComputerUseEngine.act (one action, fully guarded)
 ↓   SAFE/LOCKED mode  → observation only, actuation refused
 ↓   confirmation dialog on screen → fail closed
 ↓   HIGH/CRITICAL risk → escalated to operator approval (hard rule)
 ↓   per-task action budget → hard cap
 ↓ DesktopAgent (A35): validation → task scope grants → policy →
     hard invariants → risk → profile → single-use approval tokens →
     provider execution with redacted observations
 ↓ history recorded with redacted parameters only
```

## What A40 adds

| Area | Location | Notes |
|---|---|---|
| Bounded state | `forge/computer/state.py` | Versioned screen snapshots (capped per task), redacted action history, per-task executed-action budget, confirm-dialog flag derived from screen text |
| Element tree | `forge/computer/elements.py` | Deterministic tree over vision findings; simulated regions keep zero confidence |
| Engine | `forge/computer/engine.py` | `observe` / `propose` / `act` / `cycle` / `history` with every guard enforced |
| Control plane | `forge/control/control_plane.py` | `computer_observe/propose/act/cycle/history`, approvals list/decide, `ControlConfig.computer_max_actions` |
| API | `forge/api/routes_computer.py`, `schemas.py` | observe, propose, act, cycle, history, approvals, approve/deny |
| Cockpit | `forge/cockpit/web/` | Computer view: upload → observe → element tree → proposals → manual gated action → history → approvals |
| Tests | `tests/test_a40_*.py` | 32 tests across 4 suites |

## Security model

- **No real actions under SAFE/LOCKED.** The engine refuses every
  actuation before it reaches the desktop agent; only observations
  run.
- **Per-action permission checks.** Execution authority is the A35
  `DesktopAgent` pipeline — validation, task-scope grants, the A33
  policy gate, hard invariants, risk classification, profile, and
  single-use approval tokens. The computer engine never bypasses it.
- **Risk-based approval is a hard rule.** Even when a profile would
  auto-run an action, HIGH/CRITICAL risk is escalated to operator
  approval; a missing approval store fails closed.
- **Confirmation dialogs fail closed.** When the current screen's own
  extracted text shows a dialog ("Are you sure?", OK/Cancel, …),
  actuation is refused until a fresh, dialog-free screen is observed.
- **Action budget.** A hard cap on executed actions per task (default
  20, configurable) — no unbounded action loops.
- **Typed-text redaction.** History, logs, and memory only ever see
  redacted parameters (length-only placeholders for text, paths,
  commands, URLs, and secret-bearing keys). The real payload goes only
  to the provider at execution time.
- **Proposals never execute.** `propose` and `cycle` are dry runs /
  at-most-one-safe-action rounds; the simulated provider never changes
  the screen, so the loop honestly refuses to repeat actions against
  an unchanged screen.

## Honesty invariants

The screen understanding comes from the A39 vision provider and keeps
its labels: the A40 build has no OCR and no vision model, so element
regions are simulated heuristics with zero confidence — the cockpit
and reports say so. Execution and observations come from the real A35
desktop agent (deterministic fake provider in dev/tests, real
providers register as plugins).

## API

| Method | Effect |
|---|---|
| `POST /api/v1/computer/observe` | `{image_b64, goal}` → versioned snapshot + element tree |
| `POST /api/v1/computer/propose` | `{image_b64, goal}` → dry-run proposals (executed: false) |
| `POST /api/v1/computer/act` | `{action, target, params, reason, approval_id}` → guarded execution |
| `POST /api/v1/computer/cycle` | `{image_b64, goal}` → one bounded observe→propose→act round |
| `GET /api/v1/computer/history` | Snapshots + redacted action history |
| `GET /api/v1/computer/approvals` | Session's pending computer approvals |
| `POST .../approvals/{id}/approve` / `/deny` | Decide + mint single-use token |

Malformed actions, oversized targets, or unparseable screens → 400;
policy denials → 403.

## Testing

- `tests/test_a40_computer.py` (11) — snapshot versioning and bounds,
  redaction (incl. secret-bearing keys), dialog detection, honest
  element trees, SAFE/LOCKED actuation refusal, dialog fail-closed,
  hard budget, HIGH-risk escalation, propose-never-executes, bounded
  honest cycles.
- `tests/test_a40_plane.py` (11) — observe/propose/act/cycle over the
  plane, grants + autonomous execution, assisted approval round trip
  with single-use replay, SAFE profile, escalation under autonomous,
  dialog fail-closed + recovery, budget config, redaction, cross-
  session isolation, audit.
- `tests/test_a40_api.py` (5) — auth/input boundaries, endpoints,
  redaction over HTTP, escalation approval round trip, conflicts +
  isolation.
- `tests/test_a40_ui.py` (5) — cockpit view contracts + live
  endpoints.

A40 result: **32 new tests; full suite 1195 passed, 2 skipped** (A39
baseline: 1163 passed, 2 skipped).
