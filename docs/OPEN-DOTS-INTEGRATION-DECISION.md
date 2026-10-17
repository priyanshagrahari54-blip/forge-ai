# Open Dots + Open-Source Ecosystem Adoption Decision

**Status:** evaluated from upstream README and package metadata on 2026-10-17. This is an integration decision, not a claim that upstream code has been installed, run, or security-audited.

## Open Dots candidate

Upstream: https://github.com/HamidYaraliOfficial/Open-Dots-Open-Source-Personal-AI-Agent-Workspace

The README describes a self-hosted personal-agent workspace with streaming chat, personas, model-provider configuration, SQLite conversation state, a governed action gateway, approval prompts, audit events, optional Docker/Playwright computer runtime, and connector integration. Upstream explicitly labels itself a prototype / active development and says multi-user hosting and hostile-web isolation are not production-ready.

### Decision: compose, do not blindly merge

Use Open Dots as an **optional workspace/UI and action-gateway candidate**, while Forge remains the authority for:
- requirement contracts, task decomposition, dynamic agent selection and lifecycle;
- model/provider truth, capability verification and quota-aware routing;
- project memory, provenance, tests, security review, acceptance and rollback;
- task permissions, approval policy, audit and final quality decisions.

Do not copy its entire server/client into Forge or make its action gateway a security boundary until version-pinned source review and threat-model verification are complete. Avoid running two competing schedulers or two sources of truth for permissions and task status.

### Integration sequence

1. **Inventory only:** pin an upstream commit; inspect license, dependencies, auth/session code, gateway policy, computer runtime and provider contract.
2. **Contract mapping:** map Forge task IDs, event status, project scope, model-provider config, approvals and audit records to an explicit adapter interface.
3. **Read-only prototype:** connect UI to a Forge status/stream API or use a separate Open Dots workspace against a mock/read-only adapter. No shell, write, browser or computer-use privileges.
4. **Narrow write actions:** allow only explicitly scoped actions, enforce Forge-side policy and per-action approvals, record audit events, and add idempotency/timeouts.
5. **Sandbox computer use:** only after hostile-page isolation, credential separation, network egress rules, resource limits and escape tests are demonstrated.
6. **Acceptance:** test auth/session expiry, CSRF/origin handling, secret storage, path traversal, prompt injection through external content, gateway bypass, audit completeness and rollback. Keep integration disabled by default until gates pass.

### Constraints

- Open Dots requires Node.js/npm, Python 3.10+, and a reachable inference API for live model responses according to its README. Do not assume it can run on the Lenovo G560's Node 13 / Windows 7 32-bit setup.
- Do not assume a free API, always-on hosting, or high-end model capacity is available.
- Treat upstream README, code, connectors, web results, and downloaded packages as untrusted inputs until reviewed.
- Preserve upstream attribution and license obligations if code is ever copied or modified.

## Broader open-source adoption policy

Forge should discover candidate projects, inspect license and maintenance, map compatibility, review dependencies and threat surface, then run isolated tests before promotion. Prefer **REUSE → ADAPT → COMPOSE → EXTEND → BUILD**, but build custom components whenever existing projects fail Forge's requirements or quality gates. Discovery alone is never proof of correctness, security, compatibility, or availability.

## Current outcome

- Open Dots: **candidate for optional workspace/action-gateway composition; not yet integrated or approved for production**.
- Forge: remains the canonical orchestration, governance, verification and memory layer.
- Next engineering gate: define a narrow adapter contract and validate it against a pinned upstream revision before enabling any action execution.
