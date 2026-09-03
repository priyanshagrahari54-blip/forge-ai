from pathlib import Path

from forge.intelligence.symbols import Symbol, SymbolIndex, SymbolIndexer


def test_symbol_index_queries():
    index = SymbolIndex()

    index.add(
        Symbol(
            name="hello",
            kind="function",
            file="app.py",
            line=10,
        )
    )

    index.add(
        Symbol(
            name="User",
            kind="class",
            file="models.py",
            line=5,
        )
    )

    assert len(index.by_name("hello")) == 1
    assert len(index.by_file("app.py")) == 1
    assert len(index.by_kind("class")) == 1
    assert len(index.find("hello")) == 1
    assert len(index.find("app.py:hello")) == 1


def test_symbol_indexer(tmp_path: Path):
    (tmp_path / ".gitignore").write_text(
        "__pycache__/\n",
        encoding="utf-8",
    )

    (tmp_path / "app.py").write_text(
        """
def hello():
    pass

class User:
    pass

async def worker():
    pass
""",
        encoding="utf-8",
    )

    cache = tmp_path / "__pycache__"
    cache.mkdir()

    (cache / "ignored.py").write_text(
        """
def should_not_exist():
    pass
""",
        encoding="utf-8",
    )

    index = SymbolIndexer(tmp_path).build()

    names = {symbol.name for symbol in index.symbols}

    assert "hello" in names
    assert "User" in names
    assert "worker" in names
    assert "should_not_exist" not in names
