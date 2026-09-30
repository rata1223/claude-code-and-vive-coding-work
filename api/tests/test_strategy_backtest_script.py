"""#188 — the backtest route runs saved scripts through the child-process runner.

The route used to build ``ScriptStrategy`` and call ``on_start()`` in the API
worker itself, then loop over every bar there. It now hands the script to
``strategy.script_backtest.run_script_backtest`` and turns its errors into an
error response. The runner's own limits are covered in
``tests/integration/test_script_backtest_limits.py``; this pins the wiring.
"""
import pytest

import strategy.script_backtest as sb
from api.models import Strategy
from api.routers import strategies
from api.tests.test_quick_trade_close_position import (
    db,          # noqa: F401 - pytest fixtures
    engine,      # noqa: F401
    user,        # noqa: F401
)


@pytest.fixture()
def script_strategy(db):
    s = Strategy(id=1, user_id=1, name="s", type="script", symbol="AAPL",
                 script_code="def on_bar(bar, ctx):\n    return None\n",
                 config={"p": 1})
    db.add(s)
    db.commit()
    return s


@pytest.fixture()
def no_user_code_here(monkeypatch):
    """The route must not compile or run the script in this process."""
    import strategy.script_strategy as ss

    def boom(*_a, **_k):
        raise AssertionError("user code ran in the API process")

    monkeypatch.setattr(ss.ScriptStrategy, "compile", boom)
    monkeypatch.setattr(ss.ScriptStrategy, "on_start", boom)


def test_the_script_goes_to_the_runner(monkeypatch, db, user, script_strategy,
                                       no_user_code_here):
    seen = {}

    def fake_runner(code, params, symbol, *, initial_capital, period, **_kw):
        seen.update(code=code, params=params, symbol=symbol,
                    initial_capital=initial_capital, period=period)
        return {"symbol": symbol, "total_trades": 3}

    monkeypatch.setattr(sb, "run_script_backtest", fake_runner)
    resp = strategies.run_backtest(
        {"strategy_id": 1, "period": "6mo", "initial_capital": 500_000}, user, db)

    assert resp.code == 1, resp.msg
    assert resp.data == {"symbol": "AAPL", "total_trades": 3}
    assert seen == {"code": script_strategy.script_code, "params": {"p": 1},
                    "symbol": "AAPL", "initial_capital": 500_000.0, "period": "6mo"}


def test_a_timeout_is_an_error_response(monkeypatch, db, user, script_strategy,
                                        no_user_code_here):
    def timed_out(*_a, **_k):
        raise sb.ScriptBacktestTimeout("스크립트 실행 시간 초과(30초)")

    monkeypatch.setattr(sb, "run_script_backtest", timed_out)
    resp = strategies.run_backtest({"strategy_id": 1}, user, db)

    assert resp.code == -1
    assert "시간 초과" in resp.msg
