"""
종합 리스크 엔진.

1. 트레일링 스탑 추적 (포지션별 peak price 관리)
2. 변동성 목표 포지션 스케일링
3. 상관관계 인식 노출 한도
4. 일일 손실 / MDD 킬스위치
5. 노출 상한 (심볼·전체 포트폴리오)
"""
import logging
import threading
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def _seoul_today() -> date:
    """Today's **risk day** (07:00 KST to 07:00 KST) — delegated to the one
    definition, ``models.trading_day()`` (issues #160, #166).

    The name predates the 07:00 boundary and is kept because this module reads
    it in a dozen places and tests patch it; what it returns is the
    ``DailyRiskState`` key, not the Seoul calendar date. Imported inside the
    call so patching the one helper reaches every caller, here and elsewhere.
    """
    from backend.database.models import trading_day
    return trading_day()


# ── 설정 ──────────────────────────────────────────────────────────────────────

@dataclass
class RiskConfig:
    # 포지션 사이징
    target_vol_ann: float = 0.10          # 변동성 목표 연환산 10%
    max_position_pct: float = 0.05        # 종목당 최대 5%
    max_portfolio_exposure: float = 0.15  # 총 노출 15% (1.5M 기준 225K)

    # 트레일링 스탑
    trailing_stop_pct: float = 0.07       # 고점 대비 -7% 청산
    hard_stop_pct: float = 0.10           # 진입가 대비 -10% 하드스탑

    # 손실 한도
    daily_loss_limit_pct: float = 0.03    # 일일 3% 손실 → 당일 매수 차단
    weekly_loss_limit_pct: float = 0.06   # 주간 6% 손실 → 킬스위치
    mdd_limit_pct: float = 0.15           # MDD 15% → 전량 청산

    # 상관관계
    max_corr_overlap: float = 0.80        # 0.80 이상 상관 → 2번째 포지션 차단
    corr_window: int = 63                 # 상관계수 계산 기간 (영업일)

    # 변동성 스케일링
    vol_scale_floor: float = 0.3          # 스케일 최소값 (극단 변동성 시)
    vol_scale_cap: float = 1.5            # 스케일 최대값 (레버리지 방지)


# ── 트레일링 스탑 추적 ────────────────────────────────────────────────────────

@dataclass
class PositionStop:
    symbol: str
    entry_price: float
    entry_date: str
    peak_price: float
    trailing_stop: float
    hard_stop: float
    qty: int
    trailing_stop_pct: float = 0.07  # instance-level; avoids global state corruption

    def update_peak(self, current_price: float) -> None:
        if current_price > self.peak_price:
            self.peak_price = current_price
            self.trailing_stop = current_price * (1 - self.trailing_stop_pct)

    def is_stopped(self, current_price: float) -> tuple[bool, str]:
        if current_price <= self.hard_stop:
            return True, "hard_stop"
        if current_price <= self.trailing_stop:
            return True, "trailing_stop"
        return False, ""


class TrailingStopManager:
    """
    포지션별 트레일링 스탑 관리.

    사용 예:
        mgr = TrailingStopManager(RiskConfig())
        mgr.open("069500", qty=5, entry_price=10000)
        stops = mgr.check_stops({"069500": 9200})
        # stops = [("069500", "trailing_stop")]
    """

    def __init__(self, config: RiskConfig):
        self.config = config
        self._positions: dict[str, PositionStop] = {}

    def open(self, symbol: str, qty: int, entry_price: float,
             entry_date: Optional[str] = None) -> None:
        from backend.database.models import seoul_date
        ts = entry_price * (1 - self.config.trailing_stop_pct)
        hs = entry_price * (1 - self.config.hard_stop_pct)
        self._positions[symbol] = PositionStop(
            symbol=symbol,
            entry_price=entry_price,
            # A label of when the position was opened: the calendar date, not
            # the risk day the loss limits are kept on.
            entry_date=entry_date or str(seoul_date()),
            peak_price=entry_price,
            trailing_stop=ts,
            hard_stop=hs,
            qty=qty,
            trailing_stop_pct=self.config.trailing_stop_pct,
        )
        logger.debug("TS 오픈 %s entry=%.2f ts=%.2f hs=%.2f", symbol, entry_price, ts, hs)

    def update(self, price_map: dict[str, float]) -> None:
        """현재가로 peak 및 트레일링 스탑 갱신."""
        for sym, pos in self._positions.items():
            price = price_map.get(sym)
            if price:
                pos.update_peak(price)

    def check_stops(self, price_map: dict[str, float]) -> list[tuple[str, str]]:
        """스탑 발동 종목 목록 반환. [(symbol, reason), ...]"""
        triggered = []
        for sym, pos in self._positions.items():
            price = price_map.get(sym)
            if price is None:
                continue
            stopped, reason = pos.is_stopped(price)
            if stopped:
                triggered.append((sym, reason))
                logger.warning("스탑 발동 %s @ %.2f (%s)", sym, price, reason)
        return triggered

    def close(self, symbol: str) -> Optional[PositionStop]:
        return self._positions.pop(symbol, None)

    def get_all(self) -> dict[str, PositionStop]:
        return dict(self._positions)


