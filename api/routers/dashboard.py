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


def _market_summary(fetch, market: str, errors: dict) -> Optional[dict]:
    """One market's balance, or ``None`` with ``errors[market]`` set.

    The detail goes to the log, never to the client: a KIS error body is
    internal, and the user only needs to know this side is unknown.
    """
    try:
        return fetch()
    except Exception as e:  # noqa: BLE001 - reported, never masked as zeros
        logger.warning("KIS %s balance fetch failed: %s", market.upper(), e)
        errors[market] = f"{market.upper()} 잔고 조회 실패"
        return None


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
        kr_result = us_result = None
        try:
            _client, portfolio = _build_kis_client_from_cred(cred)
        except CredentialUnreadable as e:
            errors["credential"] = str(e)
        except Exception as e:  # noqa: BLE001
            logger.warning("KIS client build failed: %s", e)
            errors["credential"] = "KIS 클라이언트를 만들 수 없습니다"
        else:
            kr_result = _market_summary(portfolio.get_kr_balance, "kr", errors)
            us_result = _market_summary(portfolio.get_us_balance, "us", errors)

        if kr_result is not None:
            kr_summary = kr_result.get("summary", {})
            kr_eval = float(kr_summary.get("tot_evlu_amt", 0) or 0)
            kr_pnl = float(kr_summary.get("evlu_pfls_smtl_amt", 0) or 0)
            total_assets_krw = kr_eval
            total_profit_krw = kr_pnl
            total_profit_rate = 0.0
            if kr_eval > 0 and (kr_eval - kr_pnl):
                total_profit_rate = round(kr_pnl / (kr_eval - kr_pnl) * 100, 2)
            kr_positions = kr_result.get("positions", [])

        if us_result is not None:
            us_summary = us_result.get("summary", {})
            total_assets_usd = float(us_summary.get("tot_evlu_amt", 0) or 0)
            us_positions = us_result.get("positions", [])

        if kr_result is not None and us_result is not None:
            status = "ok"
        elif kr_result is not None or us_result is not None:
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
