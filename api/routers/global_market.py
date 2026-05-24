"""Global market overview, economic calendar, and sentiment."""
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import yfinance as yf
from fastapi import APIRouter, Query

from api.schemas import Resp

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/global-market", tags=["global-market"])

# ── Simple in-memory cache (TTL seconds) ────────────────────────────────

_cache: Dict[str, Any] = {}
_CACHE_TTL = 300  # 5 minutes


def _cached(key: str, ttl: int = _CACHE_TTL):
    def decorator(fn):
        def wrapper(*args, **kwargs):
            entry = _cache.get(key)
            if entry and time.time() - entry["ts"] < ttl:
                return entry["data"]
            result = fn(*args, **kwargs)
            _cache[key] = {"ts": time.time(), "data": result}
            return result
        return wrapper
    return decorator


# ── Global index definitions ──────────────────────────────────────────────

GLOBAL_INDICES = [
    {"symbol": "^GSPC",  "name": "S&P 500",      "region": "US"},
    {"symbol": "^IXIC",  "name": "NASDAQ",        "region": "US"},
    {"symbol": "^DJI",   "name": "Dow Jones",     "region": "US"},
    {"symbol": "^RUT",   "name": "Russell 2000",  "region": "US"},
    {"symbol": "^KS11",  "name": "KOSPI",         "region": "KR"},
    {"symbol": "^KQ11",  "name": "KOSDAQ",        "region": "KR"},
    {"symbol": "^N225",  "name": "Nikkei 225",    "region": "JP"},
    {"symbol": "^HSI",   "name": "Hang Seng",     "region": "HK"},
    {"symbol": "000001.SS", "name": "Shanghai",   "region": "CN"},
    {"symbol": "^FTSE",  "name": "FTSE 100",      "region": "GB"},
    {"symbol": "^GDAXI", "name": "DAX",           "region": "DE"},
    {"symbol": "GC=F",   "name": "Gold",          "region": "CMDTY"},
    {"symbol": "CL=F",   "name": "Crude Oil",     "region": "CMDTY"},
    {"symbol": "DX-Y.NYB", "name": "DXY",         "region": "FX"},
    {"symbol": "EURUSD=X", "name": "EUR/USD",     "region": "FX"},
    {"symbol": "BTC-USD", "name": "Bitcoin",      "region": "CRYPTO"},
]


def _fetch_index(sym: str) -> Optional[dict]:
    try:
        t = yf.Ticker(sym)
        fi = t.fast_info
        price = float(fi.last_price or 0)
        prev = float(fi.previous_close or 0)
        change = round(price - prev, 4)
        change_pct = round(change / prev * 100, 4) if prev else 0.0
        return {"price": price, "change": change, "change_pct": change_pct, "prev_close": prev}
    except Exception as e:
        logger.debug("index fetch %s failed: %s", sym, e)
        return None


@router.get("/overview")
def get_overview():
    """Return major global market indices with latest prices."""
    indices = []
    for idx in GLOBAL_INDICES:
        data = _fetch_index(idx["symbol"])
        entry = {
            "symbol": idx["symbol"],
            "name": idx["name"],
            "region": idx["region"],
            "price": data["price"] if data else 0.0,
            "change": data["change"] if data else 0.0,
            "change_pct": data["change_pct"] if data else 0.0,
            "prev_close": data["prev_close"] if data else 0.0,
        }
        indices.append(entry)

    return Resp.ok({"indices": indices, "updated_at": datetime.utcnow().isoformat()})


# ── Economic calendar (static / mock) ────────────────────────────────────