# ── 노출 및 상관관계 관리 ─────────────────────────────────────────────────────

class ExposureManager:
    """
    종목 간 상관관계 기반 노출 한도 관리.

    - 전체 포트폴리오 노출이 max_portfolio_exposure 초과 → 신규 매수 차단
    - 신규 진입 종목이 기존 종목과 max_corr_overlap 이상 상관 → 차단
    """

    def __init__(self, config: RiskConfig):
        self.config = config

    def can_add(
        self,
        candidate: str,
        existing_symbols: list[str],
        price_histories: dict[str, pd.DataFrame],
        current_exposure: float,
        capital: float,
    ) -> tuple[bool, str]:
        """
        신규 종목 추가 가능 여부.
        반환: (허용 여부, 이유)
        """
        # 총 노출 한도
        if capital > 0 and current_exposure / capital > self.config.max_portfolio_exposure:
            return False, f"포트폴리오 노출 한도 초과 ({current_exposure/capital:.1%})"

        # 상관관계 체크
        if candidate in price_histories and existing_symbols:
            corr = self._max_correlation(candidate, existing_symbols, price_histories)
            if corr > self.config.max_corr_overlap:
                return False, f"상관관계 과다 (r={corr:.2f} > {self.config.max_corr_overlap})"

        return True, "ok"

    def _max_correlation(
        self,
        candidate: str,
        existing: list[str],
        price_histories: dict[str, pd.DataFrame],
    ) -> float:
        """후보와 기존 포지션 간 최대 상관계수."""
        w = self.config.corr_window
        try:
            r_cand = (price_histories[candidate]["Close"]
                      .pct_change().dropna().iloc[-w:])
        except Exception:
            return 0.0

        max_corr = 0.0
        for sym in existing:
            if sym not in price_histories or sym == candidate:
                continue
            try:
                r_sym = (price_histories[sym]["Close"]
                         .pct_change().dropna().iloc[-w:])
                # 공통 인덱스 정렬
                common = r_cand.index.intersection(r_sym.index)
                if len(common) < 20:
                    continue
                corr = float(r_cand.loc[common].corr(r_sym.loc[common]))
                max_corr = max(max_corr, abs(corr))
            except Exception:
                continue
        return max_corr


# ── 킬스위치 + 손실 한도 ──────────────────────────────────────────────────────

#: How an MDD halt's reason begins. Written by ``_evaluate`` and read back by
#: ``_restore_state`` to recognise a restored MDD halt, so it is one constant.
MDD_REASON_PREFIX = "MDD 한도 초과"

