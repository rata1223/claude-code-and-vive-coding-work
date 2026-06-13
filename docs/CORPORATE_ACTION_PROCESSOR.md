# Corporate Action Processor — Design Specification

This document specifies the design of `backend/data/corporate_actions.py`
(TASK 3-4B), a corporate-action detector + adjuster that closes the
"three independent copies of position state" gap identified by
`docs/CORPORATE_ACTION_AUDIT.md` (TASK 3-4A, merged via PR #73). It is
structured to mirror `docs/STALE_DATA_DETECTOR.md` (TASK 3-3B, the most
recent sibling design doc) — see §7 for the section-by-section
correspondence and deliberate divergences.

This is a **design document** — `backend/data/corporate_actions.py` and
`tests/data/test_corporate_actions.py` are specified here (signatures,
dataclasses, algorithms, integration points) but **not implemented** in this
task. See "Future Work" at the end for the implementation task list.

> **Naming supersession.** TASK 3-4A's audit (§6, §11) referred to the future
> module only generically as "`backend/data/corporate_actions.py` (or
> similarly named module)" and did not commit to concrete class/function
> names. This document is the **first** to define concrete names — there is
> nothing to supersede, but the table below is provided in the same format as
> `STALE_DATA_DETECTOR.md`'s naming table so that future documents can refer
> back to it as the canonical source:
>
> | Name | Kind | Defined in |
> |---|---|---|
> | `CorporateActionType` | `str, Enum` | §3.1 |
> | `CorporateActionStatus` | `str, Enum` | §3.1 |
> | `CorporateActionEvent` | `dataclass` | §3.2 |
> | `CorporateActionError` | `Exception` subclass | §3.3 |
> | `AdjustmentRatio` | `dataclass(frozen=True)` | §3.4 |
> | `AdjustmentResult` | `dataclass` | §3.4 |
> | `CorporateActionConfig` / `DEFAULT_CA_CONFIG` | `dataclass(frozen=True)` / instance | §4 |
> | `CorporateActionProcessor` | class | §8 |
> | `process_corporate_actions()` | module function | §8.3 |
> | `CAGateDecision`, `evaluate_ca_gate()`, `should_suppress_pnl_contribution()` | `str, Enum` + functions | §9 |
> | `AdjustmentHook` | `Protocol` | §10 |
> | `SymbolMapping` | new ORM table | §11.5 |

> **LIVE-ONLY module (CA-13).** Every entry point in this design
> (`process_corporate_actions()`, `CorporateActionProcessor.*`,
> `evaluate_ca_gate()`) is meaningful **only** for live trading. A single
> `auto_adjust=True` backtest fetch is internally consistent by
> construction — Copy1 (`PositionTracker`) ≈ Copy0 (broker) ≈ Copy3
> (`TrailingStopManager`) for the entire run, because there is only one
> price basis. §13.2 specifies the exact no-op guard
> (`getattr(broker, "is_live", True)`), mirroring `strategy/base.py`'s
> `_is_bar_stale` (`backend/strategy/base.py:80-101`).

---

## 1. Purpose and Scope

This document specifies the data model, detection algorithm, adjustment
algorithm, audit trail, and trading gate for handling the 7
`CorporateActionType` values — `SPLIT`, `REVERSE_SPLIT`, `DIVIDEND`,
`TICKER_CHANGE`, `MERGER`, `SPINOFF`, `UNKNOWN` — across the three
position-state copies identified by the audit (§2.3):

- **Copy0** — broker-reported position (`KISBroker.get_positions()`,
  `backend/brokers/kis.py:71-104`). Ground truth; auto-adjusted by the
  exchange/broker for real corporate actions, but carries **zero** explicit
  event metadata.
- **Copy1** — `PositionTracker._positions[symbol]`
  (`backend/execution/position_tracker.py:36`), in-memory, fill-driven only.
- **Copy2** — DB `Position` row (`backend/database/models.py:78-89`),
  synced to Copy0 only by the reconciler.
- **Copy3** — `TrailingStopManager._positions[symbol]` /
  `PositionStop` (`backend/quant/risk/engine.py:58-95`), in-memory,
  price-denominated risk state, **never** touched by the reconciler today.

### In scope

- **Corporate Action Model** (§3): `CorporateActionType`,
  `CorporateActionStatus`, `CorporateActionEvent`, `CorporateActionError`,
  `AdjustmentRatio`, `AdjustmentResult` — all 7 event types.
- **Position Adjustment** (§5, §10, §11.2–11.3): quantity/avg-price
  adjustment formulas with portfolio-value preservation; `PositionStop`
  rescaling; the atomic 8-step apply algorithm covering Copy1+Copy2+Copy3
  together.
