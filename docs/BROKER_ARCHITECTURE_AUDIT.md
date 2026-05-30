# Broker Architecture Audit

**Document type:** Evidence-based repository audit (read-only analysis)
**Scope:** KIS, Kiwoom, `BrokerAdapter`, order execution, position tracking, portfolio retrieval
**Date:** 2026-05-30
**Method:** Source-traced. Every finding cites a file path and function/line. No code was modified. No findings are inferred without a source reference.

> Companion documents: `PHILOSOPHY.md` (operating constraints), `AUDIT.md` (general fragility audit), `BROKER_SEMANTICS.md` (target broker separation: **KIS = US overseas ONLY, Kiwoom = domestic Korean ONLY**). This document is narrower — it traces only the broker/execution code paths and maps ownership.

---

## 0. System at a glance

| Layer | Module | Status (evidence) |
|---|---|---|
| Broker abstraction | `backend/brokers/base.py` → `BrokerAdapter` | Active, 5 abstract methods |
| Shared models | `backend/brokers/models.py` | Active, `Order`/`Position`/`Balance` + enums |
| KIS adapter (low-level) | `kis_adapter/` | Active, sole live broker |
| KIS adapter (BrokerAdapter impl) | `backend/brokers/kis.py` → `KISBroker` | Active, 271 lines, fully implemented |
| Kiwoom adapter (BrokerAdapter impl) | `backend/brokers/kiwoom.py` → `KiwoomBroker` | **Stub** — every method raises `NotImplementedError` |
| Kiwoom adapter (low-level) | `kiwoom_adapter/` | **Disconnected** — never imported by runtime code |
| Execution | `backend/execution/{order_machine,position_tracker,order_poller,reconciler}.py` | Active |
| Worker orchestration | `backend/worker/{runner,recovery,scheduler,emergency,heartbeat}.py` | Active |
| Legacy monolith | `bot/main.py`, `bot/scheduler.py`, `strategy/` | **Dead** (disabled in `docker-compose.yml`) except `bot/notifier.py` |

**One-line verdict:** A single live broker (KIS) currently serves **both** Korean domestic and US overseas markets, in direct violation of the intended separation. Kiwoom exists only as a non-functional stub. A complete legacy trading engine sits dormant in `bot/`, sharing one module (`bot/notifier.py`) with the live system.

---

## 1. Call Graph — Order Execution Pipeline

The live path from a strategy signal to a persisted fill:

```
StrategyBase.buy() / .sell()                      backend/strategy/base.py:80, :90
  ├─ _live_trade_allowed(broker, name, symbol, side)   base.py:81/:91
  │     ├─ SAFE_MODE.can_trade()                  worker/recovery.py
  │     └─ ENABLE_LIVE_TRADING env gate
  └─ broker.place_order(symbol, side, qty, price, order_type)
        │
        ▼  (KISBroker — the only live BrokerAdapter)
KISBroker.place_order()                           backend/brokers/kis.py:89
  ├─ is_kr = symbol in KR_ETF or (len==6 and isdigit())   kis.py:90   ◄ ROUTING HEURISTIC
  ├─ KR → KISOrders.buy_kr()/sell_kr()            kis_adapter/orders.py:59/:71
  └─ US → KISOrders.buy_us()/sell_us()            kis_adapter/orders.py:31/:45
        │ returns Order(status=SUBMITTED)         kis.py:98–101
        ▼
OrderFillPoller.register(order, on_filled, on_timeout)   execution/order_poller.py:74
  └─ background thread _loop() polls every ~5s    order_poller.py:100
        └─ KISBroker.get_order_status(order_id, symbol)   kis.py:135   ◄ ROUTING HEURISTIC
              ├─ US → _get_us_order_status()       kis.py:196
              └─ KR → _get_kr_order_status()       kis.py:145
        └─ on fill → on_filled(order)
              ▼
on_filled callback  (StrategyWorker._make_fill_callback)   worker/runner.py:380
  ├─ 1. OrderStateMachine.process_fill()          execution/order_machine.py:58
  ├─ 2. PositionTracker.on_fill()                 execution/position_tracker.py:71
  ├─ 3. PersistentLossTracker.record_pnl()        quant/risk/engine.py
  ├─ 4. _persist_fill()        → DB: fills, orders
  ├─ 5. _upsert_position_db()  → DB: positions
  └─ 6. _publish_order_update()→ WebSocket push
```

