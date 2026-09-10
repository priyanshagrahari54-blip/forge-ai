from forge.self_development import (
    Finding,
    FindingCategory,
    FindingSeverity,
    ImprovementGenerator,
    ImprovementPriority,
)


def test_improvement_generator():
    findings = [
        Finding(
            id="FINDING-001",
            category=FindingCategory.TODO_FIXME.value,
            severity=FindingSeverity.LOW.value,
            evidence="forge/foo.py:1: # TODO fix this",
            affected_files=["forge/foo.py"],
            description="Low severity TODO",
            proposed_improvement="Fix TODO in forge/foo.py",
            estimated_complexity="low",
            estimated_risk="low",
        ),
        Finding(
            id="FINDING-002",
            category=FindingCategory.SECURITY.value,
            severity=FindingSeverity.CRITICAL.value,
            evidence="forge/bar.py:5: hardcoded key",
            affected_files=["forge/bar.py"],
            description="Critical security issue",
            proposed_improvement="Remove secret from forge/bar.py",
            estimated_complexity="low",
            estimated_risk="low",
        ),
    ]

    generator = ImprovementGenerator()
    candidates = generator.generate(findings)

    assert len(candidates) == 2
    # The critical finding should be ranked first (higher score)
    assert candidates[0].finding_id == "FINDING-002"
    assert candidates[0].priority == ImprovementPriority.CRITICAL.value
    assert candidates[0].score > candidates[1].score
