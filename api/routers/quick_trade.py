"""Quick-trade endpoints: balance, positions, order placement, history."""
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.orm import Session

from api.crypto import decrypt
from api.database import get_db
from api.deps import get_current_user
from api.models import Credential, Strategy, Trade, User
from api.schemas import ClosePositionRequest, PlaceOrderRequest, Resp
from api.services.quick_trade_service import (
    IdempotencyConflict,
    derive_idempotency_key,
    request_fingerprint,
    reserve_and_submit,
)
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
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cred = _get_cred(body.credential_id, current_user.id, db)
    if not cred:
        return Resp.err("Credential not found")

    try:
        qty = int(body.qty)
        market = body.market.lower()
        order_type = "limit"  # KIS quick-trade submits ORD_DVSN "00" — always limit
        exchange = body.exchange or "NASD"
        req = {
            "symbol": body.symbol, "side": body.side, "qty": float(qty),
            "price": body.price, "market": market, "order_type": order_type,
        }

        # Idempotency key: explicit header if the caller supplied one, else a
        # server-derived double-click fingerprint (no frontend change needed).
        fp_args = dict(
            user_id=current_user.id, credential_id=body.credential_id,
            symbol=body.symbol, side=body.side, qty=float(qty), price=body.price,
            market=market, order_type=order_type,
        )
        key = idempotency_key or derive_idempotency_key(**fp_args)
        req_hash = request_fingerprint(**fp_args)

        mapper = KIS_DOMESTIC_MAPPER if market == "kr" else KIS_OVERSEAS_MAPPER

        def broker_submit():
            # The ONLY broker call site — invoked by the service strictly after
            # the reservation is committed, at most once per idempotency key.
            _, orders, _ = _load_kis(cred)
            if market == "kr":
                if body.side.lower() == "buy":
                    return orders.buy_kr(body.symbol, qty, int(body.price))
                return orders.sell_kr(body.symbol, qty, int(body.price))
            if body.side.lower() == "buy":
                return orders.buy_us(body.symbol, exchange, qty, body.price)
            return orders.sell_us(body.symbol, exchange, qty, body.price)

        order = reserve_and_submit(
            db,
            user_id=current_user.id,
            credential_id=body.credential_id,
            request=req,
            idempotency_key=key,
            request_hash=req_hash,
            broker_submit=broker_submit,
            extract_order_id=mapper.extract_broker_order_id,
        )
        return Resp.ok(
            {
                "order_id": order.broker_order_id or "",
                "symbol": order.symbol,
                "side": order.side,
                "qty": qty,
                "price": order.price,
                "status": order.status,  # submitted / reserved / rejected / failed
            }
        )
    except IdempotencyConflict:
        return Resp.err("Duplicate idempotency key with different parameters")
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