@dataclass
class LossTracker:
    """일별·주별 손실 추적 + 킬스위치."""

    config: RiskConfig
    daily_pnl: float = 0.0
    weekly_pnl: float = 0.0
    peak_equity: float = 0.0
    current_equity: float = 0.0
    kill_switch: bool = False
    kill_reason: str = ""
    trade_date: date = field(default_factory=_seoul_today)
    #: ``daily_pnl`` of the risk days before ``trade_date`` still inside the
    #: weekly window. The weekly figure is a rolling 7 risk days — these plus
    #: today — so it is rebuilt from here at each rollover and, in the
    #: persistent tracker, from the stored rows at boot. It used to be a window
    #: that started when the tracker was built, so every worker restart gave the
    #: week a fresh 6% budget.
    _prior_days: dict = field(default_factory=dict, repr=False, compare=False)
    #: Called with the reason when a fresh MDD breach should liquidate the book
    #: (P0-03). The worker wires this to ``EmergencyFlattenManager``. It runs
    #: under the caller's lock, so it must hand the work off and return at once.
    on_mdd_breach: Optional[Callable[[str], None]] = field(
        default=None, repr=False, compare=False)
    #: Set once a flatten has been requested; re-armed only when the kill switch
    #: is cleared. Without it every fill during the breach re-requests one.
    _mdd_flatten_requested: bool = field(default=False, repr=False, compare=False)

    def reset_daily(self) -> None:
        self.daily_pnl = 0.0
        self.trade_date = _seoul_today()

    def reset_weekly(self) -> None:
        """Clear the weekly window, today's P&L so far included.

        In memory only — not part of the rollover, and a restart rebuilds the
        window from the stored rows' ``daily_pnl``.
        """
        self.weekly_pnl = 0.0
        self._prior_days.clear()

    #: Risk days in the weekly window, today included.
    WEEK_DAYS = 7

    def roll_over(self, today: date) -> bool:
        """Start ``today``'s risk day if the tracker is still on an earlier one.

        The day used to roll only inside ``record_pnl``, i.e. at the first fill
        of a new day. Anything that writes the tracker's numbers before that
        fill — a reset, the shutdown checkpoint — has to roll first, or it puts
        the closed day's P&L on the new day's row and a restart counts it twice
        (in the day and again in the week). Returns True if it rolled.
        """
        if today == self.trade_date:
            return False
        self._roll_week(today)
        self.daily_pnl = 0.0
        self.trade_date = today
        return True

    def _roll_week(self, today: date) -> None:
        """Move the closing day into the window and rebuild the weekly figure.

        Called at the rollover *before* the day is zeroed. The closing day's
        share is what it added to the weekly figure — normally its
        ``daily_pnl``, but a manual ``reset_daily``/``reset_weekly`` during the
        day moves one counter without the other, and the week keeps what the
        week saw. Days older than the window drop out by date, so a gap of a
        week or more empties it.
        """
        self._prior_days[self.trade_date] = (
            self.weekly_pnl - sum(self._prior_days.values()))
        self._prune_week(today)
        self.weekly_pnl = sum(self._prior_days.values())

    def _prune_week(self, today: date) -> None:
        self._prior_days = {
            d: v for d, v in self._prior_days.items()
            if 0 < (today - d).days < self.WEEK_DAYS
        }

    def record_pnl(self, pnl: float, current_equity: float) -> str:
        """Record realized P&L and re-evaluate the limits.

        Returns what **this call** did to the kill switch — ``"triggered"`` if it
        halted, otherwise ``"unchanged"``. Callers need a per-call answer:
        inspecting ``self.kill_switch`` before and after cannot distinguish this
        call's decision from a concurrent one's, and fills arrive on several
        poller threads at once. ``PersistentLossTracker`` extends the vocabulary
        with ``"adopted"``.
        """
        self.roll_over(_seoul_today())

        self.daily_pnl += pnl
        self.weekly_pnl += pnl
        self.current_equity = current_equity
        if current_equity > self.peak_equity:
            self.peak_equity = current_equity

        was_halted = self.kill_switch
        self._evaluate()
        return "triggered" if (self.kill_switch and not was_halted) else "unchanged"

    def _mark_kill_switch_changed(self) -> None:
        """Called wherever **this process** decides the kill switch's value.

        A no-op here; ``PersistentLossTracker`` overrides it to record that it
        has something to assert the next time it writes the row. Deliberately
        an explicit call at each site rather than a property setter on
        ``kill_switch``: a setter would also fire for ``_restore_state()``, which
        is reading the DB's value back, not forming an opinion — and that
        distinction is the entire fix (issue #158).
        """

    def _evaluate(self) -> None:
        """Halt on the first limit breached — **MDD checked first**.

        The order matters (P0-03). MDD is the only breach that liquidates, and it
        used to be checked last, behind early returns: on a crash day the daily
        limit (3%) and MDD (15%) break together, the daily branch returned, and
        MDD was never evaluated — no flatten on exactly the day it was for.

        Every branch re-runs on each PnL write while the breach holds; that is
        what re-halts after a reset (see ``api/routers/risk.py``). The flatten
        request is therefore guarded separately, in ``_request_mdd_flatten``.
        """
        capital = max(self.peak_equity, 1.0)

        # MDD 한도 — 유일하게 청산까지 가는 위반이라 먼저 본다
        if self.peak_equity > 0:
            mdd = (self.current_equity - self.peak_equity) / self.peak_equity
            if mdd < -self.config.mdd_limit_pct:
                self.kill_switch = True
                self._mark_kill_switch_changed()
                self.kill_reason = f"{MDD_REASON_PREFIX} ({mdd:.2%})"
                logger.error("킬스위치 [MDD] %s", self.kill_reason)
                self._fire_kill_switch_alert(self.kill_reason)
                self._request_mdd_flatten(self.kill_reason)
                return

        # 일일 손실 한도
        if self.daily_pnl / capital < -self.config.daily_loss_limit_pct:
            self.kill_switch = True
            self._mark_kill_switch_changed()
            self.kill_reason = f"일일 손실 한도 초과 ({self.daily_pnl/capital:.2%})"
            logger.error("킬스위치 [일일] %s", self.kill_reason)
            self._fire_kill_switch_alert(self.kill_reason)
            return

        # 주간 손실 한도
        if self.weekly_pnl / capital < -self.config.weekly_loss_limit_pct:
            self.kill_switch = True
            self._mark_kill_switch_changed()
            self.kill_reason = f"주간 손실 한도 초과 ({self.weekly_pnl/capital:.2%})"
            logger.error("킬스위치 [주간] %s", self.kill_reason)
            self._fire_kill_switch_alert(self.kill_reason)

    def _request_mdd_flatten(self, reason: str) -> None:
        """Ask for the book to be liquidated — once per breach (P0-03).

        Keyed on its own flag, not on ``kill_switch``: a book already halted for
        the daily limit must still be flattened when MDD breaks later.

        Re-armed by ``_rearm_mdd_flatten`` in three cases only:

        * the kill switch is cleared — ``manual_reset``, or an operator clear
          the tracker adopts. That adoption happens only once the breach no
          longer holds: while it does, ``_evaluate`` re-halts first and the
          clear is overwritten (issue #158), so no second flatten follows;
        * the worker's flatten sent nothing and failed, so the next fill
          retries it (``StrategyWorker._emergency_flatten``).

        Only a breach this process *measured* gets here. A halt adopted from
        another process never does, and an MDD halt restored at boot counts as
        already requested (``_restore_state``) — a restart must not fire a
        liquidation by itself (ROADMAP R-CRIT-07).
        """
        if self._mdd_flatten_requested or self.on_mdd_breach is None:
            return
        self._mdd_flatten_requested = True
        try:
            self.on_mdd_breach(reason)
        except Exception as e:  # noqa: BLE001 - the halt itself must stand
            logger.error("MDD 비상청산 요청 실패: %s", e)

    def _rearm_mdd_flatten(self) -> None:
        """Let the next MDD breach request a flatten again (see
        ``_request_mdd_flatten`` for when). Callers from other threads hold the
        tracker's lock."""
        self._mdd_flatten_requested = False

    def _fire_kill_switch_alert(self, reason: str) -> None:
        """Telegram + WebSocket 동시 발행 — 실패해도 킬스위치 자체는 영향 없음."""
        # Disable SAFE_MODE first so all subsequent *entries* are blocked.
        # P0-07 S1: this is a RISK_BREACH, not untrusted state — position data
        # is still reliable, the exposure is the problem. Declaring the cause is
        # what lets stop-losses and other proven risk-reducing exits keep
        # running; blocking them would freeze the book at maximum drawdown.
        try:
            from backend.risk.halt_policy import HaltCause
            from backend.worker.recovery import SAFE_MODE
            SAFE_MODE.disable(f"킬스위치: {reason}", cause=HaltCause.RISK_BREACH)
        except Exception as e:
            logger.warning("SAFE_MODE 비활성화 실패: %s", e)
        try:
            from bot.notifier import alert_emergency
            alert_emergency(f"킬스위치 발동\n{reason}")
        except Exception as e:
            logger.warning("Telegram 킬스위치 알림 실패: %s", e)
        try:
            from backend.websocket.server import publish_alert
            publish_alert(reason, level="critical")
        except Exception as e:
            logger.warning("WebSocket 킬스위치 알림 실패: %s", e)

    def can_buy(self) -> tuple[bool, str]:
        if self.kill_switch:
            return False, self.kill_reason
        capital = max(self.peak_equity, 1.0)
        if self.daily_pnl / capital < -self.config.daily_loss_limit_pct * 0.8:
            return False, "일일 손실 80% — 예방적 매수 차단"
        return True, "ok"

    def manual_reset(self) -> None:
        self.kill_switch = False
        self._mark_kill_switch_changed()
        self.kill_reason = ""
        self._rearm_mdd_flatten()
        logger.info("킬스위치 수동 해제")


