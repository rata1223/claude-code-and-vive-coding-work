"""Indicator definitions and kline/price endpoints."""
import logging
from typing import Optional

import yfinance as yf
from fastapi import APIRouter, Depends, Query

from api.deps import get_current_user
from api.models import User
from api.schemas import Resp

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/indicator", tags=["indicators"])

# ── Static indicator catalogue ────────────────────────────────────────────

INDICATORS = [
    {
        "id": "SMA",
        "name": "Simple Moving Average",
        "short_name": "SMA",
        "category": "trend",
        "description": "Average closing price over a rolling window.",
    },
    {
        "id": "EMA",
        "name": "Exponential Moving Average",
        "short_name": "EMA",
        "category": "trend",
        "description": "Weighted moving average giving more weight to recent prices.",
    },
    {
        "id": "RSI",
        "name": "Relative Strength Index",
        "short_name": "RSI",
        "category": "momentum",
        "description": "Momentum oscillator measuring speed and change of price movements (0-100).",
    },
    {
        "id": "MACD",
        "name": "MACD",
        "short_name": "MACD",
        "category": "trend",
        "description": "Moving Average Convergence/Divergence – trend-following momentum indicator.",
    },
    {
        "id": "BBANDS",
        "name": "Bollinger Bands",
        "short_name": "BB",
        "category": "volatility",
        "description": "Volatility bands placed above and below a moving average.",
    },
    {
        "id": "ATR",
        "name": "Average True Range",
        "short_name": "ATR",
        "category": "volatility",
        "description": "Measures market volatility using high/low/close ranges.",
    },
    {
        "id": "STOCH",
        "name": "Stochastic Oscillator",
        "short_name": "STOCH",
        "category": "momentum",
        "description": "Compares closing price to price range over a given period.",
    },
    {
        "id": "CCI",
        "name": "Commodity Channel Index",
        "short_name": "CCI",
        "category": "momentum",
        "description": "Identifies cyclical trends in commodity, equity, and currency markets.",
    },
    {
        "id": "WILLR",
        "name": "Williams %R",
        "short_name": "%R",
        "category": "momentum",
        "description": "Momentum indicator measuring overbought/oversold levels (0 to -100).",
    },
    {
        "id": "OBV",
        "name": "On Balance Volume",
        "short_name": "OBV",
        "category": "volume",
        "description": "Uses volume flow to predict changes in stock price.",
    },
]

INDICATOR_PARAMS = {
    "SMA": [
        {"name": "length", "label": "Period", "type": "int", "default": 20, "min": 2, "max": 500},
    ],
    "EMA": [
        {"name": "length", "label": "Period", "type": "int", "default": 20, "min": 2, "max": 500},
    ],
    "RSI": [
        {"name": "length", "label": "Period", "type": "int", "default": 14, "min": 2, "max": 200},
        {"name": "overbought", "label": "Overbought", "type": "int", "default": 70, "min": 50, "max": 100},
        {"name": "oversold", "label": "Oversold", "type": "int", "default": 30, "min": 0, "max": 50},
    ],
    "MACD": [
        {"name": "fast", "label": "Fast Period", "type": "int", "default": 12, "min": 2, "max": 100},
        {"name": "slow", "label": "Slow Period", "type": "int", "default": 26, "min": 2, "max": 200},
        {"name": "signal", "label": "Signal Period", "type": "int", "default": 9, "min": 1, "max": 50},
    ],
    "BBANDS": [
        {"name": "length", "label": "Period", "type": "int", "default": 20, "min": 2, "max": 500},
        {"name": "std", "label": "Std Dev", "type": "float", "default": 2.0, "min": 0.5, "max": 5.0},
    ],
    "ATR": [
        {"name": "length", "label": "Period", "type": "int", "default": 14, "min": 1, "max": 200},
    ],
    "STOCH": [
        {"name": "k", "label": "%K Period", "type": "int", "default": 14, "min": 1, "max": 200},
        {"name": "d", "label": "%D Period", "type": "int", "default": 3, "min": 1, "max": 50},
        {"name": "smooth_k", "label": "Smooth %K", "type": "int", "default": 3, "min": 1, "max": 50},
    ],
    "CCI": [
        {"name": "length", "label": "Period", "type": "int", "default": 20, "min": 2, "max": 200},
    ],
    "WILLR": [
        {"name": "length", "label": "Period", "type": "int", "default": 14, "min": 2, "max": 200},
    ],
    "OBV": [],
}


