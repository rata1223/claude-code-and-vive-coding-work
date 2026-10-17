"""The market-open scan loads ``SCAN_PERIOD`` of history (two years, #230)."""
from backend.execution.order_machine import OrderStateMachine
from backend.execution.position_tracker import PositionTracker
from backend.strategy.indicator.strategy import SCAN_PERIOD, IndicatorStrategy


class _Broker:
    is_live = False

    def get_balance(self):
        raise RuntimeError("not needed")


def test_the_scan_fetches_two_years(monkeypatch):
    periods = []

    def fetch(self, symbol, start=None, end=None, period="2y", interval="1d"):
        periods.append(period)
        raise RuntimeError("stop here")

    monkeypatch.setattr("backend.quant.data.loader.DataLoader.fetch", fetch)
    strat = IndicatorStrategy(broker=_Broker(), tracker=PositionTracker(OrderStateMachine()),
                              machine=OrderStateMachine(), name="t",
                              config={"universe": ["SPY", "069500"]})
    strat._scan_and_trade()
    assert SCAN_PERIOD == "2y"
    assert periods == ["2y", "2y"]
