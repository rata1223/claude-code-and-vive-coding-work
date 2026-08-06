# P0-07 S2 — Sellable Quantity (authoritative contract)

**Implemented on:** `main` @ `1dddb0d` (after S1), which includes G2 and P0-07C close-position.
**Supersedes:** the "S2 — no sellable-qty concept anywhere" gap recorded in `docs/P0_07_CLOSE_POSITION_AUDIT.md` §5. That audit is a dated snapshot and is left unmodified; this document is the current authority.

---

## The problem

Every sell path treated the **held** quantity as if it were immediately sellable — `hldg_qty` for KR, `ovrs_cblc_qty` for US. A grep for `ord_psbl_qty` / `sellable` / `매도가능` across the repository returned **zero hits**.

Held is not sellable. Shares can be unsettled, or already committed to a resting sell order. Asking the broker for more than it will actually let us sell gets the order rejected — and in an emergency that rejection is indistinguishable from "we liquidated".

The data was already there: `kis_adapter/portfolio.py` returns KIS `output1` rows **passed through unchanged**, so KIS's own 주문가능수량 (`ord_psbl_qty`) sat in the payload the whole time. Nothing read it.

## The rule

```text
sellable = max(0, min(broker_orderable, held) - locally_pending_sells)
```

- `broker_orderable` UNKNOWN → **fail closed**; never silently reverts to `held`
- capped by `held` — a broker reporting more orderable than held is inconsistent, so trust the smaller
- floors at 0, never negative
- `requested > sellable` → **blocked, never clamped**; clamping would sell a different quantity than was requested
- nothing is derived from price, notional, or cost basis. The only inputs are counts, and a count that is a `bool`, a string, NaN, infinite, negative or fractional is treated as unreadable rather than coerced

The decision lives in one place: `backend/risk/sellable_qty.py` (pure, stateless, like `halt_policy`).

## Two path-specific rules

| Situation | Every normal sell | EmergencyFlatten |
|---|---|---|
| Broker reports no orderable figure (`CAUSE_UNREPORTED`) | **BLOCK** | falls back to `held`, audited `emergency_flatten_sellable_unknown` |
| Orderable figure unreadable (`CAUSE_UNTRUSTED`) | **BLOCK** | falls back to `held`, audited `emergency_flatten_sellable_untrusted` (logged at ERROR) |
| `sellable < held` | request must fit within `sellable` | submits `sellable`, reports the shortfall in `failed`, audited `emergency_flatten_partial_sellable` |

EmergencyFlatten is the only path that falls back, so a KIS field change can never freeze the last-resort liquidation — mirroring S1, where flatten is the one halt-immune path. Its shortfall behaviour prefers a partial liquidation using the broker's own number over an all-or-nothing order that gets rejected and reads like success. G2 pricing is untouched.

The two kinds of unknown are **audited separately but behave identically on this one path**. They are different failures — a response-shape change versus malformed data — and want different follow-up, so they must not share an event. Neither fails closed here: on the last-resort liquidation, refusing to sell is the worse outcome, and `held` is a real count read from the same broker row, not an invented one. Everywhere else both are a BLOCK.

Flatten resolves the sellable quantity **before** requesting a quote, so a shortfall is still audited when the price lookup also fails, and a zero-sellable position skips the quote entirely.

## Where it is enforced

| Path | Quantity authority |
|---|---|
| Worker strategy exit | `prove_exit` (`backend/risk/halt_policy.py`) proves against sellable, not `pos.qty` |
| QuickTrade `close-position` | `_live_sellable()` + `_open_sell_qty()`; "close all" means all *sellable* |
| QuickTrade `place-order` (sell) | `_live_sellable()` + `_open_sell_qty()` + `validate_sell_qty` before reserving. Buys unaffected |
| EmergencyFlatten | `_sellable_for()` per position |

`prove_exit` carries **no pending term**, deliberately. A duplicate sell on the worker path is prevented one layer up by the per-symbol pending lock (`PositionTracker.try_mark_pending`, claimed in `backend/strategy/indicator/strategy.py` before the sell and released on fill), and the positions it resolves against come from that same tracker, which models no settlement or broker-side reservation and so reports no independent pending figure. Giving a deliberately pure, stateless policy module a pending source would duplicate the lock's job in a second place.

A held position with **zero** orderable is reported as `No sellable quantity`, not `No open position` — telling an operator mid-close that their position is gone is a different and worse answer than "none of it can be sold yet".

`Position.sellable_qty` carries the figure. `None` means *the broker reported none* → block. The KIS adapter parses `ord_psbl_qty` (capped by held); adapters that model no settlement or reservation — the paper broker and the in-memory tracker — state `sellable == held` at the **read** boundary, because `qty` is mutated in place as fills arrive and a value stamped at construction would go stale.

## Pending sells

Both QuickTrade sell paths — `close-position` and a direct `place-order` sell — subtract quantity already committed to their own open sells, read from the durable `quick_trade_orders` rows, so the figure survives a restart. Without it, two consecutive full-size sells both pass whenever the broker's orderable figure has not yet reflected the first resting order.

The status filter names the **terminal** states (`rejected`/`failed`/`blocked`) rather than the open ones, so the default is fail-closed: any status the module does not recognise — a renamed constant, or a non-terminal state added later such as a partially-filled one — still counts as holding quantity. Listing the open states instead would make both of those silently report 0 pending, which permits an over-ask.

Symbol and side are matched **case-insensitively in SQL**. Both are persisted verbatim from the request, so `aapl` and `AAPL` produce two rows for one holding; an exact match would sum only some of them and under-report pending.

The sum is scoped to one `credential_id` and `market` — the scope the broker figure it is subtracted from describes. The same ticker can be held in two accounts, or in both KR and US, and counting another account's resting sell against this one would refuse a valid sell.

## Known residual: the check and the reservation are not one atomic step

Both sell paths read the pending quantity, validate, and then call `reserve_and_submit`. Two concurrent sells carrying **different** idempotency keys can read the same pending figure, both pass, and both reserve. The `(user_id, idempotency_key)` unique constraint does not serialise them, because the keys differ.

Not closed here: doing so needs a per-account position lock or a serialisable transaction spanning the read and the insert, which is precisely the "order persistence / idempotency transaction semantics" this task is scoped not to alter. Worth noting what the window actually is — before S2 there was **no** capacity check on either path at all, so this narrows a pre-existing hole rather than opening one, and the remaining exposure is two genuinely simultaneous distinct-key sells. The duplicate-click case, which is the common one, is still fully closed by the derived idempotency key. Fixing it properly belongs with whichever task owns the reservation transaction.

The row a request would **replay is excluded** from that sum. Without that exclusion the first close reserves the quantity and the identical retry — which submits nothing and just returns the existing order — looks like a second ask and is refused. `broker_nets_pending=True` is available for a broker whose orderable figure already excludes resting orders, so it is not subtracted twice.

## Not changed

S1 halt-vs-exit policy (entries still blocked under every cause; `UNTRUSTED_STATE` still blocks a fully sellable exit; unhalted trading still does no position lookup), G2 flatten pricing, `/history`, QuickTrade reconciliation liveness and sweep loops, KIS pagination and credential/auth architecture, order persistence and idempotency transaction semantics, broker adapter / risk engine redesign, frontend.
