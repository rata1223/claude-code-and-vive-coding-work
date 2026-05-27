"""
Flask REST API — 포트 5000
실행: python -m backend.api.server
"""
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime

import redis
from flask import Flask, g, jsonify, request

from backend.database.models import (
    Command, Order, Position, StrategyRun,
    init_db_factory,
)

logger = logging.getLogger(__name__)

app = Flask(__name__)
_redis = redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379"))
_db_factory = None

# API key auth — set KIS_API_KEY env var to enable. Unset = open (dev mode, logged warning).
_API_KEY = os.environ.get("KIS_API_KEY", "")
_OPEN_ROUTES = {"/api/health", "/api/status"}

if not _API_KEY:
    logger.warning("KIS_API_KEY not set — API running without authentication (dev mode)")


@app.before_request
def _check_api_key():
    if request.path in _OPEN_ROUTES or request.method == "OPTIONS":
        return None
    if not _API_KEY:
        return None  # auth disabled
    provided = request.headers.get("X-API-Key", "")
    if provided != _API_KEY:
        return jsonify({"error": "인증 실패"}), 401


def _get_factory():
    global _db_factory
    if _db_factory is None:
        db_url = os.environ.get("DB_URL", "postgresql://quantdinger:quantdinger@postgres:5432/quantdinger")
        _db_factory = init_db_factory(db_url)
    return _db_factory


def get_db():
    """Per-request DB session — auto-closed at request teardown via Flask's g."""
    if "db" not in g:
        g.db = _get_factory()()
    return g.db


@app.teardown_appcontext
def _close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _redis_ok() -> bool:
    try:
        _redis.ping()
        return True
    except Exception:
        return False


def _db_ok() -> bool:
    try:
        from sqlalchemy import text
        get_db().execute(text("SELECT 1"))
        return True
    except Exception:
        return False


# ── 헬스 / 상태 ───────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/api/status")
def get_status():
    """Operational status: kill-switch, infrastructure connectivity.

    NOTE: safe_mode is inferred from DB state — the worker process owns
    the in-memory SafeModeState object and it cannot be read cross-process.
    """
    kill_switch, kill_reason = False, ""
    pending_orders = -1

    try:
        from backend.database.models import DailyRiskState
        from datetime import date
        db = get_db()
        row = db.get(DailyRiskState, date.today())
        if row:
            kill_switch = row.kill_switch
            kill_reason = row.kill_reason or ""
        pending_orders = db.query(Order).filter(
            Order.status.in_(["pending", "submitted", "partial_filled"])
        ).count()
    except Exception as e:
        logger.debug("status DB 조회 실패: %s", e)

    return jsonify({
        "timestamp": datetime.utcnow().isoformat(),
        "kill_switch": kill_switch,
        "kill_reason": kill_reason,
        "pending_orders": pending_orders,
        "redis": _redis_ok(),
        "db": _db_ok(),
        "kis_env": os.environ.get("KIS_ENV", "unknown"),
        "live_trading_enabled": os.environ.get("ENABLE_LIVE_TRADING", "false"),
    })


# ── 포지션 ───────────────────────────────────────────────────────────────
@app.get("/api/positions")
def get_positions():
    rows = get_db().query(Position).all()
    return jsonify([
        {"symbol": r.symbol, "qty": r.qty, "avg_price": r.avg_price,
         "market": r.market, "broker": r.broker}
        for r in rows
    ])


# ── 주문 ─────────────────────────────────────────────────────────────────
@app.get("/api/orders")
def get_orders():
    limit = int(request.args.get("limit", 50))
    rows = get_db().query(Order).order_by(Order.created_at.desc()).limit(limit).all()
    return jsonify([
        {"id": r.id, "symbol": r.symbol, "side": r.side,
         "qty": r.qty, "price": r.price, "status": r.status,
         "created_at": r.created_at.isoformat()}
        for r in rows
    ])


# ── 전략 ─────────────────────────────────────────────────────────────────
@app.get("/api/strategies")
def list_strategies():
    rows = get_db().query(StrategyRun).order_by(StrategyRun.started_at.desc()).limit(50).all()
    return jsonify([
        {"id": r.id, "name": r.name, "type": r.strategy_type,
         "is_active": r.is_active, "started_at": r.started_at.isoformat()}
        for r in rows
    ])


_strategy_start_calls: list[float] = []


@app.post("/api/strategies/start")
def start_strategy():
    import time
    now = time.monotonic()
    _strategy_start_calls[:] = [t for t in _strategy_start_calls if now - t < 60]
    if len(_strategy_start_calls) >= 5:
        return jsonify({"error": "전략 시작 요청 과다 (분당 5회 제한)"}), 429
    _strategy_start_calls.append(now)

    body = request.json or {}
    required = {"name", "strategy_type", "config"}
    missing = required - body.keys()
    if missing:
        return jsonify({"error": f"필수 필드 누락: {missing}"}), 400

    db = get_db()
    run = StrategyRun(
        name=body["name"],
        strategy_type=body["strategy_type"],
        config=json.dumps(body.get("config", {})),
        broker=body.get("broker", "kis"),
        is_active=True,
    )
    db.add(run)
    db.flush()  # get run.id without committing yet

    # Write command to DB before Redis publish — survives Redis outage
    payload = json.dumps({
        "run_id": run.id,
        "name": run.name,
        "strategy_type": run.strategy_type,
        "config": body.get("config", {}),
        "broker": run.broker,
    })
    db.add(Command(channel="strategy:start", payload=payload))
    db.commit()

    try:
        _redis.publish("strategy:start", payload)
    except Exception as e:
        logger.warning("Redis 시작 신호 실패 (Worker가 DB 폴링으로 처리): %s", e)

    logger.info("전략 시작 요청: id=%d name=%s", run.id, run.name)
    return jsonify({"run_id": run.id, "status": "starting"}), 201


