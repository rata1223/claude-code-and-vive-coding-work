# P5-ADAPTER_PLAN: Minimum-Change Integration Strategy

> **Planning only.** No backend or frontend code was changed to produce this document. This plan builds directly on `docs/P5_API_MAPPING_AUDIT.md` (84 endpoints, verified against source) and adds two new source-verified findings that change the design (see §0).

**Date:** 2026-07-14
**Principle:** frontend modification is minimized; a backend-side adapter absorbs incompatibilities wherever that's reasonable.

---

## 0. Two findings that shape this plan

**Login is worse than the mapping audit stated.** `mobile/src/views/login/index.vue:887-891` sends `{username, password, turnstile_token}` to `POST /api/auth/login` — not `{email, password}`. The backend's `LoginRequest{email: EmailStr, password: str}` requires `email` and has no `username` field, so **every login attempt returns HTTP 422 today.** The mapping audit characterized this as "turnstile_token silently dropped," which understated it — the actually-required field is never sent at all. The fix is still cheap: `loginForm.username` is populated from `forgotForm.email` after password reset (`login/index.vue:952`) and functions as the account's email throughout the UI; the `User` model (`api/models.py`) has no username column, only `email` (unique, required). **A pure key rename (`username`→`email`) fixes it — zero frontend change, highest-impact fix in this plan.**

**Quick Trade is a different trading paradigm, not a naming mismatch.** `mobile/src/views/quick-trade/index.vue` has a `leverage` field (shown only when `marketType === 'swap'`), an `amount` field with history rows rendered as `"{{ amount }} USDT"`, and a spot/swap toggle — a crypto perpetual-swap/spot UI inherited from QuantDinger. The backend's `PlaceOrderRequest`/`ClosePositionRequest` are KIS-equity-shaped: `qty: float` (share count), `price: float`, `exchange: "NASD"`, `market: "us"/"kr"`. No backend adapter can translate "USDT notional at N× leverage" into "N shares at a price" — they aren't the same order model. **This is the one place in the entire 84-endpoint inventory that genuinely requires a frontend rework**, not an adapter shim.

---

## 1. Adapter design

The adapter lives **inside the existing `api/` FastAPI app** — a new `api/compat.py` module — not a separate gateway/BFF service. Rationale: it's already the single deployable unit wired via `Dockerfile.api`/`docker-compose.yml` (`uvicorn api.main:app`), so this adds zero new infrastructure, keeps the fix reviewable route-by-route, and avoids introducing a second network hop or a second thing to deploy/monitor.

Two mechanisms, both invoked from inside the existing route handlers (not a generic catch-all proxy):

**(a) Request-side remap.** For body fields that map onto an existing-but-differently-named Pydantic field (e.g. `api_key`→`app_key`), use Pydantic's `validation_alias=AliasChoices("app_key", "api_key")` directly on the model — the model then accepts either name with no handler code change. For `Query()`-declared params that aren't backed by a reusable model (e.g. `id` vs `strategy_id`, `market_type` vs `market`), add a small `Depends()` shim in `api/compat.py`:
```python
def remap_query(request: Request, aliases: dict[str, str]):
    # if the new/canonical name is absent but an old/frontend name is present,
    # copy the value across before the route's own Query(...) params resolve it
```
wired into each affected route as an extra dependency, so the route's own `Query(...)` declarations don't need to change.

**(b) Response-side reshape.** A small `adapt_response(data: dict, key_map: dict)` helper in `api/compat.py`, called at the point each affected handler builds its `Resp.ok(...)` payload — renames/duplicates the wrapper key (`items`→`trades`, `count`→`unread`, etc.) or unwraps `{"items": [...]}` to a bare list where the frontend expects one. This is a one-line change per affected `return Resp.ok(...)` call, not a global middleware, so each transform stays visible next to the route it belongs to.

**(c) Server-side auto-resolution** (used once, for `ClosePositionRequest`): where the frontend genuinely cannot supply a required field because its UI doesn't collect it, the backend resolves the value itself from data it already has (see §3).

This keeps 100% of the adapter logic backend-side, in one file plus small per-route hooks, with no new service and no frontend change for anything covered by (a)/(b)/(c).

---

## 2. Endpoint translation strategy

Every DTO-differs / partially-implemented endpoint from `docs/P5_API_MAPPING_AUDIT.md` §2/§5, tagged with its adapter mechanism.

