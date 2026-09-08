# A54 — Agent Skills

Declarative, validated skill definitions that extend agent
capabilities honestly.

## What A54 adds

- `forge/agents/skills.py` — `SkillRegistry`: skills are named,
  versioned extensions binding one canonical-vocabulary capability
  plus a bounded description; validated (name regex, vocabulary,
  limits), session-bounded.
- `AgentDefinition` gains `skills` and `base_capabilities`:
  attaching a skill adds its capability to the agent's capability
  set; detaching recomputes capabilities from the base set plus
  remaining skills; redefining capabilities resets skill extensions.
- Control plane `skill_create/list`, `agent_attach_skill`,
  `agent_detach_skill` — audited under `skills`.
- API: `POST/GET /api/v1/skills`,
  `POST /api/v1/agents/{name}/skills`,
  `DELETE /api/v1/agents/{name}/skills/{skill}`.

## Security notes

- Skills are declarative by contract and by implementation: they
  change the capability set and notes only — never the executor,
  never the `real` binding, never policy. Execution power still
  comes exclusively from bound executors behind the A33 gates.

## Testing

`tests/test_a54_agent_skills.py` (5): validation, attach/detach
capability math, no-power guarantee (unbound agent stays unbound,
catalog unchanged), session isolation, API flow.

A54 result: **5 new tests; full suite 1298 passed, 2 skipped** (A53
baseline: 1293 passed, 2 skipped).
