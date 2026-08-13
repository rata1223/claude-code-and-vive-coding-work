"""Quick-trade endpoints: balance, positions, order placement, history."""
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.orm import Session

from api.crypto import decrypt
from api.database import get_db
from api.deps import get_current_user
from api.models import Credential, QT_SUBMITTED, Strategy, Trade, User
from api.schemas import ClosePositionRequest, PlaceOrderRequest, Resp
from api.services.quick_trade_service import (
    IdempotencyConflict,
    RiskDenied,
    derive_idempotency_key,
    request_fingerprint,
    reserve_and_submit,
)
from backend.brokers.semantic_mapper import KIS_DOMESTIC_MAPPER, KIS_OVERSEAS_MAPPER
from strategy.risk import RiskManager
from backend.risk.halt_policy import HaltCause, OperationClass, is_allowed
from backend.risk.sellable_qty import resolve_sellable, validate_sell_qty

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/quick-trade", tags=["quick-trade"])


def _halt_cause():
    """The active halt cause for the QuickTrade path, or ``None``.

    The signal is the Redis flag ``risk:trading_halted``, whose only writer is
    ``RiskManager.record_daily_loss`` — i.e. a risk-limit breach. It is
    therefore reported as ``RISK_BREACH``. Untrusted state is a worker-process
    concept (``SAFE_MODE``); unifying the two halt stores is out of S1 scope,
    so it is not consulted here.

    Raises whatever ``RiskManager`` raises — the caller must let that propagate
    so an unevaluatable gate fails closed (R3).
    """
    return HaltCause.RISK_BREACH if RiskManager().is_trading_halted() else None


def get_risk_gate():
    """FastAPI dependency: the ENTRY pre-submit risk gate (P0-05).

    Used by ``place-order``, which carries no position proof: an order that
    cannot be shown to reduce exposure is ENTRY, and every halt blocks it
    (P0-07 S1 R1). Reuses the existing production ``RiskManager`` as-is. If
    ``RiskManager()`` construction or ``is_trading_halted()`` raises (e.g. Redis
    unreachable), the exception propagates and ``reserve_and_submit`` fails closed.
    Overridable in tests via ``app.dependency_overrides``.
    """
    def risk_gate():
        if not is_allowed(_halt_cause(), OperationClass.ENTRY):
            raise RiskDenied("trading halted by RiskManager")

    return risk_gate


def get_exit_risk_gate():
    """FastAPI dependency: the EXIT pre-submit risk gate (P0-07 S1, Policy B).

    Used by ``close-position`` only. A halt must stop new risk without removing
    the ability to reduce risk already held — trapping a user in a position
    during a drawdown halt is what this gate exists to prevent.

    This is safe *because the caller has already proven the exit*: the handler
    looks up the live held quantity, rejects an over-close outright, and refuses
    to price off anything but a live quote, all before reserving. The gate is
    still evaluated on every call so that an unevaluatable halt state (Redis
    down) fails closed exactly like the ENTRY gate (R3).
    """
    def risk_gate():
        if not is_allowed(_halt_cause(), OperationClass.EXIT):
            raise RiskDenied("trading halted by RiskManager")

    return risk_gate


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


def _load_market_data(client):
    """Live quote source, built from an already request-scoped KIS client."""
    from kis_adapter import KISMarketData

    return KISMarketData(client)


# Live holding quantity field, by market. KIS returns these as strings.
_QTY_FIELD = {"kr": "hldg_qty", "us": "ovrs_cblc_qty"}
_SYMBOL_FIELDS = ("pdno", "ovrs_pdno")
# P0-07 S2: KIS states how much of a holding is actually orderable
# (주문가능수량) in the same row. Held is not sellable — shares can be unsettled
# or already committed to a resting order.
_ORDERABLE_FIELD = "ord_psbl_qty"


def _live_position_row(portfolio, symbol: str, market: str) -> dict:
    """The raw broker balance row for ``symbol``, or ``{}`` if not held.

    Raises on lookup failure so the handler can reject rather than guess.
    """
    result = portfolio.get_kr_balance() if market == "kr" else portfolio.get_us_balance()
    for pos in result.get("positions", []) or []:
        sym = next((pos.get(f) for f in _SYMBOL_FIELDS if pos.get(f)), "") or ""
        if sym.upper() == symbol.upper():
            return pos
    return {}


def _int_field(row: dict, field: str):
    raw = row.get(field)
    if raw is None or raw == "":
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


_MARKETS = ("kr", "us")