@app.post("/api/strategies/<int:run_id>/stop")
def stop_strategy(run_id: int):
    db = get_db()
    run = db.get(StrategyRun, run_id)
    if run is None:
        return jsonify({"error": "전략 없음"}), 404
    run.is_active = False

    payload = json.dumps({"run_id": run_id})
    db.add(Command(channel="strategy:stop", payload=payload))
    db.commit()

    try:
        _redis.publish("strategy:stop", payload)
    except Exception as e:
        logger.warning("Redis 중단 신호 실패 (Worker가 DB 폴링으로 처리): %s", e)

    logger.info("전략 중단 요청: id=%d", run_id)
    return jsonify({"run_id": run_id, "status": "stopping"})


# ── 잔고 ─────────────────────────────────────────────────────────────────
@app.get("/api/balance")
def get_balance():
    """KIS 실시간 잔고 조회."""
    try:
        from backend.brokers.kis import get_kis_broker
        bal = get_kis_broker().get_balance()
        return jsonify({
            "cash_krw": bal.cash_krw,
            "cash_usd": bal.cash_usd,
            "total_eval_krw": bal.total_eval_krw,
        })
    except Exception as e:
        logger.warning("잔고 조회 실패: %s", e)
        return jsonify({"error": str(e)}), 503


# ── 백테스트 ──────────────────────────────────────────────────────────────
@app.post("/api/backtest")
def run_backtest():
    body = request.json or {}
    required = {"symbol", "start", "end"}
    missing = required - body.keys()
    if missing:
        return jsonify({"error": f"필수 필드 누락: {missing}"}), 400

    try:
        from backend.strategy.indicator.backtest import run_backtest as _run
        result = _run(
            symbol=body["symbol"],
            start=body["start"],
            end=body["end"],
            buy_conditions=body.get("buy_conditions", {"sma200": True, "rsi_lt": 70}),
            sell_conditions=body.get("sell_conditions", {"rsi_gt": 80, "sma200_cross_below": True}),
            stop_loss_pct=float(body.get("stop_loss_pct", 0.07)),
            initial_cash=float(body.get("initial_cash", 2_000_000)),
        )
        return jsonify(result)
    except Exception as e:
        logger.error("백테스트 오류: %s", e)
        return jsonify({"error": str(e)}), 500


# ── 관리 (운영자 전용) ────────────────────────────────────────────────────

@app.post("/api/admin/reconcile")
def trigger_reconcile():
    """포지션·주문 수동 조정 트리거. X-API-Key 필수."""
    try:
        from backend.execution.reconciler import PositionReconciler
        from backend.brokers.kis import get_kis_broker
        import redis as _redis_mod
        r = _redis_mod.from_url(os.environ.get("REDIS_URL", "redis://redis:6379"))
        reconciler = PositionReconciler(
            broker=get_kis_broker(),
            db_factory=_get_factory(),
            redis_client=r,
        )
        result = reconciler.reconcile("manual")
        return jsonify(result.to_dict())
    except Exception as e:
        logger.error("수동 조정 실패: %s", e)
        return jsonify({"error": str(e)}), 500


@app.post("/api/admin/flatten")
def trigger_flatten():
    """비상 청산 트리거 (전체 포지션 시장가 매도). X-API-Key + confirm=true 필수."""
    body = request.json or {}
    if not body.get("confirm"):
        return jsonify({"error": "confirm=true 필요"}), 400
    try:
        from backend.worker.emergency import EmergencyFlattenManager
        from backend.brokers.kis import get_kis_broker
        dry_run = os.environ.get("ENABLE_LIVE_TRADING", "false").lower() != "true"
        mgr = EmergencyFlattenManager(
            broker=get_kis_broker(),
            db_factory=_get_factory(),
            dry_run=dry_run,
        )
        result = mgr.flatten_all(reason=body.get("reason", "수동 비상청산"))
        return jsonify(result)
    except Exception as e:
        logger.error("비상청산 실패: %s", e)
        return jsonify({"error": str(e)}), 500


@app.get("/api/admin/heartbeat")
def worker_heartbeat_status():
    """Worker 하트비트 상태 조회."""
    try:
        from backend.worker.heartbeat import HeartbeatMonitor
        alive = HeartbeatMonitor.is_alive(_redis)
        last = HeartbeatMonitor.last_beat(_redis)
        ttl = HeartbeatMonitor.ttl_seconds(_redis)
        return jsonify({"alive": alive, "last_beat": last, "ttl_seconds": ttl})
    except Exception as e:
        return jsonify({"error": str(e)}), 503


def _start_watchdog():
    """Start WorkerWatchdog in this process (kis-api), which is separate from kis-worker.
    This is the correct process boundary: crash detection only works cross-process.
    Call this from gunicorn's post_fork hook or main() below."""
    try:
        import redis as _redis_mod
        r = _redis_mod.from_url(os.environ.get("REDIS_URL", "redis://redis:6379"))
        from backend.worker.heartbeat import WorkerWatchdog
        WorkerWatchdog(r).start()
        logger.info("WorkerWatchdog 시작 (kis-api 프로세스)")
    except Exception as e:
        logger.warning("WorkerWatchdog 시작 실패: %s", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _start_watchdog()
    port = int(os.environ.get("API_PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