- **Event Detection** (§8): reconciler ratio-signature detection (primary,
  all brokers) + yfinance `actions=True`/`.splits`/`.dividends` corroboration
  (US-only, §6); `sweep_stale_events()` as the "unprocessed-event warning"
  (Item 4's third bullet).
- **Adjustment Audit Log** (§8.2, §11.4, §13.3): timestamp, symbol, event,
  adjustment method — via `AuditLog` reuse + 3 new `Position` columns.
- **Trading Gate** (§9): `CAGateDecision` + `evaluate_ca_gate()`, entry-only,
  conservative blocking on unresolved/inconsistent CA state.
- **Policies** (§5, §6, Fallback Policy section, §12, §13): adjustment rules,
  source support policy, fallback/escalation policy, audit logging policy,
  compatibility with existing strategies/backtests.

### Out of scope (explicitly — each item below stays OPEN, not silently resolved)

- **US historical price re-adjustment.** Already correctly owned by
  yfinance `auto_adjust=True` at all 6 cited call sites
  (`backend/quant/data/loader.py:74-76`,
  `backend/quant/live/safeguards.py:214`,
  `backend/strategy/indicator/backtest.py:17`,
  `backend/strategy/optimizer.py:20`, `strategy/signals.py:39`,
  `api/routers/indicators.py:163`). No change proposed.
- **CA-06 — pykrx KR adjustment verification.** This design's detection
  algorithm (§5.1's ratio-signature) works on KR qty/avg_price diffs
  regardless of pykrx's adjustment behavior, but the KR **price-history**
  adjustment layer (§6's "pykrx (KR)" row) remains **UNVERIFIED and
  unaddressed**. §11.1 sketches a *future* `_adjust_kr_ohlcv` only as a
  Future Work pointer.
- **CA-10 — dividend cash-drift auto-correction.** Stays
  observability-only by explicit design (§5.4), per
  `docs/RECONCILIATION_ENGINE.md:302-305`. This design adds a
  `CorporateActionEvent(event_type=DIVIDEND)` audit record but performs
  **no** cash-balance correction.
- **Backtest / `SimulatedBroker` path (CA-13).** No-op only (§13.2). No
  "inject a historical split into a backtest" harness is designed here
  (listed in §14/Future Work as a future test-infrastructure item).
- **CA-12 — non-yfinance event sources.** This design's only external event
  feed is yfinance `actions=True` (US-only). No other event source (news
  feed, broker calendar, etc.) is designed; `UNKNOWN` remains the
  catch-all for anything the ratio-signature detects that yfinance cannot
  corroborate.
- **Concrete `AdjustmentHook` implementations** (Telegram, etc.) — interface
  only (§10).

---

## 2. Design Principles

1. **N-state status enum, not a boolean "is_corporate_action".**
   `CorporateActionStatus` (§3.1) is a 5-state lifecycle —
   `DETECTED → CONFIRMED → APPLIED` / `REJECTED` / `STALE` — mirroring
   `StaleState`'s 4-state generalization of a binary
   (`STALE_DATA_DETECTOR.md` §2.1). Unlike `StaleState`, this is explicitly
   a **lifecycle**, not a severity ranking — there is no `combine_state()`
   analog here, because corporate-action events are evaluated per-event, not
   aggregated across sources.

2. **All-or-nothing atomicity — "worst-wins" is the wrong model here.**
   `STALE_DATA_DETECTOR.md` uses "worst wins" (`combine_state`) because
   *reporting* the worst of several freshness signals is strictly safer than
   reporting an average. Adjustment is different: a **partial** adjustment —
   e.g. Copy1 (`PositionTracker`) rescaled but Copy3 (`PositionStop`) left at
   the old basis — is *worse* than applying **no** adjustment at all, because
   it is exactly CA-05's failure mode (a `PositionStop.trailing_stop` from
   before a split, compared against a post-split `current_price`, false-stops
   the position). §10's 8-step algorithm is therefore designed so that any
   failure leaves the event's `status` un-advanced (still `CONFIRMED`,
   not `APPLIED`) — the *next* sweep retries the **entire** adjustment from
   scratch, never a "finish what was started" partial-resume. Idempotency
   replaces rollback.

3. **Entry-only gate, by construction — no `is_exit` parameter.**
   `STALE_DATA_DETECTOR.md §9`'s `evaluate_gate(report, *, is_exit, config)`
   has an `is_exit` parameter precisely because *blocking an exit on stale
   data is dangerous* (its "load-bearing safety invariant"). For corporate
   actions, the analogous danger does not exist in the same shape: by the
   time `evaluate_ca_gate()` is ever consulted, §10's atomic adjustment has
   *already* corrected `PositionStop` for any `APPLIED` event — so an exit
   driven by `TrailingStopManager.check_stops()` is **never** evaluated
   against stale CA state in the first place. `evaluate_ca_gate()` therefore
   has **no `is_exit` parameter at all** — it is called only from
   entry-decision call sites (§11.6's fusion/buy-loop equivalents). See the
   boxed invariant in §9.

4. **Fail-closed defaults.** `CorporateActionConfig.block_entries_on_unresolved_ca`
   defaults to `True`; `kill_switch_suppression_enabled` defaults to `True`.
   An operator who has not read this document gets the conservative behavior
   out of the box — entries into symbols with `CONFIRMED`/`STALE` CA events
   are blocked, and CA-pending qty-mismatches are annotated (not silently
   absorbed) into kill-switch reasoning.

5. **`CorporateActionError` is a dedicated exception type — not
   `RuntimeError`/`ValueError`.** Mirrors `DataFreshnessError`
   (`STALE_DATA_DETECTOR.md §3.4`) and `BadOHLCVError`
   (`docs/OHLCV_VALIDATION.md §3.4`) — a partial-adjustment failure inside
   §10 is an *expected*, recoverable operating condition (retry on next
   sweep), not a programming error. `ConsecutiveFailureBreaker`
   (`backend/execution/circuit_breaker.py:14-40`) must **not** count a
   `CorporateActionError` as a broker failure, for the same reason
   `DataFreshnessError` must not (§3 of this doc; cf.
   `STALE_DATA_DETECTOR.md §3.4`).

6. **Compatibility-first, additive-only.** Zero existing
   dataclass/ORM-model field is renamed, retyped, or removed. New `Position`
   columns are nullable; the new `SymbolMapping` table is standalone;
   `AuditLog` is reused via a new `event_type` value, not a new table
   (justified in §13.3). `PositionTracker`, `TrailingStopManager`, and
   `PositionReconciler` each gain **one new method** (§11.2/11.3/11.4) — no
   existing method signature changes.

7. **Stateless-ish processor + module-level convenience function.**
   `CorporateActionProcessor` (§8) is *not* purely stateless like
   `StaleDetector` — it needs injected dependencies (`position_tracker`,
   `trailing_stops`, `db_factory`, `yfinance_client`) to do its job. But the
   module also exposes `process_corporate_actions(processor, held_symbols,
   now)` (§8.3) as the single top-level entry point, mirroring
   `StaleDetector`/`check_freshness()`'s class-vs-module-function pairing
   (`STALE_DATA_DETECTOR.md §8.1/§8.3`).

8. **Single unified owner.** Before this design, qty/avg_price/peak_price/
   trailing_stop each had a different (or no) owner for corporate-action
   adjustment (audit §7). `CorporateActionProcessor.apply_adjustment()`
   (§10) is the **single** place all three copies are corrected — every
   other call site (reconciler, risk engine) delegates to it rather than
   growing its own adjustment logic.

---

## 3. Data Structures

### 3.1 `CorporateActionType` and `CorporateActionStatus`

```python
class CorporateActionType(str, Enum):
    """The 7 corporate-action categories this design addresses.

    SPLIT and REVERSE_SPLIT share identical adjustment formulas (Section 5) --
    they are distinguished only by AdjustmentRatio.raw_ratio > 1 (SPLIT,
    e.g. 2-for-1 doubles share count) vs. < 1 (REVERSE_SPLIT, e.g. 1-for-10
    reduces share count). Keeping them as separate enum members (rather than
    a single SPLIT with a signed ratio) makes CorporateActionEvent.detail
    and audit-log entries self-describing without inspecting the ratio.

    DIVIDEND carries no qty/price adjustment (Section 5.4) -- only
    cash_amount is recorded, per CA-10's observability-only design.

    TICKER_CHANGE / MERGER / SPINOFF all resolve through the symbol-mapping
    layer (Section 11.5); MERGER additionally may carry a share-conversion
    ratio (Section 5.5).

    UNKNOWN is the catch-all for a ratio-signature match (Section 5.1) that
    yfinance cannot corroborate and that does not fit any other type --
    Item 4's "unprocessed-event warning" applies most acutely to UNKNOWN
    events, since there is no automatic resolution path for them (they can
    only be escalated to a human via the Fallback Policy section).
    """
    SPLIT = "split"
    REVERSE_SPLIT = "reverse_split"
    DIVIDEND = "dividend"
    TICKER_CHANGE = "ticker_change"
    MERGER = "merger"
    SPINOFF = "spinoff"
    UNKNOWN = "unknown"


class CorporateActionStatus(str, Enum):
    """5-state lifecycle of a CorporateActionEvent. NOT a severity ranking
    (contrast StaleState's combine_state() ordering, Design Principle 1) --
    there is no "worst status wins" aggregation. Each event progresses
    independently through:

        DETECTED -> CONFIRMED -> APPLIED
                 \\-> REJECTED
        (DETECTED | CONFIRMED) -> STALE   (Section 8.2's sweep_stale_events)

    DETECTED:  a single signal fired (ratio-signature OR yfinance event),
               not yet corroborated. See Fallback Policy section.
    CONFIRMED: corroborated per the Fallback Policy's confirmation rules --
               eligible for apply_adjustment().
    APPLIED:   Section 10's 8-step algorithm completed successfully
               (AdjustmentResult.all_applied == True). Terminal.
    REJECTED:  a human or automated check determined this was NOT a real
               corporate action (e.g. a tracking-bug-shaped qty_diff that
               happened to match a ratio candidate by coincidence). Terminal.
    STALE:     DETECTED or CONFIRMED for longer than
               config.max_pending_age_hours with no resolution -- Item 4's
               "unprocessed-event warning". Terminal; evaluate_ca_gate()
               unconditionally BLOCKs entries for a STALE event's symbol
               (Section 9).
    """
    DETECTED = "detected"
    CONFIRMED = "confirmed"
    APPLIED = "applied"
    REJECTED = "rejected"
    STALE = "stale"


_PENDING_STATUSES = (CorporateActionStatus.DETECTED, CorporateActionStatus.CONFIRMED)
_TERMINAL_STATUSES = (CorporateActionStatus.APPLIED, CorporateActionStatus.REJECTED,
                       CorporateActionStatus.STALE)


def is_pending(status: CorporateActionStatus) -> bool:
    """True if `status` is DETECTED or CONFIRMED -- i.e. still awaiting
    apply_adjustment() or sweep_stale_events(). Used by evaluate_ca_gate()
    (Section 9) and sweep_stale_events() (Section 8.2)."""
    return status in _PENDING_STATUSES
```

### 3.2 `CorporateActionEvent`

```python
@dataclass
class CorporateActionEvent:
    """A single detected (or corroborated, applied, rejected, staled)
    corporate-action event for one symbol.

    Attributes:
        symbol: The (pre-action) symbol this event applies to. For
            TICKER_CHANGE/MERGER, this is the OLD symbol; new_symbol carries
            the replacement.
        event_type: CorporateActionType.
        ex_date: Ex-dividend / effective date, if known. May be None for a
            ratio-signature-only DETECTED event before yfinance corroboration
            supplies a date (Section 8.1).
        ratio: AdjustmentRatio.raw_ratio (or matched_candidate once
            confirmed) for SPLIT/REVERSE_SPLIT/MERGER-with-conversion. None
            for DIVIDEND/TICKER_CHANGE/SPINOFF/UNKNOWN-without-ratio.
        cash_amount: Per-share cash amount for DIVIDEND. None otherwise.
        new_symbol: Replacement symbol for TICKER_CHANGE/MERGER/SPINOFF.
            None for SPLIT/REVERSE_SPLIT/DIVIDEND. For SPINOFF, new_symbol is
            the *spun-off* symbol (the original `symbol` continues to exist).
        source: Free-text provenance, e.g. "reconciler_signature", "yfinance",
            "reconciler_signature+yfinance" (after corroboration, Fallback
            Policy section).
        detected_at: UTC timestamp of first detection.
        status: CorporateActionStatus.
        confidence: float in [0, 1]. 1.0 for yfinance-corroborated events;
            < 1.0 for signature-only events scaled by
            min_confirmations_for_signature_only (Fallback Policy section).
        detail: Free-form dict for provenance/debugging -- e.g.
            {"db_qty": 100, "broker_qty": 200, "matched_candidate": 2.0}
            for a ratio-signature detection (mirrors
            ReconciliationResult.gap()'s detail-string convention,
            backend/execution/reconciler.py:51-53, but as a dict here since
            this is a long-lived record, not a one-line log).

    __post_init__ validates required fields per event_type and raises
    CorporateActionError(stage="validation") on violation:
        - SPLIT, REVERSE_SPLIT: ratio is required (not None, > 0).
        - DIVIDEND: cash_amount is required (not None).
        - TICKER_CHANGE, MERGER, SPINOFF: new_symbol is required.
        - MERGER may additionally carry ratio (share-conversion, Section 5.5)
          -- optional, unlike SPLIT/REVERSE_SPLIT where it is required.
        - UNKNOWN: no additional required fields (deliberately permissive --
          it is the catch-all).
    """
    symbol: str
    event_type: CorporateActionType
    ex_date: Optional[date]
    ratio: Optional[float]
    cash_amount: Optional[float]
    new_symbol: Optional[str]
    source: str
    detected_at: datetime
    status: CorporateActionStatus
    confidence: float
    detail: dict = field(default_factory=dict)

    def summary_line(self) -> str:
        """One-line summary for log lines (Section 12), e.g.:

            "AAPL SPLIT ratio=4.0 status=CONFIRMED conf=1.00 src=reconciler_signature+yfinance"
            "FB TICKER_CHANGE -> META status=APPLIED src=reconciler_signature"
            "069500 DIVIDEND cash=350.0 status=APPLIED src=reconciler_signature"
        """
```

### 3.3 `CorporateActionError`

```python
class CorporateActionError(Exception):
    """Raised by CorporateActionProcessor.apply_adjustment() (Section 10)
    when one of the 8 steps fails, and by CorporateActionEvent.__post_init__
    on validation failure (Section 3.2).

    NOT a subclass of RuntimeError or ValueError -- mirrors
    DataFreshnessError (STALE_DATA_DETECTOR.md SS3.4) and BadOHLCVError
    (docs/OHLCV_VALIDATION.md SS3.4). A partial-adjustment failure is an
    *expected* operating condition (DB transient error, a missing
    PositionStop that vacuously succeeds rather than errors, etc.) -- not a
    programming error, and NOT a broker failure for
    ConsecutiveFailureBreaker's purposes (Design Principle 5).

    Attributes:
        symbol: The symbol being adjusted.
        event: The CorporateActionEvent being applied (or validated).
        stage: One of "copy1" | "copy2" | "copy3" | "validation" -- which
            step of Section 10's algorithm (or which __post_init__ check)
            raised. Lets callers/log lines pinpoint exactly how far the
            atomic operation got before failing, without re-deriving it from
            a stack trace.
    """

    def __init__(self, symbol: str, event: Optional["CorporateActionEvent"],
                 stage: str, message: str) -> None:
        self.symbol = symbol
        self.event = event
        self.stage = stage
        super().__init__(f"[{stage}] {symbol}: {message}")
```

### 3.4 `AdjustmentRatio` and `AdjustmentResult`

```python
@dataclass(frozen=True)
class AdjustmentRatio:
    """Result of Section 5.1's match_split_signature() -- whether a
    qty/avg_price discrepancy looks like a split/reverse-split, and if so,
    which candidate ratio it matches.

    Attributes:
        raw_ratio: The raw observed ratio, e.g. broker_qty / db_qty = 2.03.
        matched_candidate: The closest value from
            config.split_ratio_candidates within ratio_tolerance_pct, e.g.
            2.0. None if raw_ratio matched no candidate.
        tolerance_pct: The tolerance that was applied (copied from config,
            for self-contained logging).
        is_signature: True iff matched_candidate is not None -- "this looks
            like a split/reverse-split, not an arbitrary tracking-bug
            qty_diff". The single boolean the reconciler (Section 11.2)
            branches on.
        implied_type: CorporateActionType.SPLIT if matched_candidate > 1,
            CorporateActionType.REVERSE_SPLIT if matched_candidate < 1, None
            if not is_signature. (matched_candidate == 1 cannot occur --
            candidates are {2,3,4,5,10,0.5,0.2,0.25,0.1}, none equal 1.)
    """
    raw_ratio: float
    matched_candidate: Optional[float]
    tolerance_pct: float
    is_signature: bool
    implied_type: Optional[CorporateActionType]


@dataclass
class AdjustmentResult:
    """Result of Section 10's apply_adjustment() -- per-copy success flags
    plus audit linkage.

    Attributes:
        symbol: The symbol adjusted.
        event: The CorporateActionEvent that was applied.
        copy1_applied: True if PositionTracker._positions[symbol] was
            mutated (or vacuously True if no Copy1 position existed --
            Section 10 step 1).
        copy2_applied: True if the DB Position row was updated/committed (or
            vacuously True if no row existed).
        copy3_applied: True if TrailingStopManager rescaled (or vacuously
            True if no PositionStop existed for symbol).
        applied_at: UTC timestamp of completion.
        audit_log_id: AuditLog.id of the record written in step 6, or None
            if step 6 itself failed (CorporateActionError already raised by
            that point -- AdjustmentResult is only returned on success).

    Properties:
        all_applied: copy1_applied and copy2_applied and copy3_applied.
            Always True for any AdjustmentResult actually *returned* by
            apply_adjustment() (Design Principle 2 -- partial results are
            never returned, only raised as CorporateActionError). The
            property exists primarily for tests (Section 14) and for any
            future caller that wants to assert the invariant defensively.
    """
    symbol: str
    event: "CorporateActionEvent"
    copy1_applied: bool
    copy2_applied: bool
    copy3_applied: bool
    applied_at: datetime
    audit_log_id: Optional[int]

    @property
    def all_applied(self) -> bool:
        return self.copy1_applied and self.copy2_applied and self.copy3_applied
```

---

## 4. `CorporateActionConfig`

```python
@dataclass(frozen=True)
class CorporateActionConfig:
    """All tunables for CorporateActionProcessor. Frozen, like
    StaleDetectorConfig (STALE_DATA_DETECTOR.md SS4) -- a config object is
    immutable for the lifetime of a CorporateActionProcessor instance;
    changing tunables means constructing a new config (and, in practice, a
    new processor).
    """

    # -- Section 5.1: ratio-signature detection --
    split_ratio_candidates: tuple[float, ...] = (2, 3, 4, 5, 10, 0.5, 0.2, 0.25, 0.1)
    ratio_tolerance_pct: float = 0.02  # 2% -- e.g. 2.03 matches candidate 2.0

    # -- Fallback Policy section: confirmation thresholds --
    min_confirmations_for_signature_only: int = 2
    confirmation_window_hours: float = 24.0

    # -- Section 8.1: yfinance corroboration (US-only, CA-06) --
    yfinance_event_check_interval_hours: float = 12.0
    yfinance_lookback_days: int = 7

    # -- Section 8.2: sweep_stale_events() --
    max_pending_age_hours: float = 48.0

    # -- Section 9: Trading Gate --
    block_entries_on_unresolved_ca: bool = True
    block_universe_symbols_on_unresolved_ca: bool = True

    # -- Section 11.6: kill-switch annotation --
    kill_switch_suppression_enabled: bool = True
    kill_switch_suppression_window_minutes: float = 30.0

    # -- Section 5.2: fractional-share handling --
    # One of "round_down_track_remainder" | "round_to_nearest" | "reject".
    # See Section 5.2 for the full description of each policy.
    fractional_share_policy: str = "round_down_track_remainder"

    # -- Section 3.3 / 10: error-handling strictness --
    # If True, CorporateActionError propagates to the caller of
    # process_corporate_actions(); if False (default), the error is logged
    # and the event is simply left un-advanced for retry on the next sweep
    # (Design Principle 2).
    raise_on_unresolved_ca: bool = False


DEFAULT_CA_CONFIG = CorporateActionConfig()
```

`split_ratio_candidates` covers the splits/reverse-splits actually seen in
US/KR markets in the last decade (2:1, 3:1, 4:1, 5:1, 10:1 forward;
1:2, 1:5, 1:4, 1:10 reverse — i.e. `0.5, 0.2, 0.25, 0.1`). A ratio outside
this set is **not** treated as a signature (`is_signature=False`,
`AdjustmentRatio.implied_type=None`) — it falls through to `UNKNOWN` (§3.1)
rather than being force-fit to the nearest candidate, per Design Principle 4
(fail closed: an un-matched ratio is more likely a tracking bug than an
exotic split, and CA-03's existing unconditional-repair path is left
untouched for it — §11.2).

---

## 5. Core Algorithm

### 5.1 `match_split_signature` — ratio-signature detection

```python
def match_split_signature(
    db_qty: float,
    broker_qty: float,
    config: CorporateActionConfig = DEFAULT_CA_CONFIG,
) -> AdjustmentRatio:
    """Test whether (db_qty, broker_qty) looks like a split/reverse-split.

    Computes raw_ratio = broker_qty / db_qty (the direction the audit's CA-03
    finding observed: reconciler.py:206-231 currently does
    `row.qty = bp.qty` -- i.e. broker is the post-action truth, DB is
    pre-action). Tests raw_ratio against each candidate in
    config.split_ratio_candidates AND each candidate's reciprocal (so a
    db_qty=200/broker_qty=100 -- a 1:2 reverse-split as seen from this
    ratio's direction -- still matches candidate 0.5 via
    raw_ratio=0.5 -- both directions are pre-populated into
    split_ratio_candidates already, so no extra reciprocal step is actually
    needed in the implementation; documented here for clarity of intent).

    A candidate c matches if:
        abs(raw_ratio - c) / c <= config.ratio_tolerance_pct

    On match: matched_candidate=c, is_signature=True,
    implied_type = SPLIT if c > 1 else REVERSE_SPLIT.

    On no match: matched_candidate=None, is_signature=False,
    implied_type=None -- this is the "ordinary tracking-bug qty_diff" case,
    and reconciler.py's existing unconditional fix_qty path (Section 11.2)
    remains the fallback, UNCHANGED.

    db_qty == 0 is rejected up front (ZeroDivisionError avoided) and returns
    is_signature=False -- a position that doesn't exist in the DB yet is
    Section 11.2's "missing_in_db" case, not a signature case.
    """
```

### 5.2 `compute_adjusted_values` — qty/avg_price adjustment

```python
def compute_adjusted_values(
    qty: float,
    avg_price: float,
    ratio: float,
    config: CorporateActionConfig = DEFAULT_CA_CONFIG,
) -> tuple[float, float, float]:
    """Apply a SPLIT/REVERSE_SPLIT ratio to (qty, avg_price), preserving
    portfolio value: new_qty * new_avg_price ~= qty * avg_price.

    new_qty_exact   = qty * ratio
    new_avg_price   = avg_price / ratio   (so new_qty_exact * new_avg_price
                                            == qty * avg_price exactly)

    Returns (new_qty, new_avg_price, remainder) where new_qty is an int and
    remainder captures any fractional share per config.fractional_share_policy:

    - "round_down_track_remainder" (DEFAULT): new_qty = floor(new_qty_exact),
      remainder = new_qty_exact - new_qty (a fractional-share count, e.g.
      0.5 shares). The remainder is recorded in
      CorporateActionEvent.detail / AuditLog detail (Section 8.2) for
      operator visibility -- brokers typically cash-settle fractional
      shares from a split, and this design does NOT attempt to model that
      cash credit (same observability-only stance as CA-10's dividends).
    - "round_to_nearest": new_qty = round(new_qty_exact), remainder = the
      (possibly negative) rounding delta. Slightly over/under-states
      portfolio value by at most 0.5 * new_avg_price; acceptable for
      large qty where the relative error is negligible.
    - "reject": if new_qty_exact is not (within floating-point epsilon) an
      integer, raise CorporateActionError(stage="copy1",
      message="fractional share count under reject policy"). Use this
      policy in environments where fractional positions must never occur
      (e.g. a broker that rejects fractional-share orders entirely).

    ratio == 0 or ratio < 0 raises CorporateActionError(stage="validation")
    -- only SPLIT (ratio > 1) and REVERSE_SPLIT (0 < ratio < 1) call this
    function (Section 5.5 -- DIVIDEND/TICKER_CHANGE/SPINOFF do not).
    """
```

### 5.3 `rescale_position_stop` — Copy3 adjustment

```python
def rescale_position_stop(stop: "PositionStop", ratio: float) -> dict:
    """Compute the new field values for a PositionStop
    (backend/quant/risk/engine.py:58-79) after a SPLIT/REVERSE_SPLIT of
    `ratio`. Returns a dict of {field_name: new_value} -- the caller
    (TrailingStopManager.apply_ca_adjustment, Section 11.3) applies these
    via setattr, keeping this function a pure computation with no
    TrailingStopManager dependency (testable in isolation, Section 14).

    Price-denominated fields are divided by ratio (the inverse of
    compute_adjusted_values' avg_price treatment, for the same
    value-preservation reason -- a stop price represents a price level, and
    price levels move inversely to a qty-scaling split):
        entry_price   /= ratio
        peak_price    /= ratio
        trailing_stop /= ratio
        hard_stop     /= ratio

    Quantity-denominated field is scaled the same direction as
    compute_adjusted_values:
        qty *= ratio

    Unchanged fields (not price- or qty-denominated):
        symbol, entry_date, trailing_stop_pct

    trailing_stop_pct is a *percentage* (e.g. 0.07 for -7%) -- a relative
    measure, invariant under a uniform price rescaling, so it is NOT divided
    by ratio. This is the field most likely to be incorrectly "fixed" by a
    naive implementation that divides every float attribute by ratio --
    flagged explicitly here and re-flagged in Section 14's test list.
    """
```

### 5.4 Dividend handling (CA-10 — observability-only)

`DIVIDEND` events carry `cash_amount` (per-share) and `ex_date` but trigger
**no** call to `compute_adjusted_values` or `rescale_position_stop` — `qty`,
`avg_price`, and all `PositionStop` fields are left untouched. The *only*
side effect of an `APPLIED` `DIVIDEND` event is the audit record (§8.2,
§11.4): `AuditLog(event_type="corporate_action_adjustment",
detail={"corporate_action_type": "dividend", "cash_amount": ..., ...})`.
This makes a future cross-reference possible (an operator can correlate a
dividend event with the unattributed cash-balance drift
`docs/RECONCILIATION_ENGINE.md:302-305` already documents as
observability-only) **without** this design attempting the cash-balance
correction itself — CA-10 stays exactly as open as the audit left it.

In Section 10's 8-step algorithm, a `DIVIDEND` event short-circuits after
step 1 (no open position required — a dividend can be recorded for a symbol
the system no longer holds, e.g. for historical audit completeness) directly
to step 6 (audit write) and step 7 (mark `APPLIED`); steps 2–5 (locking,
Copy1/2/3 mutation) are skipped entirely. `copy1_applied`/`copy2_applied`/
`copy3_applied` are all set `True` vacuously (Design Principle 2 — "vacuous
success", not "skipped" — `AdjustmentResult.all_applied` remains a valid
invariant).

### 5.5 Ticker change / merger / spinoff — symbol-mapping, optional ratio

`TICKER_CHANGE` and `SPINOFF` carry `new_symbol` but **no** `ratio` —
adjustment is entirely Symbol-Mapping-layer (§11.5): `resolve_symbol(old) ->
new`, applied at lookup time to `PositionTracker.get_position()`,
`TrailingStopManager`'s position dict key, and reporting-time joins for
historical `Order`/`Trade`/`Fill` rows. No `compute_adjusted_values` or
`rescale_position_stop` call — qty/avg_price/peak_price etc. are carried
forward **unchanged** under the new symbol key.

`MERGER` *may* additionally carry `ratio` (a share-conversion ratio, e.g.
"2 old shares -> 1 new share + cash"). When `ratio` is present, `MERGER`
reuses `compute_adjusted_values`/`rescale_position_stop` exactly as
`SPLIT`/`REVERSE_SPLIT` do, **in addition to** the symbol-mapping step. When
`ratio` is absent (the common case — most mergers in this design's expected
scope are 1:1 stock-for-stock or all-cash, where the position simply
disappears and is handled as a `stale_db_position`/`missing_in_db`
reconciler pair, not by this module), `MERGER` behaves identically to
`TICKER_CHANGE`.

`SPINOFF` is the one case where step 1 of §10's algorithm (no-open-position
pre-check) is expected to be the **common** path on first detection: the
spun-off symbol has no existing Copy1/Copy2/Copy3 entries. §11.5 covers the
`parent_symbol`-lookup + `TrailingStopManager.open()` bootstrap for this
case.

---

## 6. Source Support Policy

| Source | SPLIT / REVERSE_SPLIT | DIVIDEND | TICKER_CHANGE | MERGER | SPINOFF | UNKNOWN |
|---|---|---|---|---|---|---|
| **yfinance (US)** | Supported — latent `Ticker.splits` / `actions=True` API (`backend/quant/data/loader.py`, currently **unused** anywhere — confirmed by audit §3/§12) provides corroboration with `confidence=1.0` | Supported — `Ticker.dividends`, same latent/unused API | Not supported — yfinance has no ticker-change calendar | Not supported | Not supported | N/A — yfinance never *produces* UNKNOWN |
| **KIS (broker qty/avg_price diff)** | **Supported — primary detection** via §5.1's ratio-signature on `reconciler.py`'s `(db_qty, broker_qty)` pair (§11.2). yfinance corroborates when available (US symbols) | Implicit only — a cash-balance delta with no qty/avg_price signature; CA-10 stays observability-only (§5.4) | Supported — new symbol appears in `broker.get_positions()` with old symbol's qty/avg_price (approximately); resolved via §11.5 | Supported via §11.5 (with optional ratio per §5.5) | Supported via §11.5 — new symbol appears with no prior history | Falls back here — a ratio-signature match with no yfinance corroboration and no symbol-mapping match |
| **pykrx (KR)** | **CA-06 stays open.** §5.1's ratio-signature still works mechanically on KR `(db_qty, broker_qty)` pairs (it only needs two integers) — but there is **no** yfinance-equivalent corroboration source for KR, so KR signature matches are permanently capped at `confidence < 1.0` (Fallback Policy section) unless/until CA-06 is resolved and a KR event source is added | Same cap as SPLIT — no corroboration source | Supported via §11.5 (broker-diff only, no corroboration) | Supported via §11.5 (broker-diff only) | Supported via §11.5 (broker-diff only) | More likely than US — without corroboration, more signature matches stay capped/UNKNOWN |
| **Kiwoom** | N/A — confirmed stub, no live data path (audit §5.2) | N/A | N/A | N/A | N/A | N/A |
| **OpenBB** | N/A — confirmed not integrated (audit §5.3, only a `pandas-ta-openbb` indicator-library fork comment exists) | N/A | N/A | N/A | N/A | N/A |

**Takeaway:** yfinance's latent `actions=True`/`.splits`/`.dividends` API
(US-only) is the **only** corroboration source in this design — exactly as
the audit's §3 flagged it as "the highest-leverage low-effort insertion
point". For KR symbols, every event type is detectable only via the
ratio-signature + symbol-mapping mechanisms, capped at reduced confidence
until CA-06 is resolved (Future Work).

---

## 7. Template Alignment Note

This document's section numbering deliberately follows
`docs/STALE_DATA_DETECTOR.md`'s skeleton (§1 Purpose/Scope, §2 Design
Principles, §3-4 Data Structures/Config, §5 Core Algorithm, §6 Source
Policy, §8-10 Main Class/Gate/Hook, §11-14 Integration/Logging/
Compatibility/Testing), so a reader already familiar with that document can
navigate this one by section number alone. Three deliberate divergences:

1. **§7 itself** — this note. `STALE_DATA_DETECTOR.md §7` performs the same
   role (it has no §7 content beyond a short alignment note relative to
   `OHLCV_VALIDATION.md`); we keep the same slot number for the same reason.
2. **§9's gate has no `is_exit` parameter** — Design Principle 3. This is
   the most consequential divergence: `STALE_DATA_DETECTOR.md §9`'s decision
   matrix has two columns (entry / exit); §9 of this document has one.
3. **§11 has 7 subsections (11.1–11.7) rather than a flat SG-01..SG-09-style
   list** — because the audit's §6 already enumerated exactly 6 insertion
   points (6.1–6.6), and §11 maps 1:1 onto them plus a §11.7 summary table,
   rather than re-deriving a new numbering scheme.

A standalone **Fallback Policy** section is inserted between §9 and §10 —
`STALE_DATA_DETECTOR.md` has no direct analog (its "fallback" logic is
folded into §5's state-resolution and §10's `RecoveryHook`); this design
pulls it out as its own section because the task description requests
"fallback policy" as one of the 5 named policies to define, and its content
(confirmation/escalation rules) is referenced by *both* §8 (event lifecycle)
and §9 (gate decisions) — a shared cross-cutting policy, not a sub-step of
either.

---

## 8. `CorporateActionProcessor` — Main Class and Module Function

### 8.1 Class vs. module function

