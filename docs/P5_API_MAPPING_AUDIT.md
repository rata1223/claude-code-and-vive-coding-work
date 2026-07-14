# P5-API_MAPPING_AUDIT: Frontend↔Backend API Compatibility Audit

> **Analysis only.** No backend or frontend code was changed to produce this document. Every classification below is taken directly from source (`mobile/src/api/index.js`, `api/main.py`, `api/routers/*.py`, `api/schemas.py`, `api/models.py`, `Dockerfile.api`, `docker-compose.yml`) — nothing is inferred.

**Date:** 2026-07-14
**Base document:** `docs/P5_FRONTEND_API_INVENTORY.md` (84 frontend endpoint functions, verified)

---

## 0. Which backend is "current backend (main)"?

Definitively resolved before any comparison work: **`api/main.py`** (FastAPI, `uvicorn api.main:app`) is the only backend the frontend talks to.

- `Dockerfile.api` CMD: `["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]`
- `docker-compose.yml`'s `api` service builds `Dockerfile.api` on port 8000; the `frontend` service sets `VITE_API_TARGET: http://api:8000` and `depends_on: [api]`.
- A second, unrelated Python HTTP service exists — `backend/api/server.py` (Flask, `"""Flask REST API — 포트 5000"""`, run via `gunicorn backend.api.server:app` as the `kis-api` compose service on port 5001, guarded by `X-API-Key`). Its routes (`/api/health`, `/api/status`, `/api/positions`, `/api/orders`, `/api/strategies`, `/api/balance`, `/api/backtest`, `/api/admin/*`, `/api/metrics`) are an internal ops/admin surface with **zero overlap** with the frontend's expected paths, and it is never wired to the frontend via `VITE_API_TARGET`. It is excluded from this audit as out of scope.

`api/main.py` mounts exactly 10 routers (`app.include_router(...)` — no more, no less): `auth`, `credentials`, `dashboard`, `strategies`, `templates`, `indicators` (prefix `/api/indicator`, singular), `watchlist` (prefix `/api/market`), `global_market`, `users`, `quick_trade`. **There is no router for `/api/fast-analysis/*`, `/api/community/*`, or `/api/billing/*` anywhere in the codebase.**

---

## 1. Endpoint Mapping Table

All 84 frontend endpoints. **BE Method+Path** is "— missing —" where no backend route exists at all. **Class** = Fully compatible (FC) / DTO differs (DTO) / Partially implemented (PI) / Missing (MISS). No case of URL-differs-only was found anywhere in the 84.

### authApi (FE base `/api/auth`)
| FE function | FE method+path | BE method+path | Class |
|---|---|---|---|
| login | POST /api/auth/login | POST /api/auth/login | DTO |
| loginWithCode | POST /api/auth/login-code | — missing — | MISS |
| register | POST /api/auth/register | POST /api/auth/register | DTO |
| sendCode | POST /api/auth/send-code | POST /api/auth/send-code | PI |
| resetPassword | POST /api/auth/reset-password | POST /api/auth/reset-password | PI |
| getSecurityConfig | GET /api/auth/security-config | GET /api/auth/security-config | FC |
| getInfo | GET /api/auth/info | GET /api/auth/info | FC |
| logout | POST /api/auth/logout | POST /api/auth/logout | FC |
| changePassword | POST /api/auth/change-password | POST /api/auth/change-password | FC |

### dashboardApi (`/api/dashboard`)
| getSummary | GET /api/dashboard/summary | GET /api/dashboard/summary | FC |
| getPendingOrders | GET /api/dashboard/pendingOrders | GET /api/dashboard/pendingOrders | FC |

### credentialsApi (`/api/credentials`)
| list | GET /api/credentials/list | GET /api/credentials/list | FC |
| get | GET /api/credentials/get | GET /api/credentials/get | FC |
| create | POST /api/credentials/create | POST /api/credentials/create | DTO |
| delete | DELETE /api/credentials/delete | DELETE /api/credentials/delete | FC |
| getEgressIp | GET /api/credentials/egress-ip | GET /api/credentials/egress-ip | FC |

