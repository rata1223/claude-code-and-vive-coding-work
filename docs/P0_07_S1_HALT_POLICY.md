# P0-07 S1 — Halt-vs-Exit Policy (authoritative contract)

**Decision:** `ENTRY HALT + EXIT ALLOWED` (Policy B), with EmergencyFlatten immune.
**Implemented on:** `main` @ `d773389`
**Supersedes:** the "S1 — Kill switch vs. exits contradiction" open question in
`docs/P0_07_CLOSE_POSITION_AUDIT.md` §5. That audit is a dated snapshot and is left
unmodified; this document is the current authority.

---

## The invariant

> A halt stops the creation of NEW risk. It must not remove the system's ability to
> reduce risk it already holds — EXCEPT when the halt exists because position state
> itself is untrusted.

Before S1 every halt was a single boolean and every gate blocked buys and sells
identically (`_live_trade_allowed` checked only `SAFE_MODE.can_trade`). An MDD breach
therefore froze the book at maximum drawdown: the control that fires to stop losses
also disabled the stop-losses, leaving a manual `/api/admin/flatten` as the only exit.

## Halt causes

| Cause | Meaning | Set by |
|---|---|---|
| `UNTRUSTED_STATE` | Position/runtime state unreliable — startup recovery incomplete, reconciliation failure | `SafeModeState()` default; any `disable()` without an explicit cause |
| `RISK_BREACH` | A risk limit fired; state is trustworthy, the exposure is the problem | `LossTracker._fire_kill_switch_alert`; QuickTrade's Redis halt flag |
| `DEGRADED_FEED` | Market data critically stale | reserved for the freshness path |

`disable(reason)` keeps its single-argument form and defaults to `UNTRUSTED_STATE` —
the most restrictive cause — so an un-migrated caller can never widen what is permitted.

## Operation classes

| Class | Definition |
|---|---|
| `ENTRY` | A buy, **or any order that cannot be proven to reduce exposure** |
| `EXIT` | A sell proven against a live position: `0 < qty <= held_qty` |
| `EMERGENCY` | `EmergencyFlattenManager.flatten_all` |
| `NON_EXPOSURE` | Cancel |

## Decision matrix

| State | ENTRY | EXIT | EMERGENCY | NON_EXPOSURE |
|---|:---:|:---:|:---:|:---:|
| RUNNING | ALLOW | ALLOW | ALLOW | ALLOW |
| HALT(`RISK_BREACH`) | BLOCK | ALLOW | ALLOW | ALLOW |
| HALT(`DEGRADED_FEED`) | BLOCK | ALLOW\* | ALLOW\* | ALLOW |
| HALT(`UNTRUSTED_STATE`) | BLOCK | **BLOCK** | ALLOW | ALLOW |

\* requires a valid live execution price (P0-07 G2 rules: finite, positive, never
rounded or clamped to pass).

## Rules

- **R1** — an order not *proven* EXIT is ENTRY. `side == "sell"` alone never grants EXIT.
- **R2** — EXIT proof is a **live** position lookup plus `0 < qty <= held_qty`. No cached
  positions, no inference, no clamping (an over-close is rejected, not reduced).
- **R3** — any failure while evaluating the gate (Redis down, position lookup raises,
  missing cause) is a BLOCK, for every class except EMERGENCY.
- **R4** — a BLOCK keeps existing audit/status semantics (`QT_BLOCKED` on the QuickTrade
  path; rejected order + warning on the worker path). No new persistence subsystem.
- **R5** — a halt never recalls an order already accepted by the broker; fill processing
  and reconciliation are not halt-gated.
- **R6** — EmergencyFlatten is never gated by halt state; G2 pricing still applies.

## Where it is enforced

| Path | Gate | Behavior |
|---|---|---|
| Worker strategies | `_live_trade_allowed` (`backend/strategy/base.py`) | Classifies per order; proves exits via `broker.get_positions()`; validates the price under `DEGRADED_FEED` |
| QuickTrade `place-order` | `get_risk_gate` (`api/routers/quick_trade.py`) | Strict ENTRY — unchanged |
| QuickTrade `close-position` | `get_exit_risk_gate` | EXIT — permitted under `RISK_BREACH`; the handler has already proven the exit before reserving |
| EmergencyFlatten | none | Immune by design (R6) |

The decision itself lives in one place: `backend/risk/halt_policy.py` (pure, stateless).

## Deliberately out of scope

Unifying the worker's in-process `SAFE_MODE` with the API's Redis halt flag. They remain
separate stores, so a worker kill switch does not halt QuickTrade and vice versa — a
pre-existing gap recorded in the S1 audit, tracked separately. Consequently
`UNTRUSTED_STATE` is not representable on the QuickTrade path today: its only halt
signal is the Redis flag, whose sole writer is `RiskManager.record_daily_loss`
(a `RISK_BREACH`).
