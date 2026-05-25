"""
Deliverable 6: 벡터화 백테스트 엔진.
Backtrader 호환 인터페이스 + 자체 벡터화 실행.
트랜잭션 비용·슬리피지 내장. No lookahead bias 보장.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from backend.quant.backtest.metrics import summarize
from backend.quant.risk.position_sizer import DEFAULT_COMMISSION, DEFAULT_SLIPPAGE, effective_price

logger = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    initial_capital: float = 2_000_000.0
    commission: float = DEFAULT_COMMISSION   # 0.015%
    slippage: float = DEFAULT_SLIPPAGE       # 0.02%
    max_position_pct: float = 0.05           # 종목당 최대 5%
    stop_loss_pct: float = 0.07              # 7% 손절
    take_profit_pct: Optional[float] = None  # None = 신호 기반 청산
    allow_short: bool = False


@dataclass
class Trade:
    symbol: str
    entry_date: str
    exit_date: str
    side: str
    qty: int
    entry_price: float
    exit_price: float
    pnl: float
    exit_reason: str


@dataclass
class BacktestResult:
    equity_curve: pd.Series
    trades: list[Trade]
    metrics: dict
    config: BacktestConfig


class BacktestEngine:
    """
    단일 종목 벡터화 백테스터.
    signal_series: DatetimeIndex, values 1/0/-1 (no-lookahead 적용된 것)
    df: OHLCV DataFrame (같은 인덱스)

    Backtrader 호환: Strategy 클래스 대신 signal Series 주입 방식 사용.
    vectorbt 호환: 결과를 equity_curve Series로 반환.
    """

    def __init__(self, config: Optional[BacktestConfig] = None):
        self.config = config or BacktestConfig()

    def run(self, df: pd.DataFrame, signal_series: pd.Series,
            symbol: str = "SYMBOL") -> BacktestResult:
        """
        df: OHLCV, signal_series: 1/-1/0 (전봉 기준, no-lookahead)
        실행 가격: 신호 발생 다음 봉 시가 (Open)로 체결 → lookahead bias 완전 제거.
        """
        cfg = self.config
        capital = cfg.initial_capital
        equity = []
        trades: list[Trade] = []

        position_qty = 0
        position_price = 0.0
        entry_date = ""

        # 신호를 1봉 뒤에 실행 (현재 봉 종가로 신호 → 다음 봉 시가로 체결)
        signals = signal_series.shift(1).fillna(0)  # 한 번 더 shift = 체결 지연

        for i in range(1, len(df)):
            row = df.iloc[i]
            prev_row = df.iloc[i - 1]
            date = str(df.index[i].date())
            sig = int(signals.iloc[i])
            open_price = row["Open"]

            # ── 보유 중 청산 조건 체크 ─────────────────────────────────
            if position_qty > 0:
                exit_price = None
                exit_reason = ""

                # 손절 (당일 저가 기준)
                stop_price = position_price * (1 - cfg.stop_loss_pct)
                if row["Low"] <= stop_price:
                    exit_price = stop_price
                    exit_reason = "stop_loss"

                # 익절
                elif cfg.take_profit_pct and row["High"] >= position_price * (1 + cfg.take_profit_pct):
                    exit_price = position_price * (1 + cfg.take_profit_pct)
                    exit_reason = "take_profit"

                # 매도 신호
                elif sig == -1:
                    exit_price = effective_price(open_price, "sell", cfg.slippage)
                    exit_reason = "signal"

                if exit_price:
                    gross = exit_price * position_qty
                    cost = gross * cfg.commission
                    pnl = (exit_price - position_price) * position_qty - cost
                    capital += gross - cost
                    trades.append(Trade(
                        symbol=symbol, entry_date=entry_date, exit_date=date,
                        side="long", qty=position_qty,
                        entry_price=round(position_price, 4),
                        exit_price=round(exit_price, 4),
                        pnl=round(pnl, 0), exit_reason=exit_reason
                    ))
                    position_qty = 0
                    position_price = 0.0

            # ── 신규 진입 ─────────────────────────────────────────────
            if position_qty == 0 and sig == 1:
                buy_price = effective_price(open_price, "buy", cfg.slippage)
                max_invest = capital * cfg.max_position_pct
                qty = int(max_invest / buy_price)
                if qty > 0:
                    cost = buy_price * qty * cfg.commission
                    total_cost = buy_price * qty + cost
                    if total_cost <= capital:
                        capital -= total_cost
                        position_qty = qty
                        position_price = buy_price
                        entry_date = date

            # 평가 자산 = 현금 + 보유 포지션 현재가
            if position_qty > 0:
                equity.append(capital + position_qty * row["Close"])
            else:
                equity.append(capital)

        equity_series = pd.Series(equity, index=df.index[1:], name="equity")
        metrics = summarize(equity_series, [t.__dict__ for t in trades], cfg.initial_capital)

        return BacktestResult(
            equity_curve=equity_series,
            trades=trades,
            metrics=metrics,
            config=cfg,
        )

    def run_from_fusion(self, df: pd.DataFrame, fusion, symbol: str = "") -> BacktestResult:
        """
        SignalFusion 객체를 받아 봉마다 신호 계산 후 백테스트.
        실전 전략 검증용.
        """
        signals = pd.Series(0, index=df.index, dtype=int)
        for i in range(52, len(df)):  # Ichimoku 최소 데이터
            sub = df.iloc[:i]
            try:
                result = fusion.evaluate(sub, symbol=symbol)
                signals.iloc[i] = result.signal
            except Exception:
                pass
        return self.run(df, signals, symbol=symbol)


def run_multi_symbol(dfs: dict[str, pd.DataFrame], signal_map: dict[str, pd.Series],
                     config: Optional[BacktestConfig] = None) -> dict[str, BacktestResult]:
    """복수 종목 병렬 백테스트."""
    engine = BacktestEngine(config)
    return {sym: engine.run(df, signal_map[sym], symbol=sym)
            for sym, df in dfs.items() if sym in signal_map}
