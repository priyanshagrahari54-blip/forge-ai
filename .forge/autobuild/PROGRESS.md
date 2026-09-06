# Forge AI repair progress

## Implemented and executable

- `Supervisor.run()` now executes the complete guarded transaction: requirement planning, capability/agent selection, model routing, model-generated code, permissioned writes, real tests, bounded `TestDebugLoop` repair/retest, independent review, security, build/compile, lint/type-equivalent verification, benchmark, acceptance, checkpoint, explicit commit, or file-scoped rollback.
- The supervisor has no caller-supplied `changes` or modifier-function argument. Providers return structured model responses and the coder/debugger validate and apply those responses through the permissioned runtime.
- Failure output from the terminal tool includes captured stdout/stderr and is passed unchanged into `DebuggerAgent`; retry count is clamped and bounded.
- Routing history records model success/failure and latency for model calls. Local, Ollama, OpenAI-compatible, and test providers remain available through the model contract.
- Git staging is explicit and rejects `.forge` runtime state. The supervisor stages only coder/debugger-reported files, so unrelated working-tree files survive and are not committed.
- Checkpoints restore changed candidate files exactly without `git reset --hard`; unknown unrelated untracked files are never deleted.
- Review and security are mandatory acceptance gates. Build/test/lint results come from actual subprocess execution, and benchmarks measure test/build outcomes and latency.

## Executable proof

- `tests/test_supervisor_autonomous_e2e.py` creates an isolated Git repository. The first model response is intentionally incorrect, a real test collection failure is captured, a second model response repairs it, tests pass, the validated files are committed, `.forge` is not committed, and unrelated working-tree work survives.
- `tests/test_supervisor_safety_e2e.py` proves bounded retry/rollback and routing failure history.
- `tests/test_verification_rejection_e2e.py` proves security and review failures reject and roll back candidates without creating commits.

## Deliberate limitations

- The dependency-free local provider safely refuses arbitrary source synthesis when no capable model is configured. Autonomous production coding requires an available Ollama or optional API provider.
- Approval and permission gates remain enabled; no unattended push or external-repository access is introduced.
