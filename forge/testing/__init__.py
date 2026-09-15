"""Unified testing pipeline (A83).

Static analysis → unit → integration → system → regression → performance,
each stage run for real and parsed into per-test outcomes. Compiling is never
reported as passing tests.
"""
from __future__ import annotations

from forge.testing.engine import (
    STAGE_ORDER,
    StageResult,
    TestEngine,
    TestReport,
    TestStage,
    render,
)
from forge.testing.results import (
    TestCase,
    TestParse,
    detect_parser,
    parse_test_output,
)

__all__ = [
    "STAGE_ORDER", "StageResult", "TestCase", "TestEngine", "TestParse",
    "TestReport", "TestStage", "detect_parser", "parse_test_output", "render",
]
