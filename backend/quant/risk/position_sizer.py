"""
Deliverable 5: 포지션 사이징 — ATR / Kelly / Fixed-Fraction.
트랜잭션 비용 + 슬리피지 모델 포함.
"""
import logging
import pandas as pd

logger = logging.getLogger(__name__)


def _calc_atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    """pandas만으로 ATR 계산 (pandas_ta 없이)."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(length).mean()


# 기본 거래 비용 (KIS 수수료 0.015% + 슬리피지 0.02%)
DEFAULT_COMMISSION = 0.00015
DEFAULT_SLIPPAGE = 0.0002


def transaction_cost(price: float, qty: int,
                     commission: float = DEFAULT_COMMISSION,
                     slippage: float = DEFAULT_SLIPPAGE) -> float:
    """총 거래비용 계산 (수수료 + 슬리피지)."""
    gross = price * qty
    return gross * (commission + slippage)


def effective_price(price: float, side: str,
                    slippage: float = DEFAULT_SLIPPAGE) -> float:
    """슬리피지 적용 체결가."""
    if side == "buy":
        return price * (1 + slippage)
    return price * (1 - slippage)


class PositionSizer:
    """
    3가지 사이징 방법 제공.
    모두 ATR 기반 손절선을 사용해 리스크를 자본 대비 % 제한.
    """

    def __init__(self, capital: float, max_position_pct: float = 0.05,
                 commission: float = DEFAULT_COMMISSION,
                 slippage: float = DEFAULT_SLIPPAGE):
        self.capital = capital
        self.max_position_pct = max_position_pct
        self.commission = commission
        self.slippage = slippage

    def atr_based(self, df: pd.DataFrame, risk_pct: float = 0.01,
                  atr_length: int = 14, atr_multiplier: float = 2.0) -> dict:
        """
        ATR 기반 사이징.
        자본의 risk_pct가 atr_multiplier × ATR 손실로 소진되는 수량 계산.
        """
        close = df["Close"]
        atr = _calc_atr(df["High"], df["Low"], close, atr_length)
        if atr is None or atr.dropna().empty:
            return self._fallback()

        last_atr = atr.iloc[-2]  # no-lookahead
        price = close.iloc[-2]
        if last_atr <= 0 or price <= 0:
            return self._fallback()

        risk_amount = self.capital * risk_pct
        stop_distance = atr_multiplier * last_atr
        shares = risk_amount / stop_distance
        position_value = shares * price
        position_value = min(position_value, self.capital * self.max_position_pct)

        cost = transaction_cost(price, int(shares), self.commission, self.slippage)
        stop_price = price - stop_distance

        return {
            "qty": max(1, int(shares)),
            "position_value": round(position_value),
            "stop_price": round(stop_price, 2),
            "entry_price": round(effective_price(price, "buy", self.slippage), 2),
            "atr": round(last_atr, 4),
            "estimated_cost": round(cost, 0),
            "method": "atr",
        }

    def kelly_based(self, win_rate: float, avg_win: float, avg_loss: float,
                    df: pd.DataFrame, fraction: float = 0.25) -> dict:
        """Kelly Criterion (분수 Kelly — 과최적화 방지)."""
        if avg_loss == 0:
            return self._fallback()
        b = avg_win / avg_loss
        kelly_pct = win_rate - (1 - win_rate) / b
        kelly_pct = max(0.0, kelly_pct) * fraction
        kelly_pct = min(kelly_pct, self.max_position_pct)
        price = df["Close"].iloc[-2]
        position_value = self.capital * kelly_pct
        qty = int(position_value / price) if price > 0 else 1
        return {
            "qty": max(1, qty),
            "position_value": round(position_value),
            "entry_price": round(effective_price(price, "buy", self.slippage), 2),
            "kelly_pct": round(kelly_pct, 4),
            "method": "kelly",
        }

    def fixed_fraction(self, df: pd.DataFrame, pct: float = 0.02) -> dict:
        """고정 비율 사이징 — 자본의 pct 투자."""
        pct = min(pct, self.max_position_pct)
        price = df["Close"].iloc[-2]
        position_value = self.capital * pct
        qty = int(position_value / price) if price > 0 else 1
        return {
            "qty": max(1, qty),
            "position_value": round(position_value),
            "entry_price": round(effective_price(price, "buy", self.slippage), 2),
            "method": "fixed_fraction",
        }

    def _fallback(self) -> dict:
        return {
            "qty": 1,
            "position_value": round(self.capital * 0.02),
            "entry_price": 0.0,
            "method": "fallback",
        }