def _normalize_market(requested: Optional[str]) -> Optional[str]:
    """``"kr"`` / ``"us"`` when ``requested`` names a market, else ``None``.

    Callers must use the *returned* value, never the raw input: `` KR `` names
    a market but is not one, and comparing the raw string against ``"kr"``
    would silently route it to the US path.
    """
    value = (requested or "").strip().lower()
    return value if value in _MARKETS else None


def _resolve_market(symbol: str, requested: Optional[str]) -> str:
    """The market this request is actually about — ``"kr"`` or ``"us"``.

    Honours ``requested`` when it names a market, and otherwise derives it from
    the symbol. Deriving is not a convenience: the UI has no market concept to
    send. Its toggle is ``spot``/``swap`` (crypto vocabulary inherited from
    QuantDinger), and there is no ``kr``/``us`` anywhere in the view or the API
    client. So every QuickTrade endpoint received either ``"spot"`` (renamed
    from ``market_type`` by ``api/compat.py``) or nothing at all, both of which
    failed the ``== "kr"`` test and routed KR requests to the US path — leaving
    KR positions impossible to see, buy, sell or close from the UI.

    The rule is not a new invention: it is ``KISBroker._is_kr``
    (``backend/brokers/kis.py``), which already decides how cancels and quotes
    are routed. Reusing it keeps one definition of "is this a KR symbol".
    """
    explicit = _normalize_market(requested)
    if explicit:
        return explicit
    from backend.brokers.kis import KISBroker

    return "kr" if KISBroker._is_kr(symbol or "") else "us"


def _live_held_qty(portfolio, symbol: str, market: str) -> int:
    """Held quantity for ``symbol`` straight from the broker, or 0 if not held.

    The broker is the only authority on close quantity (P0-07C); the caller's
    qty is an upper bound at most. Raises on lookup failure so the handler can
    reject rather than guess.
    """
    row = _live_position_row(portfolio, symbol, market)
    field = _QTY_FIELD["kr" if market == "kr" else "us"]
    return _int_field(row, field) or 0


def _live_sellable(portfolio, symbol: str, market: str, pending_sell_qty: int = 0):
    """Resolve how much of ``symbol`` may actually be sold right now (P0-07 S2).

    Returns ``(SellableResult, held_qty)``. An unknown figure fails closed
    rather than falling back to the held quantity — only EmergencyFlatten does
    that. ``held_qty`` is returned alongside so a caller can tell "nothing is
    sellable right now" from "there is no position", without a second broker
    round trip.
    """
    from backend.risk.sellable_qty import UNKNOWN, resolve_sellable

    row = _live_position_row(portfolio, symbol, market)
    if not row:
        # No holding at all — a known zero, not an unreported figure.
        return resolve_sellable(held_qty=0, broker_sellable=0), 0
    held = _int_field(row, _QTY_FIELD["kr" if market == "kr" else "us"]) or 0
    orderable = _int_field(row, _ORDERABLE_FIELD)
    if orderable is None:
        orderable = UNKNOWN
    return resolve_sellable(held_qty=held, broker_sellable=orderable,
                            pending_sell_qty=pending_sell_qty), held


def _existing_order(db, user_id: int, idempotency_key: str):
    """The already-reserved order for this key, if any.

    A replay must reach ``reserve_and_submit``'s duplicate-key short-circuit,
    which returns the existing row without re-evaluating anything. Re-running
    the live sellable guard first would re-validate the retry against a broker
    figure the *first* submission has already reduced, and reject it — turning
    a safe idempotent retry into a spurious "sellable exceeded".
    """
    from api.models import QuickTradeOrder

    return (
        db.query(QuickTradeOrder)
        .filter(QuickTradeOrder.user_id == user_id,
                QuickTradeOrder.idempotency_key == idempotency_key)
        .first()
    )


