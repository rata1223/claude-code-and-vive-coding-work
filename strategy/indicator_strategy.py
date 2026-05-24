"""
IndicatorStrategy: UI에서 인디케이터를 조합하여 만드는 전략.
JSON config → pandas-ta 신호 평가.
"""
import logging
from dataclasses import dataclass, field
from typing import Any
import pandas as pd

try:
    import pandas_ta as ta
    _HAS_PANDAS_TA = True
except ImportError:
    _HAS_PANDAS_TA = False
    ta = None

logger = logging.getLogger(__name__)

SUPPORTED_INDICATORS = {
    "SMA": {"params": [{"name": "length", "label": "기간", "default": 20, "type": "int"}]},
    "EMA": {"params": [{"name": "length", "label": "기간", "default": 20, "type": "int"}]},
    "RSI": {"params": [{"name": "length", "label": "기간", "default": 14, "type": "int"}]},
    "MACD": {
        "params": [
            {"name": "fast", "label": "단기", "default": 12, "type": "int"},
            {"name": "slow", "label": "장기", "default": 26, "type": "int"},
            {"name": "signal", "label": "시그널", "default": 9, "type": "int"},
        ]
    },
    "BBANDS": {
        "params": [
            {"name": "length", "label": "기간", "default": 20, "type": "int"},
            {"name": "std", "label": "표준편차", "default": 2.0, "type": "float"},
        ]
    },
    "ATR": {"params": [{"name": "length", "label": "기간", "default": 14, "type": "int"}]},
    "STOCH": {
        "params": [
            {"name": "k", "label": "K 기간", "default": 14, "type": "int"},
            {"name": "d", "label": "D 기간", "default": 3, "type": "int"},
        ]
    },
    "CCI": {"params": [{"name": "length", "label": "기간", "default": 20, "type": "int"}]},
    "WILLR": {"params": [{"name": "length", "label": "기간", "default": 14, "type": "int"}]},
    "OBV": {"params": []},
    "MFI": {"params": [{"name": "length", "label": "기간", "default": 14, "type": "int"}]},
    "ADX": {"params": [{"name": "length", "label": "기간", "default": 14, "type": "int"}]},
    "ROC": {"params": [{"name": "length", "label": "기간", "default": 12, "type": "int"}]},
}


@dataclass
class IndicatorConfig:
    name: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"name": self.name, "params": self.params}

    @classmethod
    def from_dict(cls, d: dict) -> "IndicatorConfig":
        return cls(name=d["name"], params=d.get("params", {}))


@dataclass
class Condition:
    """left operator right — 예: RSI > 30"""
    left: str
    operator: str
    right: Any

    OPERATORS = {">", "<", ">=", "<=", "==", "cross_up", "cross_down"}

    def to_dict(self) -> dict:
        return {"left": self.left, "operator": self.operator, "right": self.right}

    @classmethod
    def from_dict(cls, d: dict) -> "Condition":
        return cls(left=d["left"], operator=d["operator"], right=d["right"])


