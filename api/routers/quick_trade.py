"""Quick-trade endpoints: balance, positions, order placement, history."""
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from api.crypto import decrypt
from api.database import get_db
from api.deps import get_current_user
from api.models import Credential, Strategy, Trade, User
from api.schemas import ClosePositionRequest, PlaceOrderRequest, Resp
from backend.brokers.semantic_mapper import KIS_DOMESTIC_MAPPER, KIS_OVERSEAS_MAPPER

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/quick-trade", tags=["quick-trade"])


def _load_kis(cred: Credential):
    """Build request-scoped KIS clients from a credential, without touching env.

    Credentials are injected explicitly into the client instance (P0-03); the
    process-wide ``os.environ`` is never mutated, so concurrent requests from
    different users cannot leak or overwrite each other's credentials.
    """
    from kis_adapter import KISClient, KISCredentials, KISOrders, KISPortfolio

    creds = KISCredentials(
        app_key=decrypt(cred.app_key_enc) or "",
        app_secret=decrypt(cred.app_secret_enc) or "",
        account_no=decrypt(cred.account_no_enc) or "",
        hts_id=decrypt(cred.hts_id_enc) or "",
        env=cred.env,
    )
    client = KISClient(creds)
    orders = KISOrders(client)
    portfolio = KISPortfolio(client)
    return client, orders, portfolio


def _get_cred(credential_id: int, user_id: int, db: Session) -> Optional[Credential]:
    return (
        db.query(Credential)
        .filter(Credential.id == credential_id, Credential.user_id == user_id)
        .first()
    )


# ── Balance ───────────────────────────────────────────────────────────────

@router.get("/balance")
def get_balance(
    credential_id: int = Query(...),
    market: str = Query("us"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cred = _get_cred(credential_id, current_user.id, db)
    if not cred:
        return Resp.err("Credential not found")

    try:
        _, _, portfolio = _load_kis(cred)
        if market.lower() == "kr":
            result = portfolio.get_kr_balance()
            summary = result.get("summary", {})
            return Resp.ok(
                {
                    "currency": "KRW",
                    "total_eval": float(summary.get("tot_evlu_amt", 0) or 0),
                    "cash": float(summary.get("dnca_tot_amt", 0) or 0),
                    "positions": result.get("positions", []),
                }
            )
        else:
            result = portfolio.get_us_balance()
            summary = result.get("summary", {})
            return Resp.ok(
                {
                    "currency": "USD",
                    "total_eval": float(summary.get("tot_evlu_amt", 0) or 0),
                    "cash": float(summary.get("frcr_dncl_amt_2", 0) or 0),
                    "positions": result.get("positions", []),
                }
            )
    except Exception as e:
        logger.warning("balance fetch failed: %s", e)
        return Resp.ok({"currency": "USD", "total_eval": 0.0, "cash": 0.0, "positions": []})


# ── Position ──────────────────────────────────────────────────────────────

@router.get("/position")
def get_position(
    credential_id: int = Query(...),
    symbol: str = Query(...),
    market: str = Query("us"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cred = _get_cred(credential_id, current_user.id, db)
    if not cred:
        return Resp.err("Credential not found")

    try:
        _, _, portfolio = _load_kis(cred)
        if market.lower() == "kr":
            result = portfolio.get_kr_balance()
        else:
            result = portfolio.get_us_balance()

        positions = result.get("positions", [])
        # Find matching symbol
        for pos in positions:
            sym_key = pos.get("pdno") or pos.get("ovrs_pdno") or ""
            if sym_key.upper() == symbol.upper():
                return Resp.ok({"symbol": symbol, "position": pos})
        return Resp.ok({"symbol": symbol, "position": None})
    except Exception as e:
        logger.warning("position fetch failed: %s", e)
        return Resp.ok({"symbol": symbol, "position": None})


# ── Place order ───────────────────────────────────────────────────────────

@router.post("/place-order")
def place_order(
    body: PlaceOrderRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cred = _get_cred(body.credential_id, current_user.id, db)
    if not cred:
        return Resp.err("Credential not found")

    try:
        _, orders, _ = _load_kis(cred)
        qty = int(body.qty)

        if body.market.lower() == "kr":
            if body.side.lower() == "buy":
                result = orders.buy_kr(body.symbol, qty, int(body.price))
            else:
                result = orders.sell_kr(body.symbol, qty, int(body.price))
        else:
            exchange = body.exchange or "NASD"
            if body.side.lower() == "buy":
                result = orders.buy_us(body.symbol, exchange, qty, body.price)
            else:
                result = orders.sell_us(body.symbol, exchange, qty, body.price)

        # Record trade in DB (quick-trade doesn't belong to a strategy; use strategy_id=None)
        # We store it under a special "manual" strategy if needed – skip for simplicity.
        mapper = KIS_DOMESTIC_MAPPER if body.market.lower() == "kr" else KIS_OVERSEAS_MAPPER
        return Resp.ok(
            {
                "order_id": mapper.extract_broker_order_id(result),
                "symbol": body.symbol,
                "side": body.side,
                "qty": qty,
                "price": body.price,
                "status": "submitted",
            }
        )
    except Exception as e:
        logger.error("place order failed: %s", e)
        return Resp.err(f"Order failed: {e}")


# ── Close position ────────────────────────────────────────────────────────

@router.post("/close-position")
def close_position(
    body: ClosePositionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cred = _get_cred(body.credential_id, current_user.id, db)
    if not cred:
        return Resp.err("Credential not found")

    try:
        _, orders, _ = _load_kis(cred)
        qty = int(body.qty)

        if body.market.lower() == "kr":
            result = orders.sell_kr(body.symbol, qty, int(body.price))
        else:
            exchange = body.exchange or "NASD"
            result = orders.sell_us(body.symbol, exchange, qty, body.price)

        mapper = KIS_DOMESTIC_MAPPER if body.market.lower() == "kr" else KIS_OVERSEAS_MAPPER
        return Resp.ok(
            {
                "order_id": mapper.extract_broker_order_id(result),
                "symbol": body.symbol,
                "side": "sell",
                "qty": qty,
                "price": body.price,
                "status": "submitted",
            }
        )
    except Exception as e:
        logger.error("close position failed: %s", e)
        return Resp.err(f"Close position failed: {e}")


# ── Trade history ─────────────────────────────────────────────────────────

@router.get("/history")
def get_history(
    credential_id: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return trade history across all user strategies."""
    q = (
        db.query(Trade)
        .join(Strategy, Trade.strategy_id == Strategy.id)
        .filter(Strategy.user_id == current_user.id)
        .order_by(Trade.filled_at.desc())
    )
    total = q.count()
    trades = q.offset((page - 1) * page_size).limit(page_size).all()
    items = [
        {
            "id": t.id,
            "strategy_id": t.strategy_id,
            "symbol": t.symbol,
            "side": t.side,
            "qty": t.qty,
            "price": t.price,
            "pnl": t.pnl or 0.0,
            "fee": t.fee or 0.0,
            "filled_at": t.filled_at.isoformat() if t.filled_at else None,
        }
        for t in trades
    ]
    return Resp.ok({"total": total, "items": items})
