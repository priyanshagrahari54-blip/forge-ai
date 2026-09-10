## 2025-05-18 - Repository Intelligence Indexing Bottlenecks

**Learning:** `SymbolIndex` queries (`by_name`, `by_file`, `by_kind`, `find`), `DependencyGraph` insertion and lookup (`add`, `dependencies_of`, `dependents_of`), and `GitIgnoreMatcher.is_ignored` were all doing linear list scans and un-memoized filesystem syscalls (`target.resolve()`). In indexing routines called repeatedly across repository analysis, this caused quadratic cost scalings (O(N^2) additions and linear search traversals).

**Action:** Maintain internal index hash maps (`_by_name`, `_by_file`, `_by_source`, `_by_target`, `_seen`) on indexing classes as elements are added, and cache path resolution results to avoid repetitive I/O and linear scans during code intelligence traversal.

## 2025-05-19 - ContextPack Duplicate Check Quadratic Overhead

**Learning:** `ContextPack.add()` was rebuilding a set of paths (`{existing.path for existing in self.items}`) on every invocation to prevent duplicate file paths. During context pack generation for tasks across repository intelligence, adding $N$ items resulted in $O(N^2)$ set construction operations.

**Action:** Maintain an internal `_paths` set on dataclasses like `ContextPack` (updating via `__post_init__`, `__setattr__`, and `add`) to achieve $O(1)$ duplicate checking without rebuild cost.

## 2025-05-20 - Repository Intelligence Direct Dependency Linear Scanning

**Learning:** `RepositoryIntelligence._direct_dependency_paths` and `_direct_dependent_paths` performed full linear scans over all repository dependencies ($O(D)$) on every graph node expansion. During transitive dependency and dependent walks (`_transitive_dependency_paths`, `_transitive_dependent_paths`, context queries, and impact analysis), visiting $V$ graph nodes caused $O(V \cdot D)$ time complexity.

**Action:** Maintain an internal `_by_resolved_path` hash map on `DependencyGraph` alongside `_by_source` to enable $O(1)$ direct dependency and dependent lookups by resolved file path during repository graph traversals.
