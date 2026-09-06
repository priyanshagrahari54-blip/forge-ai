from pathlib import Path
from forge.self_development import (
    Finding,
    FindingCategory,
    FindingSeverity,
    ImprovementGenerator,
    SelfDevelopmentTask,
)


def test_deterministic_candidate_identity():
    finding = Finding(
        id="FINDING-001",
        category=FindingCategory.TODO_FIXME.value,
        severity=FindingSeverity.LOW.value,
        evidence="forge/sample.py:1: TODO fix",
        affected_files=["forge/sample.py"],
        description="Fix TODO comment",
        proposed_improvement="Implement function in sample.py",
    )

    gen = ImprovementGenerator()
    cands1 = gen.generate([finding])
    cands2 = gen.generate([finding])

    assert len(cands1) == 1
    assert cands1[0].id == cands2[0].id
    assert len(cands1[0].candidate_hash) == 12

    task = SelfDevelopmentTask.from_candidate(cands1[0])
    assert task.candidate_id == cands1[0].id
    assert "Modify only necessary files" in task.instructions
