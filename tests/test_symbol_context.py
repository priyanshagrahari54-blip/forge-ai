from pathlib import Path

from forge.intelligence.context import ContextItem, ContextPack, ContextQuery
from forge.intelligence.symbol_context import SymbolContextSelector


def create_project(root: Path) -> None:
    (root / "demo").mkdir()

    (root / "demo" / "__init__.py").write_text("")
    (root / "demo" / "service.py").write_text(
        "from demo.models import User\n"
        "\n"
        "class AuthService:\n"
        "    def authenticate(self, user: User) -> bool:\n"
        "        return True\n"
        "\n"
        "def logout(user: User) -> None:\n"
        "    pass\n"
    )


def test_selects_function_range(tmp_path: Path) -> None:
    create_project(tmp_path)

    pack = ContextPack(
        query=ContextQuery(task="authenticate"),
        items=[
            ContextItem(
                path="demo/service.py",
                kind="symbol",
                symbol="authenticate",
                score=100,
            )
        ],
    )

    result = SymbolContextSelector(tmp_path).select(pack)

    item = result.items[0]

    assert item.start_line == 4
    assert item.end_line == 5


def test_selects_class_range(tmp_path: Path) -> None:
    create_project(tmp_path)

    pack = ContextPack(
        query=ContextQuery(task="AuthService"),
        items=[
            ContextItem(
                path="demo/service.py",
                kind="symbol",
                symbol="AuthService",
                score=100,
            )
        ],
    )

    result = SymbolContextSelector(tmp_path).select(pack)

    item = result.items[0]

    assert item.start_line == 3
    assert item.end_line == 5


def test_preserves_non_symbol_context(tmp_path: Path) -> None:
    create_project(tmp_path)

    original = ContextItem(
        path="demo/service.py",
        kind="dependency",
        reason="dependency",
        score=40,
    )

    pack = ContextPack(
        query=ContextQuery(task="authenticate"),
        items=[original],
    )

    result = SymbolContextSelector(tmp_path).select(pack)

    assert result.items == [original]


def test_missing_symbol_is_safe(tmp_path: Path) -> None:
    create_project(tmp_path)

    pack = ContextPack(
        query=ContextQuery(task="missing"),
        items=[
            ContextItem(
                path="demo/service.py",
                kind="symbol",
                symbol="does_not_exist",
                score=50,
            )
        ],
    )

    result = SymbolContextSelector(tmp_path).select(pack)

    assert result.items[0].start_line is None
    assert result.items[0].end_line is None
