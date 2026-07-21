"""Dashboard endpoints – portfolio summary and pending orders."""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from api.crypto import decrypt
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
    """
    from kis_adapter import KISClient, KISCredentials, KISPortfolio

    creds = KISCredentials(
        app_key=decrypt(cred.app_key_enc) or "",
        app_secret=decrypt(cred.app_secret_enc) or "",
        account_no=decrypt(cred.account_no_enc) or "",
        hts_id=decrypt(cred.hts_id_enc) or "",
        env=cred.env,
    )
    client = KISClient(creds)
    portfolio = KISPortfolio(client)
    return client, portfolio


@router.get("/summary")
def get_summary(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return portfolio summary. Falls back to zeros if no KIS credential."""
    total_assets_krw = 0.0
    total_assets_usd = 0.0
    total_profit_krw = 0.0
    total_profit_rate = 0.0
    kr_positions = []
    us_positions = []

    cred = _get_kis_credential(current_user.id, db)
    if cred:
        try:
            _client, portfolio = _build_kis_client_from_cred(cred)
            kr_result = portfolio.get_kr_balance()
            us_result = portfolio.get_us_balance()

            kr_summary = kr_result.get("summary", {})
            us_summary = us_result.get("summary", {})

            kr_eval = float(kr_summary.get("tot_evlu_amt", 0) or 0)
            us_eval_usd = float(us_summary.get("tot_evlu_amt", 0) or 0)

            total_assets_krw = kr_eval
            total_assets_usd = us_eval_usd

            kr_positions = kr_result.get("positions", [])
            us_positions = us_result.get("positions", [])

            # PnL from KR balance
            kr_pnl = float(kr_summary.get("evlu_pfls_smtl_amt", 0) or 0)
            total_profit_krw = kr_pnl
            if kr_eval > 0:
                total_profit_rate = round(kr_pnl / (kr_eval - kr_pnl) * 100, 2) if (kr_eval - kr_pnl) else 0.0
        except Exception as e:
            logger.warning("KIS portfolio fetch failed: %s", e)

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
        account_no = decrypt(cred.account_no_enc) or ""
        pending = md.get_pending_us(account_no)
        return Resp.ok({"items": pending})
    except Exception as e:
        logger.warning("Pending orders fetch failed: %s", e)
        return Resp.ok({"items": []})