| Endpoint | Mismatch (see mapping audit §2 for exact detail) | Mechanism | Frontend change needed? |
|---|---|---|---|
| `authApi.login` | `username` sent, `email` required | (a) request alias | No |
| `authApi.register` | `code`/`username`/`turnstile_token`/`referral_code` extraneous | (a) alias `username`→`nickname`; rest are real feature gaps, not adapter | No (register already succeeds today without these) |
| `credentialsApi.create` | `api_key`/`secret_key`/`enable_demo_trading` vs `app_key`/`app_secret`/`env` | (a) request alias | No |
| `strategyApi.getList` | `items` vs `strategies` | (b) response reshape | No |
| `strategyApi.getTrades`/`getPositions`/`getEquityCurve`/`getPerformance`/`getLogs` | `id` vs `strategy_id`; `items` vs `trades`/`positions`/`logs`; equityCurve dict vs array | (a) query remap + (b) response reshape | No |
| `strategyApi.getUnreadNotificationCount` | `count` vs `unread` | (b) response reshape | No |
| `indicatorApi.getIndicators` | `items` vs `indicators` | (b) response reshape | No |
| `indicatorApi.parseStrategyConfig` | response keys AND semantic content wrong (naive substring scan vs real config parse) | Not adapter-fixable — needs real backend logic (§6) | No (backend-only) |
| `klineApi.getKline` | `{items:[...]}` vs bare array; `before_time` ignored | (b) response reshape for shape; real pagination support is backend-only | No |
| `watchlistApi.getList`/`search`/`getHot` | `{items:[...]}` vs bare array | (b) response reshape | No |
| `watchlistApi.getPrices` | `watchlist=<JSON>` vs `symbols=<CSV>&market=` (fundamentally different contract, possibly multi-market) | Custom adapter: parse JSON, group by market, fan out to the existing endpoint internally, merge results | No (more engineering effort — P2, see §6) |
| `quickTradeApi.getBalance`/`getPosition` | `market_type` vs `market`; missing `available`/`total`; singular `position` vs plural `positions` | (a) query remap + (b) response reshape (synthesize `available=cash`, `total=total_eval`; wrap singular into a 1-item array) | No |
| `quickTradeApi.placeOrder` | Crypto-paradigm payload (`amount`, `leverage`, `market_type: spot/swap`) vs KIS-equity model (`qty`, `price`, `exchange`) | **Not adapter-fixable** — paradigm mismatch (§0) | **Yes — Quick Trade rework** |
| `quickTradeApi.closePosition` | Same crypto-paradigm payload gap on its face, but the UI only ever offers a single "close" action with no qty/price input | (c) server-side auto-resolution — see §3 | No (see §3: qty/price resolved from the existing position/price endpoints) |
| `quickTradeApi.getHistory` | `items` vs `trades` | (b) response reshape for shape; empty-data problem (orders never persisted) is backend-only | No |
| `userApi.getMyCreditsLog`/`getMyReferrals` | Always empty, no backing model | Not adapter-fixable — no data exists to reshape | No (backend-only) |

---

## 3. DTO translation strategy

Concrete before/after mappings for the highest-value cases.

**`authApi.login`**
```
FE sends:  { username, password, turnstile_token }
BE wants:  { email, password }
Adapter:   LoginRequest.email = Field(validation_alias=AliasChoices("email", "username"))
           (turnstile_token stays unvalidated — real captcha enforcement is a separate P3/P2 decision, §5)
```

**`credentialsApi.create`**
```
FE sends:  { name, exchange_id, api_key, secret_key, passphrase, account_no, hts_id, enable_demo_trading }
BE wants:  { name, exchange_id, app_key, app_secret, account_no, hts_id, env }
Adapter:   CredentialCreate.app_key    = Field(validation_alias=AliasChoices("app_key", "api_key"))
           CredentialCreate.app_secret = Field(validation_alias=AliasChoices("app_secret", "secret_key"))
           CredentialCreate.env        = derived: "real" if enable_demo_trading is False else "paper"
                                          (needs a small pre-validation step, since `env` depends on a
                                          boolean rather than being a simple rename — a model_validator)
           passphrase: no destination field: KIS doesn't use one — correctly dropped, no action needed
```
This is the single most consequential fix in the whole plan: today, no KIS credential created through the app can produce a working broker connection at all (the encrypted `app_key`/`app_secret` columns stay empty). After this adapter change, credential creation works end-to-end with zero frontend changes.

