# Repository Intelligence v2

Forge now has a lightweight multi-language index alongside its Python AST
intelligence. It detects common source languages, extracts conservative
symbols/imports, supports search, and persists an incremental project index
under .forge/repository-index.json.

The persistent layer is a cache, not a source of truth: the repository remains
authoritative. Cache failures never change build/test truth and stale indexes
must be refreshed before high-confidence decisions.

Architecture: language detection -> source records -> persistent cache ->
search/context -> existing Python symbol/dependency/architecture intelligence.

Future parser adapters can replace the regex fallback per language without
changing the repository-intelligence contract.
