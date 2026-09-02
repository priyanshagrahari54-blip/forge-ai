from forge.core.state import ForgeState, ForgeStatus


def test_state_lifecycle():
    state = ForgeState("test-project")

    assert state.status == ForgeStatus.IDLE

    state.start_task("Build feature")

    assert state.status == ForgeStatus.RUNNING
    assert state.current_task == "Build feature"
    assert state.iteration == 1

    state.complete_task()

    assert state.status == ForgeStatus.COMPLETED
