from __future__ import annotations

from forge.runtime.runtime import ToolResult
from forge.security.verification import (
    ReviewGate,
    SecurityGate,
    VerificationPipeline,
)


def test_security_gate_detects_dangerous_eval():
    gate = SecurityGate()
    res = gate.verify({"file.py": "x = eval(user_input)"})
    assert not res.success
    assert res.stage == "security"
    assert any("eval()" in err for err in res.errors)


def test_review_gate_rejects_rejection_marker():
    gate = ReviewGate()
    res = gate.verify({"code.py": "def foo(): # REJECT_REVIEW\n pass"})
    assert not res.success
    assert res.stage == "review"


def test_verification_pipeline_accepts_clean_code():
    pipeline = VerificationPipeline()
    results = pipeline.verify_candidate(
        changes={"main.py": "print('hello')"},
        test_result=ToolResult.ok("pytest", output="passed"),
    )

    assert all(r.success for r in results)
    acceptance = results[-1]
    assert acceptance.stage == "acceptance"
    assert acceptance.success


def test_verification_pipeline_rejects_on_security_failure():
    pipeline = VerificationPipeline()
    results = pipeline.verify_candidate(
        changes={"app.py": "OPENAI_API_KEY = 'sk-1234'"},
        test_result=ToolResult.ok("pytest", output="passed"),
    )

    sec_res = [r for r in results if r.stage == "security"][0]
    acc_res = [r for r in results if r.stage == "acceptance"][0]

    assert not sec_res.success
    assert not acc_res.success