**Strategy sub-resources** (`trades`, `positions`, `equityCurve`, `performance`, `logs`)
```
FE sends query: { id, limit }
BE wants query: { strategy_id, page, page_size }
Adapter:   remap_query(request, {"strategy_id": "id"})  — copies `id`'s value into `strategy_id`
           `limit` has no backend equivalent — safe to ignore (backend has its own pagination defaults)

FE expects response key: "trades" / "positions" / "logs" (or a bare array for equityCurve)
BE returns: {"total": n, "items": [...]}
Adapter:   adapt_response(data, {"items": "trades"})   # or "positions"/"logs" per route
           for equityCurve specifically: return data["items"] directly (bare array), not the wrapper
```

**`quickTradeApi.getBalance` / `getPosition`**
```
FE sends query: { credential_id, market_type }
BE wants query: { credential_id, market }
Adapter:   remap_query(request, {"market": "market_type"})

FE expects response fields: { available, total, ... }
BE returns: { currency, total_eval, cash, positions }
Adapter:   adapt_response adds `available = cash`, `total = total_eval` alongside the existing fields
           (approximation — flag for product sign-off: "available" ⟺ free cash, "total" ⟺ total_eval,
           reasonable defaults but worth a one-line confirmation from whoever owns the trading UX)

FE expects: unwrapItems(res.data, 'positions')  — a plural array
BE returns: {"symbol": s, "position": obj_or_null}  — singular object or null
Adapter:   adapt_response wraps: {"positions": [pos] if pos else []}
```

**`quickTradeApi.closePosition`** (server-side auto-resolution, not field mapping)
```
FE sends: { credential_id, symbol, market_type, position_side, source }   — no qty, no price
BE wants: { credential_id, symbol, qty (required), price (required), market, exchange }
Adapter:   make qty/price Optional on ClosePositionRequest; when omitted:
             qty   = current open position size, fetched via the existing GET /quick-trade/position
             price = current market price, fetched via the existing GET /indicator/price
           i.e. "close position" defaults to "close the whole thing at market" — which is exactly
           what the current UI's single "Close" button implies, so no frontend change is needed here
           EVEN THOUGH placeOrder itself still requires the Quick Trade rework (§0) for the buy/sell side.
```

---

## 4. WebSocket translation strategy

Current state (per `docs/P5_FRONTEND_API_INVENTORY.md` §6): **0 frontend consumers.** The backend already runs a working Socket.IO server (`backend/websocket/server.py`, channels `order:update`/`position:update`/`equity:update`/`alert`, plus `connect`/`subscribe` handshake) as the separate `kis-ws` process on port 5002 — but nothing in `api/main.py` (port 8000, what the frontend actually talks to) proxies or mounts it, so the frontend has no path to reach it even if it tried.

Adapter strategy, in order:
1. **Expose the existing channels through the same origin the frontend already trusts.** Two options, pick one during implementation: (i) reverse-proxy `kis-ws`'s port through whatever fronts `api`/`frontend` today (nginx/ingress config change, zero application code change), or (ii) mount Socket.IO directly inside `api/main.py` alongside the existing FastAPI routes (`python-socketio`'s ASGI app can be combined with FastAPI) so there's only ever one host/port the frontend needs to know about. Either way, this is backend/infra work — no frontend change.
2. **Wire the already-scaffolded `useWebSocketStore`** (`mobile/src/stores/index.js:366-388`, currently unused, mobile-only — not even present in `frontend/`) to a real `socket.io-client` connection. Since there's no existing client code to *adapt* here (only an empty store), this is the one unavoidable **net-new, additive** frontend file — e.g. `mobile/src/utils/ws.js` — not a rework of any existing screen. Add the equivalent store to `frontend/` at the same time to close that gap.
3. **Cut over polling incrementally, screen by screen**, once the socket exists — `home/index.vue`'s 30s watchlist poll, `ai-analysis/index.vue`'s 20s live-price poll, `profile/Credits.vue`'s 5s order-status poll — each can be swapped to a store-driven reactive update independently; this isn't a single big-bang migration and each swap is low-risk since the polling fallback can stay in place until the socket path is proven.

Classification: P2 (see §7) — real effort (infra + one new client module), but not blocking any P1 item, and the existing polling continues to work while this is built.

---

## 5. Authentication compatibility