def _open_sell_qty(db, user_id: int, credential_id: int, market: str, symbol: str,
                   exclude_key: Optional[str] = None) -> int:
    """Quantity already committed to our own open SELL orders for ``symbol``.

    Reads the durable ``quick_trade_orders`` rows, so the figure survives a
    restart. Terminal statuses have released their quantity.

    ``exclude_key`` drops the row this request would be replaying. Without it an
    idempotent retry would be blocked by its own reservation: the first call
    reserves the quantity, and the identical retry — which submits nothing and
    just returns the existing order — would look like a second ask.

    Scoped to one ``credential_id`` and ``market``, because that is the scope
    the broker figure it is subtracted from describes. The same ticker can be
    held in two accounts, or in both KR and US; counting another account's
    resting sell against this one would refuse a perfectly valid sell.

    Symbol and side are compared case-insensitively **in SQL**. Both are
    persisted verbatim from the request, so ``aapl`` and ``AAPL`` produce two
    rows for one holding; an exact match would sum only some of them, under-
    report pending, and admit the over-ask this whole path exists to refuse.
    """
    from sqlalchemy import func

    from api.models import QuickTradeOrder
    from backend.risk.sellable_qty import pending_sell_qty_from_rows

    q = (
        db.query(QuickTradeOrder.symbol, QuickTradeOrder.side,
                 QuickTradeOrder.qty, QuickTradeOrder.status)
        .filter(QuickTradeOrder.user_id == user_id,
                QuickTradeOrder.credential_id == credential_id,
                func.lower(QuickTradeOrder.market) == (market or "").lower(),
                func.upper(QuickTradeOrder.symbol) == (symbol or "").upper(),
                func.lower(QuickTradeOrder.side) == "sell")
    )
    if exclude_key:
        q = q.filter(QuickTradeOrder.idempotency_key != exclude_key)
    return pending_sell_qty_from_rows(q.all(), symbol)


def _live_close_price(market_data, symbol: str, market: str, exchange: str):
    """Live quote for the close limit price, or ``None`` if unusable.

    Never falls back to the position's average purchase price: pricing a
    liquidation off cost basis can post a deeply off-market limit (the defect
    recorded as G2 in docs/P0_07_CLOSE_POSITION_AUDIT.md).
    """
    try:
        price = (market_data.get_price_kr(symbol) if market == "kr"
                 else market_data.get_price_us(symbol, exchange))
    except Exception as e:
        logger.warning("close price lookup failed %s: %s", symbol, e)
        return None
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


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
    market: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cred = _get_cred(credential_id, current_user.id, db)
    if not cred:
        return Resp.err("Credential not found")

    # No symbol to derive from — a balance request is inherently per-market, so
    # an unusable value (the UI's "spot"/"swap") can only fall back to US.
    # KNOWN GAP: the balance screen therefore still shows US only. Closing it
    # needs a market selector in the UI, which is outside this change.
    market = _normalize_market(market) or "us"

    try:
        _, _, portfolio = _load_kis(cred)
        if market == "kr":
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
    except Exception as e:  # noqa: BLE001 - reported, never masked
        # Reporting zeros here made a broker outage, an expired token and a
        # genuinely empty account identical on the wire. On a trading screen
        # that reads as "you hold nothing", which is acted on. Surface it.
        logger.warning("balance fetch failed: %s", e)
        return Resp.err(f"Balance lookup failed: {e}")


# ── Position ──────────────────────────────────────────────────────────────