Unlike `StaleDetector` (pure-stateless — every method takes all its inputs
as arguments, `STALE_DATA_DETECTOR.md §8.1`), `CorporateActionProcessor`
needs **injected collaborators** to do its job: it must read/write
`PositionTracker`'s in-memory dict, the DB `Position`/`AuditLog`/
`SymbolMapping` tables, `TrailingStopManager`'s in-memory dict, and
optionally call out to yfinance. These are constructor-injected once at
worker-startup time (alongside `PositionReconciler`'s own construction,
`backend/execution/reconciler.py:101-108`) and reused across calls — the
processor itself holds **no per-event mutable state** beyond its
collaborators' own state (events live in the DB via `AuditLog`/a future
`CorporateActionEvent` table, not in the processor instance — see §13.3 for
the storage decision). `process_corporate_actions()` (§8.3) is the
module-level convenience function that a worker loop calls once per cycle,
mirroring `check_freshness()` (`STALE_DATA_DETECTOR.md §8.3`).

### 8.2 Class skeleton

```python
class CorporateActionProcessor:
    """Detects, corroborates, and applies corporate-action adjustments
    across Copy1 (PositionTracker), Copy2 (DB Position), and Copy3
    (TrailingStopManager) for held symbols.

    Constructor dependencies:
        position_tracker: PositionTracker (backend/execution/position_tracker.py)
        trailing_stops: TrailingStopManager (backend/quant/risk/engine.py)
        db_factory: Callable -> Session (same factory shape as
            PositionReconciler's db_factory, backend/execution/reconciler.py:101)
        config: CorporateActionConfig, defaults to DEFAULT_CA_CONFIG
        yfinance_client: optional -- defaults to the `yfinance` module itself
            (yf.Ticker(symbol).splits / .dividends, Section 6). Injectable
            for testing (Section 14) without a network dependency.
    """

    def __init__(
        self,
        position_tracker: "PositionTracker",
        trailing_stops: "TrailingStopManager",
        db_factory: Callable,
        config: CorporateActionConfig = DEFAULT_CA_CONFIG,
        yfinance_client=None,
    ) -> None: ...

    # -- Detection (Item 4: event collection) --------------------------------

    def check_reconciler_signature(
        self, symbol: str, db_qty: float, broker_qty: float,
    ) -> Optional[AdjustmentRatio]:
        """Thin wrapper around match_split_signature (Section 5.1) -- the
        call site the reconciler (Section 11.2) invokes inline during its
        existing qty_mismatch branch. Returns None (not AdjustmentRatio with
        is_signature=False) if db_qty == 0, so the reconciler can
        distinguish "not applicable" from "checked, no signature"."""

    def check_yfinance_events(
        self, held_symbols: list[str],
    ) -> list[CorporateActionEvent]:
        """US-only (Section 6) -- for each symbol in held_symbols that looks
        like a US ticker (Section 11.1 / universe.py's US_ETF+US_LARGE
        membership, NOT a heuristic on symbol format), call
        yfinance_client.Ticker(symbol).splits and .dividends, filtered to
        events within the last config.yfinance_lookback_days. KR symbols
        (numeric, per universe.py's KR_ETF) are skipped entirely -- CA-06.

        Returns newly-detected CorporateActionEvent(source="yfinance",
        status=DETECTED, confidence=1.0, ...) for any split/dividend not
        already recorded (deduplicated against existing AuditLog
        corporate_action_adjustment / pending-event records by
        (symbol, event_type, ex_date)).

        Rate-limited by config.yfinance_event_check_interval_hours -- the
        caller (process_corporate_actions, Section 8.3) is responsible for
        only invoking this every N hours, not every cycle; this method
        itself does not track its own last-call time (stateless, Design
        Principle 7)."""

    # -- Lifecycle management (Item 4: status tracking) ----------------------

    def record_event(self, event: CorporateActionEvent) -> CorporateActionEvent:
        """Persist a newly-detected event, OR corroborate/promote an
        existing one per the Fallback Policy section:

        - If no matching pending event exists for (symbol, event_type,
          ~ex_date): insert as DETECTED, write AuditLog
          (event_type="corporate_action_detected").
        - If a matching pending event exists from a DIFFERENT source (e.g.
          existing source="reconciler_signature", new source="yfinance"):
          merge sources (source="reconciler_signature+yfinance"),
          confidence=1.0, status -> CONFIRMED. Write AuditLog
          (event_type="corporate_action_confirmed").
        - If a matching pending event exists from the SAME source
          (repeated ratio-signature hits across reconciler runs): increment
          a confirmation counter in detail; once
          >= config.min_confirmations_for_signature_only within
          config.confirmation_window_hours, status -> CONFIRMED at reduced
          confidence (Fallback Policy section).

        Returns the (possibly merged/promoted) event."""

    def get_pending_events(self, symbol: Optional[str] = None) -> list[CorporateActionEvent]:
        """Return all events with status in (DETECTED, CONFIRMED), optionally
        filtered to one symbol. Used by evaluate_ca_gate (Section 9) and
        should_suppress_pnl_contribution (Section 9)."""

    def sweep_stale_events(self, now: datetime) -> list[CorporateActionEvent]:
        """Item 4's 'unprocessed-event warning'. For every event with
        status in (DETECTED, CONFIRMED) where
        (now - detected_at) > config.max_pending_age_hours: set
        status=STALE, write AuditLog(event_type="corporate_action_stale",
        ...) at logger.error level (Section 12), and return the list of
        newly-staled events so the caller (process_corporate_actions,
        Section 8.3) can fire AdjustmentHook.on_adjustment_failed for each
        (Section 10) -- a STALE event is, from the hook's perspective,
        equivalent to a failed adjustment: something a human must look at.

        This is the PRIMARY mechanism by which an UNKNOWN event (Section
        3.1) that nothing ever corroborates becomes visible -- it cannot
        silently sit as DETECTED forever."""

    # -- Adjustment computation (Items 2 & 3) ---------------------------------

    def compute_ratio(self, event: CorporateActionEvent) -> AdjustmentRatio:
        """For SPLIT/REVERSE_SPLIT/MERGER-with-ratio events, resolve the
        final AdjustmentRatio to apply -- event.ratio if set by yfinance
        corroboration (authoritative), else the matched_candidate from the
        original ratio-signature detection (event.detail). Raises
        CorporateActionError(stage="validation") if event.event_type has no
        ratio concept (DIVIDEND/TICKER_CHANGE/SPINOFF without conversion)."""

    def compute_adjusted_position(
        self, symbol: str, event: CorporateActionEvent,
    ) -> tuple[float, float, float]:
        """Look up the current Copy1 position for symbol, resolve
        compute_ratio(event), and return compute_adjusted_values(...)
        (Section 5.2) -- (new_qty, new_avg_price, remainder). Does NOT
        mutate anything; apply_adjustment (below) does the mutation."""

    # -- Apply (Items 2, 3, 5 -- the atomic operation, Section 10) -----------

    def apply_adjustment(self, event: CorporateActionEvent) -> AdjustmentResult:
        """Execute Section 10's 8-step atomic algorithm for `event`. Returns
        AdjustmentResult with all_applied == True on success (Design
        Principle 2 -- no partial results are ever returned). Raises
        CorporateActionError on any failure; event.status is left
        unchanged (still CONFIRMED) so the next sweep retries from
        scratch."""

    # -- Symbol mapping (Item 1: TICKER_CHANGE/MERGER/SPINOFF, Section 11.5) -

    def resolve_symbol(self, symbol: str) -> str:
        """Return the current canonical symbol for `symbol`, following the
        SymbolMapping table (Section 11.5) if `symbol` has been renamed.
        Identity if no mapping exists. Read-only, side-effect-free."""

    def register_symbol_mapping(
        self, old_symbol: str, new_symbol: str,
        event_type: CorporateActionType, effective_date: date,
    ) -> "SymbolMapping":
        """Insert a new SymbolMapping row (Section 11.5). Called from
        apply_adjustment's step 3-5 for TICKER_CHANGE/MERGER/SPINOFF
        events."""

    # -- Audit (Item 5) --------------------------------------------------------

    def audit_adjustment(self, result: AdjustmentResult) -> int:
        """Write the AuditLog row for a successful AdjustmentResult (Section
        10 step 6, Section 11.4/13.3). Returns the new AuditLog.id, stored
        as AdjustmentResult.audit_log_id."""
```

### 8.3 Module-level convenience function

```python
def process_corporate_actions(
    processor: CorporateActionProcessor,
    held_symbols: list[str],
    now: Optional[datetime] = None,
) -> list[AdjustmentResult]:
    """Top-level entry point for a worker loop (mirrors check_freshness(),
    STALE_DATA_DETECTOR.md SS8.3).

    Step 0 (Section 13.2, CA-13): if not
    getattr(processor.position_tracker._machine.broker -- or however the
    live broker is reached, "is_live", True): return [] immediately. This
    module performs NO work for SimulatedBroker / backtests.

    Step 1: processor.sweep_stale_events(now) -- always runs first, so a
    newly-STALE event blocks step 4 below for its symbol via
    evaluate_ca_gate, even within this same cycle.

    Step 2: processor.check_yfinance_events(held_symbols) (US-only,
    rate-limited per config.yfinance_event_check_interval_hours --
    Section 8.2) -- each returned event is passed to processor.record_event().

    Step 3: for each event with status == CONFIRMED returned by
    processor.get_pending_events(), call processor.apply_adjustment(event).
    Collect successful AdjustmentResults; on CorporateActionError, log (Section
    12) and continue to the next event -- one symbol's adjustment failure
    must not block another's (mirrors the per-symbol try/except already in
    indicator/strategy.py's _scan_and_trade loop, audit CA-11).

    Step 4: return the list of AdjustmentResults from step 3.

    Note: step 2's check_reconciler_signature is NOT called from here --
    that detection happens inline inside PositionReconciler._reconcile_positions
    (Section 11.2), since it needs the (db_qty, broker_qty) pair the
    reconciler already has in hand. process_corporate_actions handles only
    the yfinance-corroboration and apply/sweep side of the pipeline.
    """
```

---

## 9. Trading Gate

This section defines Item 6 (Trading Gate): `evaluate_ca_gate()`, a pure
function from `(symbol, pending_events, config)` to a `CAGateDecision`.

```python
class CAGateDecision(str, Enum):
    """What the Trading Gate says to do about a NEW ENTRY into `symbol`."""
    ALLOW = "allow"
    ALLOW_WITH_LOG = "allow_with_log"
    BLOCK = "block"


def evaluate_ca_gate(
    symbol: str,
    pending_events: list[CorporateActionEvent],
    config: CorporateActionConfig = DEFAULT_CA_CONFIG,
) -> CAGateDecision:
    """Decide whether a NEW ENTRY into `symbol` may proceed, given
    `pending_events` (the result of
    processor.get_pending_events(symbol) UNION any STALE/REJECTED/APPLIED
    events for `symbol` from the recent past -- the caller, Section 11.6,
    is responsible for assembling this list; this function itself does not
    query the DB).

    This function has deliberately NO `is_exit` parameter -- see Design
    Principle 3 and the boxed invariant below. It is called ONLY from
    entry-decision call sites.

    Never raises. config.raise_on_unresolved_ca governs whether a CALLER
    raises CorporateActionError on BLOCK -- this function always returns a
    CAGateDecision.
    """
```

### Decision matrix

| Most-relevant event for `symbol` | `CAGateDecision` |
|---|---|
| No pending/recent events | `ALLOW` |
| `DETECTED` only (no `CONFIRMED`) | `ALLOW_WITH_LOG` |
| `CONFIRMED` | `BLOCK` if `config.block_entries_on_unresolved_ca` (default `True`), else `ALLOW_WITH_LOG` |
| `STALE` | `BLOCK` — **unconditional**, ignores `block_entries_on_unresolved_ca` |
| `APPLIED` within the last `config.kill_switch_suppression_window_minutes` | `ALLOW_WITH_LOG` |
| `APPLIED` (older) | `ALLOW` |
| `REJECTED` | `ALLOW` |

When multiple events exist for `symbol`, the **most severe** decision wins
(`BLOCK` > `ALLOW_WITH_LOG` > `ALLOW`) — this *is* a "worst wins" rule, but
note it operates on `CAGateDecision` outputs, not on `CorporateActionStatus`
inputs (Design Principle 1 — status is a lifecycle, not a severity; the
severity ranking exists only at this final gate-decision step).

> **The load-bearing invariant: this gate has no exit path, by
> construction.** `STALE_DATA_DETECTOR.md §9`'s decision matrix has an
> entry column and an exit column, with the bottom-right cell
> (`STALE`/`UNKNOWN` + exit = `ALLOW_WITH_LOG`, never `BLOCK`) called out as
> "the single most important rule in this entire design" — because blocking
> a protective exit on a *freshness* signal is actively dangerous.
>
> This design's gate has **no exit column at all** — `evaluate_ca_gate()`
> takes no `is_exit` parameter, and there is no code path, config flag, or
> override anywhere in this document that would cause it to be called for an
> exit decision. This is not an oversight; it is the design. By the time
> any exit logic runs (`TrailingStopManager.check_stops()`,
> `backend/quant/risk/engine.py:120-131`), §10's atomic adjustment has
> *already corrected* `PositionStop` for any `APPLIED` event for that
> symbol — so the exit is evaluated against correct, post-adjustment risk
> state, not against stale CA state. There is nothing for an exit-side gate
> to *do*.
>
> **If a future change ever adds an `is_exit` parameter to
> `evaluate_ca_gate()`, or calls it from a stop-loss/exit code path, that
> change has misunderstood this design** — exits are made safe by §10's
> *correction*, not by this gate's *permission*. The two mechanisms operate
> on different axes (data freshness vs. position-state correctness) and must
> not be conflated.

