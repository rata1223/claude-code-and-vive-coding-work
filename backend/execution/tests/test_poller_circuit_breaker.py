"""P2-06 — the fill poller's circuit breaker.

Poll failures used to trip nothing: ten in a row logged CRITICAL and the loop
kept calling a broker that could not answer. Worse, it kept timing orders out:
30 minutes after registration an order was timed out whether or not anyone
could read its status, and the worker converged it to CANCELED even when the
cancel failed — though the broker may have filled it.

Now ``_BREAKER_THRESHOLD`` consecutive failures open the breaker: no lookups,
no timeouts. After a cooldown one lookup tests the broker; a failure doubles the
cooldown (capped), a success closes it and polling resumes.

Driven tick by tick through ``_poll_due`` on a fake clock.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import backend.execution.order_poller as op
from backend.brokers.models import Order, OrderStatus
from backend.execution.order_poller import OrderFillPoller


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture()
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(op, "_monotonic", c)
    return c


class _Broker:
    """``get_order_status`` raises while ``down``; otherwise returns ``status``."""

    def __init__(self):
        self.down = False
        self.calls = []
        self.status = {}

    def get_order_status(self, order_id, symbol=""):
        self.calls.append(order_id)
        if self.down:
            raise RuntimeError("KIS down")
        return self.status.get(order_id)

    cancel_order = MagicMock()


def _order(oid, qty=10):
    return Order(id=oid, symbol="AAPL", side="buy", qty=qty, price=100.0,
                 status=OrderStatus.SUBMITTED)


def _setup(clock, n=1, **kw):
    broker = _Broker()
    broker.cancel_order = MagicMock()
    opened, closed = [], []
    poller = OrderFillPoller(broker, on_circuit_open=lambda c, s: opened.append((c, s)),
                             on_circuit_close=lambda d: closed.append(d), **kw)
    cbs = {}
    for i in range(n):
        oid = f"O{i}"
        cbs[oid] = {"filled": MagicMock(), "timeout": MagicMock()}
        poller.register(_order(oid), on_filled=cbs[oid]["filled"],
                        on_timeout=cbs[oid]["timeout"])
    return poller, broker, opened, closed, cbs


def _tick(poller, clock, advance=0.0):
    clock.t += advance
    for e in list(poller._entries.values()):   # make every entry due this tick
        e.next_poll_at = clock.t
    poller._poll_due(clock.t)


def _expire(poller, *ids):
    for oid, e in poller._entries.items():
        if not ids or oid in ids:
            e.registered_at = datetime.now(timezone.utc) - timedelta(minutes=31)


def _open(poller, broker, clock):
    broker.down = True
    for _ in range(op._BREAKER_THRESHOLD):
        _tick(poller, clock)
    assert poller.health.circuit_open


class TestOpening:
    def test_the_threshold_opens_it_once(self, clock):
        poller, broker, opened, _, _ = _setup(clock)
        broker.down = True
        for _ in range(op._BREAKER_THRESHOLD - 1):
            _tick(poller, clock)
        assert not poller.health.circuit_open and poller.health.is_healthy
        _tick(poller, clock)
        h = poller.health
        assert h.circuit_open and not h.is_healthy and h.circuit_opened_at is not None
        assert opened == [(op._BREAKER_THRESHOLD, op._BREAKER_COOLDOWN_SEC)]

    def test_a_success_in_between_resets_the_count(self, clock):
        poller, broker, opened, _, _ = _setup(clock)
        for down in [True] * 4 + [False] + [True] * 4:
            broker.down = down
            _tick(poller, clock)
        assert not poller.health.circuit_open and opened == []

    def test_failures_across_orders_count_together(self, clock):
        """An outage fails every order's lookup — that is the signal."""
        poller, broker, opened, _, _ = _setup(clock, n=5)
        broker.down = True
        _tick(poller, clock)            # one tick, five orders, five failures
        assert poller.health.circuit_open and len(opened) == 1

    def test_it_stops_mid_tick_once_open(self, clock):
        poller, broker, _, _, _ = _setup(clock, n=8)
        broker.down = True
        _tick(poller, clock)
        assert len(broker.calls) == op._BREAKER_THRESHOLD

    def test_a_fill_callback_error_does_not_count(self, clock):
        """A callback that raises is retried by the poller; it is not a broker
        failure and must not open the breaker."""
        poller, broker, opened, _, cbs = _setup(clock)
        cbs["O0"]["filled"].side_effect = RuntimeError("db down")
        broker.status["O0"] = Order(id="O0", symbol="AAPL", side="buy", qty=10, price=100.0,
                                    status=OrderStatus.FILLED, filled_qty=10,
                                    avg_fill_price=100.0)
        for _ in range(op._BREAKER_THRESHOLD + 2):
            _tick(poller, clock)
        assert not poller.health.circuit_open and opened == []


