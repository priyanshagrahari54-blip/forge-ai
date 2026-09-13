"""Connection state machine + automatic reconnect (A81, requirement 7).

States::

    DISCONNECTED ──connect()──▶ CONNECTING ──ok──▶ CONNECTED
        ▲                          │                  │
        │                     failure               failure
        │                          ▼                  ▼
        └──────────────────── RECONNECTING ◀─────────┘
                                       │
                    credentials wrong  ▼
                                  AUTH_FAILED   (no auto-retry;
                                                requires re-setup)

The reconnect delay follows the :class:`~forge.client.config.
ReconnectPolicy` exponential ladder plus deterministic jitter from a
seeded RNG. Clock and sleep are injectable, so tests assert the exact
schedule with a fake clock — no real waiting, no flakes.

AUTH_FAILED is terminal-by-design: wrong credentials must never hammer
a server. Changing the settings (or rotating the secret) moves the state
back to DISCONNECTED via :meth:`ConnectionManager.reset`.
"""
from __future__ import annotations

import random
import threading
import time
from enum import Enum
from typing import Callable, List, Optional

from forge.link.errors import AuthError


class ConnectionState(Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    AUTH_FAILED = "AUTH_FAILED"


Clock = Callable[[], float]
Sleeper = Callable[[float], None]

#: Jitter fraction applied to each computed delay (deterministic seed).
JITTER_FRACTION = 0.1


class ConnectionManager:
    """Tracks link connectivity and owns the reconnect schedule.

    Threading: :meth:`connect`/:meth:`ensure_connected` may be called
    from the UI poller thread; a background heartbeat thread (optional,
    :meth:`start_heartbeat`) detects drops and re-establishes the link
    automatically. All state transitions are lock-guarded.
    """

    def __init__(self, transport, policy, *,
                 clock: Optional[Clock] = None,
                 sleeper: Optional[Sleeper] = None,
                 seed: int = 0xF4B51) -> None:
        self._transport = transport
        self._policy = policy
        self._clock = clock or time.monotonic
        self._sleep = sleeper or time.sleep
        self._rng = random.Random(seed)
        self._lock = threading.RLock()
        self._state = ConnectionState.DISCONNECTED
        self._attempt = 0
        self._last_error = ""
        self._last_connected_at = 0.0
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None
        #: Observers appended by the UI: fn(state:str, detail:str).
        self.listeners: List[Callable[[str, str], None]] = []

    # -- introspection ---------------------------------------------------------

    @property
    def state(self) -> ConnectionState:
        with self._lock:
            return self._state

    @property
    def state_name(self) -> str:
        return self.state.value

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    @property
    def attempt(self) -> int:
        with self._lock:
            return self._attempt

    # -- lifecycle ---------------------------------------------------------------

    def reset(self) -> None:
        """Back to DISCONNECTED (settings changed / explicit disconnect)."""
        with self._lock:
            self._transport.reset()
            self._state = ConnectionState.DISCONNECTED
            self._attempt = 0
            self._last_error = ""
        self._notify("disconnected", "reset")

    def connect(self) -> bool:
        """One connection attempt. Returns True when connected."""
        with self._lock:
            if self._state == ConnectionState.CONNECTED \
                    and self._transport.connected:
                return True
            self._state = ConnectionState.CONNECTING
        self._notify("connecting", "")
        try:
            self._transport.connect()
        except AuthError as exc:
            with self._lock:
                self._state = ConnectionState.AUTH_FAILED
                self._last_error = str(exc)
            self._notify("auth_failed", str(exc))
            return False
        except Exception as exc:  # transport/protocol failures
            with self._lock:
                self._state = ConnectionState.RECONNECTING
                self._last_error = str(exc)
                self._attempt += 1
            self._notify("reconnecting", str(exc))
            return False
        with self._lock:
            self._state = ConnectionState.CONNECTED
            self._attempt = 0
            self._last_error = ""
            self._last_connected_at = self._clock()
        self._notify("connected", "")
        return True

    def disconnect(self) -> None:
        self.stop_heartbeat()
        self.reset()

    # -- reconnect scheduling (deterministic) -----------------------------------

    def next_delay(self) -> float:
        """Delay before the next reconnect attempt (deterministic given
        the seeded RNG): base ladder + bounded jitter.

        The first retry waits ``initial_delay``; each further failure
        multiplies the previous wait (capped at ``max_delay``).
        """
        with self._lock:
            attempt = self._attempt
        base = self._policy.delay_for(max(0, attempt - 1))
        jitter = base * JITTER_FRACTION * self._rng.random()
        return float(base + jitter)

    def ensure_connected(self, *, budget_attempts: Optional[int] = None) \
            -> bool:
        """Connect now; on failure, retry per policy (blocking, bounded
        when ``budget_attempts`` is given or the policy caps attempts).

        AUTH_FAILED never retries: the operator must fix credentials.
        """
        attempt = 0
        while True:
            if self.connect():
                return True
            with self._lock:
                if self._state == ConnectionState.AUTH_FAILED:
                    return False
            if budget_attempts is not None and attempt + 1 >= budget_attempts:
                return False
            if self._policy.exhausted(attempt + 1):
                return False
            self._sleep(self.next_delay())
            attempt += 1

    # -- background heartbeat ------------------------------------------------

    def start_heartbeat(self, interval: float, *,
                        poll: Optional[Callable[[], bool]] = None) -> None:
        """Detect drops in the background and reconnect automatically.

        ``poll`` is the liveness probe (default: transport ``get info``);
        it must raise on failure and is executed on the heartbeat thread.
        """
        self.stop_heartbeat()
        self._heartbeat_stop.clear()
        poll = poll or (lambda: bool(self._transport.get(
            "/api/v1/link/info")))
        interval = max(1.0, float(interval))

        def loop() -> None:
            while not self._heartbeat_stop.wait(interval):
                try:
                    if self._transport.connected:
                        poll()
                        with self._lock:
                            if self._state != ConnectionState.CONNECTED:
                                self._state = ConnectionState.CONNECTED
                                self._attempt = 0
                                self._notify("connected", "heartbeat")
                        continue
                    with self._lock:
                        exhausted = self._policy.exhausted(self._attempt)
                    if exhausted:
                        # Policy says stop retrying; wait for manual
                        # connect() or a settings change.
                        continue
                    # One attempt per tick: the tick interval sets the
                    # retry cadence; nothing sleeps inside ensure_connected.
                    self.ensure_connected(budget_attempts=1)
                except AuthError:
                    with self._lock:
                        self._state = ConnectionState.AUTH_FAILED
                        self._last_error = "authentication failed"
                    self._notify("auth_failed", "heartbeat rejected")
                except Exception as exc:  # noqa: BLE001 - probe failed
                    with self._lock:
                        self._state = ConnectionState.RECONNECTING
                        self._last_error = str(exc)
                    self._notify("reconnecting", str(exc))

        self._heartbeat_thread = threading.Thread(
            target=loop, name="forge-link-heartbeat", daemon=True)
        self._heartbeat_thread.start()

    def stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        thread = self._heartbeat_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        self._heartbeat_thread = None

    # -- listeners -------------------------------------------------------------

    def _notify(self, state: str, detail: str) -> None:
        for listener in list(self.listeners):
            try:
                listener(state, detail)
            except Exception:  # noqa: BLE001 - UI listeners must not break
                pass
