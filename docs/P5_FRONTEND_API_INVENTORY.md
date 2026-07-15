# P5-01B-1: Frontend API Inventory Audit

> **Analysis only.** No code was changed, no architecture was redesigned to produce this document. Every entry below is taken directly from source (`mobile/src/api/index.js`, `frontend/src/api/index.js`, `mobile/src/views/**/*.vue`, `mobile/src/components/*.vue`, `mobile/src/stores/index.js`, `backend/websocket/server.py`) — nothing is inferred.

**Date:** 2026-07-14
**Scope inspected:** `frontend/`, `mobile/`, `services/` (does not exist), `api/` (top-level — server-side Python, out of scope), `composables/`/`hooks/` (do not exist), axios/fetch usage repo-wide, WebSocket usage repo-wide, event-emitter patterns, Pinia stores. No Electron code exists anywhere in this repo.

---

## 0. Summary numbers

| Metric | Value |
|---|---|
| **Total REST endpoints** | **84** distinct exported functions across 13 API groups, each a unique method+path combination. Defined identically (byte-for-byte) in both `mobile/src/api/index.js` and `frontend/src/api/index.js`. |
| **Total WebSocket events** | **0** consumed by any frontend code. The backend (`backend/websocket/server.py`) defines 4 broadcast channels + 4 handshake events (8 total), none of which has a frontend subscriber anywhere in `mobile/` or `frontend/`. |
| **Duplicated endpoints** | 2 functional-duplicate pairs (`authApi.changePassword` / `userApi.changePassword`; `aiAnalysisApi.getHistory` / `getAllHistory`), plus the entire 84-function surface is duplicated wholesale across `mobile/` and `frontend/` (same file, byte-identical). |
| **Unused APIs** | **15 of 84** functions are defined but never called from any view, component, or store (including `authApi.loginWithCode`, marked ⛔ in §2 but omitted from this count in an earlier draft). Separately, **2 call sites invoke functions that don't exist** (phantom calls — a distinct defect category, not counted in the 84 or the 15). |

---

## 1. Base HTTP client & interceptors

Source: `mobile/src/api/index.js:1-369` (identical in `frontend/src/api/index.js`).

### Axios instance (lines 7-20)
```js
const http = axios.create({
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' }
})
```
No `baseURL` is set at creation — it's assigned per-request in the interceptor via `getBaseUrl()`:
```js
export const getBaseUrl = () => {
  const serverUrl = localStorage.getItem('serverUrl')?.trim()
  if (serverUrl) return serverUrl.replace(/\/$/, '')
  return DEFAULT_SERVER_URL
}
```
`DEFAULT_SERVER_URL` is `''` (empty string, same-origin) in both `mobile/src/config/index.js` and `frontend/src/config/index.js`. Default timeout is 30000ms; three call sites override it: `strategyApi.aiGenerate` (`{ raw: true, timeout: 180000 }`), `aiAnalysisApi.analyze` (`{ timeout: 300000 }`), and the raw `ServerConfig.vue` health-check call (`{ timeout: 5000 }`).

### Request interceptor (lines 305-316)
```js
http.interceptors.request.use((config) => {
  config.baseURL = getBaseUrl()
  const token = localStorage.getItem('token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
}, (error) => Promise.reject(error))
```
Only header ever added globally: `Authorization: Bearer <token>` from `localStorage.token`. **No per-call custom headers exist anywhere in the file** — the only per-call config overrides touch `raw`/`timeout`, never `headers`.

### Response interceptor (lines 318-369)

Success branch:
- If `response.config.raw` (set only by `strategyApi.aiGenerate`), returns `response.data` unmodified — no envelope unwrapping.
- If `res.code === 1 || res.code === 200 || res.success`, returns the full envelope `res` (`{code, data, msg}`) — callers read `.data` themselves.
- If HTTP status 200 and `res.code` is `undefined`/`null`, synthesizes `{ code: 1, data: res }`.
- Otherwise (business failure): checks `isSessionExpiredBusinessResponse(res)` — if true and the URL isn't an auth-credential endpoint, calls `redirectToLoginIfNeeded(reqUrl)`. Always shows a toast (`res.msg || res.message || '请求失败'`) and rejects with `new Error(...)`.

**Session-expiry detection** (lines 292-303), quoted verbatim:
```js
function isSessionExpiredBusinessResponse(res) {
  if (!res || typeof res !== 'object') return false
  const code = res.code
  const msg = String(res.msg || res.message || '')
  if (code === 401) return true
  if (code === -1 || code === 403) {
    return /未登录|请重新登录|请登录|登录失效|登录过期|token|Token|会话|过期|失效|鉴权|unauthorized|invalid\s*token|挤掉|elsewhere|session/i.test(msg)
  }
  return false
}
```

**Auth-credential exemption** (lines 263-264), quoted verbatim:
```js
const isAuthCredentialRequest = (url) =>
  /\/api\/auth\/(login|register|send-code|reset-password)(?:\?|$)/i.test(String(url || ''))
```

`redirectToLoginIfNeeded()` calls `clearAuthSession()` (removes `localStorage.token`, calls `useUserStore().logout()`), then `router.replace({ path: '/login', query: { redirect: <current fullPath> } })` unless already on `/login` or the failing request was itself an auth-credential call.

Error branch (network/HTTP-level failures, lines 340-367) maps `error.response.status` → Korean message (`401` → "未授权，请重新登录" + `redirectToLoginIfNeeded`; `403` → "拒绝访问"; `404` → "请求地址不存在"; `500` → "服务器错误"; default → server-provided message), plus timeout/network-error string matching. Always toasts and rejects with the original error.