**Concrete strategy entry point:** `IndicatorStrategy._execute_buy()` / `_execute_sell()` (`backend/strategy/indicator/strategy.py:122`, `:141`) call the inherited `self.buy()` / `self.sell()`.

**Key observation:** the market-routing decision (`KR` vs `US`) is made *inside* `KISBroker` by a symbol-shape heuristic, and it is duplicated at three call sites (place, status, price). See §4.

---

## 2. Broker Dependency Graph

Who constructs and depends on a broker instance:

```
get_kis_broker()  (singleton, double-checked lock)         backend/brokers/kis.py:21–28
   ▲     ▲     ▲     ▲     ▲
   │     │     │     │     └── backend/worker/scheduler.py:29   (_save_equity_snapshot)
   │     │     │     └──────── backend/api/server.py:235,280,303 (/balance, /reconcile, /flatten)
   │     │     └────────────── PositionReconciler              runner.py:164 → reconciler.py:94
   │     └──────────────────── OrderFillPoller                 runner.py:127 → order_poller.py:59
   └────────────────────────── StrategyWorker._build_strategy  runner.py:337

KISBroker.__init__()                                       backend/brokers/kis.py:32–39
   ├── KISClient            kis_adapter/client.py   (auth, rate limit, retry, hashkey)
   │     └── KISAuth        kis_adapter/auth.py     (OAuth2 token, Redis cache)
   ├── KISOrders            kis_adapter/orders.py   (buy/sell KR + US, cancel US)
   ├── KISMarketData        kis_adapter/market_data.py (price KR + US, pending US)
   └── KISPortfolio         kis_adapter/portfolio.py (balance KR + US)

KiwoomBroker                                               backend/brokers/kiwoom.py
   └── (no dependencies — all methods raise NotImplementedError)

kiwoom_adapter/  (KiwoomClient/Orders/MarketData/Portfolio)
   └── imported by: NOTHING outside its own package        ◄ disconnected (grep-verified)
```

**Instantiation facts (grep-verified):**
- `get_kis_broker` is referenced ~6 call sites (runner, reconciler, poller, api/server, scheduler, recovery).
- `KiwoomBroker()` is instantiated **zero** times anywhere in the repo.
- There is **no** `get_kiwoom_broker()` factory.
- `backend/brokers/__init__.py:15–22` lazily exposes both `KISBroker` and `KiwoomBroker` via `__getattr__`, but only KIS is wired into runtime.

---

## 3. Duplicate Implementations

### 3.1 Two complete trading engines (legacy `bot/` vs. modern `backend/`)

| Concern | Legacy (dead) | Modern (live) |
|---|---|---|
| Engine | `bot/main.py` `TradingEngine` | `backend/worker/runner.py` `StrategyWorker` |
| Scheduler | `bot/scheduler.py` `BlockingScheduler` | `backend/worker/scheduler.py` `BackgroundScheduler` |
| Risk | `strategy/risk.py` `RiskManager` | `backend/quant/risk/engine.py` `PersistentLossTracker` |
| Signals | `strategy/signals.py` | `backend/strategy/indicator/strategy.py` |

- **File(s) / module(s):** `bot/main.py`, `bot/scheduler.py`, `strategy/*`, vs. `backend/worker/*`, `backend/quant/risk/engine.py`, `backend/strategy/*`
- **Why it matters:** Both engines drive the **same KIS account**. If `kis-bot` is ever re-enabled alongside `kis-worker`, both will place orders → duplicate fills on one account. `docker-compose.yml` (kis-bot block, ~lines 71–100) is commented out precisely with this warning.
- **Risk level:** **HIGH** (latent duplicate-order hazard; the only thing preventing it is a commented-out compose block)
- **Recommended owner:** Execution / Platform team — formally decommission the legacy engine (see §5) so re-enabling it is impossible by accident.

### 3.2 Two schedulers with identical cron intent

- **Files:** `bot/scheduler.py` (BlockingScheduler) and `backend/worker/scheduler.py` (BackgroundScheduler, built via `build_scheduler()` and started in `runner.py:~579`).
- **Why it matters:** Same trading-session triggers (KR 09:05, US 22:35, daily reset, summary) defined twice. Divergence risk if one is edited.
- **Risk level:** MEDIUM
- **Recommended owner:** Worker/Scheduling team — single source of truth = `backend/worker/scheduler.py`.

### 3.3 Two risk subsystems