```python
def should_suppress_pnl_contribution(
    symbol: str,
    pending_events: list[CorporateActionEvent],
    config: CorporateActionConfig = DEFAULT_CA_CONFIG,
) -> tuple[bool, Optional[str]]:
    """Section 11.6's kill-switch annotation hook. Returns
    (should_suppress, reason_suffix).

    should_suppress=True iff config.kill_switch_suppression_enabled and
    `symbol` has a pending_events entry with status in (DETECTED, CONFIRMED)
    AND detected within the last
    config.kill_switch_suppression_window_minutes. reason_suffix is a short
    string like "CA-pending:SPLIT:AAPL" suitable for appending to
    LossTracker.kill_reason (Section 11.6) -- this function does NOT itself
    suppress anything; it returns an advisory the caller incorporates.

    Returns (False, None) once an event reaches APPLIED -- by definition,
    once apply_adjustment() has run, Copy1's avg_price/qty are correct and
    any PnL computed from them is real, not a CA artifact."""
```

---

## Fallback Policy

This section defines the confirmation/escalation policy referenced by §8.2
(`record_event`/`sweep_stale_events`) and §9 (gate decisions) — Item 6 of
the "Define" list.

1. **N reconciler-only signature confirmations within the confirmation
   window → `CONFIRMED` at reduced confidence.** If `match_split_signature`
   (§5.1) returns `is_signature=True` for the same `(symbol, implied_type,
   matched_candidate)` on **`config.min_confirmations_for_signature_only`**
   (default 2) separate reconciler runs, all within
   **`config.confirmation_window_hours`** (default 24h) of the first
   detection, `record_event` promotes the event `DETECTED → CONFIRMED` with
   `confidence = min_confirmations_for_signature_only⁻¹`-scaled (e.g.
   `0.5` for 2 confirmations, asymptotically approaching but never reaching
   `1.0` without corroboration) and `source="reconciler_signature"`. This is
   the **only** path to `CONFIRMED` for KR symbols (CA-06) and for US symbols
   where yfinance has not (yet) published the split.

2. **1 yfinance corroboration → `CONFIRMED` at `confidence=1.0`
   immediately.** If `check_yfinance_events` (§8.2) returns an event whose
   `(symbol, event_type, ex_date)` matches an existing `DETECTED` or
   `CONFIRMED` reconciler-signature event (within a small `ex_date`
   tolerance — same trading day), `record_event` immediately sets
   `status=CONFIRMED`, `confidence=1.0`,
   `source="reconciler_signature+yfinance"` — bypassing rule 1's
   multi-confirmation requirement entirely. A *standalone* yfinance event
   with no prior reconciler signature (e.g. detected before the next
   reconciler run observes the qty change) is recorded as `DETECTED,
   source="yfinance", confidence=1.0` and becomes eligible for
   `apply_adjustment` once `compute_adjusted_position` confirms a
   corresponding Copy1 qty exists to adjust — i.e. `CONFIRMED` is reached
   the moment *both* signals exist, regardless of arrival order.