- **Transport scheme is already fully compatible.** Frontend interceptor (`mobile/src/api/index.js:305-316`) sends `Authorization: Bearer <token>`; backend's `get_current_user` dependency (`api/auth.py`/`api/deps.py`) expects exactly that. No adapter work needed here at all.
- **The only real break is the login request shape** (§0, §3) — P1, fixed by the `email`/`username` alias.
- **`authApi.loginWithCode`** (`POST /api/auth/login-code`) has no backend route and is never called by the frontend (confirmed in `docs/P5_FRONTEND_API_INVENTORY.md`) — a dormant gap, not a live compatibility issue. P3.
- **Captcha/email-verification are backend feature gaps, not compatibility issues.** `sendCode`/`resetPassword` are literal no-ops server-side (no email is ever sent, no code is ever checked), and `turnstile_token` is accepted but never validated. Aliasing or reshaping can't fix "the capability doesn't exist" — this needs real implementation if the product wants these controls enforced. Classified **P3** by default (register/login work today without them); escalate to **P2** only if there's a product requirement to actually enforce captcha/email verification before launch.

---

## 6. Migration order

Phased, not flat — each phase is independently shippable and validatable.

### Phase 1 — P1 (adapter-only, one PR, zero frontend changes, unblocks core flows)
1. `authApi.login` alias fix (`username`→`email`) — **unblocks 100% of logins, currently fully broken.**
2. `credentialsApi.create` field aliasing (`api_key`→`app_key`, `secret_key`→`app_secret`, `enable_demo_trading`→`env`) — unblocks the entire KIS broker-connection flow.
3. Strategy sub-resource query/response fixes (`id`→`strategy_id`, `items`→`trades`/`positions`/`logs`, equityCurve unwrap) — unblocks strategy detail pages, which are core and reachable on both apps.
4. `strategyApi.getList` (`items`→`strategies`), `getUnreadNotificationCount` (`count`→`unread`), `indicatorApi.getIndicators` (`items`→`indicators`) — unblocks the trading list, notification badge, and bot-creation indicator picker.
5. `watchlistApi.getList`/`search`/`getHot` array-unwrapping — unblocks the watchlist everywhere it appears (dashboard, quick trade, AI analysis, bot creation).
6. `quickTradeApi.getBalance`/`getPosition` field remap + response synthesis, and `closePosition`'s server-side qty/price auto-resolution — unblocks balance/position display and the "close" action specifically (buy/sell still blocked pending Phase 2's Quick Trade rework).

All of Phase 1 ships as a single `api/compat.py`-centered backend PR. No frontend deploy is required to realize these fixes.

### Phase 2 — P2 (larger effort, or gated on a coordinated frontend change)
1. **AI Analysis backend build-out** (`/api/fast-analysis/*`, 7 endpoints) — highest-severity *missing* feature on a reachable, core screen (`ai-analysis/*.vue`, `ai-hub/index.vue`). Real implementation work (whatever `analyze` is supposed to do), not an adapter.
2. **Quick Trade paradigm rework** — the one coordinated frontend+backend item: replace the leverage/USDT-notional/spot-swap form with a KIS-shaped qty+price order form; once that exists, `placeOrder` needs no further backend adapter beyond what Phase 1 already covers for balance/position/close.
3. **WebSocket wiring** (§4) — infra exposure + new `mobile/src/utils/ws.js` + `frontend/` store parity, then incremental polling cutover.
4. **`watchlistApi.getPrices`** fan-out adapter (JSON multi-market query → grouped calls) — more engineering effort than a rename, still zero frontend change.
5. Captcha/email-verification enforcement — only if product requires it (see §5).

### Phase 3 — P3 (low usage/value, or unreachable-today screens)
1. **Community marketplace** (`/api/community/*`, 7 endpoints) and **Billing** (`/api/billing/*`, 5 endpoints) — real backend feature builds; screens that call them are only reachable on `frontend/` (web), not the shipped `mobile/` app, per the P5-01B-1 router-gap finding — lower urgency than Phase 2 items.
2. Notification-settings persistence, credits-log/referral system persistence — real models/tables needed; currently-stubbed, low-visibility features.
3. `indicatorApi.parseStrategyConfig` real parsing logic; `klineApi`/`price` real KIS/Kiwoom/crypto data sourcing (currently yfinance-only regardless of `market`).
4. Cleanup of endpoints the P5-01B-1 inventory already flagged as unused by any frontend call site: `authApi.loginWithCode`, `strategyApi.testConnection`, `marketApi.syncIndicator`, `billingApi.purchase`, `aiAnalysisApi.getHistory`/`getPerformance`/`getSimilarPatterns`, `userApi.updateProfile`/`changePassword`, `klineApi.getPrice` (unused, distinct from `getKline` which is used), `credentialsApi.get`, `strategyApi.getTemplates`/`getTemplate`/`clearNotifications`, `dashboardApi.getPendingOrders` — decide per-endpoint whether to implement, remove from the frontend, or leave dormant; none of this blocks any user-facing flow today.
5. `strategyApi.aiGenerate` real LLM integration (currently a hardcoded placeholder) — same urgency tier as the rest of the AI-feature backend gaps once Phase 2's `fast-analysis` work sets the pattern.