- **Files:** `strategy/risk.py` (peak-equity persisted to file) vs. `backend/quant/risk/engine.py` (`PersistentLossTracker`, Redis + DB).
- **Why it matters:** Two definitions of daily-loss / MDD kill-switch state. Only the modern one is live; the legacy one is reachable only from the dead `bot/main.py`.
- **Risk level:** MEDIUM
- **Recommended owner:** Risk team — retire `strategy/risk.py` with the rest of the legacy engine.

### 3.4 Low-level adapter shape duplicated (KIS vs Kiwoom)

- **Files:** `kis_adapter/{client,orders,market_data,portfolio}.py` and the parallel `kiwoom_adapter/{client,orders,market_data,portfolio}.py`.
- **Why it matters:** `kiwoom_adapter` mirrors the KIS adapter's structure (`RateLimiter`, `client.get/post`, `buy_kr/sell_kr`) but is never wired in. Maintenance cost without runtime value; also a source of confusion about which adapter is "real."
- **Risk level:** LOW (cost/clarity, not correctness)
- **Recommended owner:** Broker team — either complete and wire Kiwoom (per `BROKER_SEMANTICS.md`) or remove the dead package.

---

## 4. Mixed KIS / Kiwoom Logic — broker-semantics violations

The target architecture (`BROKER_SEMANTICS.md`) is strict: **KIS handles US overseas only; Kiwoom handles Korean domestic only.** The code violates this in two distinct ways.

### 4.1 KIS adapter implements **both** domestic-KR and overseas-US trading

KIS code carries Korean *domestic* TR_IDs and `/uapi/domestic-stock/...` endpoints throughout — KIS is currently a dual-market broker.

**Domestic-KR TR_IDs found inside KIS code (evidence):**

| TR_ID | Purpose | Location |
|---|---|---|
| `TTTC0802U` / `VTTC0802U` | KR buy | `kis_adapter/orders.py:12` |
| `TTTC0801U` / `VTTC0801U` | KR sell | `kis_adapter/orders.py:13` |
| `FHKST01010100` | KR price | `kis_adapter/market_data.py:12` |
| `TTTC8434R` / `VTTC8434R` | KR balance | `kis_adapter/portfolio.py:11` |
| `TTTC0803U` / `VTTC0803U` | KR cancel | `backend/brokers/kis.py:112` |
| `TTTC8036R` / `VTTC8036R` | KR order status | `backend/brokers/kis.py:148` |

**Domestic-KR endpoints found inside KIS code:**
- `/uapi/domestic-stock/v1/trading/order-cash` — `kis_adapter/orders.py:17`
- `/uapi/domestic-stock/v1/quotations/inquire-price` — `kis_adapter/market_data.py:8`
- `/uapi/domestic-stock/v1/trading/inquire-balance` — `kis_adapter/portfolio.py:8`
- `/uapi/domestic-stock/v1/trading/order-rvsecncl` — `backend/brokers/kis.py:124`

**Mixed-market methods:**
- `KISPortfolio.get_total_asset_krw()` (`portfolio.py:61`) calls *both* `get_kr_balance()` and `get_us_balance()`.
- `KISBroker.get_balance()` (`kis.py:41`) and `KISBroker.get_positions()` (`kis.py:54`) merge KR + US.

- **Why it matters:** Direct violation of the single-responsibility broker boundary. Domestic flow should belong to Kiwoom. Keeping KR logic in KIS means the eventual Kiwoom cutover must surgically remove KR paths from KIS without breaking US, and today a domestic order can still be routed to KIS.
- **Risk level:** **HIGH** (architectural correctness + cutover risk)
- **Recommended owner:** Broker team (KIS owner) — strip all `*_kr` paths from `kis_adapter/` and `KISBroker` once Kiwoom is live; until then, gate domestic symbols away from KIS at the router.

### 4.2 Symbol-shape routing heuristic embedded in KIS (and duplicated 3×)

```python
is_kr = symbol in KR_ETF or (len(symbol) == 6 and symbol.isdigit())
```

- **Locations:** `backend/brokers/kis.py:90` (`place_order`), `:140` (`get_order_status`), `:251` (`get_price`).
- **Why it matters:** (a) Broker-selection logic lives *inside* a single broker rather than in a routing layer; (b) the rule is copy-pasted three times — any fix must be made in three places; (c) the heuristic is brittle (any 6-digit numeric string is assumed Korean). This is exactly the "special case on shared infra" pattern that should be generalized into a `BrokerSemanticMapper` (see `BROKER_SEMANTICS.md` §6).
- **Risk level:** MEDIUM (correctness of routing + maintenance)
- **Recommended owner:** Execution/routing team — extract a single market-resolution function; long-term, route KR→Kiwoom, US→KIS at the layer above the adapters.

