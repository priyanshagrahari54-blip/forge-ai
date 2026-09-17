"""Safe deterministic arithmetic for voice and text queries.

This module intentionally accepts only a small mathematical grammar. It never
uses eval/exec and therefore cannot turn user input into Python execution.
"""
from __future__ import annotations

import ast
import operator
import re


class MathExpressionError(ValueError):
    """Raised when an expression is outside the supported math grammar."""


_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "to": "2",
    "three": "3", "four": "4", "for": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "ate": "8",
    "nine": "9", "ten": "10",
}
_OPERATORS = (
    (r"multiplied by", "*"), (r"times", "*"), (r"plus", "+"),
    (r"minus", "-"), (r"divided by", "/"), (r"over", "/"),
    (r"modulo", "%"), (r"mod", "%"),
)
_MAX_ABS = 10 ** 100
_MAX_POWER = 100


def normalize_expression(text: str) -> str:
    """Normalize common spoken arithmetic without accepting executable syntax."""
    value = (text or "").strip().lower()
    value = re.sub(r"^(?:forge[,:]?\s+)?(?:calculate|compute|what is|what's)\s+", "", value)
    value = re.sub(r"\s+(?:equals|equal to)\s*$", "", value)
    for pattern, replacement in _OPERATORS:
        value = re.sub(r"\b" + pattern + r"\b", replacement, value)
    for word, number in sorted(_WORDS.items(), key=lambda item: -len(item[0])):
        value = re.sub(r"\b" + re.escape(word) + r"\b", number, value)
    value = value.replace("×", "*").replace("÷", "/")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def calculate(text: str) -> int | float:
    """Evaluate a bounded arithmetic expression using an AST allow-list."""
    expression = normalize_expression(text)
    if not expression or len(expression) > 200:
        raise MathExpressionError("Empty or oversized arithmetic expression")
    if not re.fullmatch(r"[0-9+\-*/%.() ]+", expression):
        raise MathExpressionError("Unsupported arithmetic syntax")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise MathExpressionError("Invalid arithmetic expression") from exc

    def visit(node: ast.AST) -> int | float:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            if abs(node.value) > _MAX_ABS:
                raise MathExpressionError("Number is too large")
            return node.value
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _bounded(_UNARY[type(node.op)](visit(node.operand)))
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > _MAX_POWER:
                raise MathExpressionError("Exponent is too large")
            try:
                return _bounded(_BINOPS[type(node.op)](left, right))
            except (ArithmeticError, OverflowError) as exc:
                raise MathExpressionError("Arithmetic error") from exc
        raise MathExpressionError("Unsupported arithmetic operation")

    return visit(tree)


def _bounded(value: int | float) -> int | float:
    if isinstance(value, float) and (value != value or abs(value) == float("inf")):
        raise MathExpressionError("Result is not finite")
    if abs(value) > _MAX_ABS:
        raise MathExpressionError("Result is too large")
    return value


def format_result(value: int | float) -> str:
    """Return a compact human-readable result."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