# ── 변동성 스케일링 ───────────────────────────────────────────────────────────

def vol_position_scale(
    df: pd.DataFrame,
    target_vol: float = 0.10,
    vol_window: int = 21,
    floor: float = 0.3,
    cap: float = 1.5,
) -> float:
    """
    실현변동성 대비 목표변동성 스케일 팩터 반환 (0.3 ~ 1.5).
    포지션 수량에 곱해 사용.
    """
    try:
        close = df["Close"]
        log_ret = np.log(close / close.shift(1)).dropna()
        realized = log_ret.iloc[-vol_window:].std() * np.sqrt(252)
        if realized <= 0:
            return 1.0
        return float(np.clip(target_vol / realized, floor, cap))
    except Exception:
        return 1.0


# ── 포트폴리오 상관관계 분석 ──────────────────────────────────────────────────

def correlation_matrix(price_histories: dict[str, pd.DataFrame],
                       window: int = 63) -> pd.DataFrame:
    """종목 간 수익률 상관행렬 반환."""
    rets = {}
    for sym, df in price_histories.items():
        try:
            r = df["Close"].pct_change().dropna().iloc[-window:]
            rets[sym] = r
        except Exception:
            pass
    if not rets:
        return pd.DataFrame()
    df_rets = pd.DataFrame(rets).dropna()
    return df_rets.corr()


