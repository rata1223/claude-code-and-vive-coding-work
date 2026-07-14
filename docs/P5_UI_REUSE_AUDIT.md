# P5-01A: Open Source UI Reuse Audit

> **Analysis only.** No backend or UI code was changed to produce this document. Scope: determine maximum safe reuse from the four "authoritative" open-source repositories before any new UI implementation begins.

**Date:** 2026-07-14
**Repos in scope:** QuantDinger, QuantDinger-Mobile, QuantDinger-Vue, TradingView Lightweight Charts

---

## 0. Headline findings (read this first)

1. **Licensing blocker, not just a risk.** `mobile/LICENSE` is the *QuantDinger Frontend Source-Available License v1.0* — a non-commercial-only license. Any commercial use (explicitly including "integrating the Software into a commercial product or service") requires a **separate paid license** from the copyright holder. The license also mandates keeping "Powered by QuantDinger" branding intact unless given written permission. **The current `mobile/` fork has already rebranded to "KIS Trading" (`com.kistrade.mobile`), which appears to violate §3.1 of the license as written.** See §5.
2. **QuantDinger-Vue does not exist in this repo.** The `frontend/` directory that `docker-compose.yml` runs as the "desktop web" service is not an independent QuantDinger-Vue checkout — it is a near-byte-identical, mislabeled copy of QuantDinger-Mobile that has since drifted out of sync with `mobile/`. See §2.3.
3. **QuantDinger (backend) is no longer a live dependency**, contradicting what `CLAUDE.md` and `ROADMAP.md` P6-04 currently say. See §2.1.
4. **TradingView Lightweight Charts is a dead dependency.** It's declared in `package.json` but never imported; the real chart is a hand-rolled inline-SVG line chart. See §2.4.
5. A **live regression bug** (`credentialsStore.cryptoItems` no longer exists) currently crashes 4 views in `mobile/` — anyone reusing this code should fix it first. See §6.

---

## 1. Scope & method

Two of the four named repos are genuinely present in this codebase; two are not:

| Repo | Present locally? | Audit method |
|---|---|---|
| QuantDinger (backend) | No — external clone target only | Integration-point analysis: `docker-compose.yml`, `scripts/setup_oracle_cloud.sh`, `ROADMAP.md`, `AUDIT.md` |
| QuantDinger-Mobile | **Yes** — vendored as `mobile/` (`package.json` name `"quantdinger-mobile"`, original `README.md`/`LICENSE` intact) | Full source read: `api/`, `stores/`, `router/`, `views/`, `components/`, `styles/` |
| QuantDinger-Vue | No — not an independent checkout anywhere in this repo | Documented as a finding (mislabeled duplicate), not source-audited |
| TradingView Lightweight Charts | Declared as an npm dependency, unused | `package.json` + repo-wide import grep |

This session's GitHub access is scoped to `rata1223/claude-code-and-vive-coding-work` only. A full source-level module audit of the real `brokermr810/QuantDinger` and `brokermr810/QuantDinger-Vue` repositories was not performed because that would require adding those external repos to session scope, which wasn't confirmed with the user (an `AskUserQuestion` prompt on this point failed due to a tool/connection error and was not retried indefinitely). **If a deeper backend/QuantDinger-Vue source audit is wanted, those two repos should be explicitly added to a future session.** Everything below reflects what's verifiable from this repo today.

---

## 2. Repo-by-repo summary

### 2.1 QuantDinger (backend) — not vendored, and apparently no longer needed

`scripts/setup_oracle_cloud.sh:43-50` clones `https://github.com/brokermr810/QuantDinger.git` into `$APP_DIR/quantdinger` with the comment "QuantDinger 백엔드 소스 클론 (docker-compose 빌드에 필요)" ("required for docker-compose build"). `ROADMAP.md` P6-04 ("Gate `quantdinger` dependency behind env flag") and `AUDIT.md` DB-03 both describe `docker-compose.yml` as requiring `./quantdinger/` to exist.

**That is no longer true of the current `docker-compose.yml`.** A direct check of the file today shows:
- `frontend` service builds from `context: ./frontend` (the in-repo duplicate, not QuantDinger)
- `api` service builds from `context: .` / `dockerfile: Dockerfile.api` (the in-house `backend/api` Flask service)
- No service in `docker-compose.yml` references `./quantdinger` or `backend_api_python` anywhere

