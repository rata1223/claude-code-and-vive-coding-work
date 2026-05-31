# KIS US-Only Refactor — Migration Design

> **Status**: Planning complete. Implementation not started.
> **Audit basis**: TASK 1-3A full codebase audit (2026-05-31)

---

## Summary

The architectural intent is:
- **KIS** = US overseas trading only
- **Kiwoom** = KR domestic trading only

This intent is currently violated at every layer. KIS handles both KR and US. `kiwoom_adapter/` is 60% implemented but never called. This document records the current violation state and the staged migration plan.

---

## 1. Current Structure

```
API Routers (quick_trade.py, dashboard.py)
        │ _load_kis() — always KIS, never Kiwoom
        ▼
kis_adapter/orders.py      buy_kr, sell_kr, buy_us, sell_us, cancel_us
kis_adapter/portfolio.py   get_kr_balance, get_us_balance
kis_adapter/market_data.py get_price_kr, get_price_us
        │
        ▼
backend/brokers/kis.py     KISBroker — ALL 6 BrokerAdapter methods branch on _is_kr()
backend/brokers/kiwoom.py  KiwoomBroker — ALL 6 methods raise NotImplementedError

        ▼
BrokerAdapter ABC (base.py) — market-agnostic ✓

        ▼
Execution layer (order_poller, reconciler, order_machine) — broker-agnostic ✓
Worker (runner.py) — hard-coded get_kis_broker()
Bot (bot/main.py) — direct KIS adapter instantiation
```

### KIS KR-Specific Methods in Production Use

| File | Method | TR_ID | Active Call Sites |
|---|---|---|---|
| `kis_adapter/orders.py:59` | `buy_kr()` | TTTC0802U / VTTC0802U | quick_trade.py:139, bot/main.py:206 |
| `kis_adapter/orders.py:71` | `sell_kr()` | TTTC0801U / VTTC0801U | quick_trade.py:141,184; bot/main.py:105,192,230 |
| `kis_adapter/portfolio.py:42` | `get_kr_balance()` | TTTC8434R / VTTC8434R | quick_trade.py:62,105; dashboard.py:69; bot/main.py:52,82,187,224,254 |
| `kis_adapter/market_data.py:26` | `get_price_kr()` | FHKST01010100 | kis.py:81,273; bot/main.py:75,186,203,229 |
| `backend/brokers/kis.py:186` | `_get_kr_order_status()` | TTTC8036R / VTTC8036R | kis.py:184 (internal) |
| `backend/brokers/kis.py:153` | KR cancel path | TTTC0803U / VTTC0803U | kis.py:137 (internal) |

### Kiwoom Adapter — Implemented but Unwired

| Component | Status |
|---|---|
| `kiwoom_adapter/client.py` | REST HTTP client, Linux/Docker compatible |
| `kiwoom_adapter/orders.py` | `buy_kr`, `sell_kr` — missing `cancel_kr`, `get_order_status` |
| `kiwoom_adapter/portfolio.py` | `get_kr_balance()` implemented |
| `kiwoom_adapter/market_data.py` | `get_price_kr()` implemented |
| `kiwoom_adapter/client.py` | `is_paper` flag accepted but never used — no TR_ID switching |
| `backend/brokers/kiwoom.py` | All 6 methods `NotImplementedError`; COM comment is outdated (adapter is REST) |

---

## 2. Target Structure

```
API Routers
        │
        ▼
backend/brokers/factory.py          ← NEW — broker dispatch layer
    market="kr" + cred → KiwoomBroker
    market="us" + cred → KISBroker
        │                   │
        ▼                   ▼
KiwoomBroker           KISBroker
(KR-only)              (US-only, no _is_kr)
        │                   │
        ▼                   ▼
kiwoom_adapter/        kis_adapter/
 buy_kr, sell_kr,       buy_us, sell_us,
 cancel_kr,             cancel_us,
 get_order_status,      get_us_balance,
 get_kr_balance,        get_price_us
 get_price_kr
```

---

## 3. Migration Plan

Migration is staged into 5 phases. Phases 0 and 1 are purely additive (no risk). Phases 2–4 are gated behind a feature flag. Phase 5 is a DB cleanup run after everything is stable.

### Phase 0 — Kiwoom Adapter Completion *(additive, no risk)*

**Prerequisite for all subsequent phases.**

| Task | File | Detail |
|---|---|---|
| Add `cancel_kr(order_id)` | `kiwoom_adapter/orders.py` | Kiwoom order cancellation endpoint |
| Add `get_order_status(order_id)` | `kiwoom_adapter/orders.py` | Order-list inquiry TR_ID polling |
| Wire `is_paper` flag | `kiwoom_adapter/client.py` | Conditional TR_IDs like KIS paper mode |
| Verify + update `KiwoomDomesticMapper` | `backend/brokers/semantic_mapper.py` | Replace TBD placeholders with real Kiwoom field names |

