# Forge AI — Connected Autonomous Engineering Loop

Forge AI is an autonomous software engineering architecture that connects planning, agent selection, model routing, tool runtime, permission security, test execution, closed-loop debugging, verification gates, and atomic git checkpoints into one unified execution system.

---

## IMPLEMENTED ARCHITECTURE & EXECUTION FLOW

```
USER REQUIREMENT
  │
  ▼
SUPERVISOR (forge/core/supervisor.py)
  │
  ├── 1. TASK DECOMPOSITION & REQUIREMENTS
  │      TaskRequirementExtractor -> TaskRequirements
  │
  ├── 2. AGENT PLANNER (forge/agents/planner.py)
  │      CapabilityAgentPlanner -> AgentPlan
  │
  ├── 3. PLAN VALIDATION GATE (forge/agents/validator.py)
  │      AgentPlanValidator -> Blocks execution if invalid
  │
  ├── 4. MODEL ROUTING (forge/models/router.py)
  │      ModelRouter -> Multi-factor deterministic scoring
  │      Providers: MockProvider, OllamaProvider, OpenAIProvider
  │
  ├── 5. TOOL RUNTIME & PERMISSIONS (forge/runtime/, forge/security/)
  │      PermissionManager -> SAFE / APPROVAL_REQUIRED / BLOCKED
  │      FileSystemTool, TerminalTool, GitTool
  │
  ├── 6. CHECKPOINT CREATION (forge/tools/checkpoint.py)
  │      CheckpointManager -> Snapshot working state before changes
  │
  ├── 7. CODE MODIFICATION (forge/agents/coder.py)
  │      CoderAgent -> ToolRuntime execution
  │
  ├── 8. TEST & DEBUG RETRY LOOP (forge/agents/debugger.py)
  │      TestDebugLoop -> Code -> Test -> Fail? -> Diagnose -> Debug -> Modify -> Test again
  │      Bounded retries with diagnostic tracking
  │
  ├── 9. VERIFICATION GATES (forge/security/verification.py)
  │      Tests -> ReviewGate -> SecurityGate -> AcceptanceGate
  │
  └── 10. ACCEPT OR ROLLBACK
         ├── PASS -> Commit checkpoint -> Mark task COMPLETED
         └── FAIL -> Rollback changes -> Mark task FAILED
```

---

## CLI USAGE

### Run Autonomous Loop
Execute a real requirement through the connected supervisor pipeline:
```bash
forge run "add CSV export support"
```

### Plan Generation
Generate a plan for a requirement:
```bash
forge plan "implement authentication and tests"
```

### System Status & Analysis
Inspect state and analyze project structure:
```bash
forge status
forge analyze
```

---

## KEY COMPONENTS

* **Supervisor (`forge/core/supervisor.py`)**: Central orchestrator driving tasks from prompt to verification and checkpoint commits.
* **Agent Plan Validator (`forge/agents/validator.py`)**: Mandatory gate validating candidate plans against agent capability/role mappings and registered agents.
* **Model Router (`forge/models/router.py`)**: Deterministic scoring model assessing capability match, quality, reliability, context fit, latency, cost, and local vs. remote preference.
* **Permission Manager (`forge/security/permissions.py`)**: Categorizes operations into `SAFE`, `APPROVAL_REQUIRED`, and `BLOCKED` permission levels.
* **Closed-Loop Debugger (`forge/agents/debugger.py`)**: `TestDebugLoop` runs bounded retries, capturing failure logs, diagnoses, and corrections.
* **Verification Pipeline (`forge/security/verification.py`)**: Structured verification gates (`TestGate`, `ReviewGate`, `SecurityGate`, `AcceptanceGate`).
* **Checkpoint Manager (`forge/tools/checkpoint.py`)**: Snapshots repository state before modifications, automatically rolling back candidate changes if tests or verification fail.

---

## IMPLEMENTED VS. PLANNED

### IMPLEMENTED
- Connected Supervisor pipeline
- Mandatory Agent Plan Validator gate
- Multi-factor Model Router with Mock, Ollama, and OpenAI providers
- Permission-enforced Tool Runtime
- Coder agent tool execution
- Bounded closed-loop test/debug retry state machine
- Structured verification gates (Review, Security, Acceptance)
- Automatic snapshot checkpoint & rollback system
- End-to-end acceptance testing suite
- CLI `forge run` subcommand

### PLANNED
- Self-development loop (A26–A30) (handled in parallel development track)
- Distributed multi-worker task queue execution