The only remaining references to `./quantdinger`/`backend_api_python` in the repo are in `CLAUDE.md`, `ROADMAP.md` (P6-04), and `mobile/README*.md` — all **documentation**, not build config. In other words, the backend has apparently already been decoupled from QuantDinger (superseded by `backend/api/`, `backend/worker/`, per the Stage 4 architecture), but the docs describing this as an open risk/TODO were never updated. This should be corrected (see §7) — it also means there is effectively **no QuantDinger backend UI/API surface left to reuse or audit**; the "authoritative repo" for backend purposes is now this repo's own `backend/`.

### 2.2 QuantDinger-Mobile → `mobile/`

Real, vendored source. Stack: Vue 3.5 + Vite + Capacitor 6 + Pinia + Vant 4 (mobile component kit) + vue-router + vue-i18n (en-US/ko-KR/ja-JP/zh-CN/zh-TW).

```
mobile/src/
├── api/index.js          single axios instance + interceptors, domain API groups
├── stores/index.js        all Pinia stores in one file (12 stores)
├── router/index.js        vue-router + auth guard
├── components/            KlineChart.vue, SymbolPicker.vue
├── constants/              exchanges.js, legal.js
├── styles/index.css       design-token theme system
├── style.css               dead Vite-starter boilerplate (unused, never imported)
├── utils/oauthRedirect.js
└── views/                 11 route-group subfolders (home, assets, trading, quick-trade,
                            login, profile, market, ai, ...)
```

This is the only one of the four repos with a complete, current, first-party source tree in this checkout. §3 and §4 audit it in detail.

### 2.3 QuantDinger-Vue — not present; `frontend/` is a mislabeled duplicate

`frontend/` (the docker-compose "desktop web" service) has the **identical file listing** to `mobile/` and `frontend/package.json`'s `name` field is also `"quantdinger-mobile"` — not `"quantdinger-vue"`. `AUDIT.md` §1.10 already flags "`mobile/` and `frontend/` are structurally identical... This is copy-paste duplication, not inheritance."

Since this audit's exploration, the two have **diverged mid-migration** rather than staying identical — 7 files differ (`config/index.js`, `constants/exchanges.js`, `locales/index.js`, `locales/ko-KR.js`, `router/index.js`, `stores/index.js`, `views/profile/CredentialForm.vue`), all in the direction of `mobile/` being partially rebranded to KIS/Kiwoom while `frontend/` still carries the original QuantDinger crypto-exchange branding. This is worse than static duplication — it's an active fork with a shrinking blast radius for shared fixes (see §6).

There is no genuine QuantDinger-Vue (desktop) codebase anywhere in this repo to audit for its actual architecture. Treat `frontend/` as **reference only** at best (it shows what the "before rebrand" state looked like) and not as a stand-in for the real upstream QuantDinger-Vue project.

### 2.4 TradingView Lightweight Charts — declared, unused

`lightweight-charts@^4.2.2` is listed in both `mobile/package.json` and `frontend/package.json`. A repo-wide grep for `lightweight-charts` imports and for `createChart(` returns **zero matches** in `mobile/src` or `frontend/src`. The only chart component, `mobile/src/components/KlineChart.vue`, is a hand-written Options-API Vue component that:
- fetches OHLC candles via `klineApi.getKline()`
- renders a **single-line SVG path** (`buildMonotonePath()`, manual `<path>`/`<linearGradient>`/`<filter>` elements) showing only close price, discarding the open/high/low it fetched
- implements its own touch/mouse crosshair and timeframe tabs (5m/15m/1h/4h/1d) by hand

So "TradingView Lightweight Charts" contributes nothing today except unused bundle weight. It is real, MIT-licensed, and well suited to candlestick/volume rendering — but it would need to be **adopted from scratch**, not "reused," since none of the existing integration exists.

---

## 3. Component classification

Legend: **Reuse directly** / **Reuse w/ modification** / **Reference only** / **Do not use**

