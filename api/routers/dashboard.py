"""Dashboard endpoints – portfolio summary and pending orders."""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from api.crypto import CredentialUnreadable, kis_credential_fields
from api.database import get_db
from api.deps import get_current_user
from api.models import Credential, Strategy, Trade, User
from api.schemas import Resp

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _get_kis_credential(user_id: int, db: Session) -> Optional[Credential]:
    """Return the first KIS credential for the user, or None."""
    return (
        db.query(Credential)
        .filter(Credential.user_id == user_id, Credential.exchange_id == "kis")
        .first()
    )


def _build_kis_client_from_cred(cred: Credential):
    """
    Build request-scoped (KISClient, KISPortfolio) from the stored credential.

    Credentials are injected explicitly into the client instance (P0-03); the
    process-wide ``os.environ`` is never mutated, so concurrent requests from
    different users cannot leak or overwrite each other's credentials.

    Raises ``CredentialUnreadable`` before any client exists when a stored
    field does not open under the current key (#182).
    """
    from kis_adapter import KISClient, KISCredentials, KISPortfolio

    creds = KISCredentials(**kis_credential_fields(cred), env=cred.env)
    client = KISClient(creds)
    portfolio = KISPortfolio(client)
    return client, portfolio


def _summary_row(result: dict) -> dict:
    """``output2`` as one dict — KIS returns it as a dict or a one-row list."""
    row = result.get("summary") or {}
    if isinstance(row, list):
        row = row[0] if row else {}
    if not isinstance(row, dict):
        raise TypeError(f"unexpected balance summary: {type(row).__name__}")
    return row


def _read_kr(portfolio) -> dict:
    result = portfolio.get_kr_balance()
    row = _summary_row(result)
    eval_amt = float(row.get("tot_evlu_amt", 0) or 0)
    pnl = float(row.get("evlu_pfls_smtl_amt", 0) or 0)
    rate = 0.0
    cost = eval_amt - pnl          # guard on the cost basis: a position written
    if cost > 0:                   # down to 0 is -100%, not 0%
        rate = round(pnl / cost * 100, 2)
    return {"eval": eval_amt, "pnl": pnl, "rate": rate,
            "positions": result.get("positions", [])}


def _read_us(portfolio) -> dict:
    result = portfolio.get_us_balance()
    row = _summary_row(result)
    return {"eval": float(row.get("tot_evlu_amt", 0) or 0),
            "positions": result.get("positions", [])}


def _market_summary(read, portfolio, market: str, errors: dict) -> Optional[dict]:
    """One market's parsed balance, or ``None`` with ``errors[market]`` set.

    Fetching *and* parsing sit inside the guard, so an unexpected response
    shape fails that market rather than the whole summary. The detail goes to
    the log, never to the client: a KIS error body is internal, and the user
    only needs to know this side is unknown.
    """
    try:
        return read(portfolio)
    except Exception as e:  # noqa: BLE001 - reported, never masked as zeros
        logger.warning("KIS %s balance fetch failed: %s", market.upper(), e)
        errors[market] = f"{market.upper()} 잔고 조회 실패"
        return None


def _performance(user_id: int, db: Session) -> dict:
    """Trade statistics for the home KPIs; a ratio with nothing to divide is ``None``.

    Only trades with a non-zero ``pnl`` are closed trades — an opening buy
    carries 0 and says nothing about winning or losing. With no closed trade
    there is no win rate, and with no losing trade no profit factor: ``None``,
    which the app shows as "—", never 0.
    """
    pnls = [
        p for (p,) in db.query(Trade.pnl)
        .join(Strategy, Trade.strategy_id == Strategy.id)
        .filter(Strategy.user_id == user_id)
        .all()
    ]
    total = len(pnls)
    closed = [p for p in pnls if p]
    wins = [p for p in closed if p > 0]
    gross_loss = -sum(p for p in closed if p < 0)
    return {
        "total_trades": total,
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "profit_factor": round(sum(wins) / gross_loss, 2) if gross_loss else None,
    }