3. **No confirmation within `max_pending_age_hours` → `STALE` → gate
   `BLOCK` + Telegram alert.** This is `sweep_stale_events`'s (§8.2)
   contribution: an event stuck at `DETECTED` (rule 1 never reached its
   confirmation count, and yfinance never corroborated — e.g. an `UNKNOWN`-
   shaped ratio match) for longer than `config.max_pending_age_hours`
   (default 48h) becomes `STALE`. **Human-confirmation IS the STALE
   escalation path** — there is no separate "ask a human" state between
   `CONFIRMED` and `STALE`; reaching `STALE` *is* the request for human
   attention, delivered via `AdjustmentHook.on_adjustment_failed` (§10) →
   Telegram (mirroring `_fire_kill_switch_alert`'s pattern,
   `backend/quant/risk/engine.py:276-293`). Once a human investigates and
   either (a) manually triggers `apply_adjustment` for a confirmed-by-
   inspection event (out of band — no UI is designed here, Future Work), or
   (b) marks it `REJECTED` (also out of band), the gate's `BLOCK` is lifted
   accordingly (`REJECTED → ALLOW`, §9's matrix).

---

## 10. Adjustment Hook

This is the atomic operation behind `apply_adjustment(event)` (§8.2) —
Items 2 and 3's core, and the mechanism that closes CA-01 to CA-04 (the
audit's "Most Dangerous Pattern").

### The 8-step algorithm

1. **Pre-validation / no-op check.** Look up Copy1
   (`position_tracker.get_position(event.symbol)`). If `None` and
   `event.event_type != SPINOFF`: nothing to adjust — return
   `AdjustmentResult(copy1_applied=copy2_applied=copy3_applied=True, ...)`
   (vacuous success, Design Principle 2's "vacuous", not "skipped").
   `DIVIDEND` events also short-circuit here straight to step 6 regardless
   of position existence (§5.4). `SPINOFF` with no Copy1 entry is the
   **expected** case — proceeds to step 3 to *create* the entry (§11.5).

2. **Lock ordering.** Acquire locks in this fixed order to avoid deadlock
   with any other multi-lock path: `PositionTracker._lock`
   (`position_tracker.py:38`, `threading.RLock`) → DB transaction
   (`db_factory()` session) → `TrailingStopManager`. **Open question,
   flagged for Future Work**: `TrailingStopManager` (`risk/engine.py:82-137`)
   has **no lock today**. This design does not add one (compatibility-first,
   Design Principle 6) but documents the gap: either (a) add a
   `threading.Lock` to `TrailingStopManager` as a small additive follow-up,
   or (b) rely on the fact that `apply_adjustment` and
   `TrailingStopManager.update`/`check_stops` already run on the same
   single live-pipeline worker thread (`backend/quant/live/pipeline.py`) —
   true today, requires no code change, but is an implicit single-writer
   assumption rather than an enforced one. Future Work item.

3. **Copy1 mutation — computed now, applied after step 4.** Compute
   `(new_qty, new_avg_price, remainder) = compute_adjusted_position(symbol,
   event)` (§8.2/§5.2) for `SPLIT`/`REVERSE_SPLIT`/`MERGER`-with-ratio. For
   `TICKER_CHANGE`/`MERGER`-without-ratio/`SPINOFF`, the "mutation" is a
   symbol-key rename/creation in `PositionTracker._positions` (§11.5) —
   `qty`/`avg_price` carried forward unchanged (`SPINOFF`'s new entry seeds
   from the broker-reported spun-off qty if available, else `qty=0,
   avg_price=0` as a placeholder — §11.5). These values are computed in
   this step but applied to the live dict only after step 4 commits.

4. **Copy2 DB write FIRST + COMMIT, then mutate Copy1.** Within the
   transaction from step 2: update (or insert, for `SPINOFF`) the `Position`
   row with the new `qty`/`avg_price`/`adjustment_factor`/
   `corporate_action_type`/`ex_date` (§11.4's new columns) and `commit()`.
   **Only after the commit succeeds**, mutate
   `position_tracker._positions[symbol]` (still under the Copy1 lock from
   step 2) to the same new values. **Rationale**: if the DB write/commit
   fails, Copy1 is **untouched** — the system remains in the CA-05 status
   quo (Copy1 stale, Copy2 stale, nothing corrected yet), a state the system
   has tolerated since before this design. The reverse ordering — Copy1
   first — would risk "Copy1 correct, Copy2 stale", a combination that has
   **never existed before** and that no other code path is prepared to
   detect or recover from. `event.status` remains `CONFIRMED` (not advanced)
   on this failure path — idempotent retry (Design Principle 2).

5. **Copy3 rescale.** `trailing_stops.apply_ca_adjustment(symbol, ratio)`
   (§11.3) — internally calls `rescale_position_stop` (§5.3) and applies the
   returned dict via `setattr` on the `PositionStop` in
   `TrailingStopManager._positions[symbol]`, **if one exists**; if
   `trailing_stops.get_all().get(symbol) is None`, this step is a vacuous
   success (`copy3_applied=True`) — not every Copy1/Copy2 position
   necessarily has an open `PositionStop` (e.g. restored via
   `restore_positions()` without re-opening a stop). For `SPINOFF`, this
   step **creates** a new `PositionStop` via `trailing_stops.open()`
   (§11.5) rather than rescaling one.

6. **Audit write — after steps 3-5 succeed.** `audit_adjustment(result)`
   (§8.2) writes the `AuditLog` row (§11.4/§13.3) with
   `event_type="corporate_action_adjustment"` and a `detail` JSON blob
   containing `timestamp` (= `applied_at`), `symbol`, the serialized
   `event`, and `adjustment_method` (= `event.event_type.value` plus
   `ratio`/`cash_amount`/`new_symbol` as applicable) — directly satisfying
   Item 5's four required fields. Writing the audit record *after* the state
   mutations means a successful `AuditLog` row reliably signals that steps
   3-5 completed — no "audit says applied but state wasn't actually
   changed" ambiguity.

7. **Mark event `APPLIED`.** `event.status = CorporateActionStatus.APPLIED`,
   persisted — the last write of the algorithm; by the time this executes,
   steps 3-6 are durable.

8. **Failure handling — no generic rollback.** Any exception in steps 3-7 is
   caught, wrapped as `CorporateActionError(symbol, event, stage=
   "copy1"|"copy2"|"copy3", message=str(original_exception))`, and re-raised
   (or logged-and-swallowed if `config.raise_on_unresolved_ca is False`, per
   §4). There is deliberately **no** generic try/except-rollback-everything —
   step 4's DB-first ordering already bounds the worst case for steps 4-5's
   failures to "Copy2 updated, Copy1/Copy3 not yet". **Caveat, flagged as a
   Future Work item**: a naive retry that re-runs
   `compute_adjusted_position` from `event.ratio` against an
   already-adjusted Copy2 would double-adjust. The recommended fix (deferred
   to the implementation task) is for the retry path to detect
   "Copy2.adjustment_factor already reflects this event" (via the new §11.4
   columns) and derive Copy1's target directly from Copy2's already-adjusted
   row instead of recomputing from `event.ratio`. `event.status` is left at
   `CONFIRMED` in all failure cases — no "PARTIALLY_APPLIED" status exists
   (§3.1).

### `AdjustmentHook` Protocol

```python
class AdjustmentHook(Protocol):
    """Pluggable notification hook, invoked by process_corporate_actions
    (Section 8.3) after each apply_adjustment call (success or failure) and
    after sweep_stale_events. Mirrors RecoveryHook
    (STALE_DATA_DETECTOR.md SS10) and _fire_kill_switch_alert's
    fire-and-forget, try/except-per-channel style
    (backend/quant/risk/engine.py:276-293).
    """

    def on_adjustment_applied(self, result: AdjustmentResult) -> None:
        """Called after a successful apply_adjustment. A concrete
        implementation (Telegram, WebSocket -- Future Work, not designed
        here) should never raise; failures are logged and swallowed by the
        caller regardless."""

    def on_adjustment_failed(
        self, symbol: str, error: "CorporateActionError | CorporateActionEvent",
    ) -> None:
        """Called on a CorporateActionError from apply_adjustment, OR for
        each event newly transitioned to STALE by sweep_stale_events (the
        Fallback Policy section's escalation path) -- in the latter case
        `error` is the CorporateActionEvent itself (status=STALE), not an
        exception. Implementations distinguish via isinstance()."""
```

---

## 11. Integration Points

This section maps each of the audit's six insertion points (`CORPORATE_ACTION_AUDIT.md` SS6.1-6.6)
onto concrete file:line targets and the specific new methods/columns/tables from SS3-SS10 above.
Each subsection states which CA-IDs it addresses, the exact site, the new API surface introduced
there, and -- critically -- what does **NOT** change. SS11.7 closes with a summary table and a
single paragraph restating the blocking-policy boundary (only SS9's gate ever returns `BLOCK`;
every other insertion point is a correction or annotation).

### 11.1 Price-Adjustment Layer (CA-06, CA-07)

**Site:** `backend/quant/data/loader.py:74-76` (US, yfinance `auto_adjust=True`) and
`backend/quant/data/loader.py:107-125` (`_fetch_kr_pykrx`, KR, pykrx).

**Change: NONE.** Per SS1 (Out of Scope) and Design Principle 6 (compatibility-first), this
design makes **zero** changes to either loader function.

- **US (yfinance, lines 74-76):** `auto_adjust=True` already re-bases the entire returned series
  to the query-time basis (CA-07's "basis drift" is a *historical-consistency* concern across
  separate fetches, not a per-fetch correctness bug -- it is explicitly out of scope here). This
  design's only interaction with yfinance is **additive**: SS8.2's `check_yfinance_events()` makes
  a *second*, independent call using `actions=True` / `.splits` / `.dividends` (the "latent API"
  identified in the audit's SS5.4) purely for event *detection* (SS6 Source Policy), never for
  price re-fetching. The OHLCV path at lines 74-76 is untouched.
- **KR (pykrx, lines 107-125):** **CA-06 remains explicitly OPEN.** This design does not resolve
  whether `krx.get_market_ohlcv_by_date()` returns split-adjusted or raw prices, and does not
  add an adjustment step to `_fetch_kr_pykrx`. KR symbols are simply excluded from
  `check_yfinance_events()` (SS6 table, SS8.2) -- the *only* CA detection path available for KR
  positions is the reconciler ratio-signature (SS5.1), which operates on `qty`/`avg_price`
  deltas and is adjustment-basis-agnostic. A possible future `_adjust_kr_ohlcv` step is noted in
  Future Work as a pointer only; it is not designed here, because doing so first requires
  resolving CA-06 (i.e. determining empirically whether pykrx prices are already adjusted), which
  is a data-verification task, not a design task.
- **`backend/quant/live/safeguards.py:214`** (`OHLCVRecovery` tier-2 yfinance fallback): also
  unchanged -- it already shares the `auto_adjust=True` convention with the primary loader
  (CA-07-adjacent cache-mixing risk noted in the audit's SS5 table is unaffected by this design).

**CA-07 mitigation note:** CA-07 (cross-time adjustment-basis drift for stored vs. re-fetched US
series) is **not resolved** by this module directly. It is **mitigated as a side effect** of
SS10's Copy1/Copy2/Copy3 rescaling: once a split is detected and applied, `PositionTracker`,
`Position`, and `PositionStop` are all re-based to the new (post-split) basis, which is the same
basis a fresh `auto_adjust=True` fetch would now return -- closing the specific divergence that
CA-07 describes for *position state* (CA-07 for *historical OHLCV series* used in signal
computation remains a latent, lower-severity concern, unchanged by this design).

### 11.2 Position-Adjustment Layer -- Copy1 + Reconciler (CA-01)

**Sites:**
- `backend/execution/position_tracker.py` (`PositionTracker`, lines 26-133) -- Copy1.
- `backend/execution/reconciler.py:206-231` (`_reconcile_positions`'s `qty_mismatch` branch) --
  detection site.

**New method on `PositionTracker`:**

```python
def apply_ca_adjustment(
    self, symbol: str, new_qty: int, new_avg_price: float,
) -> Optional[Position]:
    """Corporate-action variant of on_fill's position mutation (SS10 step 3).
    Acquires self._lock (same RLock guarding _positions/_pending_symbols --
    position_tracker.py:38). Unlike on_fill, this is a direct overwrite of
    qty/avg_price/current_price, not an accumulation -- the caller
    (CorporateActionProcessor.apply_adjustment, SS8.2) has already computed
    the post-ratio values via compute_adjusted_values (SS5.2). If
    new_qty <= 0 (e.g. a reverse split below 1 share, or a SPINOFF parent
    fully consumed), the symbol is removed from _positions, mirroring the
    on_fill sell-to-zero path (position_tracker.py:111-113). Returns the
    updated (or None if removed) Position for the caller's audit record.
    Does NOT touch _pending_symbols -- a CA adjustment is independent of
    order-placement locks."""
```

**Reconciler change** (`reconciler.py:206-231`): the existing `qty_mismatch` branch currently
performs an unconditional overwrite:

```python
# current (reconciler.py:206-231, abbreviated)
if qty_diff > _QTY_TOLERANCE:
    row.qty = bp.qty
    row.avg_price = bp.avg_price
    self._audit_position_change("reconcile_fix_qty", symbol, ...)
```

This design adds a **branch before** the existing overwrite:

```python
ratio_result = match_split_signature(db_qty=dp["qty"], broker_qty=bp.qty, config=self._ca_config)
if ratio_result.is_signature:
    event = self._ca_processor.record_event(CorporateActionEvent(
        symbol=symbol, event_type=ratio_result.implied_type, ratio=ratio_result.raw_ratio,
        source="reconciler_signature", ...))
    if event.status == CorporateActionStatus.CONFIRMED:
        self._ca_processor.apply_adjustment(event)
    # else: DETECTED, awaiting Fallback Policy confirmation -- existing
    # unconditional overwrite below is SKIPPED for this symbol this cycle.
else:
    # existing unconditional overwrite path -- UNCHANGED for non-signature mismatches
    row.qty = bp.qty
    row.avg_price = bp.avg_price
    self._audit_position_change("reconcile_fix_qty", symbol, ...)
```

The reconciler gains a `_ca_processor: CorporateActionProcessor` and `_ca_config:
CorporateActionConfig` constructor dependency (additive to `__init__`, lines 101-108 --
`broker, db_factory, redis_client, poller, broker_name` are unchanged). When `ratio_result.
is_signature` is `False` (the overwhelmingly common case -- an ordinary tracking-bug
`qty_diff`), behavior is **byte-for-byte identical** to today.

### 11.3 Risk-State Adjustment Layer -- Copy3 (CA-02, CA-05)

**Site:** `backend/quant/risk/engine.py:58-137` (`PositionStop` dataclass, lines 58-79;
`TrailingStopManager`, lines 82-137).

**New method on `TrailingStopManager`:**

```python
def apply_ca_adjustment(self, symbol: str, ratio: float) -> Optional["PositionStop"]:
    """Corporate-action rescale of Copy3 (SS10 step 5). Looks up
    self._positions.get(symbol) (engine.py's internal dict, mirroring
    _positions naming in PositionTracker); if absent, returns None
    (vacuous success -- e.g. SPINOFF where no PositionStop exists yet for
    the new symbol). Otherwise calls rescale_position_stop(stop, ratio)
    (SS5.3) and replaces the stored PositionStop with the rescaled values
    in place. Returns the updated PositionStop for the caller's audit
    record.

    OPEN ISSUE (flagged in SS10 step 2 and Future Work): TrailingStopManager
    has no lock today (unlike PositionTracker's threading.RLock,
    position_tracker.py:38). This method does not introduce one -- it
    relies on SS10's documented lock ordering (PositionTracker lock held
    first) and the processor's single-writer assumption (SS8.1) to avoid a
    concurrent mutation from the live price-update path
    (TrailingStopManager.update_price, engine.py) racing with this
    adjustment. A future PR should add a lock to TrailingStopManager
    matching PositionTracker's pattern."""
```

**CA-02/CA-05 closure:** CA-02 (PositionStop's `trailing_stop`/`peak_price` left in pre-split
basis, causing a false-liquidation) is closed by this method's call to `rescale_position_stop`
(SS5.3), which divides `entry_price`, `peak_price`, `trailing_stop`, and `hard_stop` by `ratio`
and multiplies `qty` by `ratio` -- restoring the portfolio-value-preservation invariant for
Copy3, identical in spirit to SS5.2's Copy1/Copy2 treatment. CA-05 (partial-adjustment risk --
Copy1 rescaled but Copy3 not, or vice versa) is closed by SS10's all-or-nothing algorithm: this
method is step 5 of that algorithm, and a failure here raises `CorporateActionError(stage=
"copy3")`, which (per SS10 step 8) prevents the event from being marked `APPLIED` -- the entire
adjustment, including the already-written Copy1/Copy2 changes from steps 3-4, remains in a
"pending retry" state rather than a "half-applied" state, because `status` is the single source
of truth for "has this been fully applied," not the individual `*_applied` flags on
`AdjustmentResult` (which exist for *audit detail*, not for *retry gating*).

### 11.4 Reconciler Detection + Audit Trail -- Copy2 + AuditLog (CA-03, CA-04)

**Site:** `backend/database/models.py` (`Position`, lines 78-89; `AuditLog`, lines 127-136);
`backend/execution/reconciler.py:405-418` (`_audit_position_change`).

**Three new nullable columns on `Position`** (additive -- no existing column changes):

```python
class Position(Base):
    # ... existing columns (id, symbol, qty, avg_price, market, broker,
    #     updated_at, UniqueConstraint(symbol, broker)) UNCHANGED ...
    adjustment_factor: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    corporate_action_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    ex_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
```

`adjustment_factor` stores the `ratio` from the most recently applied `CorporateActionEvent`
for this symbol (e.g. `0.5` for a 2-for-1 split, matching SS5.2's `new_avg_price = avg_price *
ratio` direction -- see SS5.2 for the precise sign/direction convention).
`corporate_action_type` stores `CorporateActionEvent.event_type.value` (e.g. `"split"`).
`ex_date` stores `CorporateActionEvent.ex_date`. All three are `NULL` for positions that have
never undergone a CA adjustment -- the overwhelming majority -- so this is a pure additive
migration with no backfill requirement (Future Work item: write the migration).

**`_audit_position_change` detail dict** (`reconciler.py:405-418`): the existing call signature
`_audit_position_change(event_type: str, symbol: str, detail: dict)` (writing an `AuditLog` row,
`models.py:127-136`) is **unchanged**. SS11.2's new branch calls it with
`event_type="corporate_action_adjustment"` (a new *value* for the existing `event_type` string
column -- not a schema change) and a `detail` dict carrying additive keys: `{"ca_event_type":
event.event_type.value, "ratio": ratio_result.raw_ratio, "ex_date": str(event.ex_date),
"copy1_applied": ..., "copy2_applied": ..., "copy3_applied": ..., "source": event.source}` --
this is `AdjustmentResult` (SS3.4) serialized into the existing `detail` JSON column.

**New gap kind `"qty_mismatch_corporate_action"`:** per the audit's SS6.4(b), a split-signature
match must be **annotated**, not silently fixed (CA-03's failure mode) and not treated as an
unexplained CRITICAL gap (which `RECONCILIATION_ENGINE.md`'s SS6/SS8.1 severity model would
otherwise assign to a 100%-of-position `qty_diff`). This design introduces
`"qty_mismatch_corporate_action"` as a distinct *gap kind* string, separate from the existing
`"qty_mismatch"` (used by the unchanged non-signature path in SS11.2). `RECONCILIATION_ENGINE.md`'s
severity classifier -- which per the audit is itself not yet fully implemented -- is the intended
consumer: a future implementation should treat `"qty_mismatch_corporate_action"` as ANNOTATED
(informational, already self-corrected by SS11.2's `apply_adjustment` call), never as CRITICAL,
and never as an `EmergencyStop` trigger input. This design does not implement that classifier;
it only reserves the gap-kind string so the future classifier has something to branch on.

### 11.5 Universe / Symbol-Mapping Layer (CA-08, CA-09, CA-11)

**Site:** new table; `backend/quant/data/universe.py` (consult-only); `Order`/`Trade`/`Fill`
rows in `backend/database/models.py` (reporting-time translation only).

**New table `SymbolMapping`:**

```python
class SymbolMapping(Base):
    __tablename__ = "symbol_mappings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    old_symbol: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    new_symbol: Mapped[str] = mapped_column(String(20))
    event_type: Mapped[str] = mapped_column(String(20))  # CorporateActionType.value
    effective_date: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
```

`old_symbol` is unique+indexed because `resolve_symbol()` (SS8.2) is a hot-path lookup (called
on every held-symbol iteration in `process_corporate_actions`, SS8.3). A symbol may appear at
most once as `old_symbol` -- chained renames (A->B->C) are represented as two rows
(`A->B`, `B->C`); `resolve_symbol()` follows the chain (bounded -- see below) rather than
requiring a single-hop table.

```python
def resolve_symbol(self, symbol: str) -> str:
    """Follows symbol_mappings chains old_symbol -> new_symbol until no
    further mapping exists, capped at 5 hops (defensive bound against a
    misconfigured cycle -- CorporateActionError(stage="validation") if
    exceeded). Returns symbol unchanged if no mapping row exists. Read-only,
    no _lock needed (SymbolMapping rows are written only by
    register_symbol_mapping, append-only by convention)."""

def register_symbol_mapping(
    self, old_symbol: str, new_symbol: str,
    event_type: "CorporateActionType", effective_date: "date",
) -> "SymbolMapping":
    """Inserts a new symbol_mappings row (SS10's TICKER_CHANGE/MERGER/SPINOFF
    path, SS5.5). Idempotent on (old_symbol) -- a duplicate old_symbol with
    the same new_symbol is a no-op; a duplicate old_symbol with a
    *different* new_symbol raises CorporateActionError(stage="validation")
    (ambiguous rename -- requires manual resolution, Future Work)."""
```

**`universe.py` -- consult-only, not mutated (CA-08, CA-11):** `backend/quant/data/universe.py`'s
static `US_ETF`/`US_LARGE`/`KR_ETF`/`UNIVERSE` lists (the audit's "canonical source") are **never
written** by this module. Instead, wherever the trading engine iterates `UNIVERSE` to decide what
to scan/trade, it should call `resolve_symbol(symbol)` first -- if a delisted symbol (CA-11) has
no `new_symbol` mapping (a pure delisting, not a rename), `resolve_symbol` returns it unchanged,
and `block_universe_symbols_on_unresolved_ca` (SS4) combined with SS9's gate keeps new entries
blocked without requiring `universe.py` itself to be edited. Updating `universe.py`'s static
lists to remove a delisted/renamed symbol remains a manual, human-reviewed edit (as today) --
this design only ensures *open positions* under the old symbol are not orphaned (CA-08's failure
mode) while that manual edit is pending.

**Historical `Order`/`Trade`/`Fill` -- reporting-time JOIN, no UPDATE:** rows already written
under the old symbol keep their `symbol` column unchanged -- this design does **not** perform a
bulk `UPDATE ... SET symbol = new_symbol` migration (Design Principle 6 -- additive only, and
because such a rewrite would itself need its own audit trail and could collide with CA-04's
adjustment-factor bookkeeping if a position spans the rename). A future reporting layer
(P&L history, trade log UI) that wants to display historical fills under the *current* ticker
joins through `symbol_mappings` at query time: `SELECT ... FROM fills LEFT JOIN symbol_mappings
ON fills.symbol = symbol_mappings.old_symbol`. This is noted as a reporting-layer concern, not
designed further here.

**CA-09 spinoff -- `parent_symbol` lookup + new position open:** a SPINOFF event's
`CorporateActionEvent.new_symbol` is the *spun-off* symbol (e.g. parent `AAPL` spins off
`NEWCO`). `apply_adjustment` (SS10 step 1) finds **no existing position** for `NEWCO` in
Copy1/Copy2/Copy3 -- this is the documented vacuous-success case for steps 3-5 *for the parent
symbol's adjustment*, but SS10's algorithm additionally calls
`TrailingStopManager.open(symbol=new_symbol, ...)` (existing method, unchanged signature) to
register a `PositionStop` for the new `NEWCO` position once Copy1/Copy2 rows for `NEWCO` exist
(created via the broker's next reconciler pass, which will see a new `bp` entry for `NEWCO` with
no corresponding `dp` row -- an existing reconciler code path, `_reconcile_positions`'s
"position exists at broker but not DB" branch, unchanged by this design). The
`CorporateActionEvent.detail` dict carries `{"parent_symbol": symbol}` so `audit_adjustment`
(SS8.2) records the parent-child relationship for forensic purposes (CA-04).

### 11.6 Kill-Switch / EmergencyStop Gating (CA-01, CA-03)

**Site:** `backend/quant/risk/engine.py:248-275` (`PersistentLossTracker._evaluate`).

**Recommended: option (ii), `kill_reason` annotation (low-invasiveness, v1).** The audit's SS6.6
poses two options: (i) exclude the affected symbol's PnL contribution from `_evaluate()`'s
daily/MDD calculations entirely, or (ii) tag the resulting `kill_reason` string with
corporate-action context. This design adopts **(ii)** as the v1 recommendation:

```python
# risk/engine.py:248-275, _evaluate -- additive change
pending_ca = self._ca_processor.get_pending_events()  # SS8.2, all symbols
if pending_ca and (daily_loss_breached or mdd_breached):
    ca_symbols = {e.symbol for e in pending_ca}
    kill_reason += f" [CA-PENDING: {', '.join(sorted(ca_symbols))} -- verify before acting]"
```

This is purely a **string annotation** on an existing `kill_reason` value -- it does not change
`_evaluate()`'s pass/fail decision, does not suppress `_fire_kill_switch_alert` (`engine.py:
276-293`, unchanged), and does not change the function's return type. An operator reading the
Telegram alert (the audit confirms this is the actual consumption point) sees immediately
whether a kill-switch fire coincides with a pending CA event, without `_evaluate()` having to
make a judgment call about whether to *suppress* the fire -- which the audit's SS6.6 frames as
the riskier option (i): silently excluding a symbol's PnL from MDD math could itself mask a
*real* loss if the ratio-signature match (SS5.1) is a false positive (e.g. a large but
coincidental `qty_diff` that happens to land near a candidate ratio). Option (i) -- full PnL
exclusion via `should_suppress_pnl_contribution` (SS9) -- remains available as
`kill_switch_suppression_enabled` (SS4, default `True`) for `_evaluate()`'s *future* use, but
this design's v1 wiring is the additive annotation; full exclusion is flagged in Future Work as
a larger change requiring its own review of `_evaluate()`'s PnL aggregation internals.

**`EmergencyStop` -- N/A today, future constraint:** per CA-03, no `EmergencyStop` path
currently exists (`RECONCILIATION_ENGINE.md`'s SS8.3 model is unimplemented). This design does
not implement it. It records one constraint for whenever it *is* implemented: its "N or more
CRITICAL gaps -> emergency liquidation" trigger (per `RECONCILIATION_ENGINE.md`'s SS6 severity
model) **must exclude** gaps of kind `"qty_mismatch_corporate_action"` (SS11.4) from its
CRITICAL count -- otherwise a single detected split (which produces exactly one
`qty_mismatch_corporate_action` gap per affected symbol, by construction) could itself trigger
the very emergency-liquidation cascade that CA-01's "Most Dangerous Pattern" describes, which
would be a textbook self-inflicted false-fire.

### 11.7 Summary Table

| Insertion Point | CA-IDs | Site | New API | On `DETECTED` | On `CONFIRMED` | On `APPLIED` / no pending |
|---|---|---|---|---|---|---|
| 11.1 Price-Adjustment | CA-06, CA-07 | `loader.py:74-76` (US, unchanged), `:107-125` (KR, unchanged) | none (US event-only via 8.2) | n/a | n/a | n/a |
| 11.2 Position (Copy1) | CA-01 | `position_tracker.py`, `reconciler.py:206-231` | `apply_ca_adjustment()` | reconciler's existing overwrite skipped this cycle | `apply_adjustment()` invoked | existing overwrite path, unchanged |
| 11.3 Risk-State (Copy3) | CA-02, CA-05 | `risk/engine.py:58-137` | `TrailingStopManager.apply_ca_adjustment()` | no change yet | rescaled via SS5.3 | unchanged |
| 11.4 Reconciler Audit (Copy2) | CA-03, CA-04 | `models.py` `Position`/`AuditLog`, `reconciler.py:405-418` | 3 new nullable columns, `"qty_mismatch_corporate_action"` gap kind | gap annotated, not silently fixed | `AuditLog` row written via `audit_adjustment()` | n/a |
| 11.5 Symbol-Mapping | CA-08, CA-09, CA-11 | new `symbol_mappings` table | `resolve_symbol()`, `register_symbol_mapping()` | n/a | mapping row inserted | `TrailingStopManager.open()` for SPINOFF |
| 11.6 Kill-Switch | CA-01, CA-03 | `risk/engine.py:248-275` | `kill_reason` suffix annotation | annotation added if breach coincides | annotation added if breach coincides | no annotation |
| SS9 Trading Gate | all | strategy entry path (caller-side, not a file in this repo yet) | `evaluate_ca_gate()` | `ALLOW_WITH_LOG` | `BLOCK` (configurable) | `ALLOW` / `ALLOW_WITH_LOG` |
| SS10 Adjustment | CA-01..CA-05, CA-08, CA-09 | all of Copy1/2/3 + audit | `apply_adjustment()`, `AdjustmentHook` | n/a (pre-application) | triggers application | `APPLIED`, hooks fired |
| Fallback Policy | CA-12 | reconciler + `check_yfinance_events` | confirmation-window escalation | -> `CONFIRMED` or `STALE` | -> `APPLIED` | n/a |

**Closing paragraph.** Across all seven rows above, exactly **one** insertion point -- SS9's
`evaluate_ca_gate()` -- can return `BLOCK`, and it does so only for new *entries* (Design
Principle 3: no `is_exit` parameter exists). SS11.2 (Copy1) and SS11.3 (Copy3) are *corrections*
to existing state, not gates -- they never prevent an order from being placed; they make the
state an order would be evaluated against accurate. SS11.6 (kill-switch) never blocks a trading
cycle by itself -- it only annotates a `kill_reason` that `_evaluate()` would have produced
anyway. SS11.4 (reconciler audit) *re-classifies* a gap's kind for a future severity
classifier's benefit; it never blocks. This concentration of all blocking logic into a single,
narrowly-scoped, entry-only gate is deliberate: it means a CA-related trading pause is always
visible at one call site (SS9), never as a side effect buried in a price-adjustment or
audit-logging code path. Finally, as stated in SS2 (Principle 6) and re-confirmed here: **zero
existing public signatures change** anywhere in SS11 -- `PositionTracker.on_fill`,
`TrailingStopManager.update_price`/`open`/`close`, `Position`, `AuditLog`,
`ReconciliationResult.gap()`, `_audit_position_change()`, and `universe.py`'s exports are all
unchanged; every item in this section is a new method, a new nullable column, or a new table.

---

## 12. Logging Conventions

All logging in `backend/data/corporate_actions.py` uses
`logger = logging.getLogger("backend.data.corporate_actions")` (module-qualified, matching
`STALE_DATA_DETECTOR.md`'s convention of one logger per module rather than per class).

- **`CorporateActionEvent.summary_line()`** -- a `__str__`-style helper (referenced in SS3.2)
  returning a single human-readable line, e.g. `"AAPL SPLIT ratio=0.5 ex_date=2026-06-10
  status=CONFIRMED confidence=1.0 source=yfinance"`. Every state-transition log call
  (`record_event`, `apply_adjustment`, `sweep_stale_events`) logs this line at the appropriate
  level rather than a raw `repr()`, so log greps for a symbol surface a consistent format.
- **`record_event()`**: `INFO` on a new `DETECTED` event; `INFO` on promotion to `CONFIRMED`
  (includes `summary_line()` plus the confirmation source/count); `DEBUG` for a duplicate
  detection that doesn't change `status` (the common case once a signature repeats every
  reconciler cycle until confirmed).
- **`apply_adjustment()`**: `INFO` on success, logging the full `AdjustmentResult` (`symbol`,
  `event.summary_line()`, `copy1_applied`/`copy2_applied`/`copy3_applied`, `audit_log_id`).
  **Error-before-raise dual-logging**: on a `CorporateActionError` at any step (SS10 step 8),
  the processor logs at `ERROR` with `exc_info=True` *before* re-raising -- so the error is
  captured in the application log even if the caller (`process_corporate_actions`) catches and
  swallows it for a later retry (mirrors the audit's observation that `_fire_kill_switch_alert`'s
  try/except-per-channel pattern logs each failure individually rather than relying on a single
  top-level handler).
- **`sweep_stale_events()`**: every event newly transitioned to `STALE` is logged at **`ERROR`**
  (not `WARNING`) -- per the Fallback Policy, a `STALE` event represents an *unresolved*
  detection that will now `BLOCK` new entries (SS9) and requires human attention; `ERROR` ensures
  it surfaces in alerting pipelines that filter on level, consistent with `STALE_DATA_DETECTOR.md`'s
  treatment of its most severe state transitions.
- **`check_yfinance_events()`**: `DEBUG`-level detail dump of the raw `actions=True` /
  `.splits` / `.dividends` frames per symbol (high volume, opt-in via log level); `INFO` only
  when a new event is detected and handed to `record_event()`.
- **Gate decisions (`evaluate_ca_gate`)**: the gate function itself does not log (it is a pure
  function, SS2 Principle 7) -- the caller (strategy entry path) is responsible for logging a
  `BLOCK`/`ALLOW_WITH_LOG` decision at the point where it affects an order, consistent with how
  `STALE_DATA_DETECTOR.md`'s `evaluate_gate()` is also caller-logged.

---

## 13. Compatibility & Reconciliation

### 13.1 Zero-signature-change compatibility table

| Existing type / function | Location | Change |
|---|---|---|
| `Fill` (dataclass) | `position_tracker.py:16-23` | None |
| `Position` (brokers dataclass) | `brokers/models.py` | None |
| `Position` (ORM model) | `database/models.py:78-89` | **Additive**: 3 new nullable columns (SS11.4) |
| `PositionStop` (dataclass) | `risk/engine.py:58-79` | None (rescaled via new `apply_ca_adjustment`, SS11.3) |
| `TrailingStopManager` | `risk/engine.py:82-137` | **Additive**: 1 new method (`apply_ca_adjustment`) |
| `PositionTracker` | `position_tracker.py:26-133` | **Additive**: 1 new method (`apply_ca_adjustment`) |
| `ReconciliationResult.gap()` | `reconciler.py` | None (new gap *kind string*, not a new field) |
| `_audit_position_change()` | `reconciler.py:405-418` | None to signature; new `event_type` *value* + additive `detail` keys |
| `AuditLog` (ORM model) | `database/models.py:127-136` | None (reused, see 13.3) |
| `universe.py` exports | `quant/data/universe.py` | None (consult-only, SS11.5) |
| `OrderStateMachine`, `Order`, `Trade`, `Fill` ORM rows | various | None |

Every row above is either "None" or "Additive" -- no existing parameter list, return type,
dataclass field, or column is removed, renamed, or retyped. This satisfies Design Principle 6
and the audit's SS7 ownership-boundary requirement that this module not destabilize any existing
caller.

### 13.2 Backtest / `SimulatedBroker` -- CA-13 no-op

`process_corporate_actions()` (SS8.3) begins with:

```python
def process_corporate_actions(processor, held_symbols, now) -> list["AdjustmentResult"]:
    if not getattr(processor.broker, "is_live", True):
        return []
    ...
```

This is a **direct structural mirror** of `_is_bar_stale` (`backend/strategy/base.py:80-101`,
specifically line 83: `if not getattr(self._broker, "is_live", True): return False`). The
rationale stated in SS1/SS13 of `STALE_DATA_DETECTOR.md` for that guard applies identically here:
a `SimulatedBroker` has no `is_live` attribute set to `False` by default (the `getattr` default
of `True` means *unset* => treated as live, so a broker that forgets to declare `is_live` fails
*safe* by still running CA checks -- harmless on a backtest since SS1 already establishes that
within one `auto_adjust=True` fetch, Copy1=Copy0=Copy3 are internally consistent and no
ratio-signature will ever match `match_split_signature`, SS5.1, because `db_qty == broker_qty`
identically in a backtest). The explicit `is_live is False` check is therefore a
performance/clarity short-circuit, not a correctness requirement -- but it is included because
SS1 mandates an *explicit* CA-13 statement, not an implicit one relying on SS5.1's algebra.

### 13.3 AuditLog reuse decision + fragmented-mechanism reconciliation

**Decision: reuse `AuditLog` (`database/models.py:127-136`); do not create a new
`CorporateActionLog` table.** `AuditLog`'s existing columns -- `id, event_type, symbol,
order_id, actor, detail (JSON), created_at` -- already cover every field SS5 (Adjustment Audit
Log) of the task requires: `timestamp` = `created_at`, `symbol` = `symbol`, `event` =
`detail["ca_event_type"]` + `detail["ex_date"]`, `adjustment method` = `detail["ratio"]` +
`detail["source"]` + `copy1/2/3_applied` flags (SS11.4). Introducing a parallel
`CorporateActionLog` table would (a) violate Design Principle 6's additive-only posture for no
real benefit, (b) fragment the audit trail across two tables for what is, from an operator's
view, the same "why did this position's numbers change" question that `_audit_position_change`
already answers for reconciler-driven fixes, and (c) require its own migration, indexes, and
retention policy. The **new** schema objects this design *does* introduce -- the 3 `Position`
columns (SS11.4) and the `symbol_mappings` table (SS11.5) -- are not logs; they are queryable
*current-state* fields/lookups that `AuditLog`'s append-only `detail` JSON cannot efficiently
serve (e.g. "what is this position's current adjustment factor" needs an indexed column, not a
JSON-log scan).

**Reconciliation of fragmented mechanisms** -- restating, for each of the "three independent
copies" components named in the audit, exactly how this design relates to it:

| Mechanism | Audit's characterization | This design's relationship |
|---|---|---|
| Reconciler's unconditional `qty_mismatch` fix (`reconciler.py:206-231`) | CA-03 -- silent, no severity check | **Augmented**, not replaced: non-signature path (the overwhelming majority of mismatches) is byte-for-byte unchanged (SS11.2) |
| `_audit_position_change()` | CA-04 -- generic `db_qty`/`broker_qty` payload only | **Augmented**: new `event_type` value + additive `detail` keys (SS11.4); existing call sites/signature unchanged |
| `TrailingStopManager`/`PositionStop` (Copy3) | CA-02 -- independent, never reconciled | **Augmented**: new `apply_ca_adjustment()` method (SS11.3); existing `update_price`/`open`/`close` unchanged |
| `PersistentLossTracker._evaluate()` | CA-01 -- no CA awareness, possible false-fire | **Augmented**: `kill_reason` annotation only (SS11.6); pass/fail logic unchanged |
| `universe.py` | CA-08/CA-11 -- no rename/delisting awareness | **Unchanged**: `resolve_symbol()`/`symbol_mappings` sit *alongside* it (SS11.5), consult-only |
| `RECONCILIATION_ENGINE.md`'s severity/`EmergencyStop` model | CA-03 -- unimplemented | **Unaffected** by this design beyond reserving the `"qty_mismatch_corporate_action"` gap-kind string (SS11.4/SS11.6) for whenever it *is* implemented |

No mechanism in this table is *replaced*; every one is either augmented with new, optional
behavior or left entirely untouched. This is the practical expression of Design Principle 8
("single unified owner that closes the gap" -- by *coordinating* the existing mechanisms via a
shared `CorporateActionEvent`/`AdjustmentResult` vocabulary, not by subsuming or rewriting them).

---

## 14. Testing Plan

New file `tests/data/test_corporate_actions.py` -- **pure pytest, no network calls** (yfinance
calls in `check_yfinance_events` are mocked/monkeypatched; no live KIS/pykrx calls). 13 named
tests, mirroring `STALE_DATA_DETECTOR.md`'s "one test per design decision" philosophy:

| # | Test name | Verifies |
|---|---|---|
| 1 | `test_corporate_action_error_is_distinct_exception_type` | `CorporateActionError` is not a `RuntimeError`/`ValueError` subclass (SS2 Principle 5); carries `symbol`, `event`, `stage` attributes |
| 2 | `test_event_post_init_validation_per_type` | `__post_init__` (SS3.2) raises `CorporateActionError(stage="validation")` for each required-field violation: SPLIT/REVERSE_SPLIT without `ratio`, DIVIDEND without `cash_amount`, TICKER_CHANGE/MERGER/SPINOFF without `new_symbol`; passes for each valid construction |
| 3 | `test_match_split_signature_positive` | `match_split_signature` (SS5.1) detects `db_qty=10, broker_qty=20` as `AdjustmentRatio(raw_ratio=2.0, matched_candidate=2, is_signature=True, implied_type=SPLIT)` within `ratio_tolerance_pct` |
| 4 | `test_match_split_signature_reverse_and_negative` | reverse-split case (`broker_qty=5, db_qty=10` -> `implied_type=REVERSE_SPLIT`); **negative case**: an arbitrary `qty_diff` not near any `split_ratio_candidates` (e.g. `db_qty=10, broker_qty=13`) returns `is_signature=False` |
| 5 | `test_compute_adjusted_values_preserves_portfolio_value` | for each of the 3 `fractional_share_policy` values (SS5.2), `new_qty * new_avg_price` (plus any tracked `remainder` for round-down) `== qty * avg_price` within float tolerance |
| 6 | `test_rescale_position_stop` | `rescale_position_stop` (SS5.3) divides `entry_price`/`peak_price`/`trailing_stop`/`hard_stop` by `ratio`, multiplies `qty` by `ratio`, and leaves `trailing_stop_pct`/`entry_date`/`symbol` **unchanged** -- the exact mistake flagged at line ~1201 |
| 7 | `test_apply_adjustment_all_or_nothing_and_idempotent_retry` | a forced failure at step 5 (Copy3) leaves `event.status == CONFIRMED` (not advanced to `APPLIED`); a second `apply_adjustment` call on the same event, with Copy2 already adjusted, does not double-adjust Copy1 (the double-adjust-on-retry caveat from SS10 step 8) |
| 8 | `test_evaluate_ca_gate_decision_matrix` | every row of SS9's decision matrix (no pending -> `ALLOW`; only `DETECTED` -> `ALLOW_WITH_LOG`; `CONFIRMED` -> `BLOCK` iff `block_entries_on_unresolved_ca`; `STALE` -> `BLOCK` unconditionally; recent `APPLIED` -> `ALLOW_WITH_LOG`; `REJECTED` -> `ALLOW`); confirms `evaluate_ca_gate` has **no `is_exit` parameter** (signature introspection) |
| 9 | `test_sweep_stale_events` | an event with `status in (DETECTED, CONFIRMED)` older than `max_pending_age_hours` is promoted to `STALE` and logged at `ERROR`; an event younger than the threshold is untouched |
| 10 | `test_check_yfinance_events_us_only` | `check_yfinance_events(held_symbols)` (SS8.2, mocked yfinance client) skips KR symbols entirely (CA-06) and only issues calls for US symbols |
| 11 | `test_symbol_mapping_rename_resolution` | `register_symbol_mapping("OLD", "NEW", TICKER_CHANGE, date)` followed by `resolve_symbol("OLD") == "NEW"`; chained rename `A->B->C` resolves `resolve_symbol("A") == "C"`; duplicate `old_symbol` with a different `new_symbol` raises `CorporateActionError(stage="validation")` (CA-08) |
| 12 | `test_dividend_event_is_audit_only_no_position_mutation` | a `DIVIDEND` `CorporateActionEvent` produces an `AdjustmentResult` with `copy1_applied`/`copy2_applied`/`copy3_applied` all reflecting **no qty/avg_price change** -- only `cash_amount` is recorded in the audit `detail` (CA-10 regression guard: a future change must not accidentally start mutating positions for dividends) |
| 13 | `test_process_corporate_actions_noop_on_simulated_broker` | `process_corporate_actions(processor, held_symbols, now)` returns `[]` immediately, with zero calls to `check_reconciler_signature`/`check_yfinance_events`, when `processor.broker.is_live is False` (CA-13) |

All 13 tests construct `CorporateActionConfig`/`DEFAULT_CA_CONFIG`, `CorporateActionEvent`,
`AdjustmentRatio`, and `AdjustmentResult` directly (SS3-SS4) and use lightweight fakes for
`PositionTracker`/`TrailingStopManager`/`db_factory` -- consistent with
`STALE_DATA_DETECTOR.md`'s test-file conventions (no DB, no broker, no network).

---

## Future Work

This design intentionally leaves the following items for subsequent implementation tasks --
listed here so none are silently dropped:

1. **Implement `backend/data/corporate_actions.py`** -- the actual module: all of SS3-SS10's
   dataclasses, enums, functions, and the `CorporateActionProcessor` class, plus
   `tests/data/test_corporate_actions.py`'s 13 tests (SS14).
2. **DB migration** for the 3 new nullable `Position` columns (`adjustment_factor`,
   `corporate_action_type`, `ex_date`, SS11.4) and the new `symbol_mappings` table (SS11.5).
3. **Wire SS11.1-11.6 into the live codebase**: the `reconciler.py:206-231` branch, the new
   `PositionTracker.apply_ca_adjustment`/`TrailingStopManager.apply_ca_adjustment` methods, and
   the `risk/engine.py:248-275` `kill_reason` annotation.
4. **Add a lock to `TrailingStopManager`** (`risk/engine.py:82-137`) -- flagged as an open issue
   in SS10 step 2 and SS11.3; today's single-writer assumption for
   `apply_ca_adjustment`/`update_price` concurrency is documented but not enforced.
5. **Double-adjust-on-retry fix** (SS10 step 8 caveat): implement the "derive Copy1's target
   from Copy2's already-adjusted row, detected via `adjustment_factor`" retry-safety logic.
6. **Periodic watchdog worker** that calls `process_corporate_actions()` and
   `sweep_stale_events()` on a schedule (e.g. alongside the existing reconciler poll loop) --
   this design specifies the functions but not their scheduling.
7. **Concrete `AdjustmentHook` implementations** (SS10) -- e.g. a Telegram hook mirroring
   `bot/notifier.py`'s existing alert channel; only the `Protocol` interface is designed here.
8. **Resolve CA-06** -- empirically verify whether `pykrx.get_market_ohlcv_by_date()`
   (`loader.py:107-125`) returns split-adjusted or raw KR prices; this is a data-verification
   task, a prerequisite for any future `_adjust_kr_ohlcv` (SS11.1).
9. **CA-10 remains observability-only by design** (SS5.4, SS14 test 12) -- no automatic
   correction of cash balances for dividend events is planned; restated here as an explicit
   non-goal rather than a gap.
10. **CA-12 remains open** -- no corporate-action event source beyond yfinance (US-only) exists;
    coordination with a future price-spike validator (TASK 3-2B-equivalent) is deferred until
    such a source exists.
11. **`RECONCILIATION_ENGINE.md`'s full severity/`EmergencyStop` model** remains unimplemented;
    this design only reserves the `"qty_mismatch_corporate_action"` gap-kind string (SS11.4) and
    states the constraint it must satisfy (SS11.6) once built.
12. **Backtest "CA injection" test harness** -- a future tool to inject a synthetic
    `CorporateActionEvent` into a `SimulatedBroker` run, for validating SS10's algorithm against
    historical real-world splits/spinoffs without requiring a live brokerage account; explicitly
    out of scope for SS14's unit-test suite (which is pure pytest with no simulation harness).