| Domain | Component (path) | Classification | Rationale |
|---|---|---|---|
| Theme | `mobile/src/styles/index.css` | **Reuse directly** | Brand-neutral CSS custom-property design tokens (light/dark), Vant variable overrides, `.qd-*` recipe classes. No crypto-specific naming. |
| Theme | `mobile/src/stores/index.js` → `useSettingsStore` | **Reuse directly** | Clean theme/locale/server-URL persistence to `localStorage`, syncs native `StatusBar` on change. |
| Theme | `mobile/src/style.css` | **Do not use** | Dead Vite-starter boilerplate, never imported by `main.js`. Delete on adoption. |
| Navigation | `mobile/src/router/index.js` (guard pattern) | **Reuse w/ modification** | Clean `beforeEach` auth-guard pattern is reusable, but the route table itself is **incomplete** — `/market*`, `/profile/referral`, `/profile/credits` are linked from views but not routed (404s today). Needs reconciling against `frontend/`'s table before reuse. |
| Navigation | `mobile/src/App.vue` (tab bar) | **Reuse directly** | Vant `<van-tabbar>` + `keep-alive` + native back-button handling is generic and works for any 5-tab app shape. |
| API layer | `mobile/src/api/index.js` (interceptor pattern) | **Reuse directly** | Axios instance, bearer-token injection, response envelope normalization, session-expiry detection. Solid pattern regardless of trading domain. |
| API layer | `mobile/src/api/index.js` → `billingApi` | **Do not use** | USDT-chain crypto payment flow (`listUsdtChains`, `createUsdtOrder`) — irrelevant and legally out of scope for a KRW securities platform. |
| API layer | `mobile/src/api/index.js` → `globalMarketApi` (`GOLD_IMPACT_RULES`) | **Reference only** | Macro/commodity-sentiment heuristics built for a different product surface; interesting reference, not a fit as-is. |
| Watchlist | `mobile/src/components/SymbolPicker.vue` | **Reuse w/ modification** | Already multi-market-capable (`Crypto`/`USStock`/`HKStock`/`Forex`/`Futures` tabs) — just change the default market and drop the Crypto tab. |
| Watchlist | `useWatchlistStore` (`stores/index.js`) | **Reuse w/ modification** | Same — functionally solid, defaults to `'Crypto'`. |
| Dashboard | `mobile/src/views/home/index.vue` + `useDashboardStore` | **Reuse w/ modification** | Broker-agnostic KPI grid (PnL, win rate, positions, profit factor) is directly relevant to KIS trading; just needs its two dead links (`/market`, referral/credits) either wired or removed. |
| Dashboard | `mobile/src/views/home/StockDetail.vue`, `MacroData.vue` | **Reference only** | Present but unreachable — not linked from any route. Worth reading for ideas, not integrated. |
| Charts | `mobile/src/components/KlineChart.vue` | **Reuse w/ modification** | Reusable as a lightweight line chart, but a real trading UI needs OHLC candlesticks + volume, which this deliberately discards. Either extend it or replace it by actually wiring up the already-declared `lightweight-charts` dependency. |
| Auth | `mobile/src/views/login/index.vue`, `useUserStore`, JWT interceptor | **Reuse w/ modification** | Solid login/register/OAuth/session-expiry flow, **but fix the `cryptoItems` crash (§6) and the OAuth deep-link mismatch (§6) before reuse** — both are live bugs, not stylistic issues. |
| Portfolio | `mobile/src/views/assets/index.vue`, `AssetDetail.vue` | **Reference only** | Orphaned: `router/index.js` explicitly redirects `/assets → /home`, so this code is unreachable dead code today. Also uses a crypto-exchange icon map (`Binance`, `OKX`) that would need replacing. |
| Orders | *(no dedicated view exists)* | **Gap — nothing to reuse** | Order placement is folded into the crypto/leverage-styled Quick Trade flow; a real order-entry/blotter UI needs to be built new. See §4. |
| Positions | `useQuickTradeStore.positions`, `useBrokerStore` | **Reuse w/ modification** | `useBrokerStore` is already KRW-shaped (`totalEquityKrw`, `cashKrw`, `cashUsd`, `activeBroker: 'kis'`) but is currently **defined and unused by any view** — the best starting point for a real Positions screen, just needs wiring. |
| State mgmt | `mobile/src/stores/index.js` → `useWebSocketStore` | **Do not use as-is** | Empty scaffold (`connected`, `lastMessage`, `priceMap` state with no socket ever instantiated). No real-time push exists; current "live" updates are 30s polling. Keep the shape as a starting interface, but there's no working code behind it. |
| Mobile layout | `mobile/src/main.js` native integrations (StatusBar, SplashScreen, back-button, deep link) | **Reuse directly** | Functional and generic. |
| Mobile layout | `@capacitor/push-notifications`, `@capacitor/haptics` | **Do not use as-is** | Declared as dependencies, configured in `capacitor.config.json`, but **zero usage** in source — either implement or strip to cut bundle/permission surface. |
| Frontend (whole dir) | `frontend/` | **Do not use** | Superseded, stale duplicate of `mobile/` with the crypto-era branding still in place (`com.quantdinger.mobile` appId, generic `CredentialForm.vue`, `cryptoItems`-based stores). Retire or resync from `mobile/`, don't develop against it. |

