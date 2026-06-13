"""
Corporate action processor — splits, reverse splits, cash dividends, and
ticker changes.

Detects likely splits/reverse-splits from raw price jumps (always PENDING —
price alone cannot confirm a corporate action), computes price/position
adjustment factors that keep raw and adjusted data distinct (new
dicts/dataclasses, never mutated in place), persists detect/register/apply/
block events to AuditLog, and gates trading via assert_tradeable() while an
action is unconfirmed or confirmed-but-unapplied.

Usage:
    from backend.data.corporate_actions import CorporateActionService
    svc = CorporateActionService()
    action = svc.detect_from_bars("AAPL", prev_bar, curr_bar)
    if action is not None:
        result = svc.apply(action, bars=[curr_bar], position=snapshot)
    svc.assert_tradeable("AAPL")  # raises CorporateActionPendingError if blocked
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import Enum
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# 1. Corporate Action Model
# ─────────────────────────────────────────────────────────────────

class ActionType(str, Enum):
    SPLIT = "split"
    REVERSE_SPLIT = "reverse_split"
    CASH_DIVIDEND = "cash_dividend"
    TICKER_CHANGE = "ticker_change"


class ActionStatus(str, Enum):
    CONFIRMED = "confirmed"
    PENDING = "pending"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CorporateAction:
    action_type: ActionType
    symbol: str
    effective_date: date
    status: ActionStatus = ActionStatus.PENDING
    ratio: Optional[float] = None        # new-shares-per-old-share (SPLIT/REVERSE_SPLIT)
    cash_amount: Optional[float] = None  # per-share dividend (CASH_DIVIDEND)
    new_symbol: Optional[str] = None     # TICKER_CHANGE
    source: str = "manual"
    detail: str = ""

    def is_valid(self) -> bool:
        if self.action_type in (ActionType.SPLIT, ActionType.REVERSE_SPLIT):
            return self.ratio is not None and self.ratio > 0 and self.ratio != 1.0
        if self.action_type == ActionType.CASH_DIVIDEND:
            return self.cash_amount is not None and self.cash_amount > 0
        if self.action_type == ActionType.TICKER_CHANGE:
            return bool(self.new_symbol)
        return False

    def classified(self) -> "CorporateAction":
        """Conservative fail-closed classification: downgrades to UNKNOWN if
        required fields for this action_type are missing/invalid."""
        if self.is_valid():
            return self
        return replace(self, status=ActionStatus.UNKNOWN,
                        detail=self.detail or "incomplete or invalid fields for action_type")


class CorporateActionPendingError(Exception):
    """Raised by CorporateActionGate.assert_tradeable() when a symbol has a
    CONFIRMED-but-unapplied action, or a PENDING/UNKNOWN action while
    block_on_unconfirmed=True.

    NOT a subclass of RuntimeError — intentional, mirrors StaleFeedError
    (backend/data/stale_detector.py), InvalidCandleError
    (backend/data/validator.py) and MarketClosedError (backend/data/calendar.py)
    so ConsecutiveFailureBreaker does not count corporate-action holds as
    broker failures.
    """

    def __init__(self, action: CorporateAction, detail: str = "") -> None:
        self.action = action
        super().__init__(detail or
                          f"corporate action pending for {action.symbol}: "
                          f"{action.action_type.value} ({action.status.value})")


# ─────────────────────────────────────────────────────────────────
# 2. PriceAdjuster — pure
# ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AdjustmentFactor:
    price_factor: float = 1.0         # raw_price * price_factor = adjusted_price
    qty_factor: float = 1.0           # raw_qty * qty_factor = adjusted_qty
    cash_per_share: float = 0.0       # CASH_DIVIDEND only: income per held share
    new_symbol: Optional[str] = None  # TICKER_CHANGE only


class PriceAdjuster:
    """Pure: AdjustmentFactor computation + bar scaling. Never mutates inputs —
    adjust_bar()/adjust_bars() return new dicts (raw bar stays untouched)."""

    @staticmethod
    def factor_for(action: CorporateAction) -> AdjustmentFactor:
        if action.action_type in (ActionType.SPLIT, ActionType.REVERSE_SPLIT):
            ratio = action.ratio or 1.0
            return AdjustmentFactor(price_factor=1.0 / ratio, qty_factor=ratio)
        if action.action_type == ActionType.CASH_DIVIDEND:
            return AdjustmentFactor(cash_per_share=action.cash_amount or 0.0)
        if action.action_type == ActionType.TICKER_CHANGE:
            return AdjustmentFactor(new_symbol=action.new_symbol)
        return AdjustmentFactor()

    @staticmethod
    def adjust_price(raw_price: float, factor: AdjustmentFactor) -> float:
        return raw_price * factor.price_factor

    @staticmethod
    def adjust_bar(bar: dict, factor: AdjustmentFactor) -> dict:
        adjusted = dict(bar)
        for f in ("open", "high", "low", "close"):
            if adjusted.get(f) is not None:
                adjusted[f] = adjusted[f] * factor.price_factor
        if adjusted.get("volume") is not None:
            adjusted["volume"] = adjusted["volume"] * factor.qty_factor
        if factor.new_symbol:
            adjusted["symbol"] = factor.new_symbol
        return adjusted

    @classmethod
    def adjust_bars(cls, bars: list[dict], factor: AdjustmentFactor) -> list[dict]:
        return [cls.adjust_bar(b, factor) for b in bars]

    @staticmethod
    def combine(factors: Iterable[AdjustmentFactor]) -> AdjustmentFactor:
        """Multiplies price_factor/qty_factor across a chain (for SPLIT/
        REVERSE_SPLIT chains). cash_per_share/new_symbol are NOT combined —
        use apply_chain() for mixed chains involving dividends/ticker changes."""
        price_factor = 1.0
        qty_factor = 1.0
        for f in factors:
            price_factor *= f.price_factor
            qty_factor *= f.qty_factor
        return AdjustmentFactor(price_factor=price_factor, qty_factor=qty_factor)


# ─────────────────────────────────────────────────────────────────
# 3. PositionAdjuster — pure
# ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    qty: float
    avg_price: float

    @classmethod
    def from_obj(cls, position) -> "PositionSnapshot":
        """Duck-types backend.brokers.models.Position (or any object with
        symbol/qty/avg_price attrs) without importing it."""
        return cls(symbol=getattr(position, "symbol"),
                   qty=getattr(position, "qty"),
                   avg_price=getattr(position, "avg_price"))


@dataclass(frozen=True)
class PositionAdjustmentResult:
    position: PositionSnapshot   # adjusted snapshot
    cash_delta: float             # dividend income (0 for split/reverse/ticker_change)
    value_before: float           # qty * avg_price, pre-adjustment
    value_after: float            # qty * avg_price, post-adjustment

    @property
    def value_preserved(self) -> bool:
        return abs(self.value_after - self.value_before) <= max(1e-6, abs(self.value_before) * 1e-9)


class PositionAdjuster:
    @staticmethod
    def adjust(position: PositionSnapshot, factor: AdjustmentFactor) -> PositionAdjustmentResult:
        value_before = position.qty * position.avg_price
        new_qty = position.qty * factor.qty_factor
        new_avg_price = position.avg_price * factor.price_factor
        new_symbol = factor.new_symbol or position.symbol
        value_after = new_qty * new_avg_price
        cash_delta = position.qty * factor.cash_per_share
        return PositionAdjustmentResult(
            position=PositionSnapshot(symbol=new_symbol, qty=new_qty, avg_price=new_avg_price),
            cash_delta=cash_delta, value_before=value_before, value_after=value_after,
        )


# ─────────────────────────────────────────────────────────────────
# 4. CorporateActionDetector
# ─────────────────────────────────────────────────────────────────

# Common split / reverse-split ratios used for heuristic price-jump classification.
_KNOWN_RATIOS = (1.5, 2, 2.5, 3, 4, 5, 6, 7, 8, 10, 15, 20, 25, 50, 100)


class CorporateActionDetector:
    """Heuristic price-jump detection for SPLIT/REVERSE_SPLIT. Always returns
    status=PENDING — price alone cannot *confirm* a corporate action
    (conservative); confirmation comes via register_action()/an external feed."""

    def __init__(self, tolerance: float = 0.02) -> None:
        self._tolerance = tolerance

    def detect_from_price_jump(self, symbol: str, prev_close: Optional[float],
                                curr_close: Optional[float],
                                effective_date: date) -> Optional[CorporateAction]:
        if not prev_close or not curr_close or prev_close <= 0 or curr_close <= 0:
            return None
        ratio = curr_close / prev_close
        for r in _KNOWN_RATIOS:
            target_down = 1.0 / r
            if abs(ratio - target_down) <= self._tolerance * target_down:
                return CorporateAction(action_type=ActionType.SPLIT, symbol=symbol,
                                        effective_date=effective_date, status=ActionStatus.PENDING,
                                        ratio=float(r), source="price_jump_heuristic",
                                        detail=f"price ratio {ratio:.4f} ~ 1/{r}")
            if abs(ratio - r) <= self._tolerance * r:
                return CorporateAction(action_type=ActionType.REVERSE_SPLIT, symbol=symbol,
                                        effective_date=effective_date, status=ActionStatus.PENDING,
                                        ratio=float(1.0 / r), source="price_jump_heuristic",
                                        detail=f"price ratio {ratio:.4f} ~ {r}")
        return None


# ─────────────────────────────────────────────────────────────────
# 5. AdjustmentAuditLog
# ─────────────────────────────────────────────────────────────────

class AdjustmentAuditLog:
    """Fire-and-forget AuditLog persistence, mirrors KillReasonLog._persist
    (backend/risk/kill_switch.py) / StaleDataDetectionService._persist."""

    EVENT_DETECTED = "corporate_action_detected"
    EVENT_REGISTERED = "corporate_action_registered"
    EVENT_APPLIED = "corporate_action_applied"
    EVENT_BLOCKED = "corporate_action_blocked"

    def __init__(self, db_factory=None, actor: str = "corporate_actions") -> None:
        self._db = db_factory
        self._actor = actor

    def record_detected(self, action: CorporateAction) -> None:
        self._persist(self.EVENT_DETECTED, action)

    def record_registered(self, action: CorporateAction) -> None:
        self._persist(self.EVENT_REGISTERED, action)

    def record_applied(self, action: CorporateAction,
                        position_result: Optional[PositionAdjustmentResult] = None) -> None:
        extra = {}
        if position_result is not None:
            extra = {"cash_delta": position_result.cash_delta,
                     "value_before": position_result.value_before,
                     "value_after": position_result.value_after}
        self._persist(self.EVENT_APPLIED, action, extra)

    def record_blocked(self, action: CorporateAction) -> None:
        self._persist(self.EVENT_BLOCKED, action)

    def _persist(self, event_type: str, action: CorporateAction, extra: Optional[dict] = None) -> None:
        if self._db is None:
            return
        try:
            from backend.database.models import AuditLog
            sess = self._db()
            try:
                detail = {
                    "action_type": action.action_type.value,
                    "status": action.status.value,
                    "effective_date": action.effective_date.isoformat(),
                    "ratio": action.ratio,
                    "cash_amount": action.cash_amount,
                    "new_symbol": action.new_symbol,
                    "source": action.source,
                    "detail": action.detail,
                }
                if extra:
                    detail.update(extra)
                sess.add(AuditLog(
                    event_type=event_type, symbol=action.symbol, actor=self._actor,
                    detail=json.dumps(detail, ensure_ascii=False, default=str),
                ))
                sess.commit()
            except Exception:
                try:
                    sess.rollback()
                except Exception:
                    pass
                raise
            finally:
                sess.close()
        except Exception as exc:
            logger.warning("Corporate-action audit log 실패: %s", exc)


# ─────────────────────────────────────────────────────────────────
# 6. CorporateActionGate
# ─────────────────────────────────────────────────────────────────

class CorporateActionGate:
    """block_on_unconfirmed (default True) = fail-closed: PENDING/UNKNOWN
    actions block trading until confirmed+applied. CONFIRMED-but-unapplied
    actions ALWAYS block (price/position basis is about to change)."""

    def __init__(self, block_on_unconfirmed: bool = True) -> None:
        self._block_on_unconfirmed = block_on_unconfirmed

    def is_blocking(self, action: CorporateAction) -> bool:
        if action.status == ActionStatus.CONFIRMED:
            return True
        return self._block_on_unconfirmed

    def assert_tradeable(self, actions: Iterable[CorporateAction]) -> None:
        for action in actions:
            if self.is_blocking(action):
                raise CorporateActionPendingError(action)


# ─────────────────────────────────────────────────────────────────
# 7. CorporateActionService — orchestrator
# ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CorporateActionApplyResult:
    action: CorporateAction
    factor: AdjustmentFactor
    adjusted_bars: Optional[list[dict]]
    position_result: Optional[PositionAdjustmentResult]


class CorporateActionService:
    """Single entry point: detect/register corporate actions, apply price and
    position adjustments, gate trading while an action is unconfirmed or
    confirmed-but-unapplied, and persist the lifecycle to AuditLog."""

    def __init__(self, *,
                 detector: Optional[CorporateActionDetector] = None,
                 price_adjuster: Optional[PriceAdjuster] = None,
                 position_adjuster: Optional[PositionAdjuster] = None,
                 gate: Optional[CorporateActionGate] = None,
                 audit_log: Optional[AdjustmentAuditLog] = None,
                 db_factory=None,
                 actor: str = "corporate_actions") -> None:
        self._detector = detector or CorporateActionDetector()
        self._price = price_adjuster or PriceAdjuster()
        self._position = position_adjuster or PositionAdjuster()
        self._gate = gate or CorporateActionGate()
        self._audit = audit_log or AdjustmentAuditLog(db_factory=db_factory, actor=actor)
        self._lock = threading.Lock()
        self._pending: dict[str, list[CorporateAction]] = {}

    def detect_from_bars(self, symbol: str, prev_bar: dict, curr_bar: dict) -> Optional[CorporateAction]:
        """Heuristic split/reverse-split detection from two consecutive raw
        bars. Returns a PENDING CorporateAction (and records it as pending +
        persists EVENT_DETECTED) if the close-to-close ratio matches a known
        split ratio, else None."""
        ts = curr_bar.get("ts")
        eff_date = ts.date() if isinstance(ts, datetime) else (ts or date.today())
        action = self._detector.detect_from_price_jump(
            symbol, prev_bar.get("close"), curr_bar.get("close"), eff_date)
        if action is not None:
            self._add_pending(action)
            self._audit.record_detected(action)
        return action

    def register_action(self, action: CorporateAction) -> CorporateAction:
        """Register an externally-known/announced corporate action (cash
        dividend, ticker change, or a confirmation of a detected split).
        Runs action.classified() (fail-closed) and records it as pending."""
        classified = action.classified()
        self._add_pending(classified)
        self._audit.record_registered(classified)
        return classified

    def pending_for(self, symbol: str) -> list[CorporateAction]:
        with self._lock:
            return list(self._pending.get(symbol, []))

    def apply(self, action: CorporateAction, bars: Optional[list[dict]] = None,
              position: Optional[PositionSnapshot] = None) -> CorporateActionApplyResult:
        """Compute the AdjustmentFactor for `action` and apply it to `bars`
        (new list, raw bars untouched) and/or `position` (new snapshot).
        Removes `action` from the pending registry and persists EVENT_APPLIED."""
        factor = self._price.factor_for(action)
        adjusted_bars = self._price.adjust_bars(bars, factor) if bars is not None else None
        position_result = self._position.adjust(position, factor) if position is not None else None
        self._remove_pending(action)
        self._audit.record_applied(action, position_result)
        return CorporateActionApplyResult(action=action, factor=factor,
                                           adjusted_bars=adjusted_bars, position_result=position_result)

    def apply_chain(self, actions: list[CorporateAction], bars: Optional[list[dict]] = None,
                     position: Optional[PositionSnapshot] = None) -> list[CorporateActionApplyResult]:
        """Apply a sequence of actions in order, chaining each step's adjusted
        bars/position into the next step's input (e.g. split -> dividend ->
        ticker change for the same symbol)."""
        results = []
        cur_bars, cur_position = bars, position
        for action in actions:
            r = self.apply(action, bars=cur_bars, position=cur_position)
            if r.adjusted_bars is not None:
                cur_bars = r.adjusted_bars
            if r.position_result is not None:
                cur_position = r.position_result.position
            results.append(r)
        return results

    def assert_tradeable(self, symbol: str) -> None:
        """Raises CorporateActionPendingError if `symbol` has a pending action
        the gate considers blocking (and persists EVENT_BLOCKED)."""
        pending = self.pending_for(symbol)
        try:
            self._gate.assert_tradeable(pending)
        except CorporateActionPendingError as exc:
            self._audit.record_blocked(exc.action)
            raise

    def reset(self, symbol: Optional[str] = None) -> None:
        """Clear pending actions (all symbols if `symbol` is None)."""
        with self._lock:
            if symbol is None:
                self._pending.clear()
            else:
                self._pending.pop(symbol, None)

    def _add_pending(self, action: CorporateAction) -> None:
        with self._lock:
            self._pending.setdefault(action.symbol, []).append(action)

    def _remove_pending(self, action: CorporateAction) -> None:
        with self._lock:
            lst = self._pending.get(action.symbol)
            if lst and action in lst:
                lst.remove(action)
