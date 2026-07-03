"""
비상 청산(Emergency Flatten).

EmergencyFlattenManager : 모든 포지션 즉시 시장가 청산

NOTE (R-11): the former ``StaleDataWatchdog`` lived here but was dead code
(zero call sites). Stale-data detection is now handled by the unified
``backend/data/freshness_gate.FreshnessGate`` wired into the execution path.
"""
import logging
import threading
from contextlib import contextmanager
from typing import Callable, Optional

from backend.brokers.base import BrokerAdapter

logger = logging.getLogger(__name__)

# In-process guard against duplicate concurrent flatten runs (e.g. two
# /api/admin/flatten calls inside the rate-limit window, or auto+manual).
# Cross-process duplication is a documented remaining risk (see
# docs/EMERGENCY_FLATTEN_VALIDATION.md).
_FLATTEN_LOCK = threading.Lock()


@contextmanager
def _session(factory):
    sess = factory()
    try:
        yield sess
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()


class EmergencyFlattenManager:
    """
    모든 브로커 포지션을 즉시 매도.

    트리거:
      - MDD 한도 초과 (LossTracker → _fire_kill_switch_alert)
      - 수동 API 호출 (/api/admin/flatten)
      - 운영자 텔레그램 명령

    주의: dry_run=True 이면 실제 주문을 내지 않고 로그만 남긴다.
    """

    def __init__(self, broker: BrokerAdapter, db_factory: Optional[Callable] = None,
                 dry_run: bool = True):
        self._broker = broker
        self._factory = db_factory
        self._dry_run = dry_run
        self._lock = threading.Lock()
        self._flattening = False

        # Plain counters tracking the last flatten_all() run, incremented
        # in lockstep with the `results` dict below but NEVER derived from
        # (or read out of) the tainted "failed" string list — each is its
        # own int/bool/str literal, so API callers (backend/api/server.py)
        # can report a summary without any exception text flowing into an
        # HTTP response (CodeQL py/stack-trace-exposure).
        self.last_attempted = 0
        self.last_success = 0
        self.last_submitted = 0
        self.last_dry_run = dry_run
        self.last_failed_count = 0
        self.last_status: Optional[str] = None

    def flatten_all(self, reason: str = "비상청산") -> dict:
        """
        모든 포지션 시장가 매도.

        Returns a dict with:
          - ``attempted``  : positions seen
          - ``success``    : positions *processed* (orders submitted, OR dry-run
                             logged) — NOT a confirmation that the position is
                             closed/filled (see EMERGENCY_FLATTEN_VALIDATION.md).
          - ``submitted``  : real broker orders actually sent (0 in dry-run)
          - ``dry_run``    : whether this was a dry run
          - ``failed``     : list of "symbol: error" strings
          - ``status``     : "already_in_progress" if a flatten is already running
        """
        # Duplicate-flatten guard: never run two flattens concurrently in this
        # process — that would double-submit sells and risk an oversell/short.
        if not _FLATTEN_LOCK.acquire(blocking=False):
            logger.error("비상청산 이미 진행 중 — 중복 요청 거부: %s", reason)
            self._audit("emergency_flatten_rejected", detail={"reason": reason,
                        "cause": "already_in_progress"})
            self.last_attempted = 0
            self.last_success = 0
            self.last_submitted = 0
            self.last_dry_run = self._dry_run
            self.last_failed_count = 0
            self.last_status = "already_in_progress"
            return {"attempted": 0, "success": 0, "submitted": 0,
                    "dry_run": self._dry_run, "failed": [], "status": "already_in_progress"}
        try:
            return self._flatten_all_locked(reason)
        finally:
            _FLATTEN_LOCK.release()

    def _flatten_all_locked(self, reason: str) -> dict:
        logger.critical("비상청산 시작: %s", reason)
        self._audit("emergency_flatten_start", detail={"reason": reason, "dry_run": self._dry_run})

        try:
            positions = self._broker.get_positions()
        except Exception as e:
            logger.error("비상청산 포지션 조회 실패: %s", e)
            self._audit("emergency_flatten_positions_error", detail={"reason": reason, "error": str(e)})
            self.last_attempted = 0
            self.last_success = 0
            self.last_submitted = 0
            self.last_dry_run = self._dry_run
            self.last_failed_count = 1
            self.last_status = None
            return {"attempted": 0, "success": 0, "submitted": 0,
                    "dry_run": self._dry_run, "failed": [str(e)]}

        # qty <= 0 은 이미 청산된 포지션 — 주문 없이 스킵
        positions = [p for p in positions if p.qty > 0]

        if not positions:
            logger.info("비상청산: 보유 포지션 없음")
            self._audit("emergency_flatten_complete",
                        detail={"reason": reason, "attempted": 0, "success": 0,
                                "submitted": 0, "dry_run": self._dry_run, "failed": []})
            self.last_attempted = 0
            self.last_success = 0
            self.last_submitted = 0
            self.last_dry_run = self._dry_run
            self.last_failed_count = 0
            self.last_status = None
            return {"attempted": 0, "success": 0, "submitted": 0,
                    "dry_run": self._dry_run, "failed": []}

        results = {"attempted": len(positions), "success": 0, "submitted": 0,
                   "dry_run": self._dry_run, "failed": []}
        self.last_attempted = len(positions)
        self.last_success = 0
        self.last_submitted = 0
        self.last_dry_run = self._dry_run
        self.last_failed_count = 0
        self.last_status = None

        for pos in positions:
            try:
                price = self._broker.get_price(pos.symbol)
            except RuntimeError as e:
                if "circuit breaker" in str(e).lower():
                    logger.warning("[flatten] 회로차단 — %s 평균단가 사용 (%.4f)", pos.symbol, pos.avg_price)
                else:
                    logger.warning("[flatten] get_price 실패 %s — 평균단가 사용: %s", pos.symbol, e)
                price = pos.avg_price
            except Exception as e:
                logger.warning("[flatten] get_price 오류 %s — 평균단가 사용: %s", pos.symbol, e)
                price = pos.avg_price

            if self._dry_run:
                logger.critical("[DRY RUN] 비상청산: %s qty=%d @%.2f", pos.symbol, pos.qty, price)
                results["success"] += 1
                self.last_success += 1
                continue

            try:
                order = self._broker.place_order(pos.symbol, "sell", pos.qty, price)
                logger.critical("비상청산 주문: %s qty=%d @%.2f id=%s",
                                pos.symbol, pos.qty, price, order.id)
                results["success"] += 1
                results["submitted"] += 1
                self.last_success += 1
                self.last_submitted += 1
                self._audit("emergency_flatten_order", symbol=pos.symbol,
                            detail={"qty": pos.qty, "price": price, "order_id": order.id})
            except Exception as e:
                logger.error("비상청산 실패 %s: %s", pos.symbol, e)
                results["failed"].append(f"{pos.symbol}: {e}")
                self.last_failed_count += 1
                self._audit("emergency_flatten_failed", symbol=pos.symbol,
                            detail={"qty": pos.qty, "price": price, "error": str(e)})

        self._alert(reason, results)
        self._audit("emergency_flatten_complete",
                    detail={"reason": reason, "attempted": results["attempted"],
                            "success": results["success"], "submitted": results["submitted"],
                            "dry_run": self._dry_run, "failed": results["failed"]})
        logger.critical("비상청산 완료: %s", results)
        return results

    def _alert(self, reason: str, results: dict) -> None:
        msg = (f"비상청산 완료: {reason}\n"
               f"처리 {results['success']}/{results['attempted']} 성공"
               + (f"\n실패: {results['failed']}" if results["failed"] else ""))
        try:
            from bot.notifier import alert_emergency
            alert_emergency(msg)
        except Exception:
            pass
        try:
            from backend.websocket.server import publish_alert
            publish_alert(msg, level="critical")
        except Exception:
            pass

    def _audit(self, event_type: str, symbol: str = None, detail: dict = None) -> None:
        if self._factory is None:
            return
        try:
            from backend.database.models import AuditLog
            import json
            with _session(self._factory) as db:
                db.add(AuditLog(
                    event_type=event_type,
                    symbol=symbol,
                    actor="emergency",
                    detail=json.dumps(detail, ensure_ascii=False) if detail else None,
                ))
                db.commit()
        except Exception as e:
            logger.warning("감사 로그 저장 실패: %s", e)
