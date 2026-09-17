from forge.voice.math import MathExpressionError, calculate, format_result, normalize_expression


def test_basic_arithmetic():
    assert calculate("2+2") == 4
    assert calculate("12 * 5 - 10") == 50
    assert calculate("(10 + 2) / 3") == 4


def test_spoken_arithmetic():
    assert normalize_expression("two plus two") == "2 + 2"
    assert calculate("two plus two") == 4
    assert calculate("what is 25 times 4") == 100


def test_result_formatting():
    assert format_result(4.0) == "4"
    assert format_result(4.5) == "4.5"


def test_unsafe_syntax_is_rejected():
    for expression in ("__import__('os')", "open('x')", "2 ** 1000"):
        try:
            calculate(expression)
        except MathExpressionError:
            pass
        else:
            raise AssertionError("unsafe/oversized expression was accepted")
