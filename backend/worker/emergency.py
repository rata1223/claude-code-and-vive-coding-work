"""
비상 청산(Emergency Flatten).

EmergencyFlattenManager : 모든 포지션 즉시 시장가 청산

NOTE (R-11): the former ``StaleDataWatchdog`` lived here but was dead code
(zero call sites). Stale-data detection is now handled by the unified
``backend/data/freshness_gate.FreshnessGate`` wired into the execution path.
"""
import logging
import math
import threading
from contextlib import contextmanager
from typing import Callable, Optional, Tuple

from backend.brokers.base import BrokerAdapter

logger = logging.getLogger(__name__)

# Audit event for a position that could not be priced. Distinct from
# ``emergency_flatten_failed`` (broker rejected a *submitted* order) because
# nothing was sent to the broker at all — the forensic questions differ.
PRICE_REJECTED_EVENT = "emergency_flatten_price_rejected"

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
        self._reset_last_run(attempted=0, dry_run=dry_run)

    def _reset_last_run(self, attempted: int, dry_run: bool,
                         failed_count: int = 0, status: Optional[str] = None) -> None:
        """(Re)initialize the last-run counters at the start of each flatten_all()
        branch, so every early-return path stays in sync without repeating the
        six assignments verbatim. success/submitted always start at 0 here —
        the per-position loop increments them directly as it runs."""
        self.last_attempted = attempted
        self.last_success = 0
        self.last_submitted = 0
        self.last_dry_run = dry_run
        self.last_failed_count = failed_count
        self.last_status = status

    def _executable_price(self, symbol: str) -> Tuple[Optional[float], Optional[str]]:
        """Resolve the price this position may actually be sold at.

        Returns ``(price, None)`` for a usable live quote, else ``(None, cause)``.

        There is deliberately no fallback. The former code substituted
        ``position.avg_price`` whenever the quote was unavailable, but a cost
        basis is not a price: KIS accepts no market sell (``ORD_DVSN`` is always
        ``"00"``), so that value went out as a *limit* price. During the crash
        that triggers a flatten, the cost basis is precisely when it is furthest
        from the market, so the order rested unfilled while the position kept
        moving — a liquidation that reported success without liquidating.
        Rejecting is strictly safer than selling at an invented price.
        """
        try:
            raw = self._broker.get_price(symbol)
        except Exception as e:  # noqa: BLE001 - any quote failure is fail-closed
            return None, f"실시간 가격 조회 실패: {e}"

        # bool is an int subclass — it would pass a bare ``> 0`` check.
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None, f"실시간 가격이 숫자가 아님: {raw!r}"
        try:
            value = float(raw)
        except (OverflowError, ValueError) as e:
            # An int too large for a float still has to be rejected per position —
            # letting it raise here would abort the rest of the liquidation.
            return None, f"실시간 가격 변환 실패: {type(e).__name__}"
        if not math.isfinite(value):          # NaN / ±inf
            return None, f"실시간 가격이 유한하지 않음: {raw!r}"
        if value <= 0:
            return None, f"실시간 가격이 양수가 아님: {raw!r}"
        # Returned verbatim — never rounded or clamped to make it pass.
        return value, None

    def _sellable_for(self, pos) -> Tuple[int, Optional[str]]:
        """How much of ``pos`` to actually submit, and any shortfall to report.

        Held is not sellable — shares can be unsettled or already committed to a
        resting order, and asking for the full holding gets the whole order
        rejected (P0-07 S2). Two rules are specific to this path:

        * **Shortfall**: sell what the broker says is orderable and report the
          remainder. A partial liquidation using the broker's own number beats
          no liquidation, and no quantity is invented.
        * **Unknown**: EmergencyFlatten — and only EmergencyFlatten — falls back
          to the held quantity, so a KIS field change can never freeze the
          last-resort liquidation. Every other sell path fails closed. This
          mirrors S1, where flatten is the one halt-immune path.

        The two kinds of unknown are audited separately. A figure the broker
        never reported (``CAUSE_UNREPORTED``) and one that came back unreadable
        (``CAUSE_UNTRUSTED``) are different failures and want different
        follow-up, so they must not land in one event. Both still fall back to
        held: on this path refusing to sell is the *worse* outcome — freezing
        the last-resort liquidation over a malformed field is exactly what the
        fallback exists to prevent, and ``held`` is a real count read from the
        same broker row, not an invented one.
        """
        from backend.risk.sellable_qty import CAUSE_UNTRUSTED, sellable_from_position

        held = getattr(pos, "qty", 0) or 0
        result = sellable_from_position(pos)

        if not result.known:
            untrusted = result.cause == CAUSE_UNTRUSTED
            logger.log(
                logging.ERROR if untrusted else logging.WARNING,
                "[flatten] %s 주문가능수량 %s — 비상청산은 보유수량(%s)으로 진행: %s",
                pos.symbol, "판독 불가" if untrusted else "미보고", held, result.reason,
            )
            self._audit(
                "emergency_flatten_sellable_untrusted" if untrusted
                else "emergency_flatten_sellable_unknown",
                symbol=pos.symbol,
                detail={"held_qty": held, "cause": result.reason,
                        "cause_code": result.cause},
            )
            return held, None

        sellable = result.qty
        if sellable >= held:
            return held, None

        shortfall = held - sellable
        logger.error(
            "[flatten] %s 매도가능수량 부족 — 보유 %s 중 %s 만 제출, %s 미청산",
            pos.symbol, held, sellable, shortfall,
        )
        self._audit("emergency_flatten_partial_sellable", symbol=pos.symbol,
                    detail={"held_qty": held, "sellable_qty": sellable,
                            "shortfall": shortfall, "cause": result.reason})
        note = (f"매도가능수량 부족 — 보유 {held} 중 {sellable} 제출, "
                f"{shortfall} 미청산")
        return sellable, note

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
          - ``failed``     : list of "symbol: error" strings — includes positions
                             skipped because no valid live price was available
                             (fail-closed, nothing submitted for them)
          - ``status``     : "already_in_progress" if a flatten is already running
        """
        # Duplicate-flatten guard: never run two flattens concurrently in this
        # process — that would double-submit sells and risk an oversell/short.
        if not _FLATTEN_LOCK.acquire(blocking=False):
            logger.error("비상청산 이미 진행 중 — 중복 요청 거부: %s", reason)
            self._audit("emergency_flatten_rejected", detail={"reason": reason,
                        "cause": "already_in_progress"})
            self._reset_last_run(attempted=0, dry_run=self._dry_run, status="already_in_progress")
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
            self._reset_last_run(attempted=0, dry_run=self._dry_run, failed_count=1)
            return {"attempted": 0, "success": 0, "submitted": 0,
                    "dry_run": self._dry_run, "failed": [str(e)]}

        # qty <= 0 은 이미 청산된 포지션 — 주문 없이 스킵
        positions = [p for p in positions if p.qty > 0]

        if not positions:
            logger.info("비상청산: 보유 포지션 없음")
            self._audit("emergency_flatten_complete",
                        detail={"reason": reason, "attempted": 0, "success": 0,
                                "submitted": 0, "dry_run": self._dry_run, "failed": []})
            self._reset_last_run(attempted=0, dry_run=self._dry_run)
            return {"attempted": 0, "success": 0, "submitted": 0,
                    "dry_run": self._dry_run, "failed": []}

        results = {"attempted": len(positions), "success": 0, "submitted": 0,
                   "dry_run": self._dry_run, "failed": []}
        self._reset_last_run(attempted=len(positions), dry_run=self._dry_run)

        for pos in positions:
            # P0-07 S2: never ask for more than the broker will actually sell.
            # Resolved before the quote so a shortfall is still audited when the
            # price lookup also fails, and so a zero-sellable position skips the
            # quote request entirely.
            sell_qty, shortfall_note = self._sellable_for(pos)
            if shortfall_note:
                results["failed"].append(f"{pos.symbol}: {shortfall_note}")
                self.last_failed_count += 1
            if sell_qty <= 0:
                continue

            # P0-07 G2: the live quote is the ONLY source of an executable sell
            # price. Validation runs before the dry-run branch so a dry run
            # surfaces the same rejection instead of reporting a flatten it
            # could not actually have performed.
            price, cause = self._executable_price(pos.symbol)
            if price is None:
                logger.error(
                    "[flatten] %s 실행가 확보 실패 — 주문 미제출(fail-closed): %s",
                    pos.symbol, cause,
                )
                results["failed"].append(f"{pos.symbol}: {cause}")
                self.last_failed_count += 1
                self._audit(PRICE_REJECTED_EVENT, symbol=pos.symbol,
                            detail={"qty": sell_qty, "price": None, "cause": cause})
                continue

            if self._dry_run:
                logger.critical("[DRY RUN] 비상청산: %s qty=%d @%.2f", pos.symbol, sell_qty, price)
                results["success"] += 1
                self.last_success += 1
                continue

            try:
                order = self._broker.place_order(pos.symbol, "sell", sell_qty, price)
                logger.critical("비상청산 주문: %s qty=%d @%.2f id=%s",
                                pos.symbol, sell_qty, price, order.id)
                results["success"] += 1
                results["submitted"] += 1
                self.last_success += 1
                self.last_submitted += 1
                self._audit("emergency_flatten_order", symbol=pos.symbol,
                            detail={"qty": sell_qty, "price": price, "order_id": order.id})
            except Exception as e:  # noqa: BLE001 - continue remaining positions after broker failure
                logger.error("비상청산 실패 %s: %s", pos.symbol, e)
                results["failed"].append(f"{pos.symbol}: {e}")
                self.last_failed_count += 1
                self._audit("emergency_flatten_failed", symbol=pos.symbol,
                            detail={"qty": sell_qty, "price": price, "error": str(e)})

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