### strategyApi (`/api/strategies`, `/api/templates`)
| getTemplates | GET /api/templates | GET /api/templates | FC |
| getTemplate | GET /api/templates/{key} | GET /api/templates/{key} | FC |
| create | POST /api/strategies/create | POST /api/strategies/create | DTO |
| batchCreate | POST /api/strategies/batch-create | POST /api/strategies/batch-create | FC |
| update | PUT /api/strategies/update | PUT /api/strategies/update | FC |
| delete | DELETE /api/strategies/delete | DELETE /api/strategies/delete | FC |
| aiGenerate | POST /api/strategies/ai-generate | POST /api/strategies/ai-generate | PI |
| getList | GET /api/strategies | GET /api/strategies | DTO |
| getDetail | GET /api/strategies/detail | GET /api/strategies/detail | FC |
| start | POST /api/strategies/start | POST /api/strategies/start | FC |
| stop | POST /api/strategies/stop | POST /api/strategies/stop | FC |
| getTrades | GET /api/strategies/trades | GET /api/strategies/trades | DTO |
| getPositions | GET /api/strategies/positions | GET /api/strategies/positions | DTO |
| getEquityCurve | GET /api/strategies/equityCurve | GET /api/strategies/equityCurve | DTO |
| getPerformance | GET /api/strategies/performance | GET /api/strategies/performance | DTO |
| getLogs | GET /api/strategies/logs | GET /api/strategies/logs | DTO |
| testConnection | POST /api/strategies/test-connection | POST /api/strategies/test-connection | PI |
| getNotifications | GET /api/strategies/notifications | GET /api/strategies/notifications | FC |
| getUnreadNotificationCount | GET /api/strategies/notifications/unread-count | GET /api/strategies/notifications/unread-count | DTO |
| markNotificationRead | POST /api/strategies/notifications/read | POST /api/strategies/notifications/read | FC |
| markAllNotificationsRead | POST /api/strategies/notifications/read-all | POST /api/strategies/notifications/read-all | FC |
| clearNotifications | DELETE /api/strategies/notifications/clear | DELETE /api/strategies/notifications/clear | FC |

### quickTradeApi (`/api/quick-trade`)
| getBalance | GET /api/quick-trade/balance | GET /api/quick-trade/balance | DTO |
| getPosition | GET /api/quick-trade/position | GET /api/quick-trade/position | DTO |
| placeOrder | POST /api/quick-trade/place-order | POST /api/quick-trade/place-order | DTO |
| closePosition | POST /api/quick-trade/close-position | POST /api/quick-trade/close-position | DTO |
| getHistory | GET /api/quick-trade/history | GET /api/quick-trade/history | PI |

### aiAnalysisApi (`/api/fast-analysis`) — entirely unimplemented
| analyze | POST /api/fast-analysis/analyze | — missing — | MISS |
| getHistory | GET /api/fast-analysis/history | — missing — | MISS |
| getAllHistory | GET /api/fast-analysis/history/all | — missing — | MISS |
| deleteHistory | DELETE /api/fast-analysis/history/{id} | — missing — | MISS |
| getPerformance | GET /api/fast-analysis/performance | — missing — | MISS |
| submitFeedback | POST /api/fast-analysis/feedback | — missing — | MISS |
| getSimilarPatterns | GET /api/fast-analysis/similar-patterns | — missing — | MISS |

### marketApi (`/api/community`) — entirely unimplemented
| getIndicators | GET /api/community/indicators | — missing — | MISS |
| getIndicator | GET /api/community/indicators/{id} | — missing — | MISS |
| purchase | POST /api/community/indicators/{id}/purchase | — missing — | MISS |
| syncIndicator | POST /api/community/indicators/{id}/sync | — missing — | MISS |
| getMyPurchases | GET /api/community/my-purchases | — missing — | MISS |
| getComments | GET /api/community/indicators/{id}/comments | — missing — | MISS |
| getIndicatorPerformance | GET /api/community/indicators/{id}/performance | — missing — | MISS |