class TestWhileOpen:
    def test_no_lookups_before_the_cooldown(self, clock):
        poller, broker, _, _, _ = _setup(clock, n=3)
        _open(poller, broker, clock)
        before = len(broker.calls)
        for _ in range(5):
            _tick(poller, clock, advance=op._BREAKER_COOLDOWN_SEC / 6)
        assert len(broker.calls) == before

    def test_no_timeouts_while_open(self, clock):
        """The order's status is unknown: it must not be cancelled and closed."""
        poller, broker, _, _, cbs = _setup(clock)
        _open(poller, broker, clock)
        _expire(poller)
        _tick(poller, clock, advance=1)
        cbs["O0"]["timeout"].assert_not_called()
        broker.cancel_order.assert_not_called()
        assert "O0" in poller._entries

    def test_no_timeout_on_the_tick_that_opens_it(self, clock):
        poller, broker, _, _, cbs = _setup(clock, n=op._BREAKER_THRESHOLD + 1)
        broker.down = True
        for _ in range(op._BREAKER_THRESHOLD - 1):
            poller._health.record_poll_error()     # one failure short of opening
        _expire(poller, "O0", "O2", "O3")
        _tick(poller, clock)
        # Entries go in registration order: O0 times out (breaker still closed,
        # no lookup), O1's lookup fails and opens it, and the tick stops there —
        # O2 and O3 are expired too but are not timed out.
        assert sum(c["timeout"].call_count for c in cbs.values()) == 1
        cbs["O2"]["timeout"].assert_not_called()
        assert len(broker.calls) == 1
        assert len(poller._entries) == op._BREAKER_THRESHOLD