### Exception: one raw, non-interceptor HTTP call
`mobile/src/views/profile/ServerConfig.vue:84` and `frontend/src/views/profile/ServerConfig.vue:84` (identical):
```js
const res = await axios.get(`${url}/api/health`, { timeout: 5000 })
```
Uses the raw `axios` import directly — no `baseURL` resolution, no auth header, no envelope unwrapping. Purpose: testing connectivity to a candidate server URL before saving it to `localStorage.serverUrl`. This is the **only** HTTP call anywhere in the repo that bypasses the shared `http` instance; no other `fetch()`, `XMLHttpRequest`, or second `axios.create()` exists in any `.js/.ts/.vue` file.

---

## 2. Per-group endpoint tables

All 84 functions, from `mobile/src/api/index.js` (identical in `frontend/`). "Response" reflects the shape returned *after* interceptor transform, as coded — not inferred. ✅ = called from at least one view/component; ⛔ = never called anywhere (see §7).

### `authApi` (lines 371-381)

| Fn | Method | Path | Body | Query | Response | Used |
|---|---|---|---|---|---|---|
| `login` | POST | `/api/auth/login` | `data` | — | envelope | ✅ |
| `loginWithCode` | POST | `/api/auth/login-code` | `data` | — | envelope | ⛔ |
| `register` | POST | `/api/auth/register` | `data` | — | envelope | ✅ |
| `sendCode` | POST | `/api/auth/send-code` | `data` | — | envelope | ✅ |
| `resetPassword` | POST | `/api/auth/reset-password` | `data` | — | envelope | ✅ |
| `getSecurityConfig` | GET | `/api/auth/security-config` | — | — | envelope | ✅ |
| `getInfo` | GET | `/api/auth/info` | — | — | envelope | ✅ |
| `logout` | POST | `/api/auth/logout` | — | — | envelope | ✅ |
| `changePassword` | POST | `/api/auth/change-password` | `data` | — | envelope | ✅ |

Note: `loginWithCode` (`/api/auth/login-code`) is defined but no view in the call-site inventory (§4) invokes it — the login screen's code-based flow uses `sendCode` + `login`/`register` instead. Marking ⛔.

### `dashboardApi` (lines 383-398)

| Fn | Method | Path | Body | Query | Response | Used |
|---|---|---|---|---|---|---|
| `getSummary` | GET | `/api/dashboard/summary` | — | — | `{...res, data: res.data \|\| {}}` | ✅ |
| `getPendingOrders` | GET | `/api/dashboard/pendingOrders` | — | `params` (default `{}`) | `{...res, data: res.data \|\| {items:[], total:0}}` | ⛔ |

### `credentialsApi` (lines 400-428)

| Fn | Method | Path | Body | Query | Response | Used |
|---|---|---|---|---|---|---|
| `list` | GET | `/api/credentials/list` | — | — | `{...res, data: unwrapItems(res.data)}` | ✅ |
| `get` | GET | `/api/credentials/get` | — | `{id}` | `{...res, data: res.data \|\| null}` | ⛔ |
| `create` | POST | `/api/credentials/create` | `data` | — | envelope | ✅ |
| `delete` | DELETE | `/api/credentials/delete` | — | `{id}` | envelope | ✅ |
| `getEgressIp` | GET | `/api/credentials/egress-ip` | — | — | `{...res, data: res.data \|\| {}}` | ✅ |

### `strategyApi` (lines 430-535)

| Fn | Method | Path | Body | Query | Response | Used |
|---|---|---|---|---|---|---|
| `getTemplates` | GET | `/api/templates` | — | `params` (default `{}`) | `{...res, data: ensureArray(res.data)}` | ⛔ |
| `getTemplate` | GET | `/api/templates/${key}` | — | — | `{...res, data: res.data \|\| null}` | ⛔ |
| `create` | POST | `/api/strategies/create` | `payload` | — | envelope | ✅ |
| `batchCreate` | POST | `/api/strategies/batch-create` | `payload` | — | envelope | ✅ |
| `update` | PUT | `/api/strategies/update` | `{id, ...payload}` | — | envelope | ✅ |
| `delete` | DELETE | `/api/strategies/delete` | — | `{id}` | envelope | ✅ |
| `aiGenerate` | POST | `/api/strategies/ai-generate` | `payload` | — (`raw:true, timeout:180000` config) | raw response | ✅ |
| `getList` | GET | `/api/strategies` | — | — | `{...res, data: ensureArray(...).map(normalizeStrategy)}` | ✅ |
| `getDetail` | GET | `/api/strategies/detail` | — | `{id}` | normalized strategy or null | ✅ |
| `start` | POST | `/api/strategies/start` | `null` | `{id}` | envelope | ✅ |
| `stop` | POST | `/api/strategies/stop` | `null` | `{id}` | envelope | ✅ |
| `getTrades` | GET | `/api/strategies/trades` | — | `{id, limit=50}` | `unwrapItems(res.data,'trades')` | ✅ |
| `getPositions` | GET | `/api/strategies/positions` | — | `{id}` | `unwrapItems(res.data,'positions')` | ✅ |
| `getEquityCurve` | GET | `/api/strategies/equityCurve` | — | `{id}` | `ensureArray(res.data)` | ✅ |
| `getPerformance` | GET | `/api/strategies/performance` | — | `{id}` | `res.data \|\| {}` | ✅ |
| `getLogs` | GET | `/api/strategies/logs` | — | `{id, limit=100}` | `unwrapItems(res.data,'logs')` | ✅ |
| `testConnection` | POST | `/api/strategies/test-connection` | `data` | — | envelope | ⛔ |
| `getNotifications` | GET | `/api/strategies/notifications` | — | `params` (default `{}`) | `unwrapItems(res.data)` | ✅ |
| `getUnreadNotificationCount` | GET | `/api/strategies/notifications/unread-count` | — | — | `res.data?.unread \|\| 0` | ✅ |
| `markNotificationRead` | POST | `/api/strategies/notifications/read` | `{id}` | — | envelope | ✅ |
| `markAllNotificationsRead` | POST | `/api/strategies/notifications/read-all` | — | — | envelope | ✅ |
| `clearNotifications` | DELETE | `/api/strategies/notifications/clear` | — | — | envelope | ⛔ |