### 4.3 Kiwoom adapter present but non-functional / mis-endpointed

- **`backend/brokers/kiwoom.py`** — `KiwoomBroker` stub; all 5 methods raise `NotImplementedError` (lines 9, 12, 15, 18, 21).
- **`kiwoom_adapter/client.py`** — the **active** base URL is correct: `self.base_url = KIWOOM_BASE` = `https://openapi.kiwoom.com:10000` (line 12 / 38). However, the **unused** constants `PAPER_BASE` and `REAL_BASE` (lines 9–10) wrongly point at `https://openapi.koreainvestment.com:9443` (a KIS host). They are dead but misleading.
- **`kiwoom_adapter/orders.py`** — `buy_kr`/`sell_kr` POST to `/uapi/domestic-stock/v1/trading/order-cash` (lines 21, 38). These are **KIS-style path conventions** used against the Kiwoom host; they require verification against the real Kiwoom Open API (Kiwoom's REST paths differ from KIS's `/uapi/...` scheme). As written, this adapter is unverified against its target and almost certainly non-functional.
- **Why it matters:** Kiwoom is the intended *domestic* broker but cannot place a single order today. The stale KIS URLs in the constants invite a future copy-paste regression.
- **Risk level:** MEDIUM (no current runtime impact since disconnected; HIGH the moment anyone wires it in unverified)
- **Recommended owner:** Broker team (Kiwoom owner) — implement against verified Kiwoom API docs, delete the stale `PAPER_BASE`/`REAL_BASE` constants, add credential validation, and provide a `get_kiwoom_broker()` factory before wiring.

---

## 5. Dead Code

Verified by grep (no runtime importers found):

| Item | File(s) | Evidence | Risk | Owner |
|---|---|---|---|---|
| Legacy trading engine | `bot/main.py` (`TradingEngine`) | No `import bot.main` anywhere; `kis-bot` service commented out in `docker-compose.yml` | HIGH if re-enabled (duplicate orders) | Platform |
| Legacy scheduler | `bot/scheduler.py` | Superseded by `backend/worker/scheduler.py`; not started anywhere | MEDIUM | Worker |
| Legacy strategy package | `strategy/` (`signals.py`, `optimizer.py`, `risk.py`) | Imported **only** by `bot/main.py` (itself dead); `grep "from strategy\." backend/` → none | MEDIUM | Strategy |
| Kiwoom low-level adapter | `kiwoom_adapter/*` | Imported only within its own package | LOW | Broker |
| `KiwoomBroker` stub | `backend/brokers/kiwoom.py` | Never instantiated | LOW | Broker |
| Stale URL constants | `kiwoom_adapter/client.py:9–10` | `PAPER_BASE`/`REAL_BASE` defined, never read | LOW (misleading) | Broker |

**Important caveat — `bot/notifier.py` is NOT dead.** Despite living in the otherwise-dead `bot/` package, it is imported by live code for Telegram alerts:
- `backend/worker/scheduler.py:52` → `from bot.notifier import alert_daily_summary`
- `backend/worker/emergency.py:99` → `from bot.notifier import alert_emergency`
- `backend/worker/heartbeat.py:135` → `from bot.notifier import alert_emergency`
- `backend/quant/risk/engine.py:277` → `from bot.notifier import alert_emergency`

This couples the live `backend/` to the legacy `bot/` package and blocks a clean deletion of `bot/`. **Recommendation:** relocate `notifier.py` to `backend/notifications/` and update the four importers before removing the rest of `bot/`.

---

## 6. Recommended Ownership Map

Proposed module boundaries and owners. "Owner" denotes the team accountable for the module's correctness and its conformance to `BROKER_SEMANTICS.md`.

