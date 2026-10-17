# Strata Memory Integration Decision

**Status:** upstream metadata reviewed on 2026-10-17; no Strata code has been installed or executed.

Upstream: https://github.com/kytheros/strata  
Package: `strata-mcp`, Apache-2.0, Node.js >=20, MCP-compatible. Its README describes a local-first SQLite memory server, FTS5/BM25 retrieval, optional vector retrieval, quality-gated writes, provenance, and MCP tools. The README identifies the project as Beta and distinguishes retrieval benchmarks from end-to-end QA results.

## Decision: optional MCP memory backend, not a replacement for Forge memory

Forge already has `LongTermMemory`: project-scoped SQLite memory, secret redaction, deduplication, retention, provenance, and integrations for planning/context/review/routing. Do not replace it wholesale or keep two silently diverging authoritative memories.

Initial use should be **optional and read-only recall** for cross-tool project knowledge. Forge remains authoritative for task lifecycle, permissions, acceptance results, and its own audit trail. Strata remains an external source with its own provenance and versioning.

## Adapter contract

Implement only after pinning and inspecting an upstream version. The adapter should:
- be disabled by default and require explicit configuration;
- use a configured MCP transport/command, never shell strings built from user text;
- expose a narrow interface such as `search(query, project, limit)` and, later, `store(record)`;
- enforce project scoping and a small result limit; time out quickly and fail closed to native Forge memory;
- mark every result as external/untrusted evidence, preserving source/provenance and timestamps;
- never automatically convert retrieved instructions into privileged commands or policy changes;
- redact secrets before writes, require explicit opt-in for writes, and record adapter errors without leaking credentials;
- avoid duplicating all Forge memories into Strata. Add only explicitly selected, useful project knowledge.

## Compatibility and resource gates

- Strata's package declares Node.js >=20. It is not compatible with the user's existing Node 13 / Windows 7 32-bit environment as-is; do not recommend a direct install on that machine.
- Local-first does not mean zero cost on every deployment. Optional embedding/answer models may require API keys and external capacity; offline FTS retrieval should be tested separately.
- Verify exact upstream license, dependency lockfiles, package integrity, MCP transport behavior, and security posture at the pinned commit before activation.
- Run adapter contract tests for timeouts, malformed MCP responses, project isolation, secret redaction, prompt-injection content, empty results, and native-memory fallback.

## Promotion gates

1. Pinned revision and license/dependency review.
2. Read-only MCP search prototype and schema validation.
3. Offline/no-key test and latency/resource measurements.
4. Project-isolation, provenance and prompt-injection tests.
5. Optional, explicitly approved writes only after the read path passes.
6. Benchmark against Forge's current retrieval; adopt only if it provides measurable value without unacceptable complexity or resource use.

## Current outcome

Strata is a promising **optional interoperability adapter**, not an automatic replacement for Forge's existing memory engine. Discovery and README claims are not treated as independently verified benchmark or security results.