@router.get("/getIndicators")
def get_indicators():
    return Resp.ok({"items": INDICATORS})


@router.get("/getIndicatorParams")
def get_indicator_params(indicator_id: str = Query(...)):
    params = INDICATOR_PARAMS.get(indicator_id.upper())
    if params is None:
        return Resp.err("Indicator not found")
    return Resp.ok({"params": params})


# ── Kline / OHLCV ────────────────────────────────────────────────────────

_TF_MAP = {
    "1m": "1m", "3m": "2m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "2h": "90m", "4h": "1h", "1d": "1d", "1w": "1wk", "1M": "1mo",
}

_PERIOD_MAP = {
    "1m": "5d", "3m": "5d", "5m": "5d", "15m": "60d", "30m": "60d",
    "1h": "730d", "2h": "730d", "4h": "730d", "1d": "5y", "1w": "10y", "1M": "max",
}


def _fetch_kline_yf(symbol: str, timeframe: str, limit: int = 200) -> list:
    """Fetch OHLCV from yfinance and return list of bar dicts."""
    tf_key = timeframe if timeframe in _TF_MAP else "1h"
    interval = _TF_MAP[tf_key]
    period = _PERIOD_MAP[tf_key]

    ticker = yf.Ticker(symbol)
    df = ticker.history(period=period, interval=interval, auto_adjust=True)
    if df.empty:
        return []
    df = df.tail(limit)
    df = df.reset_index()

    bars = []
    for _, row in df.iterrows():
        ts = row.get("Datetime") or row.get("Date")
        if hasattr(ts, "timestamp"):
            t = int(ts.timestamp() * 1000)
        else:
            t = 0
        bars.append({
            "time": t,
            "open": round(float(row["Open"]), 6),
            "high": round(float(row["High"]), 6),
            "low": round(float(row["Low"]), 6),
            "close": round(float(row["Close"]), 6),
            "volume": float(row.get("Volume", 0)),
        })
    return bars


@router.get("/kline")
def get_kline(
    symbol: str = Query(...),
    timeframe: str = Query("1h"),
    market: str = Query("us"),
    limit: int = Query(200, ge=1, le=1000),
):
    """Return OHLCV bars. Uses yfinance for US; KIS for KR if available."""
    try:
        bars = _fetch_kline_yf(symbol, timeframe, limit)
        return Resp.ok({"symbol": symbol, "timeframe": timeframe, "items": bars})
    except Exception as e:
        logger.warning("kline fetch failed for %s: %s", symbol, e)
        return Resp.ok({"symbol": symbol, "timeframe": timeframe, "items": []})


@router.get("/price")
def get_price(
    symbol: str = Query(...),
    market: str = Query("us"),
):
    """Return latest price for a symbol."""
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info
        price = float(info.last_price or 0)
        prev_close = float(info.previous_close or 0)
        change = round(price - prev_close, 4)
        change_pct = round(change / prev_close * 100, 4) if prev_close else 0.0
        return Resp.ok(
            {
                "symbol": symbol,
                "price": price,
                "change": change,
                "change_pct": change_pct,
                "prev_close": prev_close,
            }
        )
    except Exception as e:
        logger.warning("price fetch failed for %s: %s", symbol, e)
        return Resp.ok({"symbol": symbol, "price": 0.0, "change": 0.0, "change_pct": 0.0})


@router.post("/parseStrategyConfig")
def parse_strategy_config(body: dict):
    """Parse strategy script code and return detected indicators/params."""
    code = body.get("code", "")
    detected = []
    for ind in INDICATORS:
        sid = ind["id"].lower()
        if sid in code.lower():
            detected.append(ind["id"])
    return Resp.ok({"indicators": detected, "params": {}})
