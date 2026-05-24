"""Strategy CRUD + lifecycle (start/stop) + trades/positions/logs/notifications."""
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from api.database import get_db
from api.deps import get_current_user
from api.models import Notification, Strategy, StrategyLog, Trade, User
from api.schemas import (
    Resp,
    StrategyCreate,
    StrategyUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/strategies", tags=["strategies"])

# ── Redis (optional – graceful fallback if unavailable) ───────────────────
_redis = None

def _get_redis():
    global _redis
    if _redis is not None:
        return _redis
    try:
        import redis

        _redis = redis.from_url(
            os.environ.get("REDIS_URL", "redis://redis:6379/0"),
            decode_responses=True,
        )
        _redis.ping()
    except Exception:
        _redis = None
    return _redis


RUNNING_KEY = "running_strategies"


def _mark_running(strategy_id: int):
    r = _get_redis()
    if r:
        try:
            r.sadd(RUNNING_KEY, str(strategy_id))
        except Exception:
            pass


def _mark_stopped(strategy_id: int):
    r = _get_redis()
    if r:
        try:
            r.srem(RUNNING_KEY, str(strategy_id))
        except Exception:
            pass


def _strategy_to_dict(s: Strategy) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "type": s.type,
        "status": s.status,
        "symbol": s.symbol,
        "timeframe": s.timeframe,
        "market_type": s.market_type,
        "direction": s.direction,
        "initial_capital": s.initial_capital,
        "config": s.config or {},
        "script_code": s.script_code,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


# ── List / detail ─────────────────────────────────────────────────────────

@router.get("")
def list_strategies(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = db.query(Strategy).filter(Strategy.user_id == current_user.id)
    if status:
        q = q.filter(Strategy.status == status)
    total = q.count()
    items = q.order_by(Strategy.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return Resp.ok({"total": total, "items": [_strategy_to_dict(s) for s in items]})


@router.get("/detail")
def get_strategy(
    id: int = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    s = (
        db.query(Strategy)
        .filter(Strategy.id == id, Strategy.user_id == current_user.id)
        .first()
    )
    if not s:
        return Resp.err("Strategy not found")
    return Resp.ok(_strategy_to_dict(s))


# ── Create / update / delete ──────────────────────────────────────────────

@router.post("/create")
def create_strategy(
    body: StrategyCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    s = Strategy(
        user_id=current_user.id,
        name=body.name,
        type=body.type,
        symbol=body.symbol,
        timeframe=body.timeframe,
        market_type=body.market_type,
        direction=body.direction,
        initial_capital=body.initial_capital,
        config=body.config or {},
        script_code=body.script_code,
        status="stopped",
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return Resp.ok(_strategy_to_dict(s))


@router.post("/batch-create")
def batch_create_strategies(
    body: list,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    created = []
    for item in body:
        s = Strategy(
            user_id=current_user.id,
            name=item.get("name", "Unnamed"),
            type=item.get("type", "indicator"),
            symbol=item.get("symbol"),
            timeframe=item.get("timeframe", "1h"),
            market_type=item.get("market_type", "spot"),
            direction=item.get("direction", "long"),
            initial_capital=item.get("initial_capital", 10000.0),
            config=item.get("config", {}),
            script_code=item.get("script_code"),
            status="stopped",
        )
        db.add(s)
        created.append(s)
    db.commit()
    for s in created:
        db.refresh(s)
    return Resp.ok({"items": [_strategy_to_dict(s) for s in created]})


@router.put("/update")
def update_strategy(
    body: StrategyUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    s = (
        db.query(Strategy)
        .filter(Strategy.id == body.id, Strategy.user_id == current_user.id)
        .first()
    )
    if not s:
        return Resp.err("Strategy not found")
    if body.name is not None:
        s.name = body.name
    if body.type is not None:
        s.type = body.type
    if body.symbol is not None:
        s.symbol = body.symbol
    if body.timeframe is not None:
        s.timeframe = body.timeframe
    if body.market_type is not None:
        s.market_type = body.market_type
    if body.direction is not None:
        s.direction = body.direction
    if body.initial_capital is not None:
        s.initial_capital = body.initial_capital
    if body.config is not None:
        s.config = body.config
    if body.script_code is not None:
        s.script_code = body.script_code
    s.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(s)
    return Resp.ok(_strategy_to_dict(s))


@router.delete("/delete")
def delete_strategy(
    id: int = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    s = (
        db.query(Strategy)
        .filter(Strategy.id == id, Strategy.user_id == current_user.id)
        .first()
    )
    if not s:
        return Resp.err("Strategy not found")
    _mark_stopped(s.id)
    db.delete(s)
    db.commit()
    return Resp.ok(None, "Deleted")


# ── Start / stop ──────────────────────────────────────────────────────────

@router.post("/start")
def start_strategy(
    id: int = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    s = (
        db.query(Strategy)
        .filter(Strategy.id == id, Strategy.user_id == current_user.id)
        .first()
    )
    if not s:
        return Resp.err("Strategy not found")
    s.status = "running"
    s.updated_at = datetime.utcnow()
    db.commit()
    _mark_running(s.id)

    log = StrategyLog(
        strategy_id=s.id,
        message="Strategy started",
        level="INFO",
    )
    db.add(log)
    db.commit()

    return Resp.ok({"id": s.id, "status": s.status})


@router.post("/stop")
def stop_strategy(
    id: int = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    s = (
        db.query(Strategy)
        .filter(Strategy.id == id, Strategy.user_id == current_user.id)
        .first()
    )
    if not s:
        return Resp.err("Strategy not found")
    s.status = "stopped"
    s.updated_at = datetime.utcnow()
    db.commit()
    _mark_stopped(s.id)

    log = StrategyLog(
        strategy_id=s.id,
        message="Strategy stopped",
        level="INFO",
    )
    db.add(log)
    db.commit()

    return Resp.ok({"id": s.id, "status": s.status})


# ── Trades ────────────────────────────────────────────────────────────────

@router.get("/trades")
def get_trades(
    strategy_id: int = Query(...),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    s = (
        db.query(Strategy)
        .filter(Strategy.id == strategy_id, Strategy.user_id == current_user.id)
        .first()
    )
    if not s:
        return Resp.err("Strategy not found")
    total = db.query(Trade).filter(Trade.strategy_id == strategy_id).count()
    trades = (
        db.query(Trade)
        .filter(Trade.strategy_id == strategy_id)
        .order_by(Trade.filled_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    items = [
        {
            "id": t.id,
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


# ── Positions (open trades with no sell) ─────────────────────────────────

@router.get("/positions")
def get_positions(
    strategy_id: int = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    s = (
        db.query(Strategy)
        .filter(Strategy.id == strategy_id, Strategy.user_id == current_user.id)
        .first()
    )
    if not s:
        return Resp.err("Strategy not found")
    # Aggregate: sum qty for buy, subtract for sell per symbol
    buy_q = (
        db.query(Trade.symbol, func.sum(Trade.qty).label("buy_qty"))
        .filter(Trade.strategy_id == strategy_id, Trade.side == "buy")
        .group_by(Trade.symbol)
        .all()
    )
    sell_q = (
        db.query(Trade.symbol, func.sum(Trade.qty).label("sell_qty"))
        .filter(Trade.strategy_id == strategy_id, Trade.side == "sell")
        .group_by(Trade.symbol)
        .all()
    )
    sell_map = {row.symbol: row.sell_qty for row in sell_q}
    positions = []
    for row in buy_q:
        net_qty = (row.buy_qty or 0) - (sell_map.get(row.symbol) or 0)
        if net_qty > 0:
            positions.append({"symbol": row.symbol, "qty": net_qty})
    return Resp.ok({"items": positions})


# ── Equity curve ──────────────────────────────────────────────────────────

@router.get("/equityCurve")
def get_equity_curve(
    strategy_id: int = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    s = (
        db.query(Strategy)
        .filter(Strategy.id == strategy_id, Strategy.user_id == current_user.id)
        .first()
    )
    if not s:
        return Resp.err("Strategy not found")

    trades = (
        db.query(Trade)
        .filter(Trade.strategy_id == strategy_id)
        .order_by(Trade.filled_at.asc())
        .all()
    )

    capital = s.initial_capital or 10000.0
    curve = [{"time": s.created_at.isoformat(), "value": capital}]
    running = capital
    for t in trades:
        running += t.pnl or 0.0
        curve.append({
            "time": t.filled_at.isoformat() if t.filled_at else None,
            "value": round(running, 2),
        })
    return Resp.ok({"items": curve})


# ── Performance metrics ───────────────────────────────────────────────────

@router.get("/performance")
def get_performance(
    strategy_id: int = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    s = (
        db.query(Strategy)
        .filter(Strategy.id == strategy_id, Strategy.user_id == current_user.id)
        .first()
    )
    if not s:
        return Resp.err("Strategy not found")

    trades = db.query(Trade).filter(Trade.strategy_id == strategy_id).all()
    total_trades = len(trades)
    if total_trades == 0:
        return Resp.ok(
            {
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "avg_pnl": 0.0,
                "max_drawdown": 0.0,
                "sharpe_ratio": 0.0,
            }
        )

    pnls = [t.pnl or 0.0 for t in trades]
    winning = sum(1 for p in pnls if p > 0)
    losing = sum(1 for p in pnls if p <= 0)
    total_pnl = sum(pnls)
    avg_pnl = total_pnl / total_trades

    # Max drawdown
    capital = s.initial_capital or 10000.0
    peak = capital
    max_dd = 0.0
    running = capital
    for p in pnls:
        running += p
        if running > peak:
            peak = running
        dd = (peak - running) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    # Simplified Sharpe (daily pnl std)
    import statistics
    sharpe = 0.0
    if len(pnls) > 1:
        try:
            mean = statistics.mean(pnls)
            std = statistics.stdev(pnls)
            sharpe = (mean / std * (252 ** 0.5)) if std > 0 else 0.0
        except Exception:
            pass

    return Resp.ok(
        {
            "total_trades": total_trades,
            "winning_trades": winning,
            "losing_trades": losing,
            "win_rate": round(winning / total_trades * 100, 2),
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(avg_pnl, 2),
            "max_drawdown": round(max_dd * 100, 2),
            "sharpe_ratio": round(sharpe, 3),
        }
    )


# ── Logs ──────────────────────────────────────────────────────────────────

@router.get("/logs")
def get_logs(
    strategy_id: int = Query(...),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    level: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    s = (
        db.query(Strategy)
        .filter(Strategy.id == strategy_id, Strategy.user_id == current_user.id)
        .first()
    )
    if not s:
        return Resp.err("Strategy not found")
    q = db.query(StrategyLog).filter(StrategyLog.strategy_id == strategy_id)
    if level:
        q = q.filter(StrategyLog.level == level.upper())
    total = q.count()
    logs = (
        q.order_by(StrategyLog.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    items = [
        {
            "id": lg.id,
            "message": lg.message,
            "level": lg.level,
            "created_at": lg.created_at.isoformat() if lg.created_at else None,
        }
        for lg in logs
    ]
    return Resp.ok({"total": total, "items": items})


# ── Notifications ─────────────────────────────────────────────────────────

@router.get("/notifications")
def get_notifications(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    is_read: Optional[bool] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = db.query(Notification).filter(Notification.user_id == current_user.id)
    if is_read is not None:
        q = q.filter(Notification.is_read == is_read)
    total = q.count()
    items = (
        q.order_by(Notification.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    data = [
        {
            "id": n.id,
            "strategy_id": n.strategy_id,
            "title": n.title,
            "message": n.message,
            "is_read": n.is_read,
            "created_at": n.created_at.isoformat() if n.created_at else None,
        }
        for n in items
    ]
    return Resp.ok({"total": total, "items": data})


@router.get("/notifications/unread-count")
def get_unread_count(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    count = (
        db.query(Notification)
        .filter(
            Notification.user_id == current_user.id,
            Notification.is_read == False,  # noqa: E712
        )
        .count()
    )
    return Resp.ok({"count": count})


@router.post("/notifications/read")
def mark_notification_read(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    nid = body.get("id")
    if nid:
        n = (
            db.query(Notification)
            .filter(Notification.id == nid, Notification.user_id == current_user.id)
            .first()
        )
        if n:
            n.is_read = True
            db.commit()
    return Resp.ok(None)


@router.post("/notifications/read-all")
def mark_all_read(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db.query(Notification).filter(
        Notification.user_id == current_user.id,
        Notification.is_read == False,  # noqa: E712
    ).update({"is_read": True})
    db.commit()
    return Resp.ok(None)


@router.delete("/notifications/clear")
def clear_notifications(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db.query(Notification).filter(Notification.user_id == current_user.id).delete()
    db.commit()
    return Resp.ok(None, "Cleared")


# ── Test connection ───────────────────────────────────────────────────────

@router.post("/test-connection")
def test_connection(body: dict):
    return Resp.ok({"connected": True, "latency_ms": 42})


# ── AI generate stub ─────────────────────────────────────────────────────

@router.post("/ai-generate")
def ai_generate(
    body: dict,
    current_user: User = Depends(get_current_user),
):
    return Resp.ok(
        {
            "script_code": "# AI-generated strategy placeholder\n# Configure your strategy logic here\n",
            "name": "AI Strategy",
            "description": "AI-generated strategy",
        }
    )


# ── Backtest ──────────────────────────────────────────────────────────────

@router.post("/backtest")
def run_backtest(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    strategy_id = body.get("strategy_id")
    symbol = body.get("symbol", "")
    period = body.get("period", "1y")
    initial_capital = float(body.get("initial_capital", 1_000_000))

    s = None
    if strategy_id:
        s = (
            db.query(Strategy)
            .filter(Strategy.id == strategy_id, Strategy.user_id == current_user.id)
            .first()
        )

    try:
        from strategy import Backtester, IndicatorStrategy, ScriptStrategy

        if s and s.type == "indicator" and s.config:
            strat = IndicatorStrategy.from_config(s.config)
            sym = symbol or s.symbol or "AAPL"
        elif s and s.type == "script" and s.script_code:
            strat = ScriptStrategy(code=s.script_code, params=s.config or {})
            strat.on_start()
            sym = symbol or s.symbol or "AAPL"
        else:
            strat = IndicatorStrategy.from_config(body.get("config", {}))
            sym = symbol or "AAPL"

        bt = Backtester(strat, sym, initial_capital=initial_capital, period=period)
        result = bt.run()
        return Resp.ok(result.to_dict())
    except Exception as e:
        logger.error("백테스트 실패: %s", e)
        return Resp.err(f"백테스트 실패: {e}", code=-1)