### Phase 1 — KiwoomBroker Implementation *(additive, no risk)*

Implement all 6 `BrokerAdapter` methods in `backend/brokers/kiwoom.py`. Remove outdated COM comment. Add `get_kiwoom_broker()` singleton.

| Method | Delegates To |
|---|---|
| `get_balance()` | `kiwoom_adapter/portfolio.get_kr_balance()` |
| `get_positions()` | parse from `get_kr_balance()` response |
| `place_order()` | `buy_kr()` / `sell_kr()` |
| `cancel_order()` | `cancel_kr()` |
| `get_order_status()` | `kiwoom_adapter/orders.get_order_status()` |
| `get_price()` | `get_price_kr()` |

No production path calls KiwoomBroker yet — zero risk.

### Phase 2 — Broker Dispatch Layer *(additive + feature-flagged)*

**New file**: `backend/brokers/factory.py`

```python
import os
from backend.brokers.base import BrokerAdapter

def get_broker_for_market(market: str, cred) -> BrokerAdapter:
    kr_broker = os.getenv("KR_BROKER", "kiwoom")  # set "kis" to rollback
    if market.lower() == "kr" and kr_broker == "kiwoom":
        return get_kiwoom_broker_from_cred(cred)
    return get_kis_broker_from_cred(cred)
```

Update call sites:
- `api/routers/quick_trade.py`: replace `_load_kis(cred)` with `factory.get_broker_for_market(body.market, cred)`
- `api/routers/dashboard.py`: same pattern

Add deprecation warnings to KIS KR paths (`logger.warning("DEPRECATED: KR through KIS")`).

**Rollback**: `KR_BROKER=kis` in `.env` → instant revert, no code change, no restart needed beyond env reload.

### Phase 3 — Bot and Worker Migration *(medium risk)*

**`bot/main.py`**: Replace direct `KISOrders`, `KISPortfolio`, `KISMarketData` instantiation with injected `kr_broker: BrokerAdapter` and `us_broker: BrokerAdapter` parameters. `TradingEngine.__init__` receives both via factory.

**`backend/worker/runner.py`**: Replace `get_kis_broker()` singleton with factory call. Build KIS (US) + Kiwoom (KR) broker instances. Derive fill `market` field from position record, not from symbol-length heuristic.

**Gate**: Paper mode only. Deploy during market-closed window. Run full KR + US session before enabling live.

### Phase 4 — KIS KR Removal *(destructive, irreversible without revert)*

Only after Phase 3 has run ≥ 1 week in paper mode without issues.

**Before starting**: `git tag pre-kr-removal` for fast revert.

| File | Action |
|---|---|
| `backend/brokers/kis.py` | Remove `_is_kr()`, `_get_kr_order_status()`, KR cancel path, KR branches in all 6 methods |
| `kis_adapter/orders.py` | Remove `buy_kr()`, `sell_kr()`, KR TR_IDs |
| `kis_adapter/portfolio.py` | Remove `get_kr_balance()`, KR part of `get_total_asset_krw()` |
| `kis_adapter/market_data.py` | Remove `get_price_kr()` |
| `api/schemas.py` | Add validator: reject `market="kr"` for KIS-only credentials |

Low-priority cleanup (backtest/simulation files — non-breaking):
- `backend/quant/backtest/engine.py:26,122`
- `backend/quant/risk/position_sizer.py:35`
- `scripts/test_connection.py:58`

### Phase 5 — Database Cleanup *(low risk)*

Run between sessions when positions are flat.

```sql
-- Forward migration
UPDATE orders    SET broker = 'kiwoom' WHERE market = 'KR' AND broker = 'kis';
UPDATE trades    SET broker = 'kiwoom' WHERE market = 'KR' AND broker = 'kis';
UPDATE positions SET broker = 'kiwoom' WHERE market = 'KR' AND broker = 'kis';

-- Rollback migration
UPDATE orders    SET broker = 'kis' WHERE market = 'KR' AND broker = 'kiwoom';
UPDATE trades    SET broker = 'kis' WHERE market = 'KR' AND broker = 'kiwoom';
UPDATE positions SET broker = 'kis' WHERE market = 'KR' AND broker = 'kiwoom';
```

---

## 4. Compatibility Plan

The API contract does not change: clients continue sending `market="kr"` / `market="us"`. Dispatch is internal.

### Rollback Mechanism by Phase

| Phase | Rollback | Time |
|---|---|---|
| 0–1 | N/A (additive) | — |
| 2–3 | Set `KR_BROKER=kis` in `.env`, restart containers | Seconds |
| 4 | `git revert` to `pre-kr-removal` tag | 10–15 min |
| 5 | Run reverse migration SQL | Minutes |