| Module / path | Responsibility | Recommended owner | Boundary rule |
|---|---|---|---|
| `backend/brokers/base.py`, `models.py` | Broker abstraction + shared DTOs | **Broker Platform** | No broker-specific logic; stable contract for all adapters |
| `kis_adapter/*`, `backend/brokers/kis.py` | KIS connectivity | **KIS owner (Broker)** | **US overseas ONLY** (target). Remove all `*_kr` paths post-Kiwoom |
| `kiwoom_adapter/*`, `backend/brokers/kiwoom.py` | Kiwoom connectivity | **Kiwoom owner (Broker)** | **Domestic KR ONLY**. Implement + verify before wiring |
| *(new)* market→broker router / `BrokerSemanticMapper` | Decide KR→Kiwoom, US→KIS | **Execution/Routing** | Owns the routing rule currently duplicated in `kis.py:90/140/251` |
| `backend/execution/order_machine.py` | Order lifecycle state machine | **Execution** | Broker-agnostic; no KIS/Kiwoom specifics |
| `backend/execution/position_tracker.py` | In-memory positions + dup-order lock | **Execution** | Single source of in-memory truth; DB is durable mirror |
| `backend/execution/order_poller.py` | Async fill detection | **Execution** | Polls via `BrokerAdapter` only |
| `backend/execution/reconciler.py` | Broker↔DB reconciliation | **Execution** | Broker is ground truth (per `PHILOSOPHY.md`) |
| `backend/worker/runner.py`, `recovery.py`, `heartbeat.py` | Orchestration, startup recovery | **Worker/Platform** | Owns the `on_filled` fan-out and 8-step recovery |
| `backend/worker/scheduler.py` | Session/cron triggers | **Worker/Platform** | Sole scheduler; retire `bot/scheduler.py` |
| `backend/worker/emergency.py` | Kill-switch flatten | **Risk** | Triggered only by risk signals |
| `backend/quant/risk/engine.py` | Loss/MDD kill-switch | **Risk** | Sole risk authority; retire `strategy/risk.py` |
| `backend/notifications/` *(proposed)* | Alerts | **Platform** | Move `bot/notifier.py` here; break `backend → bot` coupling |
| `bot/main.py`, `bot/scheduler.py`, `strategy/*` | Legacy engine | **— (decommission)** | Delete after notifier relocation |
| `backend/api/server.py` | REST surface | **API/Platform** | Calls brokers only through `get_kis_broker()` / future router |

### Ownership boundary summary

```
                 ┌─────────────────────────────────────────────┐
                 │  Execution / Routing                        │
                 │  (NEW) market→broker resolver               │
                 └───────────────┬───────────────┬─────────────┘
                       US ───────┘               └─────── KR
                         │                               │
        ┌────────────────▼──────────┐     ┌──────────────▼──────────────┐
        │ KIS owner (Broker)        │     │ Kiwoom owner (Broker)        │
        │ kis_adapter + KISBroker   │     │ kiwoom_adapter + KiwoomBroker│
        │ TARGET: US overseas ONLY  │     │ TARGET: domestic KR ONLY     │
        │ TODAY: KR + US (violation)│     │ TODAY: stub / disconnected   │
        └────────────┬──────────────┘     └──────────────┬───────────────┘
                     └────────────► BrokerAdapter ◄───────┘
                                  (Broker Platform)
                                         │
              Execution: order_machine · position_tracker · poller · reconciler
                                         │
                 Worker/Platform · Risk · API · Notifications
```

---

## Appendix — Evidence index (file:line)

- Routing heuristic: `backend/brokers/kis.py:90`, `:140`, `:251`
- KIS domestic TR_IDs: `kis_adapter/orders.py:12–13`, `market_data.py:12`, `portfolio.py:11`, `kis.py:112`, `:148`
- KIS domestic endpoints: `kis_adapter/orders.py:17`, `market_data.py:8`, `portfolio.py:8`, `kis.py:124`
- KISBroker singleton: `backend/brokers/kis.py:21–28`
- Kiwoom stub: `backend/brokers/kiwoom.py:9,12,15,18,21`
- Kiwoom active URL: `kiwoom_adapter/client.py:12,38`; stale constants: `:9–10`
- Kiwoom order endpoints: `kiwoom_adapter/orders.py:21,38`
- Lazy broker export: `backend/brokers/__init__.py:15–22`
- Fill fan-out: `backend/worker/runner.py:380` (`_make_fill_callback`)
- Startup recovery: `backend/worker/recovery.py:78` (8 steps)
- Reconciler: `backend/execution/reconciler.py:94,103,135`
- `bot/notifier.py` live importers: `backend/worker/scheduler.py:52`, `emergency.py:99`, `heartbeat.py:135`, `quant/risk/engine.py:277`
- Disabled legacy service: `docker-compose.yml` `kis-bot` block (commented)

*End of audit. No code was modified in the production of this document.*
