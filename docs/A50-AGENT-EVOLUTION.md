# A50 — Agent Evolution

Agents evolve from evidence, not stories: real terminal run outcomes
(status, attempts, elapsed time) are recorded into a per-agent ledger
that drives generation counters and honest metrics. Full lifecycle:
train → benchmark → promote → retire, driven by real metrics.

## What A50 adds

- `forge/agents/evolution.py` — `AgentEvolution` ledger per session:
  recording one real terminal run bumps the agent's generation
  exactly once and computes `runs`, `succeeded`, `failed`,
  `success_rate`, `avg_attempts`, `avg_elapsed_ms`, `last_outcome`
  from the recorded rows. No fake learning: an agent with no
  recorded outcomes has no metrics. Lifecycle decisions (promote,
  retire) come from measured performance thresholds.
- `forge/agents/training.py` — **Real training pipeline**:
  - `TrainingDataset` / `TrainingExample`: collect real training data
    from run outcomes, export in OpenAI JSONL fine-tuning format.
  - `OpenAIFineTuner`: upload training files and create fine-tuning
    jobs via the OpenAI API (requires `OPENAI_API_KEY`).
  - `AgentTrainingPipeline`: full lifecycle with
    `collect_training_data()`, `export_dataset()`,
    `start_fine_tuning()`, `should_promote()`, `should_retire()`.
- `AgentDefinition` gains `generation` (starts at 1) and `metrics`.
- Control plane `agent_record_outcome()` — only *terminal* runs may
  be recorded (running/paused/queued are refused honestly), unknown
  agents/tasks are refused, and every recording is audited under
  `agents/evolve`.
- Control plane `training_collect/export/fine_tune/status` — real
  training pipeline operations.
- API: `POST /api/v1/agents/{name}/outcomes`,
  `GET /api/v1/agents/{name}/evolution`.

## Promotion Criteria

An agent is promotion-ready when all are true:
- At least 5 recorded runs
- Success rate ≥ 80%
- Last outcome is not FAILED

## Retirement Criteria

An agent should be retired when any is true:
- At least 10 runs with success rate < 30%
- Last outcome is FAILED and ≥ 5 recorded runs

## Security notes

- Evolution is read-mostly bookkeeping: it grants nothing, changes no
  permissions, and cannot influence policy.
- Training data comes from real run outcomes — never fabricated.
- Fine-tuning requires a configured OpenAI API key and is audited.

## Testing

`tests/test_a50_agent_evolution.py` (6): unrecorded defaults,
real-terminal recording with computed metrics, refusal of unfinished
runs, unknown-target refusal, API flow, and metric arithmetic.

A50 result: **6 new tests; full suite 1276 passed, 2 skipped** (A49
baseline: 1270 passed, 2 skipped).
