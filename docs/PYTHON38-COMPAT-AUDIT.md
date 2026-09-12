# Python 3.8 / Windows 7 32-bit compatibility audit

Date: 2026-09-10 · Scope: `forge/`, `tests/`, `scripts/`, launchers, `pyproject.toml`,
`.github/workflows/ci.yml`, `docs/`, `.forge/audit/` · 452 Python files.

Every claim below is tied to the command that produced it. Where a claim could
not be verified in this sandbox it is marked **unchecked**.

## Method

| Check | Tool | Result |
|---|---|---|
| 3.8 grammar, all files | `ast.parse(..., feature_version=(3, 8))` | **0 failures / 452 files** |
| Minimum required version | `vermin 1.8.0 -t=3.8-` over `forge tests scripts launch_*.py` | **Minimum required versions: 3.8** |
| vermin self-test | deliberately-bad file (`tomllib`, `int \| None`, `removesuffix`) | correctly reported `3.11` — the tool is not silently passing |
| cp38 dependency resolution | `pip download --python-version 38 --abi cp38 --platform win32` | resolves (see §4) |
| Dependency metadata | PyPI JSON `Requires-Python` for every release | see §4 |

CPython 3.8 could **not** be executed here: only `pypi.org` / `files.pythonhosted.org`
and the GitHub API are reachable, and `release-assets.githubusercontent.com`
(python-build-standalone), `www.python.org`, `ppa.launchpadcontent.net` and the
Ubuntu mirrors all fail (`curl` exit 35 / `000`). So **no test in this repo has
been executed on a real 3.8 interpreter** — that is the one gap this audit
cannot close locally, and CI (§5) is where it gets closed.

---

## 1. Syntax newer than 3.8 — CLEAN

No `match`, no `except*`, no PEP 695 generics, no walrus misuse. Confirmed two
independent ways: `ast` `feature_version=(3, 8)` parses all 452 files, and
vermin reports the tree minimum as 3.8.

## 2. Standard-library APIs newer than 3.8 — CLEAN

Targeted greps for the usual offenders returned **zero** hits outside
`tests/test_python38_compat.py` (which names them deliberately):
`is_junction`, `isjunction`, `bit_count`, `ignore_cleanup_errors`, `root_dir=`,
`include_hidden`, `assertNoLogs`, `exit_on_error`, `counts=`, `b32hex`,
`get_annotations`, `autocommit`, `delete_on_close`, `StrEnum`, `ExceptionGroup`,
`TaskGroup`, `asyncio.timeout`, `to_thread`, `pairwise`, `batched`, `zoneinfo`,
`graphlib`, `tomllib`, `removeprefix`, `removesuffix`, `is_relative_to`,
`ast.unparse`. The single `cache(` hit is the test *function name*
`test_time_bound_rules_skip_cache`, not `functools.cache`.

## 3. Typing constructs newer than 3.8 — CLEAN, but the guard has a hole

* Every file that imports `typing` also has `from __future__ import annotations`
  — **0 exceptions** across `forge/`.
* `forge/api/` (where FastAPI/pydantic *evaluate* annotations) uses only
  `typing.Optional` / `typing.List`; `forge/api/schemas.py:9` confirms.
* No `Self`, `TypeAlias`, `ParamSpec`, `TypeGuard`, `Concatenate`, `Never`.
* A runtime scan for PEP 604 unions and builtin-generic subscripts **outside**
  annotations found 11 candidates, all verified false positives: set unions
  (`forge/core/supervisor.py:396`), regex flag ORs (`forge/research/web.py:433`),
  and `st_mode | stat.S_IEXEC` chmods in three tests.

**Guard gap (real, currently latent).** `from __future__ import annotations`
stringifies *annotations only*. A runtime subscript such as `alias = list[int]`,
`cast(dict[str, int], x)` or `set[str]()` is still `TypeError` on 3.8 — and
**neither** `tests/test_python38_compat.py` **nor** vermin catches it. Proven
with a probe file containing exactly those three lines: vermin reported
`Minimum required versions: 3.7` for code that requires 3.9. The repo has no
such construct today, so this is a hole in the net, not a live defect.

## 4. Third-party dependencies — every latest release has dropped 3.8

Read from PyPI `Requires-Python` (`info` + per-release metadata):

| Dependency | Declared | Latest | Latest needs | Newest that admits 3.8 |
|---|---|---|---|---|
| `pydantic` | `>=2.0` | 2.13.5 | `>=3.9` | **2.10.6** |
| `PyYAML` | `>=6.0` | 6.0.3 | `>=3.8` | 6.0.3 ✓ |
| `fastapi` | `>=0.110` | 0.141.1 | `>=3.10` | **0.124.4** |
| `uvicorn` | `>=0.29` | 0.52.4 | `>=3.10` | **0.33.0** |
| `pytest` | `>=8.0` | 9.1.1 | `>=3.10` | **8.3.5** |
| `httpx` | `>=0.27` | 0.28.1 | `>=3.8` | 0.28.1 ✓ |