### `quickTradeApi` (lines 537-572)

| Fn | Method | Path | Body | Query | Response | Used |
|---|---|---|---|---|---|---|
| `getBalance` | GET | `/api/quick-trade/balance` | — | `{credential_id, market_type='spot'}` | `res.data \|\| {available:0,total:0,currency:'USDT'}` | ✅ |
| `getPosition` | GET | `/api/quick-trade/position` | — | `{credential_id, symbol, market_type='spot'}` | `unwrapItems(res.data,'positions')` | ✅ |
| `placeOrder` | POST | `/api/quick-trade/place-order` | `payload` | — | envelope | ✅ |
| `closePosition` | POST | `/api/quick-trade/close-position` | `payload` | — | envelope | ✅ |
| `getHistory` | GET | `/api/quick-trade/history` | — | `params` (default `{}`) | `unwrapItems(res.data,'trades')` | ✅ |

### `aiAnalysisApi` (lines 574-611)

| Fn | Method | Path | Body | Query | Response | Used |
|---|---|---|---|---|---|---|
| `analyze` | POST | `/api/fast-analysis/analyze` | `payload` | — (`timeout:300000` config) | envelope | ✅ |
| `getHistory` | GET | `/api/fast-analysis/history` | — | `params` (default `{}`) | `unwrapItems(res.data)` | ⛔ |
| `getAllHistory` | GET | `/api/fast-analysis/history/all` | — | `params` (default `{}`) | `{list, total, page, pagesize}` | ✅ |
| `deleteHistory` | DELETE | `/api/fast-analysis/history/${memoryId}` | — | — | envelope | ✅ |
| `getPerformance` | GET | `/api/fast-analysis/performance` | — | `params` (default `{}`) | `res.data \|\| {}` | ⛔ |
| `submitFeedback` | POST | `/api/fast-analysis/feedback` | `payload` | — | envelope | ✅ |
| `getSimilarPatterns` | GET | `/api/fast-analysis/similar-patterns` | — | `params` (default `{}`) | `res.data \|\| {}` | ⛔ |

### `marketApi` (lines 613-662)

| Fn | Method | Path | Body | Query | Response | Used |
|---|---|---|---|---|---|---|
| `getIndicators` | GET | `/api/community/indicators` | — | `params` (default `{}`) | `{items, total, page, page_size}` | ✅ |
| `getIndicator` | GET | `/api/community/indicators/${id}` | — | — | `res.data \|\| null` | ✅ |
| `purchase` | POST | `/api/community/indicators/${id}/purchase` | — | — | envelope | ✅ |
| `syncIndicator` | POST | `/api/community/indicators/${id}/sync` | — | — | envelope | ⛔ |
| `getMyPurchases` | GET | `/api/community/my-purchases` | — | `params` (default `{}`) | `{items, total}` | ✅ |
| `getComments` | GET | `/api/community/indicators/${id}/comments` | — | `params` (default `{}`) | `{items, total}` | ✅ |
| `getIndicatorPerformance` | GET | `/api/community/indicators/${id}/performance` | — | — | `res.data \|\| {}` | ✅ |

### `watchlistApi` (lines 664-702)

| Fn | Method | Path | Body | Query | Response | Used |
|---|---|---|---|---|---|---|
| `getList` | GET | `/api/market/watchlist/get` | — | — | mapped array | ✅ |
| `add` | POST | `/api/market/watchlist/add` | `payload` | — | envelope | ✅ |
| `remove` | POST | `/api/market/watchlist/remove` | `{symbol}` | — | envelope | ✅ |
| `search` | GET | `/api/market/symbols/search` | — | `params` (required) | `ensureArray(res.data)` | ✅ |
| `getHot` | GET | `/api/market/symbols/hot` | — | `params` (required) | `ensureArray(res.data)` | ✅ |
| `getPrices` | GET | `/api/market/watchlist/prices` | — | `{watchlist: JSON.stringify(list\|\|[])}` | `ensureArray(res.data)` | ✅ |

### `klineApi` (lines 704-721)

| Fn | Method | Path | Body | Query | Response | Used |
|---|---|---|---|---|---|---|
| `getKline` | GET | `/api/indicator/kline` | — | `{market='Crypto', symbol, timeframe='1h', limit=200[, before_time]}` | `ensureArray(res.data)` | ✅ |
| `getPrice` | GET | `/api/indicator/price` | — | `{market='Crypto', symbol}` | `res.data \|\| null` | ⛔ |

**Note:** no `getTicker` function exists in this group (see §7 phantom call).

### `indicatorApi` (lines 723-745)

| Fn | Method | Path | Body | Query | Response | Used |
|---|---|---|---|---|---|---|
| `getList` | GET | `/api/indicator/getIndicators` | — | — | `ensureArray(res.data?.indicators \|\| res.data)` | ✅ |
| `getParams` | GET | `/api/indicator/getIndicatorParams` | — | `{indicator_id}` | array | ✅ |
| `parseStrategyConfig` | POST | `/api/indicator/parseStrategyConfig` | `{code}` | — | `res.data \|\| {strategyConfig:{}, indicatorParams:[]}` | ✅ |

