"""Automated debugging loop (A83).

Bounded failure → diagnose → fix → rebuild → re-test → regression, with every
attempt recorded in a ledger.
"""
from __future__ import annotations

from forge.debug.diagnosis import (
    CATEGORIES,
    Diagnosis,
    diagnose_build_failure,
    diagnose_test_failure,
)
from forge.debug.loop import (
    AutomatedDebugLoop,
    CallableFixStrategy,
    DebugAttempt,
    DebugReport,
    FailureContext,
    FixLedger,
    FixStrategy,
)

__all__ = [
    "AutomatedDebugLoop", "CATEGORIES", "CallableFixStrategy", "DebugAttempt",
    "DebugReport", "Diagnosis", "FailureContext", "FixLedger", "FixStrategy",
    "diagnose_build_failure", "diagnose_test_failure",
]
