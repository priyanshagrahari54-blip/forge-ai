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


def _annotation_roots(tree) -> list:
    roots = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = list(node.args.posonlyargs) + list(node.args.args) \
                + list(node.args.kwonlyargs)
            for arg in args:
                if arg.annotation is not None:
                    roots.append(arg.annotation)
            if node.args.vararg and node.args.vararg.annotation is not None:
                roots.append(node.args.vararg.annotation)
            if node.args.kwarg and node.args.kwarg.annotation is not None:
                roots.append(node.args.kwarg.annotation)
            if node.returns is not None:
                roots.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            roots.append(node.annotation)
    return roots


def _value_position_new_syntax(tree) -> list:
    """PEP 585 builtin generics evaluated at *runtime* (not annotations).

    ``from __future__ import annotations`` only defers annotation
    evaluation; a type alias or expression such as
    ``Worker = Callable[[Work], dict[str, Any]]`` is evaluated at import
    time and raises ``TypeError: 'type' object is not subscriptable`` on
    3.8. Set unions (``a | b``) are deliberately not flagged here because
    ``set | set`` is valid 3.8 — annotation unions are covered separately.
    """
    annotation_subscripts = set()
    for root in _annotation_roots(tree):
        for child in ast.walk(root):
            if isinstance(child, ast.Subscript):
                annotation_subscripts.add(id(child))
    offenders = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id in BUILTIN_GENERICS
                and id(node) not in annotation_subscripts):
            offenders.append(node)
    return offenders


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
    for node in _value_position_new_syntax(tree):
        problems.append(
            f"{path}:{node.lineno}: {node.value.id}[...] is evaluated at "
            "runtime (3.9+); use typing.List/Dict/... or keep it in an "
            "annotation")
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
