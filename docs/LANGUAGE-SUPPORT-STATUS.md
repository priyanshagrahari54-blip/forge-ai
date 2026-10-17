# Forge AI — Language Support Status & Remaining Stages

Updated: 2026-10-17. This is an evidence-based engineering estimate, not a claim that every language is production-ready.

## Current stage ratings

Ratings describe implementation maturity in the repository, not the quality of generated code in every language.

| Stage | Scope | Rating | Status |
|---|---|---:|---|
| 1. Language detection | Source extensions and ecosystem marker files | 7/10 | Implemented broadly; edge cases remain (for example ambiguous C/C++ headers and native build markers). |
| 2. Project understanding | Dependencies, entry points, conventions and repository structure | 5/10 | Shared repository intelligence exists; per-language depth is uneven. |
| 3. Code editing | Generate constrained changes that respect project style and APIs | 5/10 | Shared coder/ChangeSet path exists; language-specific editing validation is incomplete. |
| 4. Toolchain inventory | Detect installed compilers/interpreters and safely query versions | 4/10 | Allow-listed probes can now be scoped to detected project ecosystems and included in runtime reports on request; planner/UI integration remains. |
| 5. Build adapters | Select a verified ecosystem build command and run it safely | 5/10 | Shared build engine and several detected systems exist; coverage is not universal. |
| 6. Test adapters | Run project tests and parse actual outcomes | 4/10 | Unified test engine now discovers pytest, npm, Cargo, Maven, Gradle wrappers, Go, .NET, PHPUnit, Ruby, Dart, Swift and Elixir strategies; execution parsers and ecosystem fixtures remain uneven. |
| 7. Diagnostics and repair | Parse compiler/linter errors and use evidence in bounded repair | 5/10 | Shared diagnostic/debug loop exists; language parser coverage and repair quality vary. |
| 8. Formatting, lint and static analysis | Detect/run ecosystem tools and interpret results | 3/10 | Some shared/configured checks exist; per-language tool maps and CI fixtures are incomplete. |
| 9. Security checks | Language-aware dependency, secret and unsafe-code checks | 4/10 | Shared security gates exist; ecosystem-specific scanners/SAST are not comprehensively integrated. |
| 10. CI conformance matrix | Representative fixture project per ecosystem, real build/test evidence | 2/10 | Small detection fixtures exist; comprehensive matrix is not yet in place. |
| 11. End-to-end acceptance | Build + tests + lint/security + review + evidence report | 5/10 | Shared acceptance pipeline exists; not all language adapters feed it yet. |
| 12. Documentation and honest claims | Explicitly distinguish detected, understood, editable, toolchain-ready, verified | 7/10 | Runtime reports can show opt-in toolchain evidence; full UI/mastery integration remains. |

**Overall language-agnostic engineering maturity estimate: 4.6/10.** This is a rough weighted engineering assessment, not a measured benchmark. Detection is ahead of real cross-language verification.

## What is done in this continuation

- Added `forge/intelligence/toolchain_probe.py`: an allow-listed inventory of common language runtimes, compilers and build tools. It uses PATH evidence, bounded version probes, no shell, a timeout, and structured results.
- Added `tests/test_toolchain_probe.py`: checks missing tools, fixed argv/no-shell behavior, nonzero version output, timeouts and inventory counts.
- Added project-scoped probing: only tool probes whose ecosystem labels match detected project types are run; an empty/unknown project type list triggers no subprocesses.
- Runtime detection remains lightweight by default. Call `RuntimeDetector(root).detect(include_toolchains=True)` to attach relevant toolchain evidence to `RuntimeReport` and include it in `generate_runtime_report` output.
- Expanded `forge/testing/universal.py` test-strategy discovery for Go modules, .NET projects, PHPUnit, Ruby, Dart, Swift and Elixir markers; Gradle wrapper availability is based on the local wrapper file rather than PATH alone.
- Added regression fixtures for Go, .NET, Dart and Gradle wrapper discovery. Discovery is not execution; each adapter still needs real CI fixtures and result-parser coverage.
- Tool presence is intentionally not treated as proof that a project builds or its tests pass.

## Remaining stages to reach credible broad language support

1. **Wire probes into the task planner and UI** — runtime-report integration is now opt-in; show available/missing/version-unknown distinctly in project planning and cockpit.
2. **Expand build recipes by ecosystem** — Rust/Cargo, Go, JVM/Gradle/Maven, .NET, PHP/Composer, Ruby/Bundler, Swift/SwiftPM, Dart/pub, CMake/Make/Meson and script ecosystems. Never execute untrusted repository commands without policy approval.
3. **Complete test adapters and parsers** — discovery now covers additional ecosystems; add fixtures for each supported test runner, connect parsers to the unified pipeline, and prove zero tests/unparsed output never count as pass.
4. **Add formatter/linter/SAST maps** — detect installed tools and configuration files, then run only explicit, bounded, policy-approved commands.
5. **Build representative CI fixtures** — one minimal real project per ecosystem that CI can actually compile/test; missing toolchains are marked blocked/skipped, not passed.
6. **Add language-aware diagnostic and repair tests** — known broken fixtures, expected diagnostics, bounded repair attempts, rerun and rollback assertions.
7. **Expose mastery level in reports/UI** — each language gets a per-project evidence trail: detected → understood → editable → toolchain available → verified.
8. **Run full CI and audit gaps** — resolve failures, test the changed code, and only then raise ratings based on evidence.

## Language coverage groups

- **Existing broad detection vocabulary:** Python, JavaScript/TypeScript, Node, Deno, Bun, Rust, Go, Java/JVM, Kotlin, Scala, Groovy, C/C++, C#/.NET, F#, Visual Basic, PHP, Ruby, Swift, Objective-C, Dart, Lua, Perl, R, Julia, Elixir, Erlang, Haskell, Clojure/ClojureScript, Elm, Solidity, Vyper, shell, PowerShell, SQL, HTML/CSS/SCSS/Sass, Vue, Svelte and HCL/Terraform.
- **Initial toolchain probes:** Python, Node/npm, Deno/Bun, Rust/Cargo, Go, Java/JVM, Kotlin, Scala, C/C++/Clang, CMake/Make, .NET, PHP/Composer, Ruby/Bundler, Swift, Dart, Lua, Perl, R, Julia, Elixir, Erlang, Haskell, Clojure, Elm, Solidity, Vyper, Terraform, PowerShell and SQLite CLI.
- **Not yet guaranteed:** tool availability on every machine, complete language-server support, universal build/test coverage, equal code-generation quality, or production verification for every ecosystem.

## Acceptance rule

A language is **verified for a project only when that project's relevant real build/test/security checks have run and the evidence is recorded**. Detection, tool availability, or generated code alone never earns a verified status.