### watchlistApi (`/api/market`)
| getList | GET /api/market/watchlist/get | GET /api/market/watchlist/get | DTO |
| add | POST /api/market/watchlist/add | POST /api/market/watchlist/add | FC |
| remove | POST /api/market/watchlist/remove | POST /api/market/watchlist/remove | FC |
| search | GET /api/market/symbols/search | GET /api/market/symbols/search | DTO |
| getHot | GET /api/market/symbols/hot | GET /api/market/symbols/hot | DTO |
| getPrices | GET /api/market/watchlist/prices | GET /api/market/watchlist/prices | DTO |

### klineApi (`/api/indicator`)
| getKline | GET /api/indicator/kline | GET /api/indicator/kline | DTO |
| getPrice | GET /api/indicator/price | GET /api/indicator/price | PI |

### indicatorApi (`/api/indicator`)
| getList | GET /api/indicator/getIndicators | GET /api/indicator/getIndicators | DTO |
| getParams | GET /api/indicator/getIndicatorParams | GET /api/indicator/getIndicatorParams | FC |
| parseStrategyConfig | POST /api/indicator/parseStrategyConfig | POST /api/indicator/parseStrategyConfig | DTO |

### userApi (`/api/users`)
| getProfile | GET /api/users/profile | GET /api/users/profile | FC |
| updateProfile | PUT /api/users/profile/update | PUT /api/users/profile/update | FC |
| getNotificationSettings | GET /api/users/notification-settings | GET /api/users/notification-settings | PI |
| updateNotificationSettings | PUT /api/users/notification-settings | PUT /api/users/notification-settings | PI |
| testNotificationSettings | POST /api/users/notification-settings/test | POST /api/users/notification-settings/test | PI |
| changePassword | POST /api/users/change-password | POST /api/users/change-password | FC |
| getMyCreditsLog | GET /api/users/my-credits-log | GET /api/users/my-credits-log | PI |
| getMyReferrals | GET /api/users/my-referrals | GET /api/users/my-referrals | PI |

### globalMarketApi (`/api/global-market`)
| getOverview | GET /api/global-market/overview | GET /api/global-market/overview | FC |
| getCalendar | GET /api/global-market/calendar | GET /api/global-market/calendar | FC |
| getSentiment | GET /api/global-market/sentiment | GET /api/global-market/sentiment | FC |

### billingApi (`/api/billing`) — entirely unimplemented
| listUsdtChains | GET /api/billing/usdt/chains | — missing — | MISS |
| getPlans | GET /api/billing/plans | — missing — | MISS |
| purchase | POST /api/billing/purchase | — missing — | MISS |
| createUsdtOrder | POST /api/billing/usdt/create | — missing — | MISS |
| getUsdtOrder | GET /api/billing/usdt/order/{orderId} | — missing — | MISS |

---

## 2. DTO Difference Table

Every entry: exact frontend expectation (quoted from `mobile/src/api/index.js`) vs. exact backend actual (quoted from `api/routers/*.py`/`api/schemas.py`), and the concrete failure mode.

