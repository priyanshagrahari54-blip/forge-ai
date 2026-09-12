"""A81 connection tests: state machine + deterministic auto-reconnect.

The fake clock makes the reconnect schedule exactly assertable: no
sleeping, no flakes, no wall-clock dependence.
"""
from __future__ import annotations

import time

from forge.client.config import ReconnectPolicy
from forge.client.connection import ConnectionManager, ConnectionState
from forge.link.errors import AuthError, TransportError


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSleeper:
    """Records sleeps; advancing the clock is explicit."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.sleeps: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock.advance(seconds)


class FakeTransport:
    """Scriptable transport: raises or succeeds on demand."""

    def __init__(self) -> None:
        self.connected = False
        self.script: list = []       # exceptions to raise, then success
        self.calls = 0
        self.expires_in = 3600.0
        self.now = 0.0
        self.reset_calls = 0

    def reset(self):
        self.reset_calls += 1
        self.connected = False

    @property
    def expires_at(self):
        return self.now + self.expires_in

    def connect(self):
        self.calls += 1
        outcome = self.script.pop(0) if self.script else "ok"
        if outcome == "auth":
            raise AuthError("authentication failed")
        if isinstance(outcome, Exception):
            raise outcome
        self.connected = True

    def get(self, path):
        if not self.connected:
            raise TransportError("offline")
        return {"workers": 2, "model_ready": True}


def _manager(transport, policy, seed=1):
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    manager = ConnectionManager(transport, policy, clock=clock,
                                sleeper=sleeper, seed=seed)
    return manager, clock, sleeper


def test_connect_success_and_state_transitions():
    transport = FakeTransport()
    manager, _, _ = _manager(transport, ReconnectPolicy())
    assert manager.state is ConnectionState.DISCONNECTED
    assert manager.connect()
    assert manager.state is ConnectionState.CONNECTED
    manager.disconnect()
    assert manager.state is ConnectionState.DISCONNECTED


def test_transport_failure_schedules_reconnect_ladder():
    transport = FakeTransport()
    transport.script = [TransportError("refused"), "ok"]
    policy = ReconnectPolicy(initial_delay=1.0, max_delay=8.0,
                             multiplier=2.0)
    manager, _, sleeper = _manager(transport, policy, seed=7)
    assert manager.ensure_connected()  # second attempt succeeds
    # Exactly one reconnect sleep happened, between base delay and
    # base + 10% jitter.
    assert len(sleeper.sleeps) == 1
    delay = sleeper.sleeps[0]
    assert 1.0 <= delay <= 1.1 + 1e-9
    assert manager.state is ConnectionState.CONNECTED
    assert manager.attempt == 0  # success resets the ladder


def test_reconnect_ladder_grows_deterministically():
    transport = FakeTransport()
    transport.script = [TransportError("down")] * 5 + ["ok"]
    policy = ReconnectPolicy(initial_delay=1.0, max_delay=8.0,
                             multiplier=2.0)
    manager, _, sleeper = _manager(transport, policy, seed=42)
    assert manager.ensure_connected()
    # 5 failures -> 5 sleeps; delays follow the ladder + jitter cap.
    assert len(sleeper.sleeps) == 5
    for i, actual in enumerate(sleeper.sleeps):
        base = min(1.0 * 2 ** i, 8.0)
        assert base <= actual <= base * 1.1 + 1e-9


def test_same_seed_same_schedule():
    def run(seed):
        transport = FakeTransport()
        transport.script = [TransportError("down"), TransportError("down"),
                            "ok"]
        policy = ReconnectPolicy(initial_delay=1.0, max_delay=30.0,
                                 multiplier=2.0)
        manager, _, sleeper = _manager(transport, policy, seed=seed)
        assert manager.ensure_connected()
        return sleeper.sleeps

    assert run(123) == run(123)
    assert run(123) != run(124)


def test_auth_failure_is_terminal_until_reset():
    transport = FakeTransport()
    transport.script = ["auth", "ok", "ok"]
    manager, _, sleeper = _manager(transport, ReconnectPolicy())
    assert not manager.ensure_connected()
    assert manager.state is ConnectionState.AUTH_FAILED
    assert sleeper.sleeps == []      # wrong credentials never retry
    # A new connect() attempt is allowed (operator fixed something)...
    assert manager.connect()          # script now yields success
    assert manager.state is ConnectionState.CONNECTED


def test_policy_exhaustion_stops_retrying():
    transport = FakeTransport()
    transport.script = [TransportError("down")] * 10
    policy = ReconnectPolicy(initial_delay=1.0, max_delay=4.0,
                             multiplier=1.0, max_attempts=3)
    manager, _, sleeper = _manager(transport, policy)
    assert not manager.ensure_connected()
    # max_attempts bounds the total connect attempts (initial included):
    # 3 attempts, 2 retry sleeps.
    assert transport.calls == 3
    assert len(sleeper.sleeps) == 2
    assert manager.state is ConnectionState.RECONNECTING


def test_budget_attempts_bounds_the_wait():
    transport = FakeTransport()
    transport.script = [TransportError("down")] * 10
    manager, _, sleeper = _manager(transport, ReconnectPolicy())
    assert not manager.ensure_connected(budget_attempts=2)
    assert len(sleeper.sleeps) == 1


def test_reset_clears_attempts_and_error():
    transport = FakeTransport()
    transport.script = [TransportError("down")]
    manager, _, _ = _manager(transport, ReconnectPolicy())
    manager.connect()
    assert manager.attempt == 1
    assert manager.last_error == "down"
    manager.reset()
    assert manager.attempt == 0
    assert manager.last_error == ""
    assert manager.state is ConnectionState.DISCONNECTED


def test_heartbeat_reconnects_after_drop():
    import threading

    transport = FakeTransport()
    manager = ConnectionManager(transport,
                                ReconnectPolicy(initial_delay=0.1),
                                clock=time.monotonic, sleeper=time.sleep,
                                seed=3)
    assert manager.connect()
    seen = threading.Event()
    events: list[str] = []

    def listener(state, detail):
        events.append(state)
        if state == "connected" and transport.connected:
            seen.set()

    manager.listeners.append(listener)
    manager.start_heartbeat(0.05)
    try:
        # Simulate a drop: the next probe sees a dead transport and the
        # loop reconnects automatically (requirement 7).
        transport.connected = False
        transport.script = ["ok"]
        assert seen.wait(timeout=5.0), f"never reconnected: {events}"
        assert manager.state is ConnectionState.CONNECTED
    finally:
        manager.stop_heartbeat()


def test_heartbeat_stops_promptly():
    transport = FakeTransport()
    manager, _, _ = _manager(transport, ReconnectPolicy())
    manager.start_heartbeat(0.05)
    manager.stop_heartbeat()
    assert manager._heartbeat_thread is None