---

## 4. Domain gaps (nothing existing to reuse)

- **Orders** — there is no order-list, order-entry form, or order-history/blotter view anywhere in `mobile/` or `frontend/`. The closest analogues are: (a) Quick Trade's buy/sell buttons (crypto/leverage-styled — spot/swap toggle, USDT-denominated), and (b) `TradeRecords.vue`, which is per-*strategy* bot fill history, not a manual order blotter. `dashboardApi.getPendingOrders()` exists in the API layer with **no consuming view**. This domain needs to be designed and built fresh; nothing here is a safe base.
- **Positions** — logic is scattered across three places (`useQuickTradeStore`, `useDashboardStore` count-only KPI, unused `useBrokerStore`) with no single "Positions" screen. `useBrokerStore` is the right shape to build on but isn't wired to any UI today.
- **WebSocket / real-time push** — `useWebSocketStore` is scaffolding with no client behind it; all "live" data is interval polling (e.g., 30s watchlist refresh). If a push architecture is wanted, it needs to be designed and implemented from zero.

---

## 5. Licensing risk (read before reusing anything from `mobile/` or `frontend/`)

`mobile/LICENSE` — **"QuantDinger Frontend Source-Available License, Version 1.0, Copyright (c) 2026 QuantDinger (quantdinger.com)."** Key terms:

- **Non-Commercial Use is free.** Defined as personal learning, research, teaching, internal evaluation, portfolio demos — explicitly **not** commercial use.
- **Commercial Use requires a separate paid license** from the copyright holder. "Commercial Use" is defined broadly: *"integrating the Software into a commercial product or service"* or *"using the Software to operate a paid, monetized, or revenue-generating service."*
- **Qualified Non-Profit Entities** get a broader free grant (including production deployment), but KIS Trading is not described anywhere in this repo as operating under non-profit status.
- **§3.1 Attribution and Branding (mandatory):** *"You must retain all copyright notices, this License text, and any 'Powered by QuantDinger' or similar branding, watermark, or attribution notices... You may not remove, obscure, alter, or misrepresent such branding... without prior written permission."*
- **§4 Termination:** violating any term terminates the license automatically and requires ceasing all use / destroying all copies.

**This matters concretely, not hypothetically:** `mobile/capacitor.config.json` has already been changed to `appId: "com.kistrade.mobile"`, `appName: "KIS Trading"` — the original QuantDinger branding has been removed. `mobile/src/views/profile/CredentialForm.vue` and `mobile/src/constants/exchanges.js` have likewise been rewritten with KIS/Kiwoom-specific Korean content, with no "Powered by QuantDinger" attribution retained anywhere found in this audit. If the KIS Trading platform is (or becomes) a revenue-generating or otherwise "commercial" service under the license's broad definition, **continued use of `mobile/`-derived code without a commercial license from `brokermr810` is a license violation as currently written**, independent of whether the branding issue is also fixed.

**Recommendation:** treat this as a business/legal decision, not an engineering one — either (a) obtain a commercial license and restore/negotiate the required attribution, or (b) treat everything under `mobile/`/`frontend/` as reference material only and re-implement UI components independently before any real-money production use. Do not resolve this by silently keeping the rebrand and hoping it doesn't matter.

Secondary risks:
- `lightweight-charts` (MIT) is safe from a licensing standpoint but is dead weight as currently integrated (§2.4) — either use it or drop the dependency.
- `frontend/`'s still-unrebranded QuantDinger appId/branding is itself evidence of what the "before" state looked like, useful if attribution needs to be restored for compliance.

