from forge.core.autonomous_admission import AutonomousAdmission

def test_autonomous_admission_bounds_concurrency_and_retries():
    policy = AutonomousAdmission(max_active_tasks=4, max_attempts_per_task=8)
    assert policy.can_start(3)
    assert not policy.can_start(4)
    assert policy.can_retry(7)
    assert not policy.can_retry(8)