| # | Endpoint | FE expects | BE actually does | Failure mode |
|---|---|---|---|---|
| 1 | `authApi.login` | body `data` incl. `turnstile_token` | `LoginRequest{email: EmailStr, password: str}` — no captcha field exists | `turnstile_token` silently dropped (no `extra="forbid"`); harmless since backend never checks it, but signals a fake security control on the frontend |
| 2 | `authApi.register` | `{email, code, username, password, turnstile_token, referral_code}` | `RegisterRequest{email, password (min_length=6), nickname: Optional}` | `code`, `username`, `turnstile_token`, `referral_code` all silently dropped — email verification code is never checked (backend has no code-verification logic at all), referral is never recorded |
| 3 | `credentialsApi.create` | `{name, exchange_id, api_key, secret_key, passphrase, account_no, hts_id, enable_demo_trading}` | `CredentialCreate{name, exchange_id, app_key: Optional, app_secret: Optional, account_no, hts_id, api_key: Optional, env: str="paper"}` | `secret_key` has no matching field → dropped, never persisted. `api_key` lands in `CredentialCreate.api_key` → stored as `api_key_enc`, but `dashboard.py`'s `_build_kis_client_from_cred()` reads `app_key_enc`/`app_secret_enc` — both stay empty. `passphrase` dropped (KIS doesn't use one, so this is fine to drop). `enable_demo_trading` dropped — `env` always defaults to `"paper"` regardless of the user's toggle. **Net: no KIS credential created via the app can ever produce a working broker connection.** |
| 4 | `strategyApi.getList` | `res.data?.strategies` (array) | `Resp.ok({"total": n, "items": [...]})` | key is `items`, not `strategies` → `getList()` always returns `[]` |
| 5 | `strategyApi.getTrades` | query `{id, limit}`; response `res.data.trades` | query requires `strategy_id` (not `id`), pagination is `page`/`page_size` (no `limit`); response `{"total", "items"}` | wrong required query param name → HTTP 422 before the `items` vs `trades` key mismatch even matters |
| 6 | `strategyApi.getPositions` | query `{id}`; response `res.data.positions` | requires `strategy_id`; response `{"items": [...]}` | same 422, then `items` vs `positions` key mismatch |
| 7 | `strategyApi.getEquityCurve` | query `{id}`; response expected to be a bare array (`ensureArray(res.data)`) | requires `strategy_id`; response `{"items": [...]}` (a dict) | 422, then `ensureArray` sees a dict, not an array → always `[]` |
| 8 | `strategyApi.getPerformance` | query `{id}` | requires `strategy_id` | 422 on every call; response body shape is otherwise fine once the param name is fixed |
| 9 | `strategyApi.getLogs` | query `{id, limit}`; response `res.data.logs` | requires `strategy_id`; pagination `page`/`page_size` (no `limit`); response `{"items": [...]}` | 422, then `items` vs `logs` key mismatch |
| 10 | `strategyApi.getUnreadNotificationCount` | `res.data?.unread` | `Resp.ok({"count": n})` | key is `count`, not `unread` → badge always renders 0 |
| 11 | `indicatorApi.getList` | `res.data?.indicators \|\| res.data` | `Resp.ok({"items": INDICATORS})` | key is `items`, not `indicators`, and it's not a bare array either → always `[]` |
| 12 | `indicatorApi.parseStrategyConfig` | `{strategyConfig: {...}, indicatorParams: [...]}` | `Resp.ok({"indicators": detected, "params": {}})` — a naive substring scan, `params` hardcoded to `{}` | keys don't match at all (`strategyConfig`/`indicatorParams` vs `indicators`/`params`), and even the semantic content is wrong (detected indicator names, not a parsed config) — not just a rename, the backend logic itself doesn't do what the frontend needs |
| 13 | `klineApi.getKline` | query incl. optional `before_time`; response a bare array (`ensureArray(res.data)`) | no `before_time` param exists (silently ignored by FastAPI); response `{"symbol", "timeframe", "items": bars}` | time-based pagination is a no-op; `ensureArray` sees a dict → charts never render (always `[]`) |
| 14 | `watchlistApi.getList` | bare array (`ensureArray(res.data)`) | `Resp.ok({"items": [...]})` | dict, not array → watchlist always renders empty |
| 15 | `watchlistApi.search` | bare array | `{"items": [...], "total": n}` | same `ensureArray` failure → always `[]` |
| 16 | `watchlistApi.getHot` | bare array | `{"items": [...]}` | same failure → always `[]` |
| 17 | `watchlistApi.getPrices` | single query param `watchlist=<JSON array>` | requires `symbols: str` (comma-separated, **required, no default**) + `market: str="us"` | `symbols` is required and never sent by the frontend → guaranteed HTTP 422 before any handler logic runs; contract is fundamentally different, not just renamed |
| 18 | `quickTradeApi.getBalance` | query `{credential_id, market_type}` | `credential_id`, `market: str="us"` (no `market_type` param — silently dropped, always defaults to `"us"`) | KR balances (`market_type: 'kr'`) are unreachable as coded; response fields are `{currency, total_eval, cash, positions}` but the UI template reads `balance?.available`/`balance?.total` — neither field exists → renders `undefined`/NaN |
| 19 | `quickTradeApi.getPosition` | `unwrapItems(res.data, 'positions')` — expects plural array | `Resp.ok({"symbol": symbol, "position": pos_or_null})` — singular object or null | plural-array vs singular-object mismatch, not just a key rename → always resolves to `[]` |
| 20 | `quickTradeApi.placeOrder` | backend requires `PlaceOrderRequest{credential_id, symbol, side, qty (required), price, market="us", exchange="NASD"}` | actual frontend payload (`quick-trade/index.vue`) sends `{credential_id, symbol, side, order_type, amount, price, leverage, market_type, source}` — **`qty` is never sent**, `amount` is sent instead | `qty` has no default → guaranteed HTTP 422 on every buy/sell attempt. Real KIS broker call (`kis_adapter/orders.py`) never gets invoked. |
| 21 | `quickTradeApi.closePosition` | backend requires `ClosePositionRequest{credential_id, symbol, qty (required), price (required), market="us", exchange="NASD"}` | actual payload sends `{credential_id, symbol, market_type, position_side, source}` — **both `qty` and `price` are missing entirely** | guaranteed HTTP 422 on every close-position attempt |
| 22 | `quickTradeApi.getHistory` | `unwrapItems(res.data, 'trades')` | `Resp.ok({"total": n, "items": [...]})` | `items` vs `trades` key mismatch; additionally see PI #30 below — even once fixed, this will stay empty since quick-trade orders are never persisted |

