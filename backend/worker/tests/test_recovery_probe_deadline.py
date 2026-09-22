"""Issue #161 — a startup probe has to bound the *step*, not just the wait.

`_step_balance` and `_step_positions` used to run the broker call inside
`with ThreadPoolExecutor(...) as ex`. `.result(timeout=...)` raised on schedule,
but `__exit__` then called `shutdown(wait=True)` and the block sat there until
the call came back. So the deadline bounded *waiting for the result* and never
the step.

That matters because PR #159 gave the worker a graceful shutdown, and Docker
sends SIGKILL 10 seconds after SIGTERM. A restart that landed during a balance
probe lost the equity checkpoint and the `worker_shutdown` audit row — the two
things #159 exists to produce.

The numbers are not close. One KIS GET is already worst-case ~32s: the HTTP
deadline is 10s (`kis_adapter.auth._http_timeout`) and GETs retry three times
with a 1s pause. `get_balance` makes two of those plus an FX lookup, and
`get_positions` makes one per holding. Against a 10s SIGKILL.

`shutdown(wait=False)` is not the fix either: `concurrent.futures.thread` builds
non-daemon workers and joins them from an `atexit` hook, so an abandoned call
would block interpreter exit instead. A daemon thread has neither problem.
"""
import threading
import time

import pytest

from backend.worker.recovery import (
    RecoveryAborted, StartupRecovery, _call_with_deadline,
)


