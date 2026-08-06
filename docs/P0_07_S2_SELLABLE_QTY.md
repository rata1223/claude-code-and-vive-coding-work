# P0-07 S2 — Sellable Quantity (authoritative contract)

**Implemented on:** `main` @ `1dddb0d` (after S1), which includes G2 and P0-07C close-position.
**Supersedes:** the "S2 — no sellable-qty concept anywhere" gap recorded in `docs/P0_07_CLOSE_POSITION_AUDIT.md` §5. That audit is a dated snapshot and is left unmodified; this document is the current authority.

---

## The problem

Every sell path treated the **held** quantity as if it were immediately sellable — `hldg_qty` for KR, `ovrs_cblc_qty` for US. A grep for `ord_psbl_qty` / `sellable` / `매도가능` across the repository returned **zero hits**.

Held is not sellable. Shares can be unsettled, or already committed to a resting sell order. Asking the broker for more than it will actually let us sell gets the order rejected — and in an emergency that rejection is indistinguishable from "we liquidated".

The data was already there: `kis_adapter/portfolio.py` returns KIS `output1` rows **passed through unchanged**, so KIS's own 주문가능수량 (`ord_psbl_qty`) sat in the payload the whole time. Nothing read it.

## The rule

```
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
| Broker reports no orderable figure | **BLOCK** | falls back to `held`, audited `emergency_flatten_sellable_unknown` |
| `sellable < held` | request must fit within `sellable` | submits `sellable`, reports the shortfall in `failed`, audited `emergency_flatten_partial_sellable` |

EmergencyFlatten is the only path that falls back, so a KIS field change can never freeze the last-resort liquidation — mirroring S1, where flatten is the one halt-immune path. Its shortfall behaviour prefers a partial liquidation using the broker's own number over an all-or-nothing order that gets rejected and reads like success. G2 pricing is untouched.

## Where it is enforced

| Path | Quantity authority |
|---|---|
| Worker strategy exit | `prove_exit` (`backend/risk/halt_policy.py`) proves against sellable, not `pos.qty` |
| QuickTrade `close-position` | `_live_sellable()` + `_open_sell_qty()`; "close all" means all *sellable* |
| QuickTrade `place-order` (sell) | `validate_sell_qty` before reserving. Buys unaffected |
| EmergencyFlatten | `_sellable_for()` per position |

`Position.sellable_qty` carries the figure. `None` means *the broker reported none* → block. The KIS adapter parses `ord_psbl_qty` (capped by held); adapters that model no settlement or reservation — the paper broker and the in-memory tracker — state `sellable == held` at the **read** boundary, because `qty` is mutated in place as fills arrive and a value stamped at construction would go stale.

## Pending sells

QuickTrade subtracts quantity already committed to its own open sells, read from the durable `quick_trade_orders` rows (`side='sell'`, status `reserved`/`submitted`), so the figure survives a restart. Terminal statuses (`rejected`/`failed`/`blocked`) have released their quantity.

The row a request would **replay is excluded** from that sum. Without that exclusion the first close reserves the quantity and the identical retry — which submits nothing and just returns the existing order — looks like a second ask and is refused. `broker_nets_pending=True` is available for a broker whose orderable figure already excludes resting orders, so it is not subtracted twice.

## Not changed

S1 halt-vs-exit policy (entries still blocked under every cause; `UNTRUSTED_STATE` still blocks a fully sellable exit; unhalted trading still does no position lookup), G2 flatten pricing, `/history`, QuickTrade reconciliation liveness and sweep loops, KIS pagination and credential/auth architecture, order persistence and idempotency transaction semantics, broker adapter / risk engine redesign, frontend.