### Dual-Credential Period (Phases 2–3)

If `cred.exchange_id == "kiwoom"` → always Kiwoom regardless of `KR_BROKER` env.
If `cred.exchange_id == "kis"` → always KIS.
This allows per-user broker selection during paper testing.

---

## 5. Ownership Transfer Map

| Capability | Current Owner | Target Owner |
|---|---|---|
| KR buy / sell | KIS | Kiwoom |
| KR cancel | KIS | Kiwoom |
| KR order status | KIS | Kiwoom |
| KR balance & positions | KIS | Kiwoom |
| KR price quotes | KIS | Kiwoom |
| KR status normalization | `KISDomesticMapper` *(wrong)* | `KiwoomDomesticMapper` |
| US buy / sell / cancel | KIS | KIS (no change) |
| US balance, positions, quotes | KIS | KIS (no change) |

---

## 6. Risk Register

| ID | Risk | Severity | Mitigation |
|---|---|---|---|
| R1 | Kiwoom paper flag unwired → live endpoint hit | HIGH | Phase 0 must wire `is_paper` before Phase 1 |
| R2 | `KiwoomDomesticMapper` field names unverified → all statuses UNKNOWN | HIGH | Phase 0 verifies against Kiwoom API response |
| R3 | `cancel_kr()` missing → open KR orders uncancel-able | HIGH | Phase 0 adds cancel_kr |
| R4 | Bot disruption during Phase 3 | HIGH | Deploy during market-closed window, paper only |
| R5 | `_is_kr()` duplicated (kis.py, runner.py, semantic_mapper.py) | MEDIUM | Consolidate to `semantic_mapper.market_for_symbol()` in Phase 0 |
| R6 | DB migration on live positions | MEDIUM | Phase 5 runs only when positions are flat |
| R7 | `credential.exchange_id` phantom field confuses operators | MEDIUM | Factory explicitly logs which broker is selected per request |
| R8 | Phase 4 before Phase 3 is stable | HIGH | Enforce ≥ 1 week paper run gate |

---

## 7. Remaining Work Checklist

### Phase 0 (Kiwoom Adapter)
- [ ] `kiwoom_adapter/orders.py` — add `cancel_kr(order_id)`
- [ ] `kiwoom_adapter/orders.py` — add `get_order_status(order_id)`
- [ ] `kiwoom_adapter/client.py` — wire `is_paper` → TR_ID switching
- [ ] `backend/brokers/semantic_mapper.py` — verify and update `KiwoomDomesticMapper` field names

### Phase 1 (KiwoomBroker)
- [ ] `backend/brokers/kiwoom.py` — implement all 6 BrokerAdapter methods
- [ ] `tests/brokers/test_kiwoom_broker.py` — unit tests

### Phase 2 (Dispatch)
- [ ] `backend/brokers/factory.py` — create broker dispatch factory
- [ ] `api/routers/quick_trade.py` — replace `_load_kis()` with factory
- [ ] `api/routers/dashboard.py` — replace direct KIS calls with factory
- [ ] Add `KR_BROKER` to `.env.example`

### Phase 3 (Bot + Worker)
- [ ] `bot/main.py` — inject `kr_broker` / `us_broker` BrokerAdapter
- [ ] `backend/worker/runner.py` — replace `get_kis_broker()` with factory

### Phase 4 (KIS KR Removal — gate: 1 week paper)
- [ ] `git tag pre-kr-removal`
- [ ] `backend/brokers/kis.py` — strip KR paths
- [ ] `kis_adapter/orders.py` — remove KR methods
- [ ] `kis_adapter/portfolio.py` — remove KR methods
- [ ] `kis_adapter/market_data.py` — remove KR methods
- [ ] `api/schemas.py` — enforce KIS=US-only at schema level

### Phase 5 (DB Cleanup — gate: positions flat)
- [ ] Write + test forward migration SQL
- [ ] Write + test reverse migration SQL
- [ ] Execute forward migration

---

## 8. Verification

```bash
# After Phase 1:
pytest tests/brokers/test_kiwoom_broker.py -v

# After Phase 2:
KR_BROKER=kiwoom pytest tests/api/test_quick_trade.py -v
# KR orders → KiwoomBroker; US orders → KISBroker

# After Phase 4:
grep -rn "buy_kr\|sell_kr\|get_kr_balance\|get_price_kr\|_is_kr" \
  backend/brokers/kis.py kis_adapter/ --include="*.py"
# Expected: 0 matches

# After Phase 5:
SELECT COUNT(*) FROM orders WHERE market='KR' AND broker='kis';
# Expected: 0
```