class IndicatorStrategy:
    """GUI 기반 인디케이터 조합 전략."""

    def __init__(
        self,
        indicators: list[IndicatorConfig],
        entry_conditions: list[Condition],
        exit_conditions: list[Condition],
        stop_loss_pct: float = 0.07,
        take_profit_pct: float = 0.15,
    ):
        self.indicators = indicators
        self.entry_conditions = entry_conditions
        self.exit_conditions = exit_conditions
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct

    @classmethod
    def from_config(cls, config: dict) -> "IndicatorStrategy":
        indicators = [IndicatorConfig.from_dict(i) for i in config.get("indicators", [])]
        entry = [Condition.from_dict(c) for c in config.get("entry_conditions", [])]
        exit_ = [Condition.from_dict(c) for c in config.get("exit_conditions", [])]
        return cls(
            indicators=indicators,
            entry_conditions=entry,
            exit_conditions=exit_,
            stop_loss_pct=float(config.get("stop_loss_pct", 0.07)),
            take_profit_pct=float(config.get("take_profit_pct", 0.15)),
        )

    @staticmethod
    def _sma(series: pd.Series, length: int) -> pd.Series:
        return series.rolling(window=length).mean()

    @staticmethod
    def _ema(series: pd.Series, length: int) -> pd.Series:
        return series.ewm(span=length, adjust=False).mean()

    @staticmethod
    def _rsi(series: pd.Series, length: int) -> pd.Series:
        delta = series.diff()
        gain = delta.clip(lower=0).rolling(window=length).mean()
        loss = (-delta.clip(upper=0)).rolling(window=length).mean()
        rs = gain / loss.replace(0, float("nan"))
        return 100 - (100 / (1 + rs))

    def _compute(self, df: pd.DataFrame) -> dict[str, pd.Series]:
        close = df["Close"]
        high = df.get("High", close)
        low = df.get("Low", close)
        volume = df.get("Volume", pd.Series([0] * len(close), index=close.index))
        computed: dict[str, pd.Series] = {}

        for ind in self.indicators:
            name = ind.name.upper()
            p = ind.params
            try:
                if name == "SMA":
                    length = int(p.get("length", 20))
                    computed[f"SMA_{length}"] = (
                        ta.sma(close, length=length) if _HAS_PANDAS_TA
                        else self._sma(close, length)
                    )
                elif name == "EMA":
                    length = int(p.get("length", 20))
                    computed[f"EMA_{length}"] = (
                        ta.ema(close, length=length) if _HAS_PANDAS_TA
                        else self._ema(close, length)
                    )
                elif name == "RSI":
                    length = int(p.get("length", 14))
                    computed["RSI"] = (
                        ta.rsi(close, length=length) if _HAS_PANDAS_TA
                        else self._rsi(close, length)
                    )
                elif name == "MACD":
                    fast = int(p.get("fast", 12))
                    slow = int(p.get("slow", 26))
                    sig = int(p.get("signal", 9))
                    if _HAS_PANDAS_TA:
                        macd = ta.macd(close, fast=fast, slow=slow, signal=sig)
                        if macd is not None:
                            computed["MACD"] = macd.iloc[:, 0]
                            computed["MACD_signal"] = macd.iloc[:, 1]
                            computed["MACD_hist"] = macd.iloc[:, 2]
                    else:
                        ema_fast = self._ema(close, fast)
                        ema_slow = self._ema(close, slow)
                        macd_line = ema_fast - ema_slow
                        signal_line = self._ema(macd_line, sig)
                        computed["MACD"] = macd_line
                        computed["MACD_signal"] = signal_line
                        computed["MACD_hist"] = macd_line - signal_line
                elif name == "BBANDS":
                    length = int(p.get("length", 20))
                    std_mult = float(p.get("std", 2.0))
                    if _HAS_PANDAS_TA:
                        bb = ta.bbands(close, length=length, std=std_mult)
                        if bb is not None:
                            computed["BB_lower"] = bb.iloc[:, 0]
                            computed["BB_mid"] = bb.iloc[:, 1]
                            computed["BB_upper"] = bb.iloc[:, 2]
                    else:
                        mid = self._sma(close, length)
                        std = close.rolling(length).std()
                        computed["BB_lower"] = mid - std_mult * std
                        computed["BB_mid"] = mid
                        computed["BB_upper"] = mid + std_mult * std
                elif name == "ATR":
                    length = int(p.get("length", 14))
                    computed["ATR"] = (
                        ta.atr(high, low, close, length=length) if _HAS_PANDAS_TA
                        else (high - low).rolling(length).mean()
                    )
                elif name == "STOCH":
                    if _HAS_PANDAS_TA:
                        stoch = ta.stoch(high, low, close, k=int(p.get("k", 14)), d=int(p.get("d", 3)))
                        if stoch is not None:
                            computed["STOCH_K"] = stoch.iloc[:, 0]
                            computed["STOCH_D"] = stoch.iloc[:, 1]
                elif name == "CCI":
                    length = int(p.get("length", 20))
                    computed["CCI"] = (
                        ta.cci(high, low, close, length=length) if _HAS_PANDAS_TA
                        else (close - self._sma(close, length)) / (0.015 * close.rolling(length).std())
                    )
                elif name == "WILLR":
                    length = int(p.get("length", 14))
                    computed["WILLR"] = (
                        ta.willr(high, low, close, length=length) if _HAS_PANDAS_TA
                        else -100 * (high.rolling(length).max() - close) / (high.rolling(length).max() - low.rolling(length).min())
                    )
                elif name == "OBV":
                    computed["OBV"] = (
                        ta.obv(close, volume) if _HAS_PANDAS_TA
                        else (volume * close.diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))).cumsum()
                    )
                elif name == "MFI":
                    if _HAS_PANDAS_TA:
                        computed["MFI"] = ta.mfi(high, low, close, volume, length=int(p.get("length", 14)))
                elif name == "ADX":
                    if _HAS_PANDAS_TA:
                        adx = ta.adx(high, low, close, length=int(p.get("length", 14)))
                        if adx is not None:
                            computed["ADX"] = adx.iloc[:, 0]
                elif name == "ROC":
                    length = int(p.get("length", 12))
                    computed["ROC"] = (
                        ta.roc(close, length=length) if _HAS_PANDAS_TA
                        else close.pct_change(periods=length) * 100
                    )
            except Exception as e:
                logger.warning("인디케이터 계산 실패 %s: %s", name, e)

        computed["CLOSE"] = close
        computed["OPEN"] = df.get("Open", close)
        computed["HIGH"] = high
        computed["LOW"] = low
        computed["VOLUME"] = volume
        return computed

    def _resolve(self, key: str, computed: dict[str, pd.Series], idx: int) -> float | None:
        if key in computed:
            s = computed[key]
            if s is not None and len(s) > idx:
                val = s.iloc[idx]
                return float(val) if pd.notna(val) else None
        try:
            return float(key)
        except (ValueError, TypeError):
            return None

    def _eval_condition(self, cond: Condition, computed: dict[str, pd.Series], idx: int) -> bool:
        lv = self._resolve(cond.left, computed, idx)
        if lv is None:
            return False

        if cond.operator in ("cross_up", "cross_down"):
            if idx < 1:
                return False
            lv_prev = self._resolve(cond.left, computed, idx - 1)
            rv = self._resolve(str(cond.right), computed, idx)
            rv_prev = self._resolve(str(cond.right), computed, idx - 1)
            if None in (lv_prev, rv, rv_prev):
                return False
            if cond.operator == "cross_up":
                return lv_prev < rv_prev and lv >= rv
            return lv_prev > rv_prev and lv <= rv

        rv = self._resolve(str(cond.right), computed, idx) if isinstance(cond.right, str) else float(cond.right)
        if rv is None:
            return False

        ops = {">": lv > rv, "<": lv < rv, ">=": lv >= rv, "<=": lv <= rv, "==": lv == rv}
        return ops.get(cond.operator, False)

    def buy_signal(self, df: pd.DataFrame) -> bool:
        if df.empty or not self.entry_conditions:
            return False
        computed = self._compute(df)
        idx = len(df) - 1
        return all(self._eval_condition(c, computed, idx) for c in self.entry_conditions)

    def sell_signal(self, df: pd.DataFrame, entry_price: float) -> bool:
        last_close = float(df["Close"].iloc[-1])
        if last_close <= entry_price * (1 - self.stop_loss_pct):
            return True
        if last_close >= entry_price * (1 + self.take_profit_pct):
            return True
        if df.empty or not self.exit_conditions:
            return False
        computed = self._compute(df)
        idx = len(df) - 1
        return all(self._eval_condition(c, computed, idx) for c in self.exit_conditions)

    def to_config(self) -> dict:
        return {
            "indicators": [i.to_dict() for i in self.indicators],
            "entry_conditions": [c.to_dict() for c in self.entry_conditions],
            "exit_conditions": [c.to_dict() for c in self.exit_conditions],
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
        }