---

## 3. Compatibility Matrix

| Group | FC | DTO | PI | MISS | Total |
|---|---|---|---|---|---|
| authApi | 6 | 2 | 2 | 1 | 9 |
| dashboardApi | 2 | 0 | 0 | 0 | 2 |
| credentialsApi | 4 | 1 | 0 | 0 | 5 |
| strategyApi (incl. templates) | 11 | 8 | 3 | 0 | 22 |
| quickTradeApi | 0 | 4 | 1 | 0 | 5 |
| aiAnalysisApi | 0 | 0 | 0 | 7 | 7 |
| marketApi | 0 | 0 | 0 | 7 | 7 |
| watchlistApi | 2 | 4 | 0 | 0 | 6 |
| klineApi | 0 | 1 | 1 | 0 | 2 |
| indicatorApi | 1 | 2 | 0 | 0 | 3 |
| userApi | 3 | 0 | 5 | 0 | 8 |
| globalMarketApi | 3 | 0 | 0 | 0 | 3 |
| billingApi | 0 | 0 | 0 | 5 | 5 |
| **Total** | **31** | **22** | **11** | **20** | **84** |

No **URL differs (path-only)** cases exist anywhere — every mismatch found is either a full absence (Missing) or a parameter/response-shape problem (DTO/Partially implemented), never just a different route path for an otherwise-identical contract.

---

## 4. Missing Backend APIs (20 total)