class TestProbe:
    def test_one_lookup_after_the_cooldown(self, clock):
        poller, broker, _, _, _ = _setup(clock, n=4)
        _open(poller, broker, clock)
        before = len(broker.calls)
        _tick(poller, clock, advance=op._BREAKER_COOLDOWN_SEC)
        assert len(broker.calls) == before + 1

    def test_a_failed_probe_doubles_the_cooldown_up_to_the_cap(self, clock):
        poller, broker, _, _, _ = _setup(clock)
        _open(poller, broker, clock)
        waits = []
        cooldown = op._BREAKER_COOLDOWN_SEC
        for _ in range(5):
            _tick(poller, clock, advance=cooldown)          # probe fails
            calls = len(broker.calls)
            # Not due again until the new cooldown has passed.
            nxt = min(cooldown * 2, op._BREAKER_MAX_COOLDOWN_SEC)
            _tick(poller, clock, advance=nxt - 1)
            assert len(broker.calls) == calls, f"probed early (cooldown {nxt})"
            clock.t += 1 - nxt                                # rewind to the probe time
            waits.append(nxt)
            cooldown = nxt
        assert waits == [120, 240, 300, 300, 300]
        assert poller.health.circuit_open

    def test_a_successful_probe_closes_it_and_polling_resumes(self, clock):
        poller, broker, _, closed, cbs = _setup(clock, n=3)
        _open(poller, broker, clock)
        broker.down = False
        broker.status["O1"] = Order(id="O1", symbol="AAPL", side="buy", qty=10, price=100.0,
                                    status=OrderStatus.FILLED, filled_qty=10,
                                    avg_fill_price=100.0)
        _tick(poller, clock, advance=op._BREAKER_COOLDOWN_SEC)
        h = poller.health
        assert not h.circuit_open and h.consecutive_poll_errors == 0 and h.is_healthy
        assert len(closed) == 1 and closed[0] >= op._BREAKER_COOLDOWN_SEC
        before = len(broker.calls)
        _tick(poller, clock, advance=1)
        assert len(broker.calls) - before == 3          # every entry polled again
        cbs["O1"]["filled"].assert_called_once()        # the fill made during the outage

    def test_after_recovery_an_expired_open_order_times_out_as_before(self, clock):
        poller, broker, _, _, cbs = _setup(clock)
        _open(poller, broker, clock)
        _expire(poller)
        broker.down = False
        _tick(poller, clock, advance=op._BREAKER_COOLDOWN_SEC)   # probe closes it
        _tick(poller, clock, advance=1)
        cbs["O0"]["timeout"].assert_called_once()

    def test_the_cooldown_starts_over_after_it_closes(self, clock):
        poller, broker, opened, _, _ = _setup(clock)
        _open(poller, broker, clock)
        _tick(poller, clock, advance=op._BREAKER_COOLDOWN_SEC)   # failed probe → 120
        broker.down = False
        _tick(poller, clock, advance=120)                         # closes
        _open(poller, broker, clock)
        assert opened[-1] == (op._BREAKER_THRESHOLD, op._BREAKER_COOLDOWN_SEC)


class TestCallbacks:
    def test_a_raising_alert_does_not_stop_the_loop(self, clock):
        broker = _Broker()
        poller = OrderFillPoller(broker, on_circuit_open=MagicMock(side_effect=RuntimeError("tg")),
                                 on_circuit_close=MagicMock(side_effect=RuntimeError("tg")))
        poller.register(_order("O0"), on_filled=MagicMock())
        broker.down = True
        for _ in range(op._BREAKER_THRESHOLD):
            _tick(poller, clock)
        assert poller.health.circuit_open
        broker.down = False
        _tick(poller, clock, advance=op._BREAKER_COOLDOWN_SEC)
        assert not poller.health.circuit_open

    def test_without_callbacks_it_still_works(self, clock):
        broker = _Broker()
        poller = OrderFillPoller(broker)
        poller.register(_order("O0"), on_filled=MagicMock())
        broker.down = True
        for _ in range(op._BREAKER_THRESHOLD):
            _tick(poller, clock)
        assert poller.health.circuit_open


def test_the_worker_alerts_on_both_transitions(monkeypatch):
    import bot.notifier as notifier
    from backend.worker import runner

    sent = []
    monkeypatch.setattr(notifier, "alert_emergency", lambda m: sent.append(("emergency", m)))
    monkeypatch.setattr(notifier, "send_alert", lambda m: sent.append(("info", m)))
    runner._alert_poll_circuit_open(5, 60)
    runner._alert_poll_circuit_close(150.0)
    assert sent[0][0] == "emergency" and "5회" in sent[0][1] and "타임아웃 보류" in sent[0][1]
    assert sent[1][0] == "info" and "2.5분" in sent[1][1]


def test_the_worker_wires_the_alerts_into_its_poller(monkeypatch):
    """``StrategyWorker.__init__`` builds the poller with both callbacks."""
    import inspect
    from backend.worker import runner
    src = inspect.getsource(runner.StrategyWorker.__init__)
    assert "on_circuit_open=_alert_poll_circuit_open" in src
    assert "on_circuit_close=_alert_poll_circuit_close" in src