@router.get("/position")
def get_position(
    credential_id: int = Query(...),
    symbol: str = Query(...),
    market: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cred = _get_cred(credential_id, current_user.id, db)
    if not cred:
        return Resp.err("Credential not found")

    try:
        _, _, portfolio = _load_kis(cred)
        if _resolve_market(symbol, market) == "kr":
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
    risk_gate=Depends(get_risk_gate),
):
    cred = _get_cred(body.credential_id, current_user.id, db)
    if not cred:
        return Resp.err("Credential not found")

    try:
        qty = int(body.qty)
        market = _resolve_market(body.symbol, body.market)
        order_type = "limit"  # KIS quick-trade submits ORD_DVSN "00" — always limit
        exchange = body.exchange or "NASD"

        # Reject before reserving: a non-positive qty/price must never reach the
        # broker, and KR orders are cast with int() below, so a sub-1 KRW price
        # would truncate to a price-0 limit order. Same guard close-position
        # carries (P0-07C); the buy/direct-sell path was missing it (P0-08 D-1).
        if qty != body.qty:
            # KIS trades whole shares. Truncating would submit — and fingerprint,
            # and persist — a different quantity than the caller requested.
            return Resp.err(f"qty must be a whole number of shares (got {body.qty})")
        if qty <= 0:
            return Resp.err(f"qty must be greater than 0 (got {body.qty})")
        if body.price <= 0:
            return Resp.err(f"price must be greater than 0 (got {body.price})")
        if market == "kr" and int(body.price) <= 0:
            return Resp.err(f"KR price would truncate to 0 (got {body.price})")

        req = {
            "symbol": body.symbol, "side": body.side, "qty": float(qty),
            "price": body.price, "market": market, "exchange": exchange,
            "order_type": order_type,
        }

        # Idempotency key: explicit header if the caller supplied one, else a
        # server-derived double-click fingerprint (no frontend change needed).
        # Derived before the sell guard below, which needs it to exclude the row
        # a retry is replaying.
        fp_args = dict(
            user_id=current_user.id, credential_id=body.credential_id,
            symbol=body.symbol, side=body.side, qty=float(qty), price=body.price,
            market=market, exchange=exchange, order_type=order_type,
        )
        key = idempotency_key or derive_idempotency_key(**fp_args)
        req_hash = request_fingerprint(**fp_args)

        # Built at most once per request and shared with broker_submit below —
        # each _load_kis call decrypts four credential fields and constructs
        # three clients. Lazy so a buy still builds them inside broker_submit,
        # exactly as before: constructing them earlier would move a credential
        # failure from "order failed" to "rejected before reservation".
        _clients = []

        def _kis():
            if not _clients:
                _clients.append(_load_kis(cred))
            return _clients[0]

        # P0-07 S2: a direct sell may not exceed what the broker will actually
        # sell. Held is not sellable — shares can be unsettled or already
        # committed to a resting order — and quantity already committed to our
        # own open sells is subtracted on top, because the broker figure may not
        # reflect a resting order yet. ``exclude_key`` keeps an idempotent retry
        # from being blocked by its own reservation. Buys are unaffected.
        if body.side.lower() == "sell" and not _existing_order(db, current_user.id, key):
            try:
                pending = _open_sell_qty(db, current_user.id, body.credential_id,
                                         market, body.symbol, exclude_key=key)
            except ValueError as e:
                return Resp.err(f"Pending sell lookup failed for {body.symbol}: {e}")
            try:
                _, _, portfolio = _kis()
                sellable, _held = _live_sellable(portfolio, body.symbol, market,
                                                 pending_sell_qty=pending)
            except Exception as e:  # noqa: BLE001 - any lookup failure fails closed
                logger.warning("sellable lookup failed %s: %s", body.symbol, e)
                return Resp.err(f"Sellable lookup failed for {body.symbol}: {e}")
            ok, why = validate_sell_qty(qty, sellable)
            if not ok:
                return Resp.err(f"{why} (대기매도 {pending})" if pending else why)

        mapper = KIS_DOMESTIC_MAPPER if market == "kr" else KIS_OVERSEAS_MAPPER

        def broker_submit():
            # The ONLY broker call site — invoked by the service strictly after
            # the reservation is committed, at most once per idempotency key.
            _, orders, _ = _kis()
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
            risk_gate=risk_gate,
            broker_submit=broker_submit,
            extract_order_id=mapper.extract_broker_order_id,
        )
        payload = {
            "order_id": order.broker_order_id or "",
            "symbol": order.symbol,
            "side": order.side,
            "qty": qty,
            "price": order.price,
            "status": order.status,  # submitted / reserved / rejected / failed
        }
        if order.status == QT_SUBMITTED:
            return Resp.ok(payload)
        # Rejected / reserved(indeterminate) / failed → error envelope so clients
        # that branch on Resp.err keep detecting failed orders (prior behaviour).
        return Resp.err(f"Order {order.status}: {order.error or 'no broker order id'}")
    except IdempotencyConflict:
        return Resp.err("Duplicate idempotency key with different parameters")
    except Exception as e:
        logger.error("place order failed: %s", e)
        return Resp.err(f"Order failed: {e}")


# ── Close position ────────────────────────────────────────────────────────