Grouped by feature, with real-world reachability (from `docs/P5_FRONTEND_API_INVENTORY.md`'s call-site/router audit) so severity reflects actual user impact, not just a count.

### AI Analysis (7 endpoints, `/api/fast-analysis/*`) — **highest severity**
`analyze`, `getHistory`, `getAllHistory`, `deleteHistory`, `getPerformance`, `submitFeedback`, `getSimilarPatterns`. No backend router exists anywhere (confirmed via repo-wide grep for `fast-analysis`/`fast_analysis` — zero Python matches). Of these, `analyze`, `getAllHistory`, `deleteHistory`, and `submitFeedback` are **actively called** from `views/ai-analysis/index.vue`, `views/ai-analysis/History.vue`, and `views/ai-hub/index.vue` — all reachable on **both** `mobile/` and `frontend/`. This is a core, shipped, reachable feature that is entirely non-functional today. `getHistory`, `getPerformance`, `getSimilarPatterns` are additionally unused by the frontend (per P5-01B-1), so fixing/implementing them is lower priority than the four that are live.

### Community indicator marketplace (7 endpoints, `/api/community/*`)
`getIndicators`, `getIndicator`, `purchase`, `syncIndicator`, `getMyPurchases`, `getComments`, `getIndicatorPerformance`. No backend implementation anywhere (confirmed via repo-wide grep for `community` — zero Python matches). Called from `views/market/index.vue`, `views/market/Detail.vue`, `views/market/MyPurchases.vue` — all three are **unreachable on `mobile/`** (missing router entries, per P5-01B-1) but reachable on `frontend/` (web). `syncIndicator` is additionally unused by any frontend call site. Real-world severity: fully broken for web users today, moot for the shipped mobile app.

### Billing (5 endpoints, `/api/billing/*`)
`listUsdtChains`, `getPlans`, `purchase`, `createUsdtOrder`, `getUsdtOrder`. No backend implementation anywhere (confirmed via repo-wide grep for `billing`/`usdt` — zero Python matches). Called from `views/profile/Credits.vue`, which is likewise **unreachable on `mobile/`**, reachable on `frontend/`. `purchase` (the non-USDT plan-purchase path) is unused by any frontend call site — only the USDT flow (`listUsdtChains`/`createUsdtOrder`/`getUsdtOrder`) is actually exercised, and `getPlans` feeds the plan list. Same severity profile as the community marketplace: broken on web, moot on mobile.

### Auth (1 endpoint)
`authApi.loginWithCode` (`POST /api/auth/login-code`) — no backend route. This one is **unused by the frontend today** (per P5-01B-1's call-site audit — no view invokes it), so it's a dormant gap rather than a live bug; lowest priority of the four Missing groups.

---

## 5. Adapter Candidates

Mismatches fixable by a thin request/response translation layer (a BFF/gateway adapter, or equivalently a direct frontend patch) **without any backend logic change**, because the backend already has the right data or capability, just under a different name or shape. For each: the reason, the transform needed, and whether frontend changes are avoidable (i.e., could a gateway-side adapter absorb this instead of touching `mobile/`/`frontend/` source).

| Mismatch | Reason | Required adapter | Frontend changes avoidable? |
|---|---|---|---|
| Systemic `{"items":[...]}` vs bare-array/renamed-key (watchlist get/search/hot, strategy list/trades/positions/equityCurve/logs, indicator getIndicators, kline) | Backend has one uniform list-wrapping convention (`items`); frontend was written against per-resource semantic key names | A single response-unwrapping adapter: map `items`→(bare array, or `trades`/`positions`/`strategies`/`indicators`/`logs` as appropriate per route) | **Yes** — one gateway-side transform rule per route covers all ~9 instances; equally fixable by patching `ensureArray`/`unwrapItems` call sites in `mobile/src/api/index.js` directly, which is arguably simpler than standing up a gateway for this alone |
| `strategy_id` vs `id` query param (trades/positions/equityCurve/performance/logs) | Backend's five strategy sub-resource routes all named their required query param `strategy_id`; frontend consistently sends `id` | Rename `id`→`strategy_id` on the way out (or in on the gateway) | **Yes** — trivial rename, no data loss either direction |
| `strategyApi.getUnreadNotificationCount`: `count` vs `unread` | Pure key-naming difference | Rename `count`→`unread` in the response | **Yes** |
| `credentialsApi.create` field mismatch | Backend's `CredentialCreate` model already has `app_key`, `app_secret`, `env` fields — the frontend just uses different names (`api_key`, `secret_key`, `enable_demo_trading`) and the backend has no field for `passphrase` (fine — KIS doesn't use one) | Adapter maps `api_key`→`app_key`, `secret_key`→`app_secret`, `enable_demo_trading` (`true`/`false`)→`env` (`"real"`/`"paper"`), drops `passphrase` | **Yes** — no backend schema change needed at all, since every destination field already exists; this is the cheapest possible fix for the single most consequential bug in this audit |
| `quickTradeApi.getBalance`/`getPosition`: `market_type` vs `market` | Pure param-naming difference | Rename `market_type`→`market` | **Yes** |
| `quickTradeApi.placeOrder`: `amount` vs `qty` | Frontend computes an `amount` value that the backend calls `qty` | Rename `amount`→`qty` before sending | **Yes** — assuming `amount` and `qty` mean the same quantity (share count), which the field names suggest but should be confirmed against `kis_adapter/orders.py`'s actual usage before treating this as a pure rename |
| `quickTradeApi.closePosition`: missing `qty`/`price` | Frontend doesn't currently collect/send these at all | Frontend (or a stateful gateway that already knows the open position's qty/price) must supply real values — a *stateless* adapter cannot invent `qty`/`price` out of nothing, so this is only adapter-fixable if the gateway maintains position state itself; otherwise it's a frontend form-field addition | **Partially** — needs either a frontend change to collect/send the values, or a stateful gateway (more complex than a simple rename) |
| `watchlistApi.getPrices`: `watchlist=<JSON>` vs `symbols=<CSV>&market=` | Backend wants a comma-separated symbol list plus a separate market param; frontend sends one JSON-encoded param | Adapter parses the JSON, extracts symbols, joins as CSV, forwards `market` from the first item (or a sensible default) | **Yes**, though the adapter needs to decide how to collapse a mixed-market watchlist into a single `market` value — the backend's contract doesn't support per-symbol markets in one call, so this may need either an adapter that batches by market or a backend enhancement (see §6 note) |