_STATIC_CALENDAR = [
    {
        "id": "fomc-rate",
        "title": "Fed Interest Rate Decision",
        "title_en": "Fed Interest Rate Decision",
        "country": "US",
        "impact": "high",
        "time": "2026-06-11T18:00:00Z",
        "forecast": "4.25%",
        "previous": "4.25%",
        "actual": None,
    },
    {
        "id": "us-cpi-may",
        "title": "US CPI (MoM)",
        "title_en": "US CPI Month over Month",
        "country": "US",
        "impact": "high",
        "time": "2026-06-11T12:30:00Z",
        "forecast": "0.2%",
        "previous": "0.2%",
        "actual": None,
    },
    {
        "id": "us-nonfarm",
        "title": "Non-Farm Payrolls",
        "title_en": "Non-Farm Payrolls",
        "country": "US",
        "impact": "high",
        "time": "2026-06-05T12:30:00Z",
        "forecast": "185K",
        "previous": "177K",
        "actual": None,
    },
    {
        "id": "us-gdp-q1",
        "title": "US GDP Q1 (Final)",
        "title_en": "US GDP Q1 Final",
        "country": "US",
        "impact": "high",
        "time": "2026-05-29T12:30:00Z",
        "forecast": "1.2%",
        "previous": "2.4%",
        "actual": "1.3%",
    },
    {
        "id": "kr-boi-rate",
        "title": "한국은행 기준금리 결정",
        "title_en": "Bank of Korea Rate Decision",
        "country": "KR",
        "impact": "high",
        "time": "2026-05-29T01:00:00Z",
        "forecast": "2.75%",
        "previous": "2.75%",
        "actual": None,
    },
    {
        "id": "us-pce",
        "title": "PCE Price Index (YoY)",
        "title_en": "PCE Price Index YoY",
        "country": "US",
        "impact": "high",
        "time": "2026-05-30T12:30:00Z",
        "forecast": "2.3%",
        "previous": "2.3%",
        "actual": None,
    },
    {
        "id": "eu-ecb",
        "title": "ECB Interest Rate Decision",
        "title_en": "ECB Interest Rate Decision",
        "country": "EU",
        "impact": "high",
        "time": "2026-06-05T11:15:00Z",
        "forecast": "2.4%",
        "previous": "2.65%",
        "actual": None,
    },
    {
        "id": "us-initial-claims",
        "title": "Initial Jobless Claims",
        "title_en": "Initial Jobless Claims",
        "country": "US",
        "impact": "medium",
        "time": "2026-06-05T12:30:00Z",
        "forecast": "225K",
        "previous": "227K",
        "actual": None,
    },
    {
        "id": "cn-caixin-pmi",
        "title": "Caixin China PMI",
        "title_en": "Caixin China Composite PMI",
        "country": "CN",
        "impact": "medium",
        "time": "2026-06-03T01:45:00Z",
        "forecast": "51.2",
        "previous": "51.1",
        "actual": None,
    },
    {
        "id": "jp-boj",
        "title": "Bank of Japan Rate Decision",
        "title_en": "Bank of Japan Policy Rate",
        "country": "JP",
        "impact": "high",
        "time": "2026-06-16T03:00:00Z",
        "forecast": "0.5%",
        "previous": "0.5%",
        "actual": None,
    },
]


@router.get("/calendar")
def get_calendar(
    country: Optional[str] = Query(None),
    impact: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None),
    to_date: Optional[str] = Query(None),
):
    events = list(_STATIC_CALENDAR)
    if country:
        events = [e for e in events if e["country"].upper() == country.upper()]
    if impact:
        events = [e for e in events if e["impact"] == impact.lower()]
    if from_date:
        events = [e for e in events if e["time"] >= from_date]
    if to_date:
        events = [e for e in events if e["time"] <= to_date + "Z"]

    return Resp.ok(events)


# ── Market sentiment (Fear & Greed) ──────────────────────────────────────

@router.get("/sentiment")
def get_sentiment():
    """
    Return a market sentiment index.
    Derives a simple proxy from VIX: high VIX = fear, low = greed.
    Falls back to static data if yfinance is unavailable.
    """
    try:
        vix = yf.Ticker("^VIX")
        fi = vix.fast_info
        vix_price = float(fi.last_price or 20)
        # Map VIX to 0-100 fear/greed (inverted: low VIX = greed)
        # Typical range: 10 (extreme greed) to 80 (extreme fear)
        clamped = max(10.0, min(80.0, vix_price))
        fear_greed = round(100 - (clamped - 10) / 70 * 100)
        if fear_greed >= 75:
            classification = "Extreme Greed"
        elif fear_greed >= 55:
            classification = "Greed"
        elif fear_greed >= 45:
            classification = "Neutral"
        elif fear_greed >= 25:
            classification = "Fear"
        else:
            classification = "Extreme Fear"
    except Exception as e:
        logger.warning("VIX fetch failed: %s", e)
        fear_greed = 50
        classification = "Neutral"
        vix_price = 20.0

    return Resp.ok(
        {
            "fear_greed": fear_greed,
            "classification": classification,
            "vix": round(vix_price, 2),
            "updated_at": datetime.utcnow().isoformat(),
        }
    )
