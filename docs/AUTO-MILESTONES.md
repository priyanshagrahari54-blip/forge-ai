# Forge Automatic Milestone Execution

Forge now has a dependency-aware `AutoMilestoneRunner` in
`forge/orchestration/auto_milestones.py`.

## Behavior

The runner:

1. loads a milestone DAG;
2. validates dependencies and rejects cycles/unknown dependencies;
3. finds every dependency-ready milestone;
4. executes it without an interactive continue prompt;
5. retries a failed milestone up to its bounded `max_attempts`;
6. persists state after every meaningful transition;
7. resumes from the persisted state after a process restart;
8. automatically unlocks dependent milestones after success;
9. blocks dependent milestones when an upstream milestone permanently fails;
10. exposes a compact snapshot for the cockpit/server.

## Safety boundaries

Automatic progression does **not** mean bypassing Forge governance. A
milestone can declare `requires_approval=True`; an approval callback must then
allow it. The executor remains responsible for actual build/test/deploy work,
security policy, secrets, and external-provider authorization.

The runner therefore solves the repeated "continue?" interaction while keeping
existing execution and approval boundaries intact.

## Persistent state

The default state file is `.forge/auto-milestones.json`. Writes use a temporary
file followed by an atomic replacement so a process interruption does not leave
an intentionally half-written state file.

## Intended worker integration

The persistent Forge worker should construct the milestone plan for a task and
invoke `AutoMilestoneRunner.run()` from its existing background execution path.
The runner is deliberately callback-based so it can reuse Forge's existing
queue, checkpoint, event-log, model-fabric, test, and rollback infrastructure
instead of creating a second task engine.