**Note:** no `getIndicators` function exists on `indicatorApi` (see §7 phantom call — `getList` is the actual name; the literal path `/api/indicator/getIndicators` is what `getList` calls, which is presumably the source of the naming confusion).

### `userApi` (lines 747-782)

| Fn | Method | Path | Body | Query | Response | Used |
|---|---|---|---|---|---|---|
| `getProfile` | GET | `/api/users/profile` | — | — | envelope | ✅ |
| `updateProfile` | PUT | `/api/users/profile/update` | `data` | — | envelope | ⛔ |
| `getNotificationSettings` | GET | `/api/users/notification-settings` | — | — | envelope | ✅ |
| `updateNotificationSettings` | PUT | `/api/users/notification-settings` | `data` | — | envelope | ✅ |
| `testNotificationSettings` | POST | `/api/users/notification-settings/test` | — | — | envelope | ✅ |
| `changePassword` | POST | `/api/users/change-password` | `data` | — | envelope | ⛔ |
| `getMyCreditsLog` | GET | `/api/users/my-credits-log` | — | `params` (default `{}`) | `{list, items, total, page, page_size, total_pages}` | ✅ |
| `getMyReferrals` | GET | `/api/users/my-referrals` | — | `params` (default `{}`) | `{list, total, referral_code, referral_bonus, register_bonus}` | ✅ |

### `globalMarketApi` (lines 784-810)

| Fn | Method | Path | Body | Query | Response | Used |
|---|---|---|---|---|---|---|
| `getOverview` | GET | `/api/global-market/overview` | — | — | `res.data \|\| {indices:[]}` | ✅ |
| `getCalendar` | GET | `/api/global-market/calendar` | — | `params` (default `{}`) | normalized calendar events | ✅ |
| `getSentiment` | GET | `/api/global-market/sentiment` | — | — | normalized sentiment | ✅ |

### `billingApi` (lines 812-842)

| Fn | Method | Path | Body | Query | Response | Used |
|---|---|---|---|---|---|---|
| `listUsdtChains` | GET | `/api/billing/usdt/chains` | — | — | `res.data \|\| {chains:[]}` | ✅ |
| `getPlans` | GET | `/api/billing/plans` | — | — | `res.data \|\| {}` | ✅ |
| `purchase` | POST | `/api/billing/purchase` | `{plan}` | — | envelope | ⛔ |
| `createUsdtOrder` | POST | `/api/billing/usdt/create` | `{plan[, chain]}` | — | envelope | ✅ |
| `getUsdtOrder` | GET | `/api/billing/usdt/order/${orderId}` | — | `{refresh: refresh?1:0}` (default `true`) | envelope | ✅ |

---

## 3. Call-site inventory, by screen (`mobile/src/`)

Only files with API calls are listed (27 of 31 `.vue` files under `views/`+`components/`; `assets/AssetDetail.vue`, `profile/Language.vue`, `profile/ServerConfig.vue` (raw axios only — see §1), and `trading/CreateBot.vue` make no API-layer calls). No Pinia store action calls the API layer directly — confirmed by full read of `mobile/src/stores/index.js` (every action is a synchronous local/`localStorage` setter).

Legend: 🚫 = screen has no registered route in `mobile/src/router/index.js` (unreachable in the shipped mobile app).

### `views/home/index.vue` — `Home`
- `dashboardApi.getSummary`, `strategyApi.getList`, `credentialsApi.list`, `strategyApi.getUnreadNotificationCount`, `watchlistApi.getList` — `Promise.allSettled` in `loadData()`, called from `mounted()` and `refreshData()` (`@click` on asset-hero card). → `dashboardStore.setSummary`, `strategyStore.setStrategies`, `credentialsStore.setItems`, `notificationStore.setUnreadCount`, `watchlistStore.setItems`.
- `watchlistApi.getPrices` — `refreshPrices()`, called at end of `loadData()` and from a **30s `setInterval`** (`priceTimer`, set in `mounted`, cleared in `beforeUnmount`). → local `this.priceMap`.
- `watchlistApi.getList` — `onSymbolPicked`/`onSymbolPickerClose` (SymbolPicker `@pick`/`@close`). → `watchlistStore.setItems`, then `refreshPrices()`.

### `views/login/index.vue` — `Login`
- `authApi.getSecurityConfig` — `initSecurity()`, `async mounted()`. → local `securityConfig`/`oauthConfig`.
- `authApi.sendCode` — `sendCode(type)`, `@click="sendCode('register')"` / `@click="sendCode('reset_password')"`. → toast only.
- `authApi.getInfo` — `finalizeLogin()`, called from `handleLogin()` and `handleOAuthCallback()` (itself from `mounted()` and a `watch: { '$route.query' }` on `oauth_token`/`oauth_error`). → `userStore.setUserInfo`.
- `authApi.login` — `handleLogin()`, `@click="handleSubmit"` / `@keyup.enter`. → `userStore.setToken` (writes `localStorage.token`), `userStore.setUserInfo`.
- `authApi.register` — `handleRegister()`, `handleSubmit` chain when `mode==='register'`. → `userStore.setToken`/`setUserInfo`.
- `authApi.resetPassword` — `handleReset()`, `handleSubmit` chain when `mode==='forgot'`. → toast only, local mode switch.