Transitive, resolved by pip for cp38/win32: `pydantic-core 2.27.2`,
`starlette 0.44.0`, `anyio 4.5.2`, `typing-extensions 4.13.2`,
`annotated-types 0.7.0`, `click 8.1.8`, `idna 3.15`, `pluggy 1.5.0`,
`iniconfig 2.1.0`, `packaging 26.2`.

Build backend: `setuptools` latest 84.0.0 needs `>=3.10`; newest admitting 3.8
is **75.3.4**. `wheel` latest needs `>=3.9` (3.8 → 0.45.1); `pip` latest needs
`>=3.10` (3.8 → 25.0.1).

**Honest severity.** This is *not* currently a hard failure: `pip download
--python-version 38 --implementation cp --abi cp38 --platform win32` on the
declared specifiers resolved successfully, backtracking to exactly the versions
in the table above. What is wrong is that the specifiers are **unbounded**
(`>=` with no ceiling), so the 3.8 build is (a) non-reproducible, (b) dependent
on slow resolver backtracking, and (c) silently exposed the day any of these
publishes a release whose `Requires-Python` is wrong or missing. Nothing in the
repo pins the known-good 3.8 set.

## 5. CI — the 3.8 leg is on a runner that cannot provide 3.8

`.github/workflows/ci.yml` runs matrix `["3.8", "3.11"]` on `runs-on: ubuntu-latest`.

