from forge.intelligence.python_parser import (
    PythonParser,
)


def test_python_parser():
    source = """
import os
import pathlib


class Example:
    def run(self):
        pass


def helper():
    pass
"""

    result = PythonParser().parse(
        "example.py",
        source,
    )

    assert "os" in result.imports
    assert "pathlib" in result.imports

    names = {
        symbol.name
        for symbol in result.symbols
    }

    assert "Example" in names
    assert "run" in names
    assert "helper" in names
