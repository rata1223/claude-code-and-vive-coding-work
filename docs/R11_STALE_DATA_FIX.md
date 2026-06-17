# R-11 Fix — Unified Stale-Data Protection

> Status: implemented (Phase M4). Audit ref: `AUDIT.md` R-11, `ROADMAP.md` P1-04.

## 1. Original R-11 cause

`backend/worker/emergency.py:StaleDataWatchdog` was a complete, unit-tested
staleness detector with **zero production call sites** — its only reference was
its own docstring example. The dedicated freshness gate the system was supposed
to have simply never ran.

In its place, staleness was handled by **fragmented, inconsistent, mostly
non-blocking ad-hoc checks**, so stale OHLCV reached signal computation and
stale quotes reached order sizing — silently, with no exception and no alert.

| Pre-fix check | Location | Threshold | Blocked? |
|---|---|---|---|
| `StaleDataWatchdog` | `worker/emergency.py` | 2 h | dead code (never ran) |
| Inline scan gate | `strategy/indicator/strategy.py` | 3 days | yes, but wrapped in `except: pass` |
| `_is_bar_stale` | `strategy/base.py` | 600 s | on_bar only |
| Loader daily check | `quant/data/loader.py` | 26 h | **WARN only — never blocked** |
| `get_price()` sizing | `strategy/indicator/strategy.py` | — | **no check at all** |

## 2. Previous fragmented flow

```text
loader.fetch() ── 26h check → logger.warning(), returns stale df ──┐
                                                                    ▼
_scan_and_trade() ── 3-day gate (except: pass) ─────────► fusion.evaluate(df)  ── may emit signal on stale data
                                                                    │
_execute_buy/sell() ── price = get_price()  (NO freshness check) ──► size & place order
on_bar() ── _is_bar_stale() (600s, dormant path) ─────────► _check_exit()
StaleDataWatchdog ──────────────────────────────────────► (never called)
```

Four different thresholds, two of the most important paths not blocking at all,
and the one dedicated component dead.

## 3. New unified flow

One authoritative gate, one threshold config, fail-closed, kill-switch wired.

```text
                 backend/data/freshness_config.py   ← THE single threshold location (env-driven, per tier)
                            │
                 backend/data/freshness_gate.FreshnessGate   ← process singleton, authoritative gate
                            │ (built on)
                 backend/data/stale_detector.StaleDataDetectionService   ← single source of truth (tier-aware)
                            │
   ┌────────────────────────┼─────────────────────────────┬───────────────────────────┐
   ▼                        ▼                             ▼                           ▼
_scan_and_trade()      _execute_buy/sell()           base._is_bar_stale()       (CRITICAL stale)
validate_dataframe()   assert_tradeable()            validate_timestamp()       → halt_callback
DAILY_BAR tier         DAILY_BAR tier                INTRADAY_BAR tier          → KillSwitch HALTED
skip symbol on block   skip order on block           skip bar on block
```

Key properties:
- **Fail-closed.** Missing timestamp, never-seen feed, or any internal error →
  `UNKNOWN`/`STALE` → blocked (`block_on_unknown=True` by default; an internal
  evaluation error is treated as `STALE`).
- **One threshold location.** `backend/data/freshness_config.py` resolves every
  tier from env vars (`FRESHNESS_*`); defaults preserve prior behaviour
  (intraday stale 600 s, daily stale 72 h, daily warn 26 h). No thresholds are
  hardcoded elsewhere.
- **Tiered.** `DAILY_BAR`, `INTRADAY_BAR`, `INTRADAY_QUOTE` — the same symbol can
  be daily-fresh yet intraday-stale; tracked under tier-scoped keys in the one
  service instance.
- **Structured logging** on every non-fresh decision: `symbol`, `source`,
  `last_timestamp`, `age_seconds`, `state`, `reason`, `blocking`.
- **Kill-switch integration.** STALE (== CRITICAL) fires the gate's halt
  callback. `make_kill_switch_halt_callback(ks)` routes it through
  `KillSwitch.report_watchdog_failure(Severity.CRITICAL, …)` → `HALTED`. (UNKNOWN
  blocks the single symbol but does not halt the whole engine.)

### Order-sizing note
`get_price()` returns a bare float with no timestamp, so the sizing gate
(`assert_tradeable`) re-checks the **symbol's most recently recorded feed
freshness** (recorded microseconds earlier by the daily-bar scan). A symbol whose
feed was never recorded is `UNKNOWN` → blocked. This gives a freshness guarantee
on the sizing path without changing the broker quote API.

## 4. Removed / replaced code

| Removed | Replaced by |
|---|---|
| `StaleDataWatchdog` class (`worker/emergency.py`) | `FreshnessGate` |
| `backend/worker/tests/test_emergency.py` (watchdog-only) | `backend/data/tests/test_freshness_gate.py` |
| 3-day inline gate (`strategy/indicator/strategy.py`) | `_is_data_stale()` → `validate_dataframe()` |
| `_BAR_STALE_SECONDS` constant + manual age math (`strategy/base.py`) | `_is_bar_stale()` → `validate_timestamp()` (INTRADAY_BAR) |
| Loader 26h WARN block + `stale_hours` param (`quant/data/loader.py`) | gate at execution boundary (loader serves backtests too, so it no longer judges freshness) |

`StaleDataError` in `loader.py` is left intact (harmless, never raised; distinct
from `StaleFeedError`).

## 5. New / changed files

- **new** `backend/data/freshness_config.py` — single threshold config (tiers, env).
- **new** `backend/data/freshness_gate.py` — `FreshnessGate`, singleton, kill-switch helper.
- **changed** `backend/data/stale_detector.py` — backward-compatible `tier` support.
- **changed** `backend/strategy/indicator/strategy.py` — scan + sizing gates.
- **changed** `backend/strategy/base.py` — `_is_bar_stale` delegates to gate.
- **changed** `backend/quant/data/loader.py` — removed duplicated 26h logic.
- **changed** `backend/worker/emergency.py` — removed dead watchdog.
- **new** tests: `test_freshness_gate.py`, `test_indicator_freshness.py`.

## 6. Tests

`backend/data/tests/test_freshness_gate.py` (18) +
`backend/strategy/tests/test_indicator_freshness.py` (3) cover: fresh passes;
stale blocks signal generation; stale blocks order sizing; stale blocks execution
(raises `StaleFeedError`); missing timestamp; unknown source; threshold override
(both programmatic and env); fail-closed (block_on_unknown, internal-error →
STALE); kill-switch transition to HALTED; tier isolation. Existing
`test_stale_detector.py` and `test_kill_switch.py` remain green
(backward-compatible).

## 7. Remaining risks

- **Quotes still carry no timestamp.** The sizing gate proxies on the daily-bar
  feed freshness, not the live tick. A broker quote that is itself stale while the
  daily bar is fresh is not directly caught. A follow-up could thread a real quote
  timestamp from `kis_adapter.market_data` into `INTRADAY_QUOTE`.
- **Default daily thresholds tolerate long holidays.** `daily_stale=72h` lets a
  3-day-old bar through to avoid weekend false positives; a market closed >3 days
  (rare holiday clusters) would block legitimately. Tune via
  `FRESHNESS_DAILY_STALE_SECONDS` / a calendar-aware threshold later.
- **FX-rate staleness (ROADMAP P4-04)** is a sibling of R-11
  (`kis.py:_get_fx` returns stale FX with WARN only). Not in scope here; should
  reuse this gate.
- **Backtests are intentionally un-gated** (`is_live` guard), matching the prior
  `_is_bar_stale` contract. A backtest fed accidentally-stale live data would not
  be caught by this gate.