class PersistentLossTracker(LossTracker):
    """
    LossTracker with Redis TTL + DB dual-write so daily PnL survives restarts.

    Restore priority: min(redis_val, db_val) — take the more pessimistic number
    to avoid loss under-reporting across crashes.
    """

    _REDIS_KEY_TEMPLATE = "risk:daily_pnl:{date}"
    _REDIS_TTL_SEC = 25 * 3600  # 25 hours covers overnight restarts

    def __init__(self, config: RiskConfig, redis_client=None,
                 db_session=None, db_factory=None):
        super().__init__(config=config)
        # RLock, not Lock: methods that take it call others that take it again
        # (record_pnl → _persist → _write_db). Rollover used to go through the
        # overridden reset_daily(), and with a plain Lock that was a permanent
        # hang on the first fill of a new day (found while reviewing #158).
        self._lock = threading.RLock()  # serialises concurrent record_pnl() calls
        # ── kill-switch ownership (issue #158) ───────────────────────────────
        # State alone cannot distinguish "a fresh breach" from "a stale True
        # sitting on top of somebody else's clear", so intent is recorded
        # instead. Bumped by _mark_kill_switch_changed() whenever *this process*
        # decides the value; _write_db() asserts the flag only while these two
        # differ, and otherwise lets the row on disk win.
        #
        # A counter rather than a bool: a breach can land between a write's
        # snapshot and its commit, and clearing a bool afterwards would discard
        # it. Comparing epochs means only an *unchanged* epoch is marked caught
        # up. Set before _restore_state() below, which must not count as intent.
        self._ks_epoch = 0
        self._ks_written = 0
        self._redis = redis_client
        # Prefer db_factory (creates per-op sessions) over a long-lived db_session.
        # Long-lived sessions cause stale connections and pool exhaustion on 24h+ processes.
        if db_factory is not None:
            self._db_factory = db_factory
            self._db = None  # unused when factory is available
        else:
            self._db_factory = None
            self._db = db_session  # legacy: long-lived session, kept for compat
        self._restore_state()

    def _mark_kill_switch_changed(self) -> None:
        """This process has formed an opinion; the next write asserts it."""
        self._ks_epoch += 1

    # Deliberately no public accessor for ``_ks_epoch``. An earlier revision
    # exposed one so the fill pipeline could tell "we just halted" from "we
    # adopted somebody else's halt" by diffing it across a call — but a
    # process-global counter read outside the lock cannot attribute a decision
    # to one call when fills arrive concurrently. ``record_pnl()`` returns that
    # answer per call instead; re-adding the accessor would invite the bug back.

    def _redis_key(self) -> str:
        return self._REDIS_KEY_TEMPLATE.format(date=_seoul_today().isoformat())

    def _restore_state(self) -> None:
        today = _seoul_today()
        # The dataclass default read the clock earlier; if 07:00 passed in
        # between, the first rollover would push a day that is already among
        # the prior days and zero its loss in the week.
        self.trade_date = today
        redis_val = self._load_redis(today)
        db_val = self._load_db(today)

        if redis_val is not None or db_val is not None:
            candidates = [v for v in [redis_val, db_val] if v is not None]
            self.daily_pnl = min(candidates)  # most pessimistic
            logger.info(
                "PersistentLossTracker 복원: daily_pnl=%.4f (redis=%s db=%s)",
                self.daily_pnl, redis_val, db_val,
            )

        db_state = self._load_db_full(today)

        # The weekly figure is the last 7 risk days, so it is rebuilt from the
        # earlier rows rather than read from today's `weekly_pnl` column — that
        # column only exists once today has a row, and a restart before the
        # day's first write used to hand the week a fresh budget.
        self._prior_days = self._load_prior_days(today)
        self.weekly_pnl = sum(self._prior_days.values()) + self.daily_pnl

        # Peak equity is the MDD baseline and belongs to no single day: the
        # latest recorded peak within the weekly window. Taken from today's row
        # alone, a restart before the day's first write came up with 0 and the
        # worker re-seeded it from the current balance — resetting the drawdown
        # it exists to measure. Bounded so a worker idle for weeks, or a peak
        # from before a capital change, is re-seeded as before rather than
        # trusted.
        if db_state is not None and db_state.peak_equity:
            self.peak_equity = db_state.peak_equity
        else:
            self.peak_equity = self._load_latest_peak(today) or 0.0

        # The halt is read from the previous risk day too: it is never cleared
        # by the date changing, and a restart before the first write of a new
        # day finds no row for it yet. Looking at one row let that worker come
        # up unhalted, and `StartupRecovery._step_risk` branches on this flag —
        # fail-open.
        #
        # A live process does not hit this: `_write_db`'s `is_new` path carries
        # the halt onto the new day's row at its first write. Only a restart in
        # the gap before that write does.
        #
        # Every still-halted day counts, not just yesterday: a worker down for
        # two whole risk days carried nothing forward, and the halt aged out of
        # a two-day window. If that lookup fails, the two days are still read.
        from backend.database.models import risk_days_in_play, trading_days_in_play
        days = (self._query(lambda s: risk_days_in_play(s, today), "정지 날짜")
                or list(trading_days_in_play()))
        for key in days:
            row = db_state if key == today else self._load_db_full(key)
            if row is not None and row.kill_switch:
                self.kill_switch = True
                self.kill_reason = row.kill_reason or ""
                logger.warning("킬스위치 복원 (%s): %s", key, self.kill_reason)
                if self.kill_reason.startswith(MDD_REASON_PREFIX):
                    # The process that measured this breach already requested
                    # the flatten; its sells may still be resting. Requesting
                    # again on the first fill after boot is the startup
                    # liquidation R-CRIT-07 forbids. A daily/weekly halt stays
                    # armed: MDD breaking later still flattens (P0-03).
                    self._mdd_flatten_requested = True
                if key != today:
                    # Restoring from *today's* row is not this process's opinion
                    # — the row already says it, and counting it would re-break
                    # issue #158. A halt found on an **earlier** day is different:
                    # today's row does not carry it yet, so carrying it forward is
                    # this process's job and has to be recorded as intent.
                    #
                    # Without this, `_write_db` sees nothing to assert, reads
                    # today's row, adopts its `False` as an external clear and
                    # **wipes the live halt on the very first write**, and the
                    # halt is then carried by nothing newer than its old row.
                    self._mark_kill_switch_changed()
                break

    def record_pnl(self, pnl: float, current_equity: float) -> str:
        """As the base, plus ``"adopted"`` when the write picked up a halt that
        was set outside this process.

        The base call runs **inside ``self._lock``**, so its ``"triggered"``
        answer describes this call and no other — which is the whole point.
        Comparing ``kill_switch`` or the decision counter across the call from
        outside cannot do that: fills arrive concurrently on poller threads, and
        another fill's breach lands between the two reads.
        """
        with self._lock:
            outcome = super().record_pnl(pnl, current_equity)
        adopted = self._persist()
        if outcome == "unchanged" and adopted:
            return "adopted"
        return outcome

    def reset_daily(self) -> None:
        with self._lock:
            super().reset_daily()
        self._persist()

    def manual_reset(self) -> None:
        with self._lock:
            super().manual_reset()
        self._persist()

    def _fire_kill_switch_alert(self, reason: str) -> None:
        # Called under self._lock — dispatch I/O to a daemon thread to avoid blocking
        # record_pnl() for concurrent fills arriving at the same moment.
        t = threading.Thread(
            target=self._do_kill_switch_io,
            args=(reason,),
            daemon=True,
            name="kill-switch-alert",
        )
        t.start()

    def _do_kill_switch_io(self, reason: str) -> None:
        """Telegram + WebSocket + DB audit — runs outside the lock."""
        super()._fire_kill_switch_alert(reason)
        self._write_kill_switch_audit(reason)

    def _write_kill_switch_audit(self, reason: str) -> None:
        if self._db_factory is None:
            return
        try:
            import json
            from backend.database.models import AuditLog
            sess = self._db_factory()
            try:
                sess.add(AuditLog(
                    event_type="kill_switch",
                    actor="risk_engine",
                    detail=json.dumps({"reason": reason}, ensure_ascii=False),
                ))
                sess.commit()
            except Exception as e:
                logger.warning("킬스위치 감사 로그 저장 실패: %s", e)
                sess.rollback()
            finally:
                sess.close()
        except Exception as e:
            logger.warning("킬스위치 감사 로그 예외: %s", e)

    def _persist(self) -> bool:
        """Returns True when the write adopted a halt set outside this process."""
        self._write_redis()
        return self._write_db()

    def _write_redis(self) -> None:
        if self._redis is None:
            return
        try:
            # Keyed by the day the number belongs to, read with it — not the
            # wall clock: until `_write_db` rolls the tracker below, the number
            # is still the closed day's.
            with self._lock:
                key = self._REDIS_KEY_TEMPLATE.format(
                    date=self.trade_date.isoformat())
                value = self.daily_pnl
            self._redis.setex(key, self._REDIS_TTL_SEC, str(value))
        except Exception as e:
            logger.warning("Redis PnL 기록 실패: %s", e)

    def _write_db(self) -> bool:
        """Persist the day's numbers, and settle who owns the kill switch.

        The equity columns are always this process's to write. The halt flag is
        not: ``api/routers/risk.py`` clears it and ``WorkerWatchdog`` sets it,
        both from outside this process. This used to overwrite whatever they had
        written with the in-memory value, which broke in both directions —
        an operator's clear was undone, and a watchdog halt was *erased* (issue
        #158). So the row is re-read, and the flag is asserted only while this
        process has an opinion it has not yet written; otherwise the row wins and
        memory converges to it.

        Returns True when this write adopted a halt from the row.
        """
        from backend.database.models import lock_risk_row, trading_day
        today = trading_day()
        with self._lock:
            # The numbers must be today's before they go on today's row.
            self.roll_over(today)
            daily_pnl = self.daily_pnl
            weekly_pnl = self.weekly_pnl
            peak_eq = self.peak_equity
            ks = self.kill_switch
            kr = self.kill_reason or None
            epoch = self._ks_epoch
            asserting = epoch != self._ks_written

        def _apply(row, is_new: bool) -> tuple[bool, str | None] | None:
            """Write into ``row``; return the flag to adopt, or None if asserted.

            ``is_new`` matters: a row this call just created holds nobody's
            decision — its ``kill_switch`` is only the column default, so
            adopting it would **clear a live halt** every day at the first
            write against a new date key. There is nothing external to defer to,
            so this process's value is simply carried forward.
            """
            row.daily_pnl = daily_pnl
            row.weekly_pnl = weekly_pnl
            row.peak_equity = peak_eq
            if asserting or is_new:
                row.kill_switch = ks
                row.kill_reason = kr
                return None
            return bool(row.kill_switch), row.kill_reason

        def _settle(adopted) -> bool:
            """Record the outcome against the epoch the write was based on.

            Returns True when this write adopted a halt set outside this process,
            so ``record_pnl()`` can report ``"adopted"`` for **this call** rather
            than leaving the caller to infer it from shared state.

            If the epoch moved while the write was in flight, a breach landed
            after the snapshot: leave the marker alone so the next write asserts
            it, and do not adopt a row that is now out of date.
            """
            adopt_halt = None
            with self._lock:
                if self._ks_epoch != epoch:
                    return False
                if asserting or is_new_row[0]:
                    self._ks_written = epoch
                elif adopted is not None:
                    was = self.kill_switch
                    self.kill_switch, self.kill_reason = adopted[0], adopted[1] or ""
                    if adopted[0] and not was:
                        adopt_halt = adopted[1] or "외부 킬스위치"
                    elif was and not adopted[0]:
                        # Somebody cleared it (the operator reset): a breach
                        # after this is a new one, and flattens again (P0-03).
                        self._rearm_mdd_flatten()

            if adopt_halt is not None:
                # Converging the attribute is not enough to stop anything:
                # can_buy() has no production callers and the real order gate is
                # SAFE_MODE (backend/strategy/base.py). A halt somebody else set
                # — the API-side WorkerWatchdog, typically — has to close that
                # gate here or this process keeps trading through it.
                #
                # No Telegram/WebSocket alert: whoever set the flag already
                # raised one, and this runs on every subsequent write.
                logger.warning("외부 킬스위치 감지 — 매매 차단: %s", adopt_halt)
                try:
                    from backend.risk.halt_policy import HaltCause
                    from backend.worker.recovery import SAFE_MODE
                    SAFE_MODE.disable(f"킬스위치(외부): {adopt_halt}",
                                      cause=HaltCause.RISK_BREACH)
                except Exception as e:
                    logger.warning("SAFE_MODE 비활성화 실패: %s", e)
            # Adopting a *clear* deliberately does NOT re-enable SAFE_MODE:
            # resuming has to go through StartupRecovery's checks, which is the
            # half of P0-12 that is still open.
            return adopt_halt is not None

        # One helper, applied on both paths — these two branches were copy-pasted
        # and are exactly where a fix lands on one side only.
        def _abandon_failed_clear() -> None:
            """A failed write leaves this process's intent unwritten, so the next
            write re-asserts it. That is right for a halt — fail-closed — and
            wrong for a clear: by then somebody else may have halted the
            deployment, and replaying our stale ``False`` would erase it, which
            is the #158 overwrite wearing a different hat.

            So a clear that never reached the DB is dropped. The operator retries
            if they still want it, and memory converges to whatever the row
            actually says on the next write.
            """
            if ks:
                return
            with self._lock:
                if self._ks_epoch == epoch:
                    self._ks_written = epoch
                    logger.warning(
                        "킬스위치 해제 기록 실패 — 해제 의사를 폐기한다. "
                        "DB 값이 우선이며, 필요하면 다시 해제할 것")

        is_new_row = [False]

        if self._db_factory is not None:
            sess = self._db_factory()
            try:
                # Locked, so the halt flag read here is the committed one and no
                # other writer can commit between this read and our commit (#164).
                row, is_new_row[0] = lock_risk_row(sess, today)
                adopted = _apply(row, is_new_row[0])
                sess.commit()
                return _settle(adopted)
            except Exception as e:
                logger.warning("DB PnL 기록 실패: %s", e)
                sess.rollback()
                _abandon_failed_clear()
            finally:
                sess.close()
        elif self._db is not None:
            # Legacy: long-lived session path
            try:
                row, is_new_row[0] = lock_risk_row(self._db, today)
                adopted = _apply(row, is_new_row[0])
                self._db.commit()
                return _settle(adopted)
            except Exception as e:
                logger.warning("DB PnL 기록 실패 (legacy): %s", e)
                try:
                    self._db.rollback()
                except Exception:
                    pass
                _abandon_failed_clear()

        return False

    def _load_redis(self, today: date) -> Optional[float]:
        if self._redis is None:
            return None
        try:
            key = self._REDIS_KEY_TEMPLATE.format(date=today.isoformat())
            val = self._redis.get(key)
            return float(val) if val is not None else None
        except Exception:
            return None

    def _load_db(self, today: date) -> Optional[float]:
        row = self._load_db_full(today)
        return row.daily_pnl if row else None

    def _load_db_full(self, today: date):
        from backend.database.models import DailyRiskState
        return self._query(lambda s: s.get(DailyRiskState, today),
                           f"{today} 행")

    def _load_prior_days(self, today: date) -> dict:
        """``{risk day: daily_pnl}`` for the days before ``today`` in the weekly window."""
        from backend.database.models import DailyRiskState
        first = today - timedelta(days=self.WEEK_DAYS - 1)

        def _rows(s):
            return {r.trade_date: r.daily_pnl or 0.0
                    for r in s.query(DailyRiskState)
                    .filter(DailyRiskState.trade_date >= first,
                            DailyRiskState.trade_date < today)}
        return self._query(_rows, "주간 창") or {}

    def _load_latest_peak(self, today: date) -> Optional[float]:
        """The most recent recorded peak equity within the weekly window."""
        from backend.database.models import DailyRiskState
        first = today - timedelta(days=self.WEEK_DAYS - 1)

        def _peak(s):
            row = (s.query(DailyRiskState)
                   .filter(DailyRiskState.peak_equity > 0,
                           DailyRiskState.trade_date >= first,
                           DailyRiskState.trade_date <= today)
                   .order_by(DailyRiskState.trade_date.desc())
                   .first())
            return row.peak_equity if row is not None else None
        return self._query(_peak, "고점")

    def _query(self, fn, what: str):
        """Run ``fn(session)``; ``None`` on any error or with no database.

        A failed read at boot leaves that piece of risk state empty — the week
        without its earlier days, the peak re-seeded from the balance — so it
        is logged rather than passed over in silence.
        """
        sess = None
        try:
            if self._db_factory is not None:
                sess = self._db_factory()
                return fn(sess)
            if self._db is not None:
                return fn(self._db)
        except Exception as e:
            logger.error("리스크 상태 복원 조회 실패(%s) — 이 값 없이 기동: %s", what, e)
        finally:
            if sess is not None:
                sess.close()
        return None


def redundant_pairs(corr: pd.DataFrame, threshold: float = 0.80) -> list[tuple[str, str, float]]:
    """
    상관계수 threshold 이상인 전략/종목 쌍 반환.
    [(sym_a, sym_b, corr_value), ...]
    """
    pairs = []
    syms = list(corr.columns)
    for i, a in enumerate(syms):
        for b in syms[i + 1:]:
            val = corr.loc[a, b]
            if abs(val) >= threshold:
                pairs.append((a, b, round(float(val), 4)))
    return sorted(pairs, key=lambda x: -abs(x[2]))