### `views/profile/index.vue` — `Profile`
- `userApi.getProfile`, `credentialsApi.list`, `strategyApi.getUnreadNotificationCount`, `userApi.getMyReferrals({page:1,page_size:1})` — `Promise.allSettled` in `loadData()`, `mounted()`. → `userStore.setUserInfo`, `credentialsStore.setItems`, `notificationStore.setUnreadCount`, local `referralData`.
- `authApi.logout` — `handleLogout()`, `@click` after `showConfirmDialog`. → `userStore.logout()` regardless of API outcome, then `router.replace('/login')`.
- ⚠️ `credentialCount()` computed reads `credentialsStore.cryptoItems.length` — **undefined getter, throws at runtime** (see §5).

### `views/profile/Credentials.vue` — `CredentialList`
- `credentialsApi.list` + `credentialsApi.getEgressIp` — `Promise.all` in `loadData()`, `mounted()`. → `credentialsStore.setItems`, `credentialsStore.setEgressIp`.
- `credentialsApi.delete` — `removeCredential(item)`, `@click` after confirm dialog. → toast, re-calls `loadData()`.
- ⚠️ `credentials()` computed reads `credentialsStore.cryptoItems` — same undefined-getter bug.

### `views/profile/CredentialForm.vue` — `CredentialForm`
- `credentialsApi.create` — `submit()`, `@click="submit"` after local `validate()`. → toast, `router.replace('/profile/credentials')`.

### `views/profile/Notifications.vue` — `Notifications`
- `strategyApi.getNotifications({limit:100})` + `strategyApi.getUnreadNotificationCount` — `Promise.all` in `loadNotifications()`, `mounted()` + `onRefresh()` (pull-to-refresh). → `notificationStore.setNotifications`, `.setUnreadCount`.
- `strategyApi.markNotificationRead` — `markRead(item)`, `@click`. → `notificationStore.markAsRead`.
- `strategyApi.markAllNotificationsRead` — `markAllRead()`, `@click` in nav bar. → `notificationStore.markAllAsRead`.

### `views/profile/NotificationSettings.vue` — `NotificationSettings`
- `userApi.getNotificationSettings` — `load()`, `mounted()`. → local `channels`/`form`.
- `userApi.updateNotificationSettings` — `handleSave()`, `@click`. → toast only.
- `userApi.testNotificationSettings` — `handleTest()`, `@click`. → toast only.

### `views/profile/Security.vue` — `ProfileSecurity`
- `authApi.sendCode({type:'change_password'})` — `handleSendCode()`, `@click`. → toast + local cooldown.
- `authApi.changePassword` — `handleSubmit()`, `@click`. → toast, form reset, delayed `router.back()`.

### `views/profile/About.vue` — `About`
- `authApi.getSecurityConfig` — `prefetchVersion()` (`mounted()`) and `checkUpdate()` (`@click`). → local version-check fields.

### `views/profile/Credits.vue` — `ProfileCredits` 🚫 *(unreachable on mobile — route exists on `frontend/` only)*
- `userApi.getProfile`, `billingApi.getPlans`, `userApi.getMyCreditsLog({page:1,page_size:30})` — `Promise.allSettled` in `load()`, `mounted()`. → local `billing`/`plans`/`log`.
- `billingApi.listUsdtChains` — `handlePurchase(plan)`, `@click` on plan card. → local `availableChains`/`selectedChain`.
- `billingApi.createUsdtOrder` — `confirmChain()`, `@click`. → local `order`, then `generateQr()`/`startPolling()`.
- `billingApi.getUsdtOrder(order_id, true)` — `refreshOrder(isPolling)`, from a **5s `setInterval`** (`pollTimer`, started after order creation) and `@click="refreshOrder"` (manual refresh). → local `order`; on `status==='confirmed'` re-calls `load()` and stops the poll.

### `views/profile/Referral.vue` — `ProfileReferral` 🚫 *(unreachable on mobile)*
- `userApi.getMyReferrals({page:1,page_size:20})` — `load()`, `mounted()`. → local `data`/`list`/`total`.

### `views/assets/index.vue` — `Assets` 🚫 *(router redirects `/assets` → `/home`)*
- `dashboardApi.getSummary` — `loadData()`, `mounted()` + `onRefresh()` (pull-to-refresh). → `dashboardStore.setSummary`.

### `views/home/MacroData.vue` — `MacroData` 🚫 *(no route registered anywhere, either app)*
- `globalMarketApi.getOverview`/`getCalendar({limit:10})`/`getSentiment` — `Promise.all` in `loadData()`, `mounted()` + `onRefresh()`. → local `indices`/`calendar`/`sentiment`.

### `views/home/StockDetail.vue` — `StockDetail` 🚫 *(no route registered anywhere; also broken — see §7)*
- `klineApi.getTicker(symbol, market)` — `loadData()`, `mounted()`. **Function does not exist** — see §7.

### `views/market/index.vue` — `Market` 🚫 *(unreachable on mobile)*
- `marketApi.getIndicators({page, page_size, keyword, pricing_type, sort_by})` — `loadMore()`. Triggered by `reload()` from `mounted()`, `@search="reload"` (van-search submit), filter-chip `@click`, sort-picker confirm, and the `van-list` `@load="loadMore"` infinite-scroll event. → local `items`.

### `views/market/Detail.vue` — `MarketDetail` 🚫 *(unreachable on mobile)*
- `marketApi.getIndicator(id)` + `marketApi.getIndicatorPerformance(id)` — `Promise.allSettled` in `load()`, `mounted()`. → local `indicator`/`performance`.
- `marketApi.getComments(id, {page, page_size})` — `loadComments(page)` from `load()` and `loadMoreComments()` (`@click`). → local `comments`.
- `marketApi.purchase(id)` — `handlePurchase()`, `@click` after confirm dialog. → toast, re-calls `load()`.

