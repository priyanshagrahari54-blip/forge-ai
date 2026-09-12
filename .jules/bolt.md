## 2025-05-18 - Repository Intelligence Indexing Bottlenecks

**Learning:** `SymbolIndex` queries (`by_name`, `by_file`, `by_kind`, `find`), `DependencyGraph` insertion and lookup (`add`, `dependencies_of`, `dependents_of`), and `GitIgnoreMatcher.is_ignored` were all doing linear list scans and un-memoized filesystem syscalls (`target.resolve()`). In indexing routines called repeatedly across repository analysis, this caused quadratic cost scalings (O(N^2) additions and linear search traversals).

**Action:** Maintain internal index hash maps (`_by_name`, `_by_file`, `_by_source`, `_by_target`, `_seen`) on indexing classes as elements are added, and cache path resolution results to avoid repetitive I/O and linear scans during code intelligence traversal.

## 2025-05-19 - ContextPack Duplicate Check Quadratic Overhead

**Learning:** `ContextPack.add()` was rebuilding a set of paths (`{existing.path for existing in self.items}`) on every invocation to prevent duplicate file paths. During context pack generation for tasks across repository intelligence, adding $N$ items resulted in $O(N^2)$ set construction operations.

**Action:** Maintain an internal `_paths` set on dataclasses like `ContextPack` (updating via `__post_init__`, `__setattr__`, and `add`) to achieve $O(1)$ duplicate checking without rebuild cost.

## 2026-09-11 - Native AI Context Budget Must Price the Render, Not the Parts

**Learning:** The A81 context engine first estimated its token budget over section *contents* only, while `NativeContext.render()` emits per-section headers (`## name [status]`) and `"(nothing)"` placeholders for empty sections. Small-budget runs on the 2 GB target still exceeded the declared budget after "successful" trimming (318 estimated tokens vs a 256 cap) because headers and placeholders were unpriced, and a fingerprint/estimate mismatch made the budget claim false.

**Action:** Budget against the exact composed cost (`_render_cost`: header + body-or-placeholder + separator) so the post-trim render provably fits; and keep the status label mutation (`present` -> `empty`) inside the trim step so the priced and rendered strings never diverge.
