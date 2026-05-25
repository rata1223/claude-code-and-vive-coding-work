"""
Flask REST API — 포트 5000
실행: python -m backend.api.server
"""
import json
import logging
import os

import redis
from flask import Flask, jsonify, request

from backend.database.models import Order, Position, StrategyRun, init_db

logger = logging.getLogger(__name__)

app = Flask(__name__)
_redis = redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379"))
_db = None  # lazy init


def get_db():
    global _db
    if _db is None:
        db_url = os.environ.get("DB_URL", "postgresql://quantdinger:quantdinger@postgres:5432/quantdinger")
        _db = init_db(db_url)
    return _db


# ── 헬스 ─────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return jsonify({"status": "ok"})


# ── 포지션 ───────────────────────────────────────────────────────────────
@app.get("/api/positions")
def get_positions():
    db = get_db()
    rows = db.query(Position).all()
    return jsonify([
        {"symbol": r.symbol, "qty": r.qty, "avg_price": r.avg_price,
         "market": r.market, "broker": r.broker}
        for r in rows
    ])


# ── 주문 ─────────────────────────────────────────────────────────────────
@app.get("/api/orders")
def get_orders():
    db = get_db()
    limit = int(request.args.get("limit", 50))
    rows = db.query(Order).order_by(Order.created_at.desc()).limit(limit).all()
    return jsonify([
        {"id": r.id, "symbol": r.symbol, "side": r.side,
         "qty": r.qty, "price": r.price, "status": r.status,
         "created_at": r.created_at.isoformat()}
        for r in rows
    ])


# ── 전략 ─────────────────────────────────────────────────────────────────
@app.get("/api/strategies")
def list_strategies():
    db = get_db()
    rows = db.query(StrategyRun).order_by(StrategyRun.started_at.desc()).limit(50).all()
    return jsonify([
        {"id": r.id, "name": r.name, "type": r.strategy_type,
         "is_active": r.is_active, "started_at": r.started_at.isoformat()}
        for r in rows
    ])


@app.post("/api/strategies/start")
def start_strategy():
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
    db.commit()
    db.refresh(run)

    # Worker에 시작 신호
    _redis.publish("strategy:start", json.dumps({
        "run_id": run.id,
        "name": run.name,
        "strategy_type": run.strategy_type,
        "config": body.get("config", {}),
        "broker": run.broker,
    }))
    logger.info("전략 시작 요청: id=%d name=%s", run.id, run.name)
    return jsonify({"run_id": run.id, "status": "starting"}), 201


@app.post("/api/strategies/<int:run_id>/stop")
def stop_strategy(run_id: int):
    db = get_db()
    run = db.get(StrategyRun, run_id)
    if run is None:
        return jsonify({"error": "전략 없음"}), 404
    run.is_active = False
    db.commit()

    _redis.publish("strategy:stop", json.dumps({"run_id": run_id}))
    logger.info("전략 중단 요청: id=%d", run_id)
    return jsonify({"run_id": run_id, "status": "stopping"})


# ── 잔고 ─────────────────────────────────────────────────────────────────
@app.get("/api/balance")
def get_balance():
    """KIS 실시간 잔고 조회."""
    try:
        from backend.brokers.kis import KISBroker
        broker = KISBroker()
        bal = broker.get_balance()
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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    port = int(os.environ.get("API_PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
