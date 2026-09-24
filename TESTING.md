# FORGE AI — TESTING STRATEGY
## Levels
Unit, integration, API, provider adapter, routing, agent, task/worker, memory, security, UI smoke, E2E, load and failure-injection.

## Required representative agents
Planner, architect, researcher, coder, frontend, backend, database, debugger, tester, reviewer, security, devops, browser, computer-use, vision, memory, orchestration and release.

## Required failures
Provider timeout/auth failure/model unavailable, agent failure, tool failure, test failure, worker restart, cancellation, rollback, invalid tool arguments and malicious model output.

## Release gate
Tests pass; no weakened assertions; real inference verified; representative task succeeds; recovery succeeds; security checks pass.
