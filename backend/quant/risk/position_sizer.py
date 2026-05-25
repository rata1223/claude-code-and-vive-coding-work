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


# ── 비용 모델 ────────────────────────────────────────────────────────────────
# KIS 수수료 0.015%. 슬리피지 0.10%.
# 한국 ETF 매도 시 증권거래세 0.20% 추가 (매수에는 없음).
# 실질 왕복 비용: 매수 0.115% + 매도 0.315% = ~0.43% round-trip.
DEFAULT_COMMISSION = 0.00015        # 편도 수수료
DEFAULT_SLIPPAGE   = 0.0010        # 편도 슬리피지 (0.10%)
KR_SECURITIES_TAX  = 0.0020        # 한국 ETF 매도 증권거래세 0.20%


def transaction_cost(price: float, qty: int,
                     commission: float = DEFAULT_COMMISSION,
                     slippage: float = DEFAULT_SLIPPAGE,
                     side: str = "buy",
                     is_kr: bool = True) -> float:
    """
    총 거래비용 (수수료 + 슬리피지 + 한국 증권거래세).
    is_kr=True: 매도 시 KR_SECURITIES_TAX 추가.
    """
    gross = price * qty
    tax = (KR_SECURITIES_TAX if side == "sell" and is_kr else 0.0)
    return gross * (commission + slippage + tax)


def effective_price(price: float, side: str,
                    slippage: float = DEFAULT_SLIPPAGE,
                    is_kr: bool = True) -> float:
    """슬리피지 + 증권거래세 반영 실체결가."""
    if side == "buy":
        return price * (1 + slippage)
    tax = KR_SECURITIES_TAX if is_kr else 0.0
    return price * (1 - slippage - tax)


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

    def volatility_target(self, df: pd.DataFrame, target_vol: float = 0.10,
                          vol_window: int = 21, is_kr: bool = True) -> dict:
        """
        변동성 타겟팅 사이징.
        포지션 크기 = (target_vol / realized_vol) × capital × max_position_pct.
        target_vol: 연환산 목표 변동성 (기본 10%).
        """
        import numpy as np
        close = df["Close"]
        if len(close) < vol_window + 1:
            return self._fallback()
        log_ret = np.log(close / close.shift(1)).dropna()
        realized_vol = log_ret.iloc[-vol_window:].std() * np.sqrt(252)
        if realized_vol <= 0:
            return self._fallback()

        vol_scale = min(target_vol / realized_vol, 1.5)  # 최대 1.5× 레버리지 제한
        position_pct = min(vol_scale * self.max_position_pct, self.max_position_pct)
        price = close.iloc[-2]
        position_value = self.capital * position_pct
        qty = int(position_value / price) if price > 0 else 1

        return {
            "qty": max(1, qty),
            "position_value": round(position_value),
            "entry_price": round(effective_price(price, "buy", self.slippage, is_kr), 2),
            "realized_vol_ann": round(realized_vol, 4),
            "vol_scale": round(vol_scale, 4),
            "method": "vol_target",
        }

    def drawdown_scaled(self, df: pd.DataFrame, equity_curve: "pd.Series",
                        base_method: str = "atr", dd_floor: float = 0.5) -> dict:
        """
        드로다운 비례 사이징 축소.
        현재 MDD 대비 포지션 크기를 비례 감소 (dd_floor가 최소 배율).
        MDD=0 → 1.0×, MDD=15% → dd_floor× (선형 보간).
        """
        import numpy as np
        if equity_curve is None or len(equity_curve) < 2:
            return self.atr_based(df) if base_method == "atr" else self.fixed_fraction(df)

        peak = equity_curve.cummax().iloc[-1]
        current = equity_curve.iloc[-1]
        current_dd = (current - peak) / peak if peak > 0 else 0.0  # 음수

        mdd_cap = 0.15  # 15% MDD에서 dd_floor로 감소
        dd_ratio = max(0.0, min(abs(current_dd) / mdd_cap, 1.0))
        scale = 1.0 - dd_ratio * (1.0 - dd_floor)

        base = self.atr_based(df) if base_method == "atr" else self.fixed_fraction(df)
        base["qty"] = max(1, int(base["qty"] * scale))
        base["position_value"] = round(base.get("position_value", 0) * scale)
        base["dd_scale"] = round(scale, 4)
        base["current_dd_pct"] = round(current_dd * 100, 2)
        return base

    def _fallback(self) -> dict:
        return {
            "qty": 1,
            "position_value": round(self.capital * 0.02),
            "entry_price": 0.0,
            "method": "fallback",
        }
