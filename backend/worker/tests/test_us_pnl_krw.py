"""
A US sell's realized P&L reaches the loss tracker in won.

The fill price is in USD; the daily/weekly limits are in won. Recorded raw, a
$13 loss was a 13-won loss and the limits never saw US losses.
"""
from unittest.mock import MagicMock

import backend.worker.runner as runner
from backend.brokers.models import Order as BOrder, OrderStatus
from backend.execution.order_machine import OrderStateMachine
from backend.execution.position_tracker import Fill, PositionTracker
from backend.worker.tests.test_engine_stabilization import (  # noqa: F401 - fixture
    _FakeLossTracker, _worker_for_callback, db_factory,
)


def _broker(monkeypatch, rate):
    broker = MagicMock()
    broker.get_balance.return_value = MagicMock(total_eval_krw=2_000_000.0, equity_verified=True)
    if isinstance(rate, Exception):
        broker.valuation_fx_rate.side_effect = rate
    else:
        broker.valuation_fx_rate.return_value = rate
    monkeypatch.setattr(runner, "get_kis_broker", lambda: broker)
    return broker


def _sell(symbol, price, qty=1):
    return BOrder(id="S1", symbol=symbol, side="sell", qty=qty, price=price,
                  status=OrderStatus.FILLED, filled_qty=qty, avg_fill_price=price)


def _run(monkeypatch, db_factory, symbol, entry, exit_, market):
    monkeypatch.setattr(runner, "_SessionFactory", db_factory)
    lt = _FakeLossTracker()
    w = _worker_for_callback(monkeypatch, lt)
    tracker = PositionTracker(OrderStateMachine())
    tracker.on_fill(Fill(order_id="b", symbol=symbol, side="buy", qty=1, price=entry,
                         market=market))
    w._make_fill_callback(tracker, OrderStateMachine(), run_id=1)(_sell(symbol, exit_))
    return lt.calls


def test_a_us_loss_is_recorded_in_won(db_factory, monkeypatch):
    _broker(monkeypatch, 1_400.0)
    calls = _run(monkeypatch, db_factory, "AAPL", 185.0, 172.0, "US")
    assert calls == [(-13.0 * 1_400.0, 2_000_000.0)]


def test_a_kr_pnl_is_unchanged(db_factory, monkeypatch):
    broker = _broker(monkeypatch, RuntimeError("never asked"))
    calls = _run(monkeypatch, db_factory, "005930", 70_000.0, 69_000.0, "KR")
    assert calls == [(-1_000.0, 2_000_000.0)]
    broker.valuation_fx_rate.assert_not_called()


def test_a_failed_rate_still_records_the_loss_at_the_seed_rate(db_factory, monkeypatch):
    _broker(monkeypatch, RuntimeError("fx down"))
    calls = _run(monkeypatch, db_factory, "AAPL", 185.0, 172.0, "US")
    assert calls == [(-13.0 * 1_350.0, 2_000_000.0)]
