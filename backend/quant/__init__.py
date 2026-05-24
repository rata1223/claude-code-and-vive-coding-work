"""
QuantDinger Quant Engine — backend/quant/

Architecture:
  data/       → unified OHLCV loader (yfinance + PyKRX + broker adapter)
  indicators/ → no-lookahead indicator library (pandas-ta based)
  signals/    → composable signal modules → fusion scorer
  risk/       → position sizing (ATR/Kelly) + portfolio optimization
  backtest/   → vectorized engine + walk-forward validation + metrics
  live/       → live trading pipeline (reuses BrokerAdapter interface)
  tests/      → unit + integration tests

Data flow (live):
  DataLoader → Indicators → SignalFusion → RiskManager → BrokerAdapter → OrderStateMachine

Data flow (backtest):
  DataLoader → Indicators → SignalFusion → BacktestEngine → Metrics → WalkForward
"""
__all__ = [
    "DataLoader",
    "BacktestEngine",
    "BacktestMetrics",
    "SignalFusion",
    "PositionSizer",
]


def __getattr__(name):
    if name == "DataLoader":
        from backend.quant.data.loader import DataLoader
        return DataLoader
    if name == "BacktestEngine":
        from backend.quant.backtest.engine import BacktestEngine
        return BacktestEngine
    if name == "BacktestMetrics":
        from backend.quant.backtest.metrics import summarize
        return summarize
    if name == "SignalFusion":
        from backend.quant.signals.fusion import SignalFusion
        return SignalFusion
    if name == "PositionSizer":
        from backend.quant.risk.position_sizer import PositionSizer
        return PositionSizer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