### `views/market/MyPurchases.vue` — `MyPurchases` 🚫 *(unreachable on mobile)*
- `marketApi.getMyPurchases({page:1, page_size:50})` — `load()`, `mounted()`. → local `items`.

### `views/quick-trade/index.vue` — `QuickTrade`
- `credentialsApi.list`, `quickTradeApi.getHistory`, `watchlistApi.getList` — `Promise.allSettled` in `bootstrap()`, `async mounted()`. → `credentialsStore.setItems`, `quickTradeStore.setHistory`, `watchlistStore.setItems`, possibly `quickTradeStore.setSelectedCredential`.
- `watchlistApi.getList` — `loadWatchlist()`, `activated()` (keep-alive re-entry). → `watchlistStore.setItems`.
- `quickTradeApi.getBalance`/`.getHistory`/conditionally `.getPosition` — `Promise.allSettled` in `refreshTradeData()`, from `watch: { selectedCredentialId: {immediate:true} }`, `watch: { marketType }`, and `@click="refreshTradeData"`. → `quickTradeStore.setBalance`/`.setHistory`/`.setPositions`.
- `quickTradeApi.placeOrder` — `submitOrder(side)`, `@click="submitOrder('buy'|'sell')"`. → toast, `refreshTradeData()`.
- `quickTradeApi.closePosition` — `closePosition(position)`, `@click` after confirm dialog. → toast, `refreshTradeData()`.
- ⚠️ `credentials()` computed reads `credentialsStore.cryptoItems` — undefined-getter bug (§5).