class _Hang:
    """A broker that accepts the call and never answers — a half-open socket."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self):
        self.entered.set()
        self.release.wait()          # released in teardown, never by the test
        return "late"


@pytest.fixture(autouse=True)
def _isolate_safe_mode():
    """SAFE_MODE is a module-level singleton; `run()` writes to it."""
    from backend.worker.recovery import SAFE_MODE
    saved = (SAFE_MODE._can_trade, SAFE_MODE._reason, SAFE_MODE._cause)
    yield
    SAFE_MODE._can_trade, SAFE_MODE._reason, SAFE_MODE._cause = saved


@pytest.fixture()
def hang():
    h = _Hang()
    yield h
    h.release.set()                  # let the daemon thread finish and exit


class TestTheDeadlineBoundsTheCall:
    def test_it_returns_at_the_deadline_instead_of_waiting_for_the_call(
            self, hang):
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            _call_with_deadline(hang, 0.5, label="test")
        elapsed = time.monotonic() - t0

        assert hang.entered.is_set(), "the call never started — wrong thing timed"
        assert elapsed < 2.0, (
            f"returned after {elapsed:.2f}s; the deadline did not bound the call")

    def test_the_abandoned_call_runs_on_a_daemon_thread(self, hang):
        """Otherwise it blocks interpreter exit instead of the step.

        `concurrent.futures` workers are non-daemon and joined at exit, which is
        why swapping in `shutdown(wait=False)` would have moved the stall rather
        than removed it.
        """
        before = {t for t in threading.enumerate()}
        with pytest.raises(TimeoutError):
            _call_with_deadline(hang, 0.3, label="probe")

        new = [t for t in threading.enumerate() if t not in before]
        assert new, "expected the abandoned call to still be running"
        assert all(t.daemon for t in new), (
            f"non-daemon threads left behind: {[t.name for t in new]}")

    def test_a_stop_signal_cuts_the_wait_short(self, hang):
        """A SIGTERM mid-probe must not sit out the whole deadline."""
        t0 = time.monotonic()
        with pytest.raises(RecoveryAborted):
            _call_with_deadline(hang, 30.0, should_abort=lambda: True,
                                label="probe")
        elapsed = time.monotonic() - t0

        assert elapsed < 2.0, (
            f"took {elapsed:.2f}s to notice the stop signal")

    def test_a_result_is_returned_and_an_error_is_relayed(self):
        assert _call_with_deadline(lambda: 7, 5.0, label="ok") == 7

        def _boom():
            raise ValueError("broker said no")

        with pytest.raises(ValueError, match="broker said no"):
            _call_with_deadline(_boom, 5.0, label="boom")

    def test_it_does_not_wait_the_full_deadline_on_a_fast_call(self):
        """The poll interval must not become a floor on every probe."""
        t0 = time.monotonic()
        _call_with_deadline(lambda: "quick", 10.0, label="fast")
        assert time.monotonic() - t0 < 1.0


class TestTheStepsUseIt:
    @staticmethod
    def _recovery(broker):
        return StartupRecovery(None, redis_client=None, broker=broker,
                               poller=None)

    def test_balance_step_fails_fast_when_the_broker_hangs(
            self, hang, monkeypatch):
        monkeypatch.setattr("backend.worker.recovery._BROKER_STARTUP_TIMEOUT", 0.5)

        class _Broker:
            get_balance = staticmethod(hang)

        t0 = time.monotonic()
        assert self._recovery(_Broker())._step_balance() is False
        elapsed = time.monotonic() - t0

        assert elapsed < 2.0, (
            f"_step_balance took {elapsed:.2f}s — the step is still unbounded")

    def test_positions_step_fails_fast_when_the_broker_hangs(
            self, hang, monkeypatch):
        monkeypatch.setattr("backend.worker.recovery._BROKER_STARTUP_TIMEOUT", 0.5)

        class _Broker:
            get_positions = staticmethod(hang)

        t0 = time.monotonic()
        assert self._recovery(_Broker())._step_positions() is False
        elapsed = time.monotonic() - t0

        assert elapsed < 2.0, (
            f"_step_positions took {elapsed:.2f}s — the step is still unbounded")

    def test_a_stop_signal_during_the_balance_probe_ends_the_step(
            self, hang, monkeypatch):
        """The case #159 could not reach: abort *inside* a step, not between."""
        monkeypatch.setattr("backend.worker.recovery._BROKER_STARTUP_TIMEOUT", 30.0)

        class _Broker:
            get_balance = staticmethod(hang)

        rec = StartupRecovery(None, redis_client=None, broker=_Broker(),
                              poller=None, should_abort=lambda: True)

        t0 = time.monotonic()
        with pytest.raises(RecoveryAborted):
            rec._step_balance()
        assert time.monotonic() - t0 < 2.0, "sat out the deadline instead"

    def test_an_abort_is_not_recorded_as_a_broker_failure(
            self, hang, monkeypatch):
        """A stop signal and a KIS outage must not look the same afterwards.

        Flattening the abort to `return False` made `run()` record
        `복구 실패: 브로커 잔고 조회` — indistinguishable from a real outage, and
        it discarded the deliberate-shutdown reason the boundary check is
        careful to write.
        """
        from backend.worker.recovery import SAFE_MODE

        monkeypatch.setattr("backend.worker.recovery._BROKER_STARTUP_TIMEOUT", 30.0)
        monkeypatch.setattr(StartupRecovery, "_step_db", lambda self: True)
        monkeypatch.setattr(StartupRecovery, "_step_redis", lambda self: True)
        monkeypatch.setattr(StartupRecovery, "_step_risk", lambda self: True)

        class _Broker:
            get_balance = staticmethod(hang)

        # Abort only once the balance probe is actually running, so the boundary
        # check cannot be what stops the sequence.
        rec = StartupRecovery(None, redis_client=None, broker=_Broker(),
                              poller=None,
                              should_abort=lambda: hang.entered.is_set())

        assert rec.run() is False
        assert hang.entered.is_set(), "the probe never ran — wrong path tested"
        assert SAFE_MODE.can_trade is False
        assert "종료 요청" in (SAFE_MODE._reason or ""), (
            f"recorded {SAFE_MODE._reason!r}, which reads as a broker failure")

    def test_a_healthy_broker_still_succeeds(self):
        class _Bal:
            total_eval_krw = 2_000_000.0

        class _Broker:
            @staticmethod
            def get_balance():
                return _Bal()

            @staticmethod
            def get_positions():
                return []

        rec = self._recovery(_Broker())
        assert rec._step_balance() is True
        assert rec._step_positions() is True
