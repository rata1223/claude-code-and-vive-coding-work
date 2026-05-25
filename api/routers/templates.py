"""Strategy script templates."""
from fastapi import APIRouter, Query
from typing import Optional

from api.schemas import Resp

router = APIRouter(prefix="/api/templates", tags=["templates"])

TEMPLATES = [
    {
        "key": "momentum",
        "name": "Momentum Strategy",
        "description": "Buy when RSI > 50 and price above EMA20; sell when RSI < 45 or price drops below EMA20.",
        "category": "trend",
        "script_code": """\
# Momentum Strategy
# Uses RSI + EMA to ride price trends.
import pandas_ta as ta

def on_bar(ctx):
    df = ctx.get_kline(symbol=ctx.symbol, timeframe=ctx.timeframe, limit=60)
    df['rsi'] = ta.rsi(df['close'], length=14)
    df['ema20'] = ta.ema(df['close'], length=20)

    last_close = float(df['close'].iloc[-1])
    last_rsi = float(df['rsi'].iloc[-1])
    last_ema = float(df['ema20'].iloc[-1])

    position = ctx.get_position(ctx.symbol)

    if not position and last_rsi > 50 and last_close > last_ema:
        qty = ctx.calc_qty(ctx.symbol, risk_pct=0.02)
        ctx.buy(ctx.symbol, qty=qty, price=last_close)
        ctx.log(f"BUY signal: RSI={last_rsi:.1f} close={last_close} EMA={last_ema:.2f}")

    elif position and (last_rsi < 45 or last_close < last_ema):
        ctx.sell(ctx.symbol, qty=position['qty'], price=last_close)
        ctx.log(f"SELL signal: RSI={last_rsi:.1f} close={last_close}")
""",
    },
    {
        "key": "mean_reversion",
        "name": "Mean Reversion Strategy",
        "description": "Buy when price drops below lower Bollinger Band; sell at middle band.",
        "category": "mean_reversion",
        "script_code": """\
# Mean Reversion Strategy
# Buys oversold dips and exits at the mean.
import pandas_ta as ta

def on_bar(ctx):
    df = ctx.get_kline(symbol=ctx.symbol, timeframe=ctx.timeframe, limit=60)
    bbands = ta.bbands(df['close'], length=20, std=2.0)
    df['bb_upper'] = bbands['BBU_20_2.0']
    df['bb_mid']   = bbands['BBM_20_2.0']
    df['bb_lower'] = bbands['BBL_20_2.0']

    last_close  = float(df['close'].iloc[-1])
    bb_lower    = float(df['bb_lower'].iloc[-1])
    bb_mid      = float(df['bb_mid'].iloc[-1])

    position = ctx.get_position(ctx.symbol)

    if not position and last_close < bb_lower:
        qty = ctx.calc_qty(ctx.symbol, risk_pct=0.02)
        ctx.buy(ctx.symbol, qty=qty, price=last_close)
        ctx.log(f"BUY: price={last_close} below lower BB={bb_lower:.2f}")

    elif position and last_close >= bb_mid:
        ctx.sell(ctx.symbol, qty=position['qty'], price=last_close)
        ctx.log(f"SELL: price={last_close} reached mid BB={bb_mid:.2f}")
""",
    },
    {
        "key": "ma_crossover",
        "name": "MA Crossover Strategy",
        "description": "Golden/death cross between SMA20 and SMA50 for entries and exits.",
        "category": "trend",
        "script_code": """\
# MA Crossover Strategy
# Golden cross (SMA20 > SMA50) = buy; Death cross = sell.
import pandas_ta as ta

def on_bar(ctx):
    df = ctx.get_kline(symbol=ctx.symbol, timeframe=ctx.timeframe, limit=100)
    df['sma20'] = ta.sma(df['close'], length=20)
    df['sma50'] = ta.sma(df['close'], length=50)

    prev_sma20 = float(df['sma20'].iloc[-2])
    prev_sma50 = float(df['sma50'].iloc[-2])
    curr_sma20 = float(df['sma20'].iloc[-1])
    curr_sma50 = float(df['sma50'].iloc[-1])
    last_close = float(df['close'].iloc[-1])

    position = ctx.get_position(ctx.symbol)

    golden_cross = prev_sma20 <= prev_sma50 and curr_sma20 > curr_sma50
    death_cross  = prev_sma20 >= prev_sma50 and curr_sma20 < curr_sma50

    if not position and golden_cross:
        qty = ctx.calc_qty(ctx.symbol, risk_pct=0.02)
        ctx.buy(ctx.symbol, qty=qty, price=last_close)
        ctx.log(f"GOLDEN CROSS: SMA20={curr_sma20:.2f} SMA50={curr_sma50:.2f}")

    elif position and death_cross:
        ctx.sell(ctx.symbol, qty=position['qty'], price=last_close)
        ctx.log(f"DEATH CROSS: SMA20={curr_sma20:.2f} SMA50={curr_sma50:.2f}")
""",
    },
]

TEMPLATE_MAP = {t["key"]: t for t in TEMPLATES}


@router.get("")
def list_templates(
    category: Optional[str] = Query(None),
):
    items = TEMPLATES
    if category:
        items = [t for t in TEMPLATES if t.get("category") == category]
    return Resp.ok({"items": items})


@router.get("/{key}")
def get_template(key: str):
    t = TEMPLATE_MAP.get(key)
    if not t:
        return Resp.err("Template not found")
    return Resp.ok(t)