* `ubuntu-latest` is Ubuntu 24.04, and `actions/setup-python` **cannot install
  Python 3.8 on the 24.04 runner** ([actions/setup-python#879](https://github.com/actions/setup-python/issues/879)).
* Python 3.8 was removed from the Ubuntu 22.04 and Windows Server 2019/2022
  runner images on 2025-06-06, though archives remain installable *via
  setup-python* on images that have builds
  ([actions/runner-images#12034](https://github.com/actions/runner-images/issues/12034)).

So the 3.8 leg is, at best, fragile and at worst red on setup. Secondary gaps:
no Windows job at all (despite Windows being the primary target), no job that
proves the *resolved dependency set* installs on cp38/win32, and
`python -m compileall forge` only proves the installed interpreter can compile —
it is not a 3.8 grammar gate on the 3.11 leg.

**Unverified here:** whether `actions/setup-python@v6` behaves differently from
`@v5` on 24.04 — the runner is not reachable from this sandbox.

## 6. Packaging metadata

`pyproject.toml`: `requires-python = ">=3.8"` is correct. Missing: any
`classifiers` (so no `Programming Language :: Python :: 3.8` advertised),
any upper bound on any dependency, and a pinned build backend. No `setup.py`,
`setup.cfg` or `requirements*.txt` exists.

## 7. Desktop build

`scripts/build_desktop.py`:

* Docstring lines 20–21 state *"Python 3.11+ is required … Forge targets >=3.11
  per pyproject.toml"* — **false**; `pyproject.toml:9` says `>=3.8`.
* It passes `--collect-submodules forge`, which drags every `forge.*` submodule
  into the bundle, including `forge/api` → `fastapi` → `pydantic` →
  the Rust-compiled `pydantic_core*.pyd`.
* PyInstaller 6.22.2 declares `<3.16,>=3.8`, so 3.8 is supported, and it ships
  32-bit Windows bootloaders. Its own PyPI page says: *"PyInstaller should work
  on Windows 7 or newer, but we only officially support Windows 8+"*
  ([pypi.org/project/pyinstaller](https://pypi.org/project/pyinstaller/)).

**The Windows 7 32-bit blocker is `pydantic_core`, and it is avoidable.**
`pydantic_core-2.27.2-cp38-cp38-win32.whl` was produced by `maturin 1.7.8`
(read from its `WHEEL` metadata), i.e. a Rust toolchain well past 1.78 — and
**Rust 1.78 (2024-05-02) raised the `*-pc-windows-msvc` targets, including
`i686`, to require Windows 10**
([rust-lang.org blog](https://blog.rust-lang.org/2024-02-26/Windows-7/),
[release notes](https://doc.rust-lang.org/beta/releases.html)). That `.pyd` is
therefore not expected to load on Windows 7 32-bit.

The escape hatch is verified, not assumed. An import walk from each entry point:

| Entry | Modules walked | Third-party reached |
|---|---|---|
| `forge.desktop_app.app` (what `launch_desktop.py` imports) | 199 | `yaml`, `modal` |
| `forge.desktop_app.backend` | 193 | `yaml`, `modal` |
| `forge.desktop` | 37 | **none** |
| `forge.cli` | 234 | fastapi, starlette, uvicorn, yaml, modal |
| `forge.api` | 203 | fastapi, starlette, yaml, modal |

> **Correction.** An earlier pass of this audit walked `forge.desktop_app`
> itself, which is only its `__init__.py` (a docstring plus `__version__`), and
> reported "2 modules, no third-party". That was wrong: the real entry point is
> `forge.desktop_app.app`, and through it `backend.py` reaches `forge.control`
> and `forge.models.config`, which import **PyYAML**. The numbers above are from
> the corrected walk.

So the desktop app is *not* dependency-free — it needs PyYAML — but it never
reaches `pydantic`, which only `forge/api/schemas.py` imports. PyYAML 6.0.3
ships a plain C-extension `cp38-cp38-win32` wheel with no Rust and therefore no
Windows 10 floor. A desktop bundle that excludes `fastapi`, `starlette`,
`uvicorn`, `pydantic` and `pydantic_core` therefore contains no Windows-10-only
binary. (`modal` is an optional, `try`-guarded import at
`forge/compute/remote.py:753`, not a hard dependency.)

## 8. Contradictions claiming Python 3.11+

| Location | Claim | Truth |
|---|---|---|
| `forge/cli.py:261` | `python_ok = sys.version_info >= (3, 11)` | floor is 3.8 |
| `forge/cli.py:277` | `"TOO OLD - Forge needs >=3.11"` | floor is 3.8 |
| `scripts/build_desktop.py:20-21` | "Python 3.11+ is required … targets >=3.11 per pyproject.toml" | `pyproject.toml:9` says `>=3.8` |
| `.forge/audit/FRESH_AUDIT.md:19` | "Python \| 3.11 (repo requires >=3.11)" | floor is 3.8 |

Already correct: `docs/DESKTOP-APP.md:8-10` ("Python 3.8+ … CI-tested on 3.8 and
3.11") and `README.md:711` ("across Python 3.8 and 3.11").

`forge doctor` is the serious one: on a supported 3.8 interpreter it prints
"TOO OLD". `python_ok` is only asserted to *exist*
(`tests/test_task_failure_recovery.py:284`), so its value is unguarded.

## 9. Tests claiming 3.8 compatibility

`tests/test_python38_compat.py` is substantially sound — it passes, it does ban
`match`, 3.9+ attrs, 3.9+ modules, 3.10+ `typing` names, 3.11+ bare names,
callee-scoped banned kwargs, and new-style annotations lacking the future
import. Its weaknesses:

1. The runtime-construct hole in §3.
2. It never ties itself to `requires-python`, so `cli.py`'s `>=3.11` check and a
   `>=3.9` metadata bump could both land silently.
3. It validates no dependency metadata, so §4 is entirely unguarded.

## 10. Windows 7 / 32-bit

* **Hard crash, `forge/desktop/local_provider.py:293`** — `signum = signal.SIGTERM
  if op == "terminate" else signal.SIGKILL`. `signal.SIGKILL` does not exist on
  Windows, so `terminate`/`kill` raises `AttributeError`. The surrounding
  handlers catch `ProcessLookupError`/`PermissionError`/`OSError`, none of which
  is `AttributeError`.
* **Hard crash, `forge/compute/remote.py:528,532`** — `os.killpg(...)` and
  `signal.SIGKILL`; `os.killpg` does not exist on Windows and `AttributeError`
  is not caught by `except (OSError, subprocess.TimeoutExpired)`.
* Grep for `sys.platform` / `os.name` across all of `forge/`: **zero hits**.
  There is no platform guard anywhere in the package.
* 32-bit ⇒ 2 GiB user address space; CPython 3.8 is the last series with
  Windows 7 support and 32-bit installers, which is consistent with the target.
* Forge's HTTPS paths (Ollama/OpenAI) need TLS 1.2, which Windows 7 supports but
  does not always have enabled by default. **Unchecked here** — no Windows host
  available.

---

# Repair plan

| # | Item | Fix |
|---|---|---|
| P1 | Packaging (§4, §6) | Bound every dependency; add `python_version` markers so 3.8 gets the pinned known-good set and 3.9+ keeps modern releases. Add `classifiers` incl. 3.8. Pin the build backend. |
| P2 | Reproducible 3.8 lock (§4) | New `requirements/py38.txt` + `requirements/py38-dev.txt`, exactly the versions pip resolved for cp38/win32, consumed by CI and by the Windows 7 install instructions. |
| P3 | Source crashes (§10) | Portable signal handling in `forge/desktop/local_provider.py` and `forge/compute/remote.py`; POSIX keeps process-group kill, Windows gets a real fallback. No security check weakened. |
| P4 | `forge doctor` (§8) | Derive the floor from `requires-python` instead of a hardcoded `(3, 11)`; fix the message. |
| P5 | Guard holes (§3, §9) | Extend `tests/test_python38_compat.py`: runtime subscripts/unions, and assert the declared floor and the dependency caps match the guard. Offline only. |
| P6 | CI (§5) | Pin the 3.8 job to `ubuntu-22.04`; add a Windows job; add a cp38/win32 resolution gate; add a vermin gate. Keep the existing 3.8 checks. |
| P7 | Desktop build (§7) | Stop `--collect-submodules forge` from dragging in FastAPI/pydantic; keep the old behaviour behind a flag; fix the docstring. |
| P8 | Docs (§7, §8) | Fix `scripts/build_desktop.py` and `.forge/audit/FRESH_AUDIT.md`; add `docs/PYTHON38-WINDOWS7.md`; correct the README CI paragraph. |

Explicitly **not** doing: changing `requires-python` alone, deleting
functionality to make tests pass, filtering warnings, or skipping the failing
paths.