---

## 6. Backend-only resolution candidates

Mismatches no adapter can fix because the backend genuinely lacks the data, persistence, or logic — the fix has to happen in `api/`, not in a translation layer.

- **All 20 Missing endpoints** (§4: AI Analysis ×7, Community marketplace ×7, Billing ×5, `login-code` ×1) — there is no existing backend capability to reshape or rename toward; the feature has to be built.
- **`strategyApi.aiGenerate`** — currently returns a hardcoded placeholder script regardless of input (`intent`, `prompt`, `symbol`, etc. are all ignored). No adapter can produce a real AI-generated strategy; requires an actual LLM integration server-side.
- **`strategyApi.testConnection`** — always returns `{"connected": true, "latency_ms": 42}` without testing anything. Needs a real connectivity check against the stored broker credential.
- **`authApi.sendCode` / `authApi.resetPassword`** — both are literal no-ops (`"Code sent (not implemented)"` / `"Not implemented"`), no email/SMS is ever sent, no code is ever generated or checked. Needs a real email-delivery + code-verification implementation.
- **`userApi.getNotificationSettings` / `updateNotificationSettings` / `testNotificationSettings`** — settings are hard-coded on GET and merely echoed (not persisted) on PUT; there is no `NotificationSettings` table/column on `User` in `api/models.py`. Needs a real persistence model plus an actual test-dispatch code path.
- **`userApi.getMyCreditsLog` / `getMyReferrals`** — both always return empty results; no credits-log or referral table/model exists anywhere in `api/models.py`. Needs the feature built from scratch (model, write paths, read paths), not just a response reshape.
- **`quickTradeApi.getHistory`** — even after the `items`→`trades` key rename (§5), this will remain permanently empty: `place-order`/`close-position` never write to the `Trade` table (confirmed — no `Trade(` / `db.add(Trade` call anywhere in `api/routers/quick_trade.py`, and the code has an explicit comment acknowledging quick-trade fills aren't persisted). Needs the order-placement handlers to actually persist fills.
- **`indicatorApi.parseStrategyConfig`** — the backend's response isn't just mis-keyed, its *content* is wrong: it does a naive substring scan for indicator names and hardcodes `params: {}`, rather than actually parsing a strategy config out of submitted code. A key-rename adapter would just relabel the wrong data; the parsing logic itself needs to be implemented.
- **`klineApi.getKline` / `klineApi.getPrice` data source** — both always call yfinance regardless of the `market` parameter, despite the docstring's claim of KIS-routing for Korean symbols. Beyond the response-shape fix (§5-adjacent, §2 #13), delivering real KR-market data (or crypto, since the frontend defaults `market: 'Crypto'`) requires wiring in the actual KIS/Kiwoom/crypto data sources — an adapter can reshape yfinance's response but cannot make it correct for symbols yfinance doesn't cover.
- **`watchlistApi.getPrices`'s multi-market limitation** (extends the §5 note) — if a real fix requires per-symbol market routing rather than one `market` value per call, that's a backend query-contract change (accept a list of `{symbol, market}` pairs), not something a stateless adapter can paper over.