@router.post("/close-position")
def close_position(
    body: ClosePositionRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    risk_gate=Depends(get_exit_risk_gate),
):
    """Close an open position through the hardened order path (P0-07C).

    live *sellable* qty (P0-07 S2) → qty validation → live price →
    ``reserve_and_submit`` with
    ``side="sell"`` and the P0-05 risk gate. The backend owns quantity and price
    outright. The three pre-reservation rejections (no position, over-close, no
    live price) abort before any DB row is written and before the broker is
    contacted; qty is never silently clamped and price 0 is never submitted.
    """
    cred = _get_cred(body.credential_id, current_user.id, db)
    if not cred:
        return Resp.err("Credential not found")

    # Resolved before the replay short-circuit because the persisted row stores
    # the *resolved* market (``req["market"]`` below), so that is what a replay
    # must be compared against — matching the raw body value would refuse a
    # legitimate retry the moment the UI sends "spot" or omits the field.
    # Safe to hoist: ``_resolve_market`` is pure and contacts no broker.
    market = _resolve_market(body.symbol, body.market)
    # Hoisted for the same reason, and because ``exchange`` is part of the order
    # identity: ``request_fingerprint`` includes it precisely because the same
    # symbol on NASD and on NYSE is a distinct order. The replay short-circuit
    # must not be weaker than the conflict check it bypasses.
    exchange = body.exchange or "NASD"

    # An explicit replay short-circuits before any broker lookup. This handler
    # derives its quantity from the live figure, so re-running the resolution
    # would judge the retry against a broker figure the first submission has
    # already reduced — and reject it as "no sellable quantity" instead of
    # returning the order that call created. Only possible for a caller-supplied
    # key: a server-derived one is a function of the resolved quantity.
    # The short-circuit must not weaken the conflict check it is bypassing.
    # ``reserve_and_submit`` raises IdempotencyConflict when a key is reused with
    # different parameters; returning unconditionally here would hand back an
    # unrelated order for a key reused against another symbol or account. The
    # stored request_hash covers the *resolved* qty and price, which cannot be
    # recomputed without the broker lookup this path is skipping — so compare the
    # client-supplied parameters that are persisted verbatim instead.
    # Wrapped: this runs before the handler's own try/except, so an unusable
    # body value here would escape as a 500 instead of an error envelope.
    try:
        replay = (_existing_order(db, current_user.id, idempotency_key)
                  if idempotency_key else None)
    except Exception as e:  # noqa: BLE001 - a lookup failure must not 500
        logger.warning("close replay lookup failed: %s", e)
        return Resp.err(f"Close position failed: {e}")
    if replay is not None:
        try:
            same_request = (
                replay.credential_id == body.credential_id
                # A close only ever creates a sell. A key whose row is a buy is
                # somebody else's order, not this close's replay — returning it
                # would tell the caller their position was closed by a buy.
                and (replay.side or "").lower() == "sell"
                and (replay.symbol or "").upper() == (body.symbol or "").upper()
                and (replay.market or "").lower() == market
                and (replay.exchange or "").upper() == exchange.upper()
                # Exact, not int()-truncated: 5.9 must not match a stored 5 here
                # when the same 5.9 is refused as a fractional share below.
                #
                # RESIDUAL: an omitted qty (close-all) matches whatever quantity
                # is stored, so a close-all cannot be told apart from an explicit
                # request whose qty happened to equal the resolved one. Nothing
                # persisted records which of the two was asked for, and adding
                # that is a schema change to the order table — out of scope here.
                # Documented in docs/P0_07_S2_SELLABLE_QTY.md.
                and (body.qty is None or float(replay.qty) == float(body.qty))
            )
            if not same_request:
                return Resp.err("Duplicate idempotency key with different parameters")
            payload = {
                "order_id": replay.broker_order_id or "",
                "symbol": replay.symbol, "side": replay.side,
                "qty": int(replay.qty), "price": replay.price,
                "status": replay.status,
            }
            if replay.status == QT_SUBMITTED:
                return Resp.ok(payload)
            return Resp.err(
                f"Close position {replay.status}: {replay.error or 'no broker order id'}"
            )
        except Exception as e:  # noqa: BLE001 - an unusable body must not 500
            logger.warning("close replay comparison failed: %s", e)
            return Resp.err(f"Close position failed: {e}")

    try:
        client, orders, portfolio = _load_kis(cred)

        # 1. Live position — the only authority on how much can be closed.
        #    P0-07 S2: that authority is the *sellable* quantity, not the held
        #    one. Shares can be unsettled or already committed to a resting
        #    sell order; asking for the full holding gets the order rejected.
        try:
            sellable, held_qty = _live_sellable(portfolio, body.symbol, market)
        except Exception as e:
            logger.warning("close position lookup failed %s: %s", body.symbol, e)
            return Resp.err(f"Position lookup failed for {body.symbol}: {e}")
        if not sellable.known:
            return Resp.err(f"Sellable quantity unavailable for {body.symbol}: "
                            f"{sellable.reason}")
        sellable_qty = sellable.qty
        if sellable_qty <= 0:
            # Holding nothing and holding something that cannot be sold right
            # now are different answers. Reporting the second as the first tells
            # an operator mid-close that their position is gone.
            if held_qty > 0:
                return Resp.err(f"No sellable quantity for {body.symbol}: "
                                f"{sellable.reason}")
            return Resp.err(f"No open position for {body.symbol}")

        # 2. Quantity — omitted means close everything the *broker* says is
        #    sellable; an over-close is rejected outright, never silently clamped.
        #
        #    Deliberately independent of local pending state. Netting pending in
        #    here was tried and reverted: the server-derived idempotency key is a
        #    function of this quantity, so letting local state move it means two
        #    identical close-all clicks derive different keys, dedup never fires,
        #    and the position is sold twice. A close refused while one of our own
        #    sells is still in flight is the safe outcome; a duplicate order is
        #    not. Pending is applied below, after the key is fixed.
        if body.qty is None:
            close_qty = sellable_qty
        else:
            requested = int(body.qty)
            if requested != body.qty:
                # Whole shares only — truncating would close a different quantity
                # than requested (and record the truncated one).
                return Resp.err(
                    f"qty must be a whole number of shares (got {body.qty})"
                )
            if requested <= 0:
                return Resp.err("qty must be greater than 0")
            if requested > sellable_qty:
                return Resp.err(
                    f"Requested qty exceeds sellable quantity: {requested} > {sellable_qty}"
                )
            close_qty = requested

        # 3. Live price — no quote, no order (never priced off average cost).
        price = _live_close_price(_load_market_data(client), body.symbol, market, exchange)
        if price is None:
            return Resp.err(f"Live price unavailable for {body.symbol}")
        if market == "kr":
            # KIS domestic orders take an integer price; truncation must not be
            # allowed to turn a valid sub-1 quote into a price-0 order.
            price = float(int(price))
            if price <= 0:
                return Resp.err(f"Live price unavailable for {body.symbol}")

        # 4. Same hardened funnel as place-order: durable reservation →
        #    idempotency → risk gate (fail-closed) → single broker call.
        req = {
            "symbol": body.symbol, "side": "sell", "qty": float(close_qty),
            "price": price, "market": market, "exchange": exchange,
            "order_type": "limit",
        }
        fp_args = dict(
            user_id=current_user.id, credential_id=body.credential_id,
            symbol=body.symbol, side="sell", qty=float(close_qty), price=price,
            market=market, exchange=exchange, order_type="limit",
        )
        key = idempotency_key or derive_idempotency_key(**fp_args)
        req_hash = request_fingerprint(**fp_args)

        # P0-07 S2: quantity committed to our own in-flight sells reserves
        # sellable quantity. Applied after the key so the key stays a function
        # of broker state only (see above), and skipped for a replay — which
        # must reach ``reserve_and_submit``'s duplicate-key short-circuit rather
        # than be blocked by the very reservation it is replaying. That skip
        # covers the server-derived key too, which is why it lives here and not
        # only in the caller-supplied short-circuit above.
        try:
            pending = (0 if _existing_order(db, current_user.id, key)
                       else _open_sell_qty(db, current_user.id, body.credential_id,
                                           market, body.symbol, exclude_key=key))
        except ValueError as e:
            return Resp.err(f"Pending sell lookup failed for {body.symbol}: {e}")
        if pending:
            net = resolve_sellable(held_qty=sellable_qty, broker_sellable=sellable_qty,
                                   pending_sell_qty=pending)
            ok, why = validate_sell_qty(close_qty, net)
            if not ok:
                return Resp.err(f"{why} — pending sells {pending}")

        mapper = KIS_DOMESTIC_MAPPER if market == "kr" else KIS_OVERSEAS_MAPPER

        def broker_submit():
            # The ONLY broker call site for a close — invoked by the service
            # strictly after the reservation is committed and risk allowed.
            if market == "kr":
                return orders.sell_kr(body.symbol, close_qty, int(price))
            return orders.sell_us(body.symbol, exchange, close_qty, price)

        order = reserve_and_submit(
            db,
            user_id=current_user.id,
            credential_id=body.credential_id,
            request=req,
            idempotency_key=key,
            request_hash=req_hash,
            risk_gate=risk_gate,
            broker_submit=broker_submit,
            extract_order_id=mapper.extract_broker_order_id,
        )
        payload = {
            "order_id": order.broker_order_id or "",
            "symbol": order.symbol,
            "side": order.side,
            "qty": close_qty,
            "price": order.price,
            "status": order.status,
        }
        if order.status == QT_SUBMITTED:
            return Resp.ok(payload)
        # Blocked / rejected / reserved(indeterminate) / failed — report the real
        # runtime status instead of asserting a submission that never happened.
        return Resp.err(
            f"Close position {order.status}: {order.error or 'no broker order id'}"
        )
    except IdempotencyConflict:
        return Resp.err("Duplicate idempotency key with different parameters")
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
