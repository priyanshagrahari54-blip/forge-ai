from forge.core.autonomous_workload import AutonomousWorkloadController, WorkBudget, WorkItem


def test_queue_has_no_total_task_count_limit():
    controller = AutonomousWorkloadController(budget=WorkBudget(max_active_tasks=1))
    for index in range(100):
        controller.submit(WorkItem(str(index), f"task {index}"))
    assert controller.snapshot()["queued"] == 100


def test_retry_is_bounded_per_task():
    controller = AutonomousWorkloadController(budget=WorkBudget(max_attempts_per_task=2))
    controller.submit(WorkItem("a", "do it"))
    item = controller.next()
    assert item is not None
    assert controller.retry("a", reason="temporary")
    item = controller.next()
    assert item is not None
    assert not controller.retry("a", reason="still broken")
    assert controller.snapshot()["failed"] == 1
