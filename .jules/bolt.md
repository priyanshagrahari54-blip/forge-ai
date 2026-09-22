## 2025-05-18 - Repository Intelligence Indexing Bottlenecks

**Learning:** `SymbolIndex` queries (`by_name`, `by_file`, `by_kind`, `find`), `DependencyGraph` insertion and lookup (`add`, `dependencies_of`, `dependents_of`), and `GitIgnoreMatcher.is_ignored` were all doing linear list scans and un-memoized filesystem syscalls (`target.resolve()`). In indexing routines called repeatedly across repository analysis, this caused quadratic cost scalings (O(N^2) additions and linear search traversals).

**Action:** Maintain internal index hash maps (`_by_name`, `_by_file`, `_by_source`, `_by_target`, `_seen`) on indexing classes as elements are added, and cache path resolution results to avoid repetitive I/O and linear scans during code intelligence traversal.

## 2025-05-19 - ContextPack Duplicate Check Quadratic Overhead

**Learning:** `ContextPack.add()` was rebuilding a set of paths (`{existing.path for existing in self.items}`) on every invocation to prevent duplicate file paths. During context pack generation for tasks across repository intelligence, adding $N$ items resulted in $O(N^2)$ set construction operations.

**Action:** Maintain an internal `_paths` set on dataclasses like `ContextPack` (updating via `__post_init__`, `__setattr__`, and `add`) to achieve $O(1)$ duplicate checking without rebuild cost.

## 2025-05-20 - Dependency Graph Resolved Path Linear Traversals

**Learning:** `RepositoryIntelligence._direct_dependency_paths` and `_direct_dependent_paths` were scanning the entire list of `DependencyGraph.dependencies` ($M$ dependencies) on every invocation, causing $O(K \times M)$ overhead during transitive context expansion across $K$ files. Additionally, `TestMapper` ran `rglob("*.py")` twice across disk.

**Action:** Maintain an internal `_by_resolved` dict index in `DependencyGraph` for $O(1)$ resolved dependency lookups, use `deque.popleft()` for BFS traversals, and combine filesystem scans into single-pass traversals.

## 2025-05-21 - DAGScheduler Cycle Detection Quadratic Overhead

**Learning:** `DAGScheduler.add_task` called `_detect_cycle()` on every single task insertion. For a graph of $N$ tasks, adding nodes incrementally caused $O(N^2)$ cycle detection passes during graph construction. Since `add_task` validates that dependencies exist before adding a new node with no dependents, adding nodes cannot create a cycle in an already-acyclic graph.

**Action:** Defer full graph cycle detection to `DAGScheduler.run()` prior to execution. This eliminates quadratic graph construction cost, speeding up 2,000 task additions by ~100x (>99% latency reduction from ~2.02s to ~0.019s).

## 2025-05-22 - TaskEngine and TaskDependencyGraph Linear Scan Overhead

**Learning:** `TaskEngine._find()` and `_exists()` performed linear list scans over `self.tasks` ($O(N)$). When adding $N$ tasks with dependency validation, building a graph required $O(N^2)$ linear searches. Furthermore, `TaskDependencyGraph.find_cycle()` called `_find()` at every node during DFS traversal, compounding graph validation cost to $O(N \cdot (V + E))$.

**Action:** Maintain an internal `_task_map: dict[str, Task]` in `TaskEngine` for $O(1)$ task lookup, and use map lookup in `TaskDependencyGraph.find_cycle()`. This reduced 2,000 task additions latency by ~98% (from ~0.34s to ~0.006s) and `find_cycle()` runtime by ~93% (from ~2.15s to ~0.15s).