### `views/trading/index.vue` — `Trading`
- `strategyApi.getList` — `loadStrategies()`, `mounted()` + `onRefresh()`. → `strategyStore.setStrategies`.
- `strategyApi.delete`/`start`/`stop` — `@click.stop` handlers on each strategy row (delete confirms first; start/stop don't). → toast + `loadStrategies()`.

### `views/trading/StrategyDetail.vue` — `StrategyDetail`
- `strategyApi.getDetail(id)` + `credentialsApi.list` — `Promise.allSettled` in `loadData()`, `mounted()`. → local `strategy`/`performance`/`credentials`.
- `strategyApi.getPositions`/`.getTrades(id,30)`/`.getPerformance`/`.getEquityCurve`/`.getLogs(id,100)` — 4 more `Promise.allSettled` loaders, all fired from `loadData()`. All results local (no store writes). Re-run via `refreshData()` (`@click` on nav-bar icon).
- `strategyApi.start`/`stop`/`delete` — `@click` handlers (`startStrategy`/`stopStrategy` with confirm/`handleDelete` with confirm). → toast + `loadData()` (start/stop) or `router.back()` (delete).

### `views/trading/TradeRecords.vue` — `TradeRecords` 🚫 *(no route registered anywhere, either app)*
- `strategyApi.getTrades(id, 100)` — `loadTrades()`, `mounted()` + `onRefresh()`. → local `trades`.

### `views/trading/CreateStrategy.vue` — `CreateStrategy` 🚫 *(no route registered anywhere; also broken — see §7)*
- `indicatorApi.getIndicators()` — `loadIndicators()`, `mounted()`. **Function does not exist** — see §7.
- `strategyApi.create` — `onSubmit()`, `@submit` (van-form). → toast, `router.back()`.

### `views/trading/BotForm.vue` — `BotForm`
- `credentialsApi.list` — `loadCredentials()`, `async mounted()`. → `credentialsStore.setItems`.
- `strategyApi.getDetail(editId)` — `hydrateFromEditQuery()`, `mounted()` when `?edit=<id>` present. → local `form`.
- `strategyApi.update` or `.create` — `submit()`, `@click`. → toast, `router.replace('/trading')`.

### `views/trading/BotFromIndicator.vue` — `BotFromIndicator`
- `indicatorApi.getList` — `loadIndicators()`, `Promise.all` in `async mounted()` + `@click` on refresh icon (step 0 only). → local `indicators`.
- `credentialsApi.list` — same `mounted()` `Promise.all`. → `credentialsStore.setItems`.
- `indicatorApi.getParams(id)` + conditionally `.parseStrategyConfig(code)` — `Promise.allSettled` in `pickIndicator(ind)`, `@click` on indicator card, also auto-invoked from `mounted()` if route query matches. → local `params`/`paramValues`/`strategyDefaults`.
- `strategyApi.batchCreate` or `.create` — `submit()`, `@click`. → toast, `router.replace('/trading')`.

### `views/trading/BotAIRecommend.vue` — `BotAIRecommend`
- `strategyApi.aiGenerate({intent:'bot_recommend', ...})` — `generate()`, `@click`. → local `recommendation`; `applyAndEdit()` (`@click`) writes `sessionStorage` (`qd_ai_strategy_preset`, `qd_ai_strategy_code`) before routing.

### `views/ai-hub/index.vue` — `AiHub`
- `aiAnalysisApi.getAllHistory({page:1, pagesize:30})` — `loadDrawerHistory()`, from `watch: { showHistoryDrawer(val) }` (fires when drawer opens). → local `drawerHistory`.
- `globalMarketApi.getOverview`/`getCalendar({limit:8})`/`getSentiment` — `Promise.allSettled` in `loadMacro()`, `mounted()` + `activated()` (keep-alive re-entry). → local `indices`/`calendarEvents`/`sentiment`.
- `strategyApi.aiGenerate({intent:'bot_recommend', ...})` — `generateRecommend()`, `@click` on chat-send. → local `recommendation`; `applyRecommendAndEdit()` (`@click`) writes `sessionStorage` before routing to `/trading/create/manual`.

### `views/ai-analysis/index.vue` — `AiAnalysis`
- `watchlistApi.getPrices([{market,symbol}])` — `refreshLivePrice()`, from `mounted()`, a **20s `setInterval`** (`livePriceTimer`, cleared `beforeUnmount`), `onSymbolPicked` (SymbolPicker `@pick`), and after successful `runAnalysis()`. → local `livePrice`/`liveChange`.
- `aiAnalysisApi.analyze({market,symbol,timeframe,language})` — `runAnalysis()`, `@click`. → `aiStore.setLastResult`.
- `aiAnalysisApi.submitFeedback({memory_id, feedback})` — `submitFeedback(type)`, `@click="submitFeedback('helpful'|'not_helpful')"`. → local `userFeedback` only, not persisted.

### `views/ai-analysis/History.vue` — `AiAnalysisHistory`
- `aiAnalysisApi.getAllHistory({page, pagesize:20})` — `loadMore()`, `mounted()` + `van-list @load="loadMore"` infinite scroll. → `aiStore.setHistory`.
- `aiAnalysisApi.deleteHistory(id)` — `removeItem(item)`, `@click` after confirm dialog. → local filter + `aiStore.setHistory` + toast.

### `components/KlineChart.vue` — `KlineChart` *(embedded in `quick-trade/index.vue`)*
- `klineApi.getKline({market, symbol, timeframe, limit:200})` — `fetchData()`, `mounted()`, `watch: { symbol, market }` (parent prop changes), and `onTfChange(tf)` (`@click` on timeframe tabs). → local `candles`.

### `components/SymbolPicker.vue` — `SymbolPicker` *(embedded in `home/index.vue`, `quick-trade/index.vue`, `ai-analysis/index.vue`, `trading/BotAIRecommend.vue`, `trading/BotForm.vue`, `trading/BotFromIndicator.vue`)*
- `watchlistApi.getList` — `load()`, `watch: { show(val) }` (popup opens). → `watchlistStore.setItems`, chains to `loadHot()`.
- `watchlistApi.getHot({market, limit:8})` — `loadHot()` (chained) and `loadHotForMarket(market)` (`@click` on market-tab chip). → local `hotList`.
- `watchlistApi.search({market, keyword, limit:30})` — `doSearch(kw)`, via **300ms debounce** (`searchTimer`) on `@update:model-value`. → local `searchResults`.
- `watchlistApi.add` — `chooseResult(item)`, `@click` (only if `autoAdd` prop, default true). → re-calls `load()` (refreshes `watchlistStore`); emits `pick`.
- `watchlistApi.remove(item.symbol)` — `handleRemove(item)`, `@click.stop` on delete icon. → re-calls `load()`.

---

## 4. `frontend/` vs `mobile/` delta

- `frontend/src/api/index.js` is **byte-identical** to `mobile/src/api/index.js` (diff empty, matching MD5: `8dc77a66f17efab3f82497928992b7ea`). Same 84 functions, same endpoints, same interceptors.
- 24 of 26 shared view/component files with API calls are byte-identical between the two apps.
- **`profile/CredentialForm.vue` differs**: mobile's version is KIS/Kiwoom-specific (Korean labels, `account_no`/`hts_id` fields, `exchange_id` defaults to `'kis'`); frontend's is a generic i18n'd exchange form (no broker-specific fields, `exchange_id` defaults to `''`). Both call the same `credentialsApi.create`, with different payload shapes.
- **Router differs**: `frontend/src/router/index.js` registers `/market`, `/market/indicator/:id`, `/market/my-purchases`, `/profile/referral`, `/profile/credits` — all absent from `mobile/src/router/index.js`, making the 5 screens flagged 🚫-with-a-note above reachable on `frontend/` but not on `mobile/`. Both routers agree in omitting routes for the 5 screens flagged fully 🚫 (dead in both apps).
- **Store differs**: `frontend/src/stores/index.js` has 10 stores (missing `useBrokerStore` and `useWebSocketStore`, which exist only in `mobile/`), and `useCredentialsStore` defines a working `cryptoItems` getter directly — so the `cryptoItems`-undefined crash (§5) is **mobile-only**, not present in `frontend/`.

---

## 5. Defects found (documented, not fixed — analysis only)

1. **`credentialsStore.cryptoItems` runtime crash (mobile-only).** `mobile/src/stores/index.js`'s `useCredentialsStore` defines `hasCredentials`, `kisItems`, `kiwoomItems` — no `cryptoItems`. Four call sites in §3 read `credentialsStore.cryptoItems` and will throw `TypeError` at runtime: `views/quick-trade/index.vue:223`, `views/profile/index.vue:206`, `views/profile/Credentials.vue:145`, `views/home/index.vue:370`.
2. **Two phantom API calls** (functions that don't exist in `api/index.js`):
   - `views/home/StockDetail.vue:73` calls `klineApi.getTicker(symbol, market)` — `klineApi` only exports `getKline`/`getPrice`.
   - `views/trading/CreateStrategy.vue:137` calls `indicatorApi.getIndicators()` — `indicatorApi` only exports `getList`/`getParams`/`parseStrategyConfig`. The correctly-named function for indicator-marketplace listing is `marketApi.getIndicators` (different namespace), used correctly elsewhere in `views/market/index.vue:147`.
   Both phantom-call screens are also unreachable (no registered route in either app), so these are latent bugs rather than live-breaking ones — but they will surface immediately if either screen is ever wired into the router.

---

## 6. WebSocket

**0 events consumed by any frontend code.** Confirmed via repo-wide grep: no `new WebSocket(`, no `new EventSource(`, no `socket.io-client` dependency in either `package.json`.

`mobile/src/stores/index.js:366-388` defines `useWebSocketStore` (state: `connected`, `lastMessage`, `priceMap`; actions: `setConnected`, `setLastMessage`, `updatePrice`, `updatePrices`) — pure scaffold, no call site anywhere invokes these actions, and no socket is ever instantiated. **This store doesn't even exist in `frontend/src/stores/index.js`** — mobile-only dead code.

The backend does have a working Socket.IO server (`backend/websocket/server.py`, Flask-SocketIO, default port 5002) with real event definitions, but **zero frontend code connects to it**:

| Event | Direction | Payload | Source |
|---|---|---|---|
| `connect` (handshake) | client→server | requires `?token=<jwt>` query param | `server.py:57-64` |
| `connected` | server→client | `{"status": "ok"}` | `server.py:57-64` |
| `subscribe` | client→server | `{"channels": [...]}` | `server.py:72-75` |
| `subscribed` | server→client | `{"channels": data.get("channels", [])}` (echo — no actual per-channel filtering; server broadcasts everything to everyone, per the code's own comment) | `server.py:72-75` |
| `order:update` | server→client (broadcast, from Redis pub/sub) | order dict, via `publish_order_update(order_data)` | `server.py:79-102, 110-123` |
| `position:update` | server→client (broadcast) | list of position dicts, via `publish_position_update(positions)` | `server.py:79-102, 110-123` |
| `equity:update` | server→client (broadcast) | equity dict, via `publish_equity_update(equity)` | `server.py:79-102, 110-123` |
| `alert` | server→client (broadcast) | `{"message": message, "level": level}`, via `publish_alert(message, level="info")` | `server.py:79-102, 110-123` |

All "live" data in the frontend today is achieved via polling instead (30s watchlist refresh in `home/index.vue`, 20s live-price refresh in `ai-analysis/index.vue`, 5s billing-order-status poll in `profile/Credits.vue`), not push.

No event-emitter/pub-sub library (`mitt`, custom `EventEmitter`, `eventBus`) exists in either app. The only cross-component reactive pattern found is Pinia's built-in `$subscribe` on the settings store, identically in both `mobile/src/main.js:47` and `frontend/src/main.js:47` (watches theme/locale changes to apply side effects). All other `$emit`/`$on` usage is standard Vue parent-child component communication (e.g. `SymbolPicker.vue` emitting `close`/`update:show`/`pick` to its parent), not a global bus.

No Electron code exists anywhere in the repo — confirmed via full-repo grep for "electron" (zero matches) and via `package.json` dependency inspection (both apps depend on Capacitor 6 for native mobile wrapping, not Electron for desktop).

---

## 7. State management cross-reference

Pinia only (confirmed — `mobile/src/stores/index.js:1` and `frontend/src/stores/index.js:1` both `import { createPinia, defineStore } from 'pinia'`; zero matches for Vuex/Zustand/Redux repo-wide).

| Store | In mobile | In frontend | Fed by API calls in §3? |
|---|---|---|---|
| `useUserStore` | ✅ | ✅ | Yes — `authApi.login/register/getInfo`, `userApi.getProfile` (via `profile/index.vue`) |
| `useStrategyStore` | ✅ | ✅ | Yes — `strategyApi.getList` |
| `useCredentialsStore` | ✅ (buggy getters) | ✅ (working `cryptoItems`) | Yes — `credentialsApi.list` |
| `useDashboardStore` | ✅ | ✅ | Yes — `dashboardApi.getSummary` |
| `useSettingsStore` | ✅ | ✅ | No — pure local/`localStorage` settings, no API calls feed it |
| `useAiAnalysisStore` | ✅ | ✅ | Yes — `aiAnalysisApi.getAllHistory`, `.analyze` |
| `useMarketStore` | ✅ | ✅ | Defined but **no call site in §3 writes to it** — `marketApi.*` results all go to local component state instead, not this store. Effectively an unused store despite `marketApi` itself being well-used. |
| `useNotificationStore` | ✅ | ✅ | Yes — `strategyApi.getNotifications`, `.getUnreadNotificationCount` |
| `useWatchlistStore` | ✅ | ✅ | Yes — `watchlistApi.getList` |
| `useQuickTradeStore` | ✅ | ✅ | Yes — `quickTradeApi.getBalance/.getHistory/.getPosition` |
| `useBrokerStore` | ✅ (mobile-only) | ❌ | No — **no API call in §3 writes to it**; unused/unwired (already flagged in P5-01A) |
| `useWebSocketStore` | ✅ (mobile-only) | ❌ | No — scaffold only, see §6 |

---

## 8. Answers to the four required report items

- **Total REST endpoints: 84**, across 13 groups, identical in `mobile/` and `frontend/` (§2).
- **Total WebSocket events: 0** consumed client-side; 8 defined server-side with no consumer (§6).
- **Duplicated endpoints**: `authApi.changePassword` vs `userApi.changePassword` (same purpose, different path, only the former used); `aiAnalysisApi.getHistory` vs `getAllHistory` (overlapping purpose, only the latter used); the entire 84-function surface duplicated wholesale between `mobile/` and `frontend/` (§4).
- **Unused APIs**: 15 defined-but-never-called functions (marked ⛔ in §2, including `authApi.loginWithCode`), plus 2 phantom calls to functions that were never defined at all (§5) — a distinct, more severe category since they're active runtime bugs waiting to trigger, not just dead code.
