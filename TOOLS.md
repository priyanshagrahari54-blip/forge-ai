# FORGE AI — TOOL FABRIC
## Tool record
ID, name, description, capabilities, input schema, output schema, permissions, risk, network requirements, authentication, timeout, retry policy and audit events.

## Categories
Filesystem, Git, terminal/code execution, browser, research, HTTP, database, vision, audio, computer-use, remote compute, deployment, notifications and memory.

## Execution
Planner selects tool → schema validation → authorization → risk check → approval if needed → sandbox/fence → execution → output validation → audit.

## Security
Model-generated arguments are untrusted. No arbitrary shell execution from raw model text. Tool outputs are also untrusted and must be validated before further privileged use.
