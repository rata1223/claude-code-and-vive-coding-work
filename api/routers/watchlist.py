"""Watchlist and symbol search endpoints."""
import logging
from typing import Optional

import yfinance as yf
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from api.database import get_db
from api.deps import get_current_user
from api.models import User, WatchlistItem
from api.schemas import Resp, WatchlistAdd, WatchlistRemove
from backend.market.symbols import (
    provider_symbol_candidates,
    resolve_exchange,
    to_backend_symbol,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/market", tags=["market"])

# ── Popular symbols by market ─────────────────────────────────────────────

HOT_SYMBOLS = {
    "NASD": [
        {"symbol": "AAPL", "name": "Apple Inc.", "market": "NASD"},
        {"symbol": "MSFT", "name": "Microsoft Corp.", "market": "NASD"},
        {"symbol": "NVDA", "name": "NVIDIA Corp.", "market": "NASD"},
        {"symbol": "GOOGL", "name": "Alphabet Inc.", "market": "NASD"},
        {"symbol": "AMZN", "name": "Amazon.com Inc.", "market": "NASD"},
        {"symbol": "META", "name": "Meta Platforms", "market": "NASD"},
        {"symbol": "TSLA", "name": "Tesla Inc.", "market": "NASD"},
        {"symbol": "QQQ", "name": "Invesco QQQ Trust", "market": "NASD"},
    ],
    "NYSE": [
        {"symbol": "SPY", "name": "SPDR S&P 500 ETF", "market": "NYSE"},
        {"symbol": "BRK.B", "name": "Berkshire Hathaway B", "market": "NYSE"},
        {"symbol": "JPM", "name": "JPMorgan Chase", "market": "NYSE"},
        {"symbol": "V", "name": "Visa Inc.", "market": "NYSE"},
        {"symbol": "XOM", "name": "Exxon Mobil Corp.", "market": "NYSE"},
        {"symbol": "WMT", "name": "Walmart Inc.", "market": "NYSE"},
    ],
    "KRX": [
        {"symbol": "005930", "name": "삼성전자", "market": "KRX"},
        {"symbol": "000660", "name": "SK하이닉스", "market": "KRX"},
        {"symbol": "035420", "name": "NAVER", "market": "KRX"},
        {"symbol": "051910", "name": "LG화학", "market": "KRX"},
        {"symbol": "373220", "name": "LG에너지솔루션", "market": "KRX"},
        {"symbol": "000270", "name": "기아", "market": "KRX"},
        {"symbol": "005380", "name": "현대차", "market": "KRX"},
    ],
}


# ── Watchlist ─────────────────────────────────────────────────────────────

@router.get("/watchlist/get")
def get_watchlist(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    items = (
        db.query(WatchlistItem)
        .filter(WatchlistItem.user_id == current_user.id)
        .order_by(WatchlistItem.created_at.asc())
        .all()
    )
    result = [
        {"id": w.id, "market": w.market, "symbol": w.symbol, "name": w.name}
        for w in items
    ]
    return Resp.ok({"items": result})


@router.post("/watchlist/add")
def add_to_watchlist(
    body: WatchlistAdd,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    existing = (
        db.query(WatchlistItem)
        .filter(
            WatchlistItem.user_id == current_user.id,
            WatchlistItem.symbol == body.symbol,
        )
        .first()
    )
    if existing:
        return Resp.ok({"id": existing.id}, "Already in watchlist")

    item = WatchlistItem(
        user_id=current_user.id,
        market=body.market,
        symbol=body.symbol,
        name=body.name,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return Resp.ok({"id": item.id, "symbol": item.symbol})


@router.post("/watchlist/remove")
def remove_from_watchlist(
    body: WatchlistRemove,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = (
        db.query(WatchlistItem)
        .filter(
            WatchlistItem.user_id == current_user.id,
            WatchlistItem.symbol == body.symbol,
        )
        .first()
    )
    if item:
        db.delete(item)
        db.commit()
    return Resp.ok(None, "Removed")


@router.get("/watchlist/prices")
def get_watchlist_prices(
    symbols: str = Query(..., description="Comma-separated symbol list"),
    market: str = Query("us"),
    current_user: User = Depends(get_current_user),
):
    """Bulk price fetch for watchlist symbols (yfinance).

    Symbols arrive raw, so each is resolved to the provider's spelling before
    the lookup — a bare six-digit KR code returns nothing from yfinance, which
    would show every Korean row on the watchlist as 0.00 / 0.00%.

    The reported ``symbol`` is always the raw one the caller asked about, never
    the provider spelling, so the response lines up with the request.
    """
    sym_list = [s.strip() for s in symbols.split(",") if s.strip()]
    result = []
    for sym in sym_list:
        quote = None
        for provider_symbol in provider_symbol_candidates(sym):
            try:
                info = yf.Ticker(provider_symbol).fast_info
                price = float(info.last_price or 0)
                prev = float(info.previous_close or 0)
            except Exception as e:
                logger.debug("price skip %s (%s): %s", sym, provider_symbol, e)
                continue
            if price or prev:
                change_pct = round((price - prev) / prev * 100, 2) if prev else 0.0
                quote = {
                    "symbol": sym,
                    "price": price,
                    "change_pct": change_pct,
                    "prev_close": prev,
                }
                break
        result.append(quote or {"symbol": sym, "price": 0.0, "change_pct": 0.0})
    return Resp.ok({"items": result})


# ── Symbol search ─────────────────────────────────────────────────────────

@router.get("/symbols/search")
def search_symbols(
    q: str = Query("", min_length=0),
    market: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
):
    """Simple symbol search – matches against built-in catalogue."""
    all_symbols: list = []
    for mkt_symbols in HOT_SYMBOLS.values():
        all_symbols.extend(mkt_symbols)

    query_lower = q.lower()
    matched = []
    for s in all_symbols:
        if market and s["market"] != market.upper():
            continue
        if query_lower and query_lower not in s["symbol"].lower() and query_lower not in s["name"].lower():
            continue
        matched.append(s)
        if len(matched) >= limit:
            break

    # If no match from catalogue, try yfinance lookup. The query is a *raw*
    # symbol, so it is resolved to the provider's spelling first — a bare
    # six-digit KR code means nothing to yfinance, and searching for one used
    # to return no result at all. A query that is not a tradable equity yields
    # no candidates and is not looked up.
    if not matched and q:
        for provider_symbol in provider_symbol_candidates(q):
            try:
                info = yf.Ticker(provider_symbol).info
            except Exception:
                continue
            if info.get("symbol"):
                matched.append(
                    {
                        # Report the raw symbol, never the provider spelling:
                        # this row is what the client sends back to order entry.
                        "symbol": to_backend_symbol(q),
                        "name": info.get("shortName") or info.get("longName") or q.upper(),
                        "market": resolve_exchange(q) or market or "NASD",
                    }
                )
                break

    return Resp.ok({"items": matched, "total": len(matched)})


@router.get("/symbols/hot")
def get_hot_symbols(
    market: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
):
    if market:
        items = HOT_SYMBOLS.get(market.upper(), [])
    else:
        items = []
        for v in HOT_SYMBOLS.values():
            items.extend(v)
    return Resp.ok({"items": items[:limit]})