---

## 7. Final report

### Backend-only fixes (adapter cannot help — real implementation required)
- All 20 Missing endpoints: AI Analysis (7), Community marketplace (7), Billing (5), `login-code` (1).
- Stub routes that fabricate or discard data regardless of DTO shape: `strategyApi.aiGenerate` (hardcoded placeholder script), `strategyApi.testConnection` (always reports success), `authApi.sendCode`/`resetPassword` (no-ops, nothing is ever sent/checked), `userApi.notification-settings` ×3 (not persisted), `userApi.getMyCreditsLog`/`getMyReferrals` (always empty, no model), `quickTradeApi.getHistory`'s underlying data gap (orders never written to `Trade` at placement time), `indicatorApi.parseStrategyConfig`'s actual parsing logic, `klineApi`/`price`'s real KIS/Kiwoom/crypto data sourcing (currently yfinance-only).

### Frontend-required changes (kept deliberately short, per the stated principle)
1. **Quick Trade screen rework** — the only unavoidable UI rework, and strictly speaking only blocks `placeOrder` (1 of 84 endpoints): remove the leverage/USDT-notional/spot-swap model, replace with a KIS-shaped qty+price order form. `closePosition` does *not* require this rework — its missing `qty`/`price` are auto-resolved server-side (§3) — though a real screen rework would likely touch its UI too even though the backend doesn't require it. Everything else in the 84-endpoint inventory needs no frontend change.
2. **Net-new WebSocket client** (`mobile/src/utils/ws.js` or similar, plus a `frontend/` store to match mobile's `useWebSocketStore`) — additive, not a rework of any existing screen; there's no existing client code to adapt since none exists today.
3. **Optional, low-priority**: adjusting `ensureArray`/`unwrapItems` call sites directly in `mobile/src/api/index.js` for any case where a Phase 1 adapter transform turns out more awkward server-side than fixing the one shared frontend file — a per-item implementation-time call, not mandated by this plan.

### Estimated reuse percentage
**~99% (83 of 84) of the frontend's existing API-calling code requires zero modification.** Breakdown: 31 already fully compatible + 22 DTO-differs + 11 partially-implemented (once the backend implements them for real) = 64 endpoints fixable entirely backend-side, plus the 20 Missing endpoints also need no frontend change once built — with one qualification: `authApi.loginWithCode` is one of the 20 Missing endpoints but, per `docs/P5_FRONTEND_API_INVENTORY.md`, is never actually called by the frontend today, so "the frontend already calls them with the contract it expects" applies to 19 of the 20, not all 20; `loginWithCode` would need a real call site added before it mattered either way. The only carve-out from the 83 is `quickTradeApi.placeOrder` (1 of 84 endpoints) — `closePosition` is adapter-fixable via server-side auto-resolution (§3), and `getBalance`/`getPosition`/`getHistory` are adapter-fixable via renaming/reshaping, so only order *placement* itself needs the Quick Trade UI rework. **Caveat:** this measures *frontend call-site code that needs no edits*, not *feature completeness* — the 20 Missing endpoints still require substantial real backend feature work (AI Analysis, marketplace, billing) before those features actually function, even though zero frontend lines need to change to consume them once built.

### Remaining API gaps (after this adapter plan is executed)
- AI Analysis, Community marketplace, Billing, `login-code` — still fully unimplemented; this plan makes existing contracts *compatible*, it does not build new features (Phase 2/3 items, §6).
- Quick Trade order placement — still non-functional until the paradigm rework ships (excluded from the reuse % above).
- Notification-settings, credits-log, referrals persistence — still stubbed.
- `parseStrategyConfig` real parsing, `klineApi`/`price` real broker-backed data sourcing — still gaps even after shape fixes.

### Recommended next task (P5-02)
**Implement Phase 1** — the P1 `api/compat.py` adapter layer (§6, items 1-6). Rationale: pure backend work, zero frontend changes required, zero coordinated multi-repo deploy risk, and it unblocks login (currently 100% broken for every user), the KIS credential-creation flow (currently produces non-functional broker connections), and the strategy-detail/watchlist/notification screens across both `mobile/` and `frontend/` — the highest-value, lowest-risk slice of this entire plan, fully testable end-to-end against the existing frontend as-is.
