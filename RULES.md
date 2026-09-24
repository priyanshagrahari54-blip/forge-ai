# FORGE AI — ENGINEERING RULES
1. Never fake capability, inference, task completion or deployment.
2. Never invent model IDs.
3. Never claim discovery equals inference.
4. Never bypass authorization/security for convenience.
5. Never log secrets.
6. Treat model/web/repository/tool output as untrusted.
7. Keep heavy work server-side.
8. Preserve user intent; ask only when material ambiguity matters.
9. Consequential actions require appropriate approval.
10. Use explicit state machines.
11. Use bounded retries and timeouts.
12. Make failures observable and recoverable.
13. Prevent duplicate task execution.
14. Preserve provenance for research and memory.
15. Keep provider-specific logic inside adapters.
16. Keep UI separate from privileged execution.
17. Do not silently swallow exceptions.
18. Do not remove tests or weaken assertions to make CI green.
19. Measure performance before optimizing.
20. Update documentation with architecture changes.
21. Commit only intended files; never secrets/runtime state.
22. Verify deployment after production changes.
23. Prefer small cohesive modules over giant abstractions.
24. Backward compatibility matters for existing API consumers.
25. Self-improvement must use sandboxing, tests, security review and rollback.
