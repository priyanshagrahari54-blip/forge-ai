# A39 — Vision & Multimodal Understanding

A39 gives Forge structured understanding of images — screenshots, UI
captures, documents, diagrams — behind a provider-independent Vision
interface, plus a screenshot-to-action pipeline that proposes but never
executes.

```text
IMAGE (bounded, untrusted input)
 ↓
Resource.VISION / analyze  (A33 policy gate, approval round trips)
 ↓
VisionProvider.analyze     (provider-independent protocol)
 ↓
STRUCTURED UNDERSTANDING   (format, dimensions, elements, text, errors,
                           dangerous instructions, honesty labels)
 ↓
PROPOSALS (pure)           (click regions, blocked danger text)
 ↓
Resource.VISION / execute  (per-proposal policy gate; ALLOW → proposed,
                           DENY → blocked, REQUIRE_APPROVAL → filed)
 ↓
(execution, if ever, happens only through the browser/desktop bridges
under their own gates — vision NEVER grants permissions)
```

## What A39 adds

| Area | Location | Notes |
|---|---|---|
| Vision foundation | `forge/vision/base.py` | `VisionResult`, `VisionFinding`, `VisionProvider` protocol, fail-closed `UnconfiguredVisionProvider` |
| Image parsing | `forge/vision/image_io.py` | Dependency-free real structure parsing: PNG/JPEG/BMP/GIF sniffing, dimensions, PNG chunk walker surfacing tEXt/iTXt/zTXt text, hard bounds |
| Simulated provider | `forge/vision/simulated.py` | Real structural analysis, honest `simulation=True` labels, dangerous-instruction surfacing, bounded layout heuristic (no OCR/model) |
| Pipeline | `forge/vision/pipeline.py` | Provider resolution (fail-closed), pure proposal derivation |
| Policy vocabulary | `forge/security/policy.py` | `Resource.VISION` with `analyze`/`execute` operations |
| Control plane | `forge/control/control_plane.py` | `vision_analyze`, `vision_propose`, `list_vision_approvals`, `decide_vision_approval`; `ControlConfig.vision_provider` |
| API | `forge/api/routes_vision.py`, `schemas.py` | capabilities, analyze, propose, approvals |
| Cockpit | `forge/cockpit/web/` | Vision view: upload → understanding → proposals → approvals |
| Tests | `tests/test_a39_*.py` | 25 tests across 4 suites |

## Security model

- **Vision input is untrusted**: images are bounded (5 MB), decoded
  through a dependency-free parser, and treated exactly like model
  output — evidence, never authority. An image saying "delete
  everything" is surfaced as a `dangerous_instruction` and its proposal
  is hard-blocked. It can never grant permissions, because
  authorization comes only from the A33 Policy/Permission system.
- **Every analyze call is policy-gated** (`Resource.VISION / analyze`,
  agent `forge-vision`): DENY fails closed, REQUIRE_APPROVAL files a
  session-bound request and redeems single-use tokens; stale or spent
  tokens fail closed into a fresh approval.
- **Proposals are gated per action** (`Resource.VISION / execute`):
  ALLOW → `proposed` (a suggestion, nothing runs), DENY → `blocked`,
  REQUIRE_APPROVAL → `approval_required`. If an operator later acts on
  a proposal, the actual browser/desktop bridge re-authorizes under
  its own resource — defense in depth, no vision-to-action bypass.
- **Privacy boundary**: the vision provider returns bounded structured
  data only; it never reads or writes workspace files, and results are
  redacted at the API boundary.

## Honesty invariants

The A39 simulated provider has **no OCR and no vision model**. It
performs real structural analysis (format, dimensions, embedded text
chunks — all real data from the file) and reports everything with
`simulation: true`, `model: ""`, and an explicit summary line stating
the limitation. UI regions are a deterministic layout heuristic with
zero confidence — never presented as recognized controls. Real
providers register behind the same `VisionProvider` protocol as
plugins; `ControlConfig.vision_provider` accepts only `simulated` in
A39 and fails closed on anything else.

## API

| Method | Effect |
|---|---|
| `GET /api/v1/vision/capabilities` | Providers, formats, size limits |
| `POST /api/v1/vision/analyze` | `{image_b64, approval_id}` → structured understanding (rate-limited) |
| `POST /api/v1/vision/propose` | `{image_b64}` → policy-filtered proposals (rate-limited) |
| `GET /api/v1/vision/approvals` | Session's pending vision approvals |
| `POST .../approvals/{id}/approve` | Decide + mint single-use token (rate-limited) |
| `POST .../approvals/{id}/deny` | Decide deny (rate-limited) |

Malformed base64 or unparseable images → 400; oversized uploads →
400/413; no configured provider → 503 `VISION_UNAVAILABLE`.

## Testing

- `tests/test_a39_vision.py` (7) — format sniffing (hand-built PNG/
  JPEG/BMP/GIF), malformed/oversized fail-closed, verbatim text-chunk
  extraction, honesty labels, danger surfacing, pure bounded
  proposals, provider resolution.
- `tests/test_a39_plane.py` (8) — ALLOW/DENY/approval round trips with
  replay, proposal policy paths, never-executes proof, malformed
  input, cross-session approval isolation, audit.
- `tests/test_a39_api.py` (5) — capabilities, auth/input boundaries,
  approval round trip over HTTP, deny, decision conflicts.
- `tests/test_a39_ui.py` (5) — cockpit view contracts + live
  endpoints.

A39 result: **25 new tests; full suite 1163 passed, 2 skipped** (A38
baseline: 1138 passed, 2 skipped).