---

## 6. Duplicated functionality & concrete defects found

**Duplication:**
- `mobile/` vs `frontend/` — full-app duplication of every view/store/component (AUDIT.md §1.10), now actively diverging rather than static, which makes the duplication worse over time, not just redundant.
- Three overlapping "trade history" surfaces: `TradeRecords.vue` (per-strategy bot fills), Quick Trade's own history panel, and the dashboard's "recent trades" widget — each hits a different API and shows overlapping data with no shared component.

**Defects surfaced during this audit (relevant to reuse safety, not fixed here since this task is analysis-only):**
1. **Runtime crash risk:** `mobile/src/stores/index.js` renamed `useCredentialsStore`'s `cryptoItems` getter to `kisItems`/`kiwoomItems`, but four views still call the now-nonexistent `credentialsStore.cryptoItems`: `views/quick-trade/index.vue:223`, `views/profile/index.vue:206`, `views/profile/Credentials.vue:145`, `views/home/index.vue:370`. This throws at runtime wherever `.length`/`.map` is called on the resulting `undefined`. (Confirmed live via `grep` — not present in `frontend/`, which still defines `cryptoItems`.)
2. **OAuth deep link mismatch:** `mobile/src/utils/oauthRedirect.js` hardcodes the native deep-link scheme `com.quantdinger.mobile://login`, which matches `frontend`'s Capacitor appId but not `mobile`'s actual appId (`com.kistrade.mobile`) — native OAuth login is likely broken on the current KIS-branded mobile build.
3. **Incomplete route table:** `mobile/src/router/index.js` is missing `/market`, `/market/indicator/:id`, `/market/my-purchases`, `/profile/referral`, `/profile/credits` routes that `home/index.vue` and `profile/index.vue` both link to.
4. **Stale docs:** `AUDIT.md` §1.10 / DB-05 and `CLAUDE.md`'s known-bugs list still describe `mobile/capacitor.config.json` as carrying the original QuantDinger appId — that's no longer true for `mobile/` (it's `frontend/` that still has it). See §7.

---

## 7. Recommended reuse strategy

1. **Resolve the licensing question first (§5)** — before any further UI work ships toward production/real-money use, either secure a commercial license + attribution agreement from `brokermr810`, or commit to treating `mobile/`/`frontend/` as reference-only and re-implementing components independently. This blocks everything else in priority.
2. **Standardize on `mobile/` as the single UI codebase.** It's further along in the KIS/Kiwoom rebrand than `frontend/`. Retire `frontend/` (or regenerate it as a thin desktop-responsive wrapper around the same source) instead of hand-maintaining two divergent copies.
3. **Fix the two live regressions before reusing Auth/Quick-Trade/Profile code**: the `cryptoItems` → `kisItems`/`kiwoomItems` crash and the OAuth deep-link scheme mismatch (§6).
4. **Reuse directly, with confidence:** theme system, API interceptor pattern, router guard pattern, tab-bar shell, native Capacitor integration in `main.js`.
5. **Build fresh, not adapted:** Orders (no existing view), a real Positions screen (wire up the already-KRW-shaped but unused `useBrokerStore` instead of extending Quick Trade's crypto-styled position panel), and a real-time push layer (only an unused store scaffold exists today).
6. **Decide the charting story explicitly**: either extend `KlineChart.vue` to plot OHLC candlesticks, or actually wire up the already-declared `lightweight-charts` dependency and delete the hand-rolled SVG chart. Don't leave both an unused dependency and a partial hand-rolled implementation in place.
7. **Correct documentation drift**: update `CLAUDE.md`'s known-bugs list, `AUDIT.md` DB-05, and `ROADMAP.md` P6-04 to reflect that (a) `docker-compose.yml` no longer depends on `./quantdinger/backend_api_python` at all, and (b) `mobile/capacitor.config.json` has already been rebranded (it's `frontend/` that hasn't).
8. **If deeper backend/QuantDinger-Vue analysis is wanted**, add `brokermr810/QuantDinger` and `brokermr810/QuantDinger-Vue` to a future session's repo scope — this audit could not inspect their actual source.