@router.get("/summary")
def get_summary(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Portfolio summary plus our own strategy/trade figures.

    Portfolio fields are ``None`` — never 0 — when they are unknown, and
    ``portfolio_status`` says why: ``no_credential``, ``unavailable`` (nothing
    could be read), ``partial`` (one market failed) or ``ok``. Zeros used to
    make an outage, an unreadable credential and an empty account identical,
    which on a home screen reads as "you hold nothing" (#149 for QuickTrade).

    The response stays ``Resp.ok``: strategy counts and recent trades come from
    our DB and are valid either way.
    """
    total_assets_krw = None
    total_assets_usd = None
    total_profit_krw = None
    total_profit_rate = None
    kr_positions = None
    us_positions = None
    errors: dict[str, str] = {}

    cred = _get_kis_credential(current_user.id, db)
    if not cred:
        status = "no_credential"
    else:
        kr = us = None
        try:
            _client, portfolio = _build_kis_client_from_cred(cred)
        except CredentialUnreadable as e:
            errors["credential"] = str(e)
        except Exception as e:  # noqa: BLE001
            logger.warning("KIS client build failed: %s", e)
            errors["credential"] = "KIS 클라이언트를 만들 수 없습니다"
        else:
            kr = _market_summary(_read_kr, portfolio, "kr", errors)
            us = _market_summary(_read_us, portfolio, "us", errors)

        if kr is not None:
            total_assets_krw = kr["eval"]
            total_profit_krw = kr["pnl"]
            total_profit_rate = kr["rate"]
            kr_positions = kr["positions"]
        if us is not None:
            total_assets_usd = us["eval"]
            us_positions = us["positions"]

        if kr is not None and us is not None:
            status = "ok"
        elif kr is not None or us is not None:
            status = "partial"
        else:
            status = "unavailable"

    # Strategy counts
    strategy_count = (
        db.query(Strategy).filter(Strategy.user_id == current_user.id).count()
    )
    running_count = (
        db.query(Strategy)
        .filter(Strategy.user_id == current_user.id, Strategy.status == "running")
        .count()
    )

    performance = _performance(current_user.id, db)

    # Recent trades
    recent_trades_q = (
        db.query(Trade)
        .join(Strategy, Trade.strategy_id == Strategy.id)
        .filter(Strategy.user_id == current_user.id)
        .order_by(Trade.filled_at.desc())
        .limit(5)
        .all()
    )
    recent_trades = [
        {
            "id": t.id,
            "symbol": t.symbol,
            "side": t.side,
            "qty": t.qty,
            "price": t.price,
            "pnl": t.pnl or 0.0,
            "filled_at": t.filled_at.isoformat() if t.filled_at else None,
        }
        for t in recent_trades_q
    ]

    return Resp.ok(
        {
            "total_assets_krw": total_assets_krw,
            "total_assets_usd": total_assets_usd,
            "total_profit_krw": total_profit_krw,
            "total_profit_rate": total_profit_rate,
            "strategy_count": strategy_count,
            "running_strategies": running_count,
            "kr_positions": kr_positions,
            "us_positions": us_positions,
            "recent_trades": recent_trades,
            "performance": performance,
            "portfolio_status": status,
            "portfolio_errors": errors,
        }
    )


@router.get("/pendingOrders")
def get_pending_orders(
    credential_id: Optional[int] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return pending (unfilled) orders from KIS."""
    cred = None
    if credential_id:
        cred = (
            db.query(Credential)
            .filter(
                Credential.id == credential_id,
                Credential.user_id == current_user.id,
            )
            .first()
        )
    else:
        cred = _get_kis_credential(current_user.id, db)

    if not cred:
        return Resp.ok({"items": []})

    try:
        _client, _ = _build_kis_client_from_cred(cred)
        from kis_adapter import KISMarketData

        md = KISMarketData(_client)
        pending = md.get_pending_us(_client.auth.account_no)
        return Resp.ok({"items": pending})
    except CredentialUnreadable as e:
        # Not "no pending orders": the broker was never asked (#182).
        return Resp.err(str(e))
    except Exception as e:  # noqa: BLE001 - reported, never masked
        # An empty list here read as "nothing pending" during a broker outage.
        logger.warning("Pending orders fetch failed: %s", e)
        return Resp.err("미체결 주문 조회 실패")
