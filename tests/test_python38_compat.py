"""Lock in the Python 3.8 compatibility floor (static, runs anywhere).

The suite itself runs wherever CI runs it (3.8 + 3.11), but this test
statically forbids reintroducing anything newer than 3.8:

* PEP 604 unions / builtin-generic annotations without
  ``from __future__ import annotations`` (TypeError at import on 3.8).
* 3.9+ stdlib APIs (``removesuffix``, ``is_relative_to``,
  ``functools.cache``, ``zoneinfo``, ``graphlib``, ``ast.unparse``,
  ``Path.walk``, ``str``/``itertools``/``statistics`` newcomers…).
* 3.10+ syntax (``match``) and 3.11+ APIs (``StrEnum``, ``tomllib``,
  ``datetime.UTC``, ``except*``, ``KW_ONLY``, ``TaskGroup``…).
* Grammar must parse as 3.8 (``feature_version`` gate).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()

SOURCES = ["forge", "tests", "scripts",
           "launch_cockpit.py", "launch_desktop.py"]

BUILTIN_GENERICS = {"list", "dict", "tuple", "set", "frozenset", "type"}

#: Attribute names that only exist on 3.9+ (checked as real attributes,
#: so comments/docstrings can still mention them).
BANNED_ATTRS = {
    "removeprefix", "removesuffix", "is_relative_to", "unparse",
    "getLevelNamesMapping", "getencoding", "delete_on_close",
    "usedforsecurity", "markcoroutinefunction",
}

#: Qualified roots that only exist on 3.9+ (module.attr or type.attr).
BANNED_QUALIFIED = {
    ("functools", "cache"),
    ("datetime", "UTC"),
    ("os", "pidfd_open"),
    ("os", "waitstatus_to_exitcode"),
    ("os", "memfd_create"),
    ("math", "nextafter"),
    ("math", "ulp"),
    ("statistics", "covariance"),
    ("statistics", "correlation"),
    ("statistics", "linear_regression"),
    ("itertools", "pairwise"),
    ("itertools", "batched"),
    ("asyncio", "to_thread"),
    ("asyncio", "timeout"),
    ("contextlib", "aclosing"),
    ("contextlib", "chdir"),
}

#: Modules that only exist on 3.9+.
BANNED_MODULES = {"zoneinfo", "graphlib", "tomllib"}

#: typing-only names from 3.10+ (flagged only on typing imports).
BANNED_TYPING = {"Self", "Never", "NotRequired", "TypeGuard", "override",
                 "Concatenate", "ParamSpec", "TypeVarTuple", "TypeAlias",
                 "LiteralString", "Never"}

#: Bare names from 3.11+ (enum members, builtins-adjacent).
BANNED_NAMES = {"StrEnum", "ReprEnum", "TaskGroup", "ExceptionGroup",
                "BaseExceptionGroup"}

#: Keyword-argument names added after 3.8, scoped by callee so domain
#: kwargs with the same spelling (e.g. voice ``slots=``) never match.
BANNED_KWARGS = {
    # callee (bare or dotted attribute): frozenset of too-new kwargs.
    "dataclass": frozenset({"slots", "kw_only"}),
    "field": frozenset({"slots", "kw_only"}),
    "zip": frozenset({"strict"}),
    # Executor.shutdown(cancel_futures=) is 3.9+; TypeError on 3.8.
    "shutdown": frozenset({"cancel_futures"}),
}

#: First version supporting each banned kwarg (default: 3.10).
BANNED_KWARGS_SINCE = {
    ("shutdown", "cancel_futures"): "3.9",
}


def _callee_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _iter_files():
    for root in SOURCES:
        path = ROOT / root
        if path.is_file():
            yield path
            continue
        yield from sorted(path.rglob("*.py"))


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return tuple(reversed(parts))
    return ()


def _annotation_uses_new_syntax(node) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.BinOp) and isinstance(child.op,
                                                       ast.BitOr):
            return True
        if (isinstance(child, ast.Subscript)
                and isinstance(child.value, ast.Name)
                and child.value.id in BUILTIN_GENERICS):
            return True
    return False


def _check_file(path: Path) -> list[str]:
    if path.resolve() == SELF:
        return []  # this guard names the banned APIs by necessity
    text = path.read_text(encoding="utf-8")
    problems: list[str] = []
    try:
        tree = ast.parse(text, filename=str(path),
                         feature_version=(3, 8))
    except SyntaxError as exc:
        # Still run the remaining checks on a normal parse so one file
        # reports every violation, not just the first.
        problems.append(f"{path}: not 3.8 grammar: {exc}")
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            return problems
    future = any(
        isinstance(node, ast.ImportFrom) and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in ast.iter_child_nodes(tree))
    for node in ast.walk(tree):
        if type(node).__name__ == "Match":
            problems.append(f"{path}:{node.lineno}: match statement (3.10+)")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in BANNED_MODULES:
                    problems.append(
                        f"{path}:{node.lineno}: 3.9+ module {alias.name}")
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in BANNED_MODULES:
                problems.append(
                    f"{path}:{node.lineno}: 3.9+ module {node.module}")
            if node.module == "typing":
                for alias in node.names:
                    if alias.name in BANNED_TYPING:
                        problems.append(
                            f"{path}:{node.lineno}: typing.{alias.name} "
                            "needs 3.10+")
        if isinstance(node, ast.Attribute):
            if node.attr in BANNED_ATTRS:
                problems.append(
                    f"{path}:{node.lineno}: .{node.attr} needs 3.9+")
            dotted = _dotted(node)
            if len(dotted) == 2 and dotted in BANNED_QUALIFIED:
                problems.append(
                    f"{path}:{node.lineno}: {'.'.join(dotted)} "
                    "needs 3.9+")
            if (node.attr == "walk" and dotted[:1] not in (
                    ("os",), ("_os",), ("ast",), ("_ast",))):
                problems.append(
                    f"{path}:{node.lineno}: non-os/ast .walk() (Path.walk "
                    "needs 3.12)")
        if isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            problems.append(
                f"{path}:{node.lineno}: {node.id} needs 3.11+")
        if isinstance(node, ast.Call):
            callee = _callee_name(node)
            banned = BANNED_KWARGS.get(callee, frozenset())
            for keyword in node.keywords:
                if keyword.arg in banned:
                    since = BANNED_KWARGS_SINCE.get((callee, keyword.arg),
                                                    "3.10")
                    problems.append(
                        f"{path}:{node.lineno}: "
                        f"{callee}({keyword.arg}=) needs {since}+")
        if not future:
            anns: list[ast.AST] = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = list(node.args.args) + list(node.args.kwonlyargs)
                if node.args.vararg:
                    args.append(node.args.vararg)
                if node.args.kwarg:
                    args.append(node.args.kwarg)
                anns = [arg.annotation for arg in args if arg.annotation]
                if node.returns:
                    anns.append(node.returns)
            elif isinstance(node, ast.AnnAssign):
                anns = [node.annotation]
            for ann in anns:
                if _annotation_uses_new_syntax(ann):
                    problems.append(
                        f"{path}:{node.lineno}: new-style annotation "
                        "without 'from __future__ import annotations'")
                    break
    return problems


def test_tree_is_python38_compatible():
    problems: list[str] = []
    checked = 0
    for path in _iter_files():
        if "__pycache__" in str(path):
            continue
        checked += 1
        problems.extend(_check_file(path))
    assert checked > 400, f"compat walk found too few files: {checked}"
    assert not problems, "\n".join(problems[:20])


def test_api_annotations_evaluate_on_python38():
    """forge/api uses only typing-style annotations.

    FastAPI resolves endpoint/dependency annotations and pydantic
    resolves model fields by evaluating them at import time. With the
    future import the annotations are strings, and evaluating
    ``X | Y`` or ``list[X]`` raises TypeError on 3.8 — a collection
    failure. So the API layer must spell them ``Optional/List/...``.
    """
    problems: list[str] = []
    checked = 0
    for path in sorted((ROOT / "forge" / "api").rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        checked += 1
        tree = ast.parse(path.read_text(encoding="utf-8"),
                         filename=str(path))
        for node in ast.walk(tree):
            anns: list[ast.AST] = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = list(node.args.args) + list(node.args.kwonlyargs)
                if node.args.vararg:
                    args.append(node.args.vararg)
                if node.args.kwarg:
                    args.append(node.args.kwarg)
                anns = [arg.annotation for arg in args if arg.annotation]
                if node.returns:
                    anns.append(node.returns)
            elif isinstance(node, ast.AnnAssign):
                anns = [node.annotation]
            for ann in anns:
                if _annotation_uses_new_syntax(ann):
                    problems.append(
                        f"{path}:{node.lineno}: new-style annotation "
                        "evaluated by FastAPI/pydantic (use typing.*)")
                    break
    assert checked > 25, f"API walk found too few files: {checked}"
    assert not problems, "\n".join(problems[:20])


# ---------------------------------------------------------------------------
# Runtime (non-annotation) 3.9+/3.10+ constructs
# ---------------------------------------------------------------------------
# `from __future__ import annotations` stringifies *annotations only*. A
# subscript or union evaluated at runtime is still a TypeError on 3.8, and
# neither the checks above nor vermin catch it. These tests close that hole.

def _annotation_node_ids(tree: ast.AST) -> set:
    """Ids of every AST node that sits inside an annotation."""
    ids = set()

    def add(node):
        if node is None:
            return
        for child in ast.walk(node):
            ids.add(id(child))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = list(node.args.args) + list(node.args.kwonlyargs)
            if node.args.vararg:
                args.append(node.args.vararg)
            if node.args.kwarg:
                args.append(node.args.kwarg)
            for arg in args:
                add(arg.annotation)
            add(node.returns)
        elif isinstance(node, ast.AnnAssign):
            add(node.annotation)
    return ids


def _runtime_construct_problems(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return []  # the grammar gate already reports this
    in_annotation = _annotation_node_ids(tree)
    problems: list[str] = []
    for node in ast.walk(tree):
        if id(node) in in_annotation:
            continue
        # Runtime builtin-generic subscript: `alias = list[int]`,
        # `cast(dict[str, int], x)`, `set[str]()`. There is no legitimate
        # runtime subscript of the `list`/`dict`/... type objects, so this
        # has no false positives.
        if (isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id in BUILTIN_GENERICS):
            problems.append(
                "%s:%d: runtime %s[...] needs 3.9+ (use typing.%s outside "
                "annotations)" % (path, node.lineno, node.value.id,
                                  node.value.id.capitalize()))
        # Runtime PEP 604 union. Deliberately narrow: only `X | None` and
        # `list[...] | Y` are flagged, because a bare `Name | Name` cannot be
        # told apart from a real bitwise OR (regex flags, set unions, chmod
        # mode bits all appear in this codebase). `X | None` is never a
        # meaningful bitwise operation, so it is unambiguous.
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            sides = (node.left, node.right)
            has_none = any(isinstance(s, ast.Constant) and s.value is None
                           for s in sides)
            has_generic = any(
                isinstance(s, ast.Subscript) and isinstance(s.value, ast.Name)
                and s.value.id in BUILTIN_GENERICS for s in sides)
            if has_none or has_generic:
                problems.append(
                    "%s:%d: runtime `X | Y` union needs 3.10+ (use "
                    "typing.Optional/Union outside annotations)"
                    % (path, node.lineno))
    return problems


def test_no_runtime_post38_constructs_outside_annotations():
    problems: list[str] = []
    checked = 0
    for path in _iter_files():
        if "__pycache__" in str(path) or path.resolve() == SELF:
            continue
        checked += 1
        problems.extend(_runtime_construct_problems(path))
    assert checked > 400, f"runtime walk found too few files: {checked}"
    assert not problems, "\n".join(problems[:20])


# ---------------------------------------------------------------------------
# The declared floor must agree with everything that states it
# ---------------------------------------------------------------------------

def _toml_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        header = re.match(r"^\[([^\]]+)\]\s*$", line)
        if header:
            current = header.group(1).strip()
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return sections


def _toml_string_array(lines: list[str], key: str) -> list[str]:
    """Return the strings inside the `key = [ ... ]` array, if present."""
    collecting = False
    depth = 0
    buffer: list[str] = []
    for line in lines:
        if not collecting:
            if re.match(r"^\s*%s\s*=\s*\[" % re.escape(key), line):
                collecting = True
                depth = 1
                buffer.append(line.split("[", 1)[1])
            continue
        buffer.append(line)
        depth += line.count("[") - line.count("]")
        if depth <= 0:
            break
    body = "\n".join(buffer)
    return [a or b for a, b in re.findall(r'"([^"]*)"|\'([^\']*)\'', body)]


def _pyproject() -> str:
    return (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_minimum_python_matches_requires_python():
    """`forge doctor` and `pyproject.toml` must name the same floor.

    They disagreed before (the CLI claimed >=3.11 while the metadata said
    >=3.8), which made `forge doctor` call a supported interpreter "TOO OLD".
    """
    from forge.core.portability import MINIMUM_PYTHON

    text = _pyproject()
    match = re.search(r'^requires-python\s*=\s*"([^"]+)"', text,
                      re.MULTILINE)
    assert match, "pyproject.toml has no requires-python"
    declared = match.group(1).strip()
    floor_match = re.fullmatch(r">=\s*(\d+)\.(\d+)", declared)
    assert floor_match, f"unexpected requires-python form: {declared!r}"
    declared_floor = (int(floor_match.group(1)), int(floor_match.group(2)))
    assert MINIMUM_PYTHON == declared_floor, (
        "MINIMUM_PYTHON %s != requires-python %s"
        % (MINIMUM_PYTHON, declared_floor))
    assert declared_floor == (3, 8), (
        "this suite guards the 3.8 floor; requires-python says %s. If the "
        "floor really moved, this test and docs must move with it."
        % declared)


# ---------------------------------------------------------------------------
# Dependency ceilings (offline: metadata only, no network)
# ---------------------------------------------------------------------------
# Every one of these projects has shipped a release that dropped Python 3.8.
# The newest release of each that still admits 3.8, verified against PyPI
# `Requires-Python`:
#
#   pydantic 2.10.6 · fastapi 0.124.4 · uvicorn 0.33.0 · pytest 8.3.5
#
# An unbounded ">=X" would let a 3.8 install drift onto a release that
# cannot run there.

#: project -> exclusive ceiling that still admits 3.8, as a version tuple.
KNOWN_GOOD_CEILING = {
    "pydantic": (2, 11, 0),
    "fastapi": (0, 125, 0),
    "uvicorn": (0, 34, 0),
    "pytest": (8, 4, 0),
}


def _applies_to_python38(marker: str) -> bool:
    """Does this PEP 508 marker select Python 3.8?

    Anything this function does not recognise is treated as applying, so a new
    marker form cannot silently bypass the ceiling check.
    """
    if not marker:
        return True
    if re.search(r"python_version\s*<\s*['\"]3\.9['\"]", marker):
        return True
    if re.search(r"python_version\s*>=\s*['\"]3\.9['\"]", marker):
        return False
    return True


def _parse_version(text: str) -> tuple:
    parts = []
    for chunk in re.split(r"[.+!]", text.split("-")[0]):
        digits = re.match(r"(\d+)", chunk)
        parts.append(int(digits.group(1)) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def _requirement_entries() -> list[tuple[str, str]]:
    """(requirement string, section) for runtime + dev dependencies."""
    sections = _toml_sections(_pyproject())
    entries = [(req, "dependencies")
               for req in _toml_string_array(sections.get("project", []),
                                             "dependencies")]
    entries += [(req, "optional-dependencies")
                for req in _toml_string_array(
                    sections.get("project.optional-dependencies", []), "dev")]
    return entries


def _split_requirement(req: str) -> tuple[str, str, str]:
    """(name, specifiers, marker) for a PEP 508 requirement string."""
    name_spec, _, marker = req.partition(";")
    match = re.match(r"^\s*([A-Za-z0-9_.\-]+)\s*(.*)$", name_spec)
    assert match, f"unparseable requirement: {req!r}"
    return match.group(1).lower(), match.group(2).strip(), marker.strip()


def test_python38_dependency_requirements_are_ceilinged():
    """Each 3.8 branch of every dependency must be capped below the release
    that dropped 3.8."""
    entries = _requirement_entries()
    assert len(entries) >= 8, f"too few requirements parsed: {entries}"
    seen = set()
    problems = []
    for req, section in entries:
        name, specifiers, marker = _split_requirement(req)
        if name not in KNOWN_GOOD_CEILING:
            continue
        if not _applies_to_python38(marker):
            continue  # the modern (>=3.9) branch may stay uncapped
        seen.add(name)
        ceilings = [_parse_version(m.group(1))
                    for m in re.finditer(r"<\s*([0-9][0-9.]*)", specifiers)]
        if not ceilings:
            problems.append("%s (%s): %r has no upper bound, so a 3.8 install "
                            "can drift onto a release that dropped 3.8"
                            % (name, section, req))
            continue
        if min(ceilings) > KNOWN_GOOD_CEILING[name]:
            problems.append(
                "%s (%s): ceiling %s is above the last 3.8-capable release "
                "%s" % (name, section, min(ceilings),
                        KNOWN_GOOD_CEILING[name]))
    assert seen == set(KNOWN_GOOD_CEILING), (
        "expected a 3.8-marked entry for each of %s, found %s"
        % (sorted(KNOWN_GOOD_CEILING), sorted(seen)))
    assert not problems, "\n".join(problems)


def test_py38_lock_pins_versions_that_admit_38():
    """`requirements/py38.txt` must pin inside the known-good ceilings."""
    lock = ROOT / "requirements" / "py38.txt"
    assert lock.exists(), "requirements/py38.txt is missing"
    pins = {}
    for line in lock.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-r"):
            continue
        match = re.match(r"^([A-Za-z0-9_.\-]+)==([0-9][0-9.]*)$", line)
        assert match, f"unpinned or unparseable line in py38.txt: {line!r}"
        pins[match.group(1).lower()] = match.group(2)
    for name, ceiling in KNOWN_GOOD_CEILING.items():
        if name == "pytest":
            continue  # pytest lives in py38-dev.txt
        assert name in pins, f"py38.txt does not pin {name}"
        assert _parse_version(pins[name]) < ceiling, (
            "py38.txt pins %s==%s, at or above the last 3.8-capable release "
            "%s" % (name, pins[name], ceiling))

    dev = (ROOT / "requirements" / "py38-dev.txt").read_text(encoding="utf-8")
    match = re.search(r"^pytest==([0-9][0-9.]*)$", dev, re.MULTILINE)
    assert match, "py38-dev.txt does not pin pytest"
    assert _parse_version(match.group(1)) < KNOWN_GOOD_CEILING["pytest"], (
        "py38-dev.txt pins pytest==%s; pytest 8.4+ requires Python 3.9"
        % match.group(1))
