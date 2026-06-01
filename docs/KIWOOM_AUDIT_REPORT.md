# TASK 1-4 — Kiwoom API Verification & Compliance Audit

**Date**: 2026-05-31  
**Scope**: `kiwoom_adapter/` (client, orders, portfolio, market_data), `backend/brokers/kiwoom.py`, `backend/brokers/semantic_mapper.py` (KiwoomDomesticMapper)  
**Method**: Static analysis — no live Kiwoom API calls made  
**Purpose**: Determine whether the Kiwoom adapter implementation is correct before it is wired into any production execution path  

---

## Executive Summary

**The `kiwoom_adapter/` is not a Kiwoom implementation — it is a copy of the KIS domestic adapter with a different base URL.**

Every endpoint path, every request field name, every response field name, and every market data parameter in `kiwoom_adapter/` is taken directly from the KIS domestic (KR) REST API. The adapter will fail at runtime the moment it is wired to the Kiwoom OpenAPI+ server because the server will return field-not-found or unknown-endpoint errors for all operations.

Additionally, two safety-critical defects exist regardless of field names:
1. The paper-trading flag (`is_paper`) is never consulted — all calls would hit the real money endpoint.
2. POST requests (order placements) are retried up to 3 times unconditionally — creating duplicate order risk.

**Risk classification**: CRITICAL — do not wire to any execution path until the issues in Section 3 are resolved.

---

## 1. Current Implementation Status

### 1.1 Files Audited

| File | Lines | Status |
|---|---|---|
| `kiwoom_adapter/client.py` | 99 | Implemented — base HTTP client |
| `kiwoom_adapter/orders.py` | 43 | Partial — buy/sell only; no cancel, no status |
| `kiwoom_adapter/portfolio.py` | 62 | Implemented — balance + positions |
| `kiwoom_adapter/market_data.py` | 20 | Implemented — spot price |
| `backend/brokers/kiwoom.py` | 29 | Stub — all 6 methods raise NotImplementedError |
| `backend/brokers/semantic_mapper.py` (KiwoomDomesticMapper) | 34 | Partial — field names are TBD placeholders |

### 1.2 Functional Coverage

| Capability | Implemented in Adapter | Wired to BrokerAdapter | Status |
|---|---|---|---|
| Authenticate (OAuth2 token) | ✓ | — | Implemented, unverified |
| Place buy order | ✓ | ✗ | Wrong fields — see §3 |
| Place sell order | ✓ | ✗ | Wrong fields — see §3 |
| Cancel order | ✗ | ✗ | MISSING |
| Get order status | ✗ | ✗ | MISSING |
| Get balance | ✓ | ✗ | Wrong fields — see §3 |
| Get positions | ✓ (via balance) | ✗ | Wrong fields — see §3 |
| Get spot price | ✓ | ✗ | Wrong fields — see §3 |
| Paper trading mode | ✗ (flag ignored) | ✗ | CRITICAL defect |

---

## 2. Spec Comparison Results

Each item is classified as:
- **CONFIRMED CORRECT** — verifiable without API docs (URL structure, HTTP verbs, etc.)
- **CONFIRMED WRONG** — demonstrably incorrect by inspection
- **SUSPECTED WRONG** — inconsistent with Kiwoom OpenAPI+ patterns; unverified
- **MISSING** — required capability absent
- **UNVERIFIABLE** — cannot determine without official Kiwoom API documentation

### 2.1 `client.py` — HTTP Client

| Item | Code Location | Classification | Explanation |
|---|---|---|---|
| Kiwoom base URL | `KIWOOM_BASE = "https://openapi.kiwoom.com:10000"` (line 12) | CONFIRMED CORRECT | Official Kiwoom OpenAPI+ server |
| `PAPER_BASE` / `REAL_BASE` constants | Lines 9–10: both = `https://openapi.koreainvestment.com:9443` | CONFIRMED WRONG | Both point to KIS production URL. These constants are unused (line 38: `base_url = KIWOOM_BASE`) but are misleading dead code |
| `is_paper` flag stored | Line 37: `self.is_paper = is_paper` | — | Stored correctly |
| `is_paper` flag used | Never consulted after line 37 | CONFIRMED WRONG | Paper mode is indistinguishable from live — all calls go to `KIWOOM_BASE` regardless |
| OAuth2 token endpoint | `POST /oauth2/token` | UNVERIFIABLE | Path format is plausible for REST APIs; unconfirmed against Kiwoom spec |
| OAuth2 grant type | `"grant_type": "client_credentials"` | UNVERIFIABLE | Standard OAuth2 form; may differ in Kiwoom spec |
| OAuth2 request field | `"appkey": self.app_key` | SUSPECTED WRONG | KIS uses `appkey`+`appsecret`. Kiwoom OpenAPI+ may require `client_id`+`client_secret` or a different field name |
| Token response field | `data["token"]` (line 55) | SUSPECTED WRONG | KIS returns `data["access_token"]`. Using hardcoded `"token"` will raise `KeyError` if Kiwoom returns the OAuth2-standard `"access_token"` |
| Token expiry | `data.get("expires_in", 86400)` | UNVERIFIABLE | KIS default: 86400s (24h). Kiwoom default is unconfirmed |
| Token cache safety margin | `time.time() < self._token_expires - 3600` | CONFIRMED CORRECT | 1-hour safety margin is correct practice regardless of broker |
| Bearer token header | `"authorization": f"Bearer {token}"` | UNVERIFIABLE | Standard Bearer format; may be correct |
| `appkey` in headers | `"appkey": self.app_key` | SUSPECTED WRONG | KIS requires `appkey`+`appsecret` in every request header. Kiwoom auth model may differ (token may be sufficient) |
| Rate limit: 5/s | `RateLimiter(5)` (line 39) | UNVERIFIABLE | Kiwoom OpenAPI+ rate limit unconfirmed; may differ per endpoint |
| GET retry: safe | Lines 68–81 | CONFIRMED CORRECT | Retrying idempotent GETs is safe |
| POST retry: unsafe | Lines 83–98 | CONFIRMED WRONG | Retrying POST order placements creates duplicate order risk if first succeeded but response was dropped — see §3.1 |
| `rt_cd` success values | `not in (None, "0", 0)` (line 74, 91) | UNVERIFIABLE | KIS uses `rt_cd == "0"` for success. Kiwoom may use the same convention (it's a KIS-derived API pattern) but is unconfirmed |
| Error message field | `data.get("msg1")` (line 75, 92) | UNVERIFIABLE | KIS uses `msg1`. Kiwoom convention unconfirmed |

### 2.2 `orders.py` — Order Placement

All items below apply to both `buy_kr()` and `sell_kr()`.

| Item | Code Value | Classification | Explanation |
|---|---|---|---|
| Endpoint path | `/uapi/domestic-stock/v1/trading/order-cash` | CONFIRMED WRONG | This is the KIS domestic order endpoint. Kiwoom OpenAPI+ will have a different path |
| `CANO` field | `account_no[:8]` | CONFIRMED WRONG | KIS field name. Kiwoom's field name for account number is different. Account number splitting logic (`[:8]`, `[9:]`) also assumes KIS account format `XXXXXXXX-YY` — Kiwoom account format is unconfirmed |
| `ACNT_PRDT_CD` field | `account_no[9:]` or `"01"` | CONFIRMED WRONG | KIS product code field. Index `9` (not `8`) skips a separator character; assumes KIS format |
| `PDNO` field | `symbol` | CONFIRMED WRONG | KIS ticker symbol field name |
| `ORD_DVSN` field | `"00"` | CONFIRMED WRONG | KIS order type code. `"00"` = limit order in KIS. Kiwoom field name and codes are different |
| `ORD_QTY` field | `str(qty)` | CONFIRMED WRONG | KIS order quantity field name |
| `ORD_UNPR` field | `str(int(price))` | CONFIRMED WRONG | KIS order unit price field name |
| `SLL_TYPE` field | `"01"` (sell only) | SUSPECTED WRONG | KIS-specific short selling type indicator. Kiwoom may not have this field |
| Missing: buy/sell discriminator | No field differentiates buy from sell | CONFIRMED WRONG | KIS uses `SLL_BUY_DVSN_CD` to indicate buy/sell. Without it, both buy and sell bodies are identical (minus `SLL_TYPE`). Kiwoom's discriminator field is unknown |
| Missing: `cancel_kr()` | Not implemented | MISSING | Cannot cancel orders — critical for risk management |
| Missing: `get_order_status()` | Not implemented | MISSING | Cannot poll order fill status — breaks `OrderFillPoller` integration |

### 2.3 `portfolio.py` — Balance & Positions

| Item | Code Value | Classification | Explanation |
|---|---|---|---|
| Endpoint path | `/uapi/domestic-stock/v1/trading/inquire-balance` | CONFIRMED WRONG | KIS domestic balance endpoint |
| `AFHR_FLPR_YN` param | `"N"` | CONFIRMED WRONG | KIS after-hours estimated price flag. Kiwoom API does not use this parameter |
| `OFL_YN` param | `""` | CONFIRMED WRONG | KIS offline flag. Kiwoom-specific |
| `INQR_DVSN` param | `"02"` | CONFIRMED WRONG | KIS inquiry division code |
| `UNPR_DVSN` param | `"01"` | CONFIRMED WRONG | KIS unit price division |
| `FUND_STTL_ICLD_YN` param | `"N"` | CONFIRMED WRONG | KIS fund settlement include flag |
| `FNCG_AMT_AUTO_RDPT_YN` param | `"N"` | CONFIRMED WRONG | KIS financing auto repay flag |
| `PRCS_DVSN` param | `"01"` | CONFIRMED WRONG | KIS process division |
| `CTX_AREA_FK100` / `CTX_AREA_NK100` params | `""` | CONFIRMED WRONG | KIS pagination continuation keys |
| Response: `output1` structure | Position list | UNVERIFIABLE | KIS uses `output1` for positions — Kiwoom response structure unconfirmed |
| Response: `output2` structure | Summary object | UNVERIFIABLE | KIS uses `output2` for summary — Kiwoom response structure unconfirmed |
| `hldg_qty` field | Holding quantity | CONFIRMED WRONG | KIS response field. Kiwoom field name unconfirmed |
| `pchs_avg_pric` field | Average purchase price | CONFIRMED WRONG | KIS response field |
| `prpr` field | Current price | CONFIRMED WRONG | KIS response field |
| `evlu_pfls_amt` field | Evaluated P&L | CONFIRMED WRONG | KIS response field |
| `pdno` field | Ticker symbol | CONFIRMED WRONG | KIS response field |
| `prdt_name` field | Product name | CONFIRMED WRONG | KIS response field |
| `dnca_tot_amt` field | Cash balance | CONFIRMED WRONG | KIS summary field |
| `tot_evlu_amt` field | Total evaluation | CONFIRMED WRONG | KIS summary field |

### 2.4 `market_data.py` — Spot Price

| Item | Code Value | Classification | Explanation |
|---|---|---|---|
| Endpoint path | `/uapi/domestic-stock/v1/quotations/inquire-price` | CONFIRMED WRONG | KIS market data endpoint. Kiwoom OpenAPI+ price endpoint will differ |
| `FID_COND_MRKT_DIV_CODE` param | `"J"` | CONFIRMED WRONG | KIS FID parameter. `"J"` = KOSPI/KOSDAQ domestic. Kiwoom does not use FID-style parameters |
| `FID_INPUT_ISCD` param | `symbol` | CONFIRMED WRONG | KIS FID ticker input parameter |
| `stck_prpr` response field | Current stock price | CONFIRMED WRONG | KIS response field for stock price |

### 2.5 `KiwoomDomesticMapper` — Status Normalization

| Item | Code Value | Classification | Explanation |
|---|---|---|---|
| `_FILLED_QTY_FIELD = "ccld_qty"` | TBD placeholder | UNVERIFIABLE | Explicitly marked as TBD in comment |
| `_ORDER_QTY_FIELD = "ord_qty"` | Same as KIS | SUSPECTED WRONG | KIS field name; Kiwoom field may differ |
| `_AVG_PRICE_FIELD = "avg_prvs"` | Same as KIS | SUSPECTED WRONG | KIS domestic average price field |
| `extract_side()`: `sll_buy_dvsn_cd == "02"` | KIS field + KIS code | CONFIRMED WRONG | KIS field and code values leaked into Kiwoom mapper |
| `extract_broker_order_id()`: `output.ORD_NO` | TBD placeholder | UNVERIFIABLE | Explicitly marked as TBD in comment |
| `map_status()`: always returns UNKNOWN | — | Intentional stub | Correct behavior until Kiwoom field names are confirmed |

---

## 3. Discovered Errors

### E1 — CRITICAL: Paper trading flag never used (`client.py:37–38`)

```python
# Line 37: stored
self.is_paper = is_paper
# Line 38: ignored — always uses KIWOOM_BASE regardless of is_paper
self.base_url = KIWOOM_BASE
```

**Impact**: Any paper trading session using `KiwoomClient(is_paper=True)` silently hits the live endpoint and executes real orders against real money.

**Fix required**: Add a Kiwoom paper trading base URL (if Kiwoom provides one) and switch `self.base_url` based on `is_paper`. If Kiwoom does not offer a separate paper environment, this flag must block all calls and raise `NotImplementedError("Kiwoom does not offer paper trading via separate endpoint")`.

---

### E2 — CRITICAL: POST retry creates duplicate order risk (`client.py:83–98`)

```python
def post(self, path: str, body: dict) -> dict:
    for attempt in range(self.MAX_RETRIES):  # MAX_RETRIES = 3
        ...
        try:
            resp = requests.post(url, ...)
            ...
        except Exception as e:
            ...
            time.sleep(1)
            # retries — even if the order was accepted but response dropped
```

**Impact**: If Kiwoom receives the order and executes it but the HTTP response is lost (network timeout, connection reset), the retry submits a second identical order. Both orders execute. With 3 retries, up to 3× the intended quantity may be purchased.

**Fix required**: POST retry must not be used for idempotency-sensitive endpoints (order placement, order cancellation). Only idempotent GET requests should be retried. Order POST should be attempted once; on failure, the caller must check order status before deciding to resubmit.

---

### E3 — CONFIRMED WRONG: PAPER_BASE/REAL_BASE constants point to KIS URL (`client.py:9–10`)

```python
PAPER_BASE = "https://openapi.koreainvestment.com:9443"  # KIS URL
REAL_BASE = "https://openapi.koreainvestment.com:9443"   # KIS URL
KIWOOM_BASE = "https://openapi.kiwoom.com:10000"         # Actual Kiwoom URL
```

**Impact**: `PAPER_BASE` and `REAL_BASE` are unused dead code, but they are dangerously misleading. If referenced in a future refactor, they would silently redirect Kiwoom calls to KIS.

**Fix required**: Remove `PAPER_BASE` and `REAL_BASE`. Add `KIWOOM_REAL_BASE` and `KIWOOM_PAPER_BASE` (if applicable) instead.

---

### E4 — CONFIRMED WRONG: All endpoints are KIS domestic API paths

All three adapter modules call endpoints under `/uapi/domestic-stock/v1/` — the KIS domestic stock API namespace. Kiwoom OpenAPI+ uses a different base path structure entirely.

| Module | Current (KIS) Path | Expected |
|---|---|---|
| orders.py | `/uapi/domestic-stock/v1/trading/order-cash` | Kiwoom order endpoint (unverified) |
| portfolio.py | `/uapi/domestic-stock/v1/trading/inquire-balance` | Kiwoom balance endpoint (unverified) |
| market_data.py | `/uapi/domestic-stock/v1/quotations/inquire-price` | Kiwoom price endpoint (unverified) |

**Impact**: All requests will return HTTP 404 (or Kiwoom's equivalent of "unknown endpoint") when the adapter is wired to `KIWOOM_BASE`.

---

### E5 — CONFIRMED WRONG: All request and response field names are KIS fields

Comprehensive count of KIS-originated fields embedded in Kiwoom adapter code:

**Request fields copied from KIS** (will be rejected or ignored by Kiwoom):
`CANO`, `ACNT_PRDT_CD`, `PDNO`, `ORD_DVSN`, `ORD_QTY`, `ORD_UNPR`, `SLL_TYPE`,
`AFHR_FLPR_YN`, `OFL_YN`, `INQR_DVSN`, `UNPR_DVSN`, `FUND_STTL_ICLD_YN`,
`FNCG_AMT_AUTO_RDPT_YN`, `PRCS_DVSN`, `CTX_AREA_FK100`, `CTX_AREA_NK100`,
`FID_COND_MRKT_DIV_CODE`, `FID_INPUT_ISCD`

**Response fields copied from KIS** (will not be present in Kiwoom response):
`hldg_qty`, `pchs_avg_pric`, `prpr`, `evlu_pfls_amt`, `pdno`, `prdt_name`,
`dnca_tot_amt`, `tot_evlu_amt`, `stck_prpr`, `output1`, `output2`

**Mapper fields copied from KIS** (`KiwoomDomesticMapper`):
`ord_qty`, `avg_prvs`, `sll_buy_dvsn_cd`

**Impact**: All response field extractions will silently return default values (0, None, "") because the actual Kiwoom field names are absent from the response dict. Portfolio balances, positions, and prices will all read as zero.

---

### E6 — MISSING: `cancel_kr()` not implemented (`kiwoom_adapter/orders.py`)

`KiwoomOrders` has only `buy_kr()` and `sell_kr()`. There is no `cancel_kr()` method.

**Impact**:
- `KiwoomBroker.cancel_order()` cannot be implemented
- Open KR positions cannot be stopped in a risk event
- `OrderFillPoller` timeout handler calls `on_timeout(order)` — the caller cannot cancel the order
- Phase 1 migration (KiwoomBroker) is blocked

---

### E7 — MISSING: `get_order_status()` not implemented (`kiwoom_adapter/orders.py`)

No order status inquiry method exists.

**Impact**:
- `OrderFillPoller` calls `broker.get_order_status(order_id, symbol)` on every tick
- With `KiwoomBroker` as the broker, this method raises `NotImplementedError` immediately
- All KR orders placed through Kiwoom will never transition from SUBMITTED to FILLED
- Fills will not be recorded, positions will not be updated

---

### E8 — CONFIRMED WRONG: Outdated COM comment in `backend/brokers/kiwoom.py`

```python
class KiwoomBroker(BrokerAdapter):
    """키움증권 브로커 어댑터 — 미구현 스텁.
    Kiwoom uses a Windows COM API (HTS Ocx), not REST. Implementation requires
    a Windows sidecar process with a COM-to-REST bridge — not compatible with Docker.
    """
```

**Impact**: This comment actively misleads future contributors into believing no Docker-compatible Kiwoom path exists, when in fact `kiwoom_adapter/` is a REST client targeting `openapi.kiwoom.com:10000`. Anyone reading only `kiwoom.py` will not implement the adapter, believing it requires Windows.

**Fix required**: Remove the COM/Windows comment. State that `kiwoom_adapter/` provides the REST implementation.

---

### E9 — CONFIRMED WRONG: Account number index assumption in `orders.py` (`[:8]` and `[9:]`)

```python
"CANO": self._client.account_no[:8],
"ACNT_PRDT_CD": self._client.account_no[9:] if len(self._client.account_no) > 8 else "01",
```

The `[9:]` slice (not `[8:]`) implies the account number format is `XXXXXXXX_YY` where index 8 is a separator (dash or space). This matches KIS account format (`12345678-01`). Kiwoom account number format is different and this parsing will produce incorrect substrings.

---

### E10 — SUSPECTED WRONG: Token response field name (`client.py:55`)

```python
self._token = data["token"]  # raises KeyError if "access_token" or other field name used
```

OAuth2-standard token responses use `"access_token"`. KIS uses `"access_token"`. If Kiwoom follows the same standard, this line will raise `KeyError` on the first token fetch, preventing all API calls.

---

## 4. Design Defects

### D1 — No `get_kiwoom_broker()` factory function

`backend/brokers/kis.py` has `get_kis_broker()` which creates and caches a `KISBroker` singleton. No equivalent exists for Kiwoom. There is no way to instantiate a `KiwoomBroker` at runtime through the dispatch layer.

### D2 — `KiwoomBroker` never calls `kiwoom_adapter/`

`backend/brokers/kiwoom.py` raises `NotImplementedError` for all 6 methods and does not import or instantiate any `kiwoom_adapter/` class. The adapter and the broker are completely disconnected.

### D3 — `KiwoomDomesticMapper.map_status()` always returns UNKNOWN

`OrderFillPoller` treats UNKNOWN as an active order and continues polling. If Kiwoom order status can never return anything other than UNKNOWN, polling never stops and orders are never confirmed as filled. This is an infinite polling loop.

### D4 — No paper trading distinction in `client.py`

Even if `is_paper=True` is fixed to switch URLs, no TR_ID switching exists for Kiwoom (unlike KIS which toggles between `TTTCXXXX` and `VTTCXXXX`). Either Kiwoom uses a separate server URL for paper trading, or it uses a request field to indicate paper mode. This design must be resolved against Kiwoom API documentation.

### D5 — `kiwoom_adapter/` has no `__init__.py` exports

KIS adapter exports are imported as `from kis_adapter import KISClient, KISOrders, KISPortfolio`. `kiwoom_adapter/` has no top-level `__init__.py` with equivalent exports, making it inconvenient to import from application code.

---

## 5. Modification Priority

### P0 — Blocker (must resolve before any code change to wire Kiwoom)

| ID | Item | Action |
|---|---|---|
| E1 | `is_paper` flag never used | Determine Kiwoom paper trading URL or disable `is_paper` entirely |
| E2 | POST retry duplicates orders | Remove retry from `post()` for order endpoints; retry GETs only |
| E4 | All endpoints are KIS paths | Obtain correct Kiwoom OpenAPI+ endpoint paths |
| E5 | All field names are KIS fields | Obtain correct Kiwoom OpenAPI+ request/response field names |

### P1 — Required (Phase 0 completion, before Phase 1 KiwoomBroker wiring)

| ID | Item | Action |
|---|---|---|
| E6 | Missing `cancel_kr()` | Implement using correct Kiwoom cancel endpoint |
| E7 | Missing `get_order_status()` | Implement using correct Kiwoom order inquiry endpoint |
| E8 | COM comment in `kiwoom.py` | Remove comment; document REST adapter existence |
| E3 | Dead KIS URL constants | Remove `PAPER_BASE`/`REAL_BASE`; add `KIWOOM_REAL_BASE`/`KIWOOM_PAPER_BASE` |

### P2 — Required before Phase 1 KiwoomBroker implementation

| ID | Item | Action |
|---|---|---|
| D1 | No factory function | Add `get_kiwoom_broker(cred)` to `backend/brokers/kiwoom.py` |
| D2 | KiwoomBroker disconnected | Wire all 6 methods to `kiwoom_adapter/` |
| D3 | `map_status()` always UNKNOWN | Implement once field names are confirmed |
| E10 | Token field `data["token"]` | Verify against Kiwoom API spec; likely `data["access_token"]` |
| E9 | Account number parsing | Verify Kiwoom account format before implementing `cancel_kr()` |

### P3 — Cleanup (low urgency, before Phase 4 KIS KR removal)

| ID | Item | Action |
|---|---|---|
| D4 | Paper trading design | Resolve paper vs live mode for Kiwoom (URL or request flag) |
| D5 | No `__init__.py` exports | Add `kiwoom_adapter/__init__.py` with public exports |

---

## 6. Live-Trading Risk Assessment

If `KiwoomBroker` were wired into the execution path today (bypassing the NotImplementedError stubs):

| Risk | Severity | Probability | Scenario |
|---|---|---|---|
| Real money orders placed during paper mode | CRITICAL | CERTAIN | `is_paper` never consulted → `KIWOOM_BASE` always used → live endpoint hit |
| All API calls fail with HTTP 404 | CRITICAL | CERTAIN | KIS endpoint paths rejected by Kiwoom server |
| Duplicate orders on network hiccup | CRITICAL | HIGH | POST retry on order placement |
| Balance always reads as zero | HIGH | CERTAIN | KIS response fields absent in Kiwoom response |
| Orders never fill (infinite polling) | HIGH | CERTAIN | `map_status()` returns UNKNOWN forever |
| Open orders cannot be canceled | HIGH | CERTAIN | `cancel_kr()` not implemented |
| Portfolio positions all zero | HIGH | CERTAIN | Response field mismatch |

**Conclusion**: The adapter is not safe to wire under any circumstances without first resolving the P0 blockers. Even after P0/P1 are resolved, mandatory paper trading validation (minimum 2 weeks) must be completed before live execution, per the project's `KIS_ENV=paper → real` policy.

---

## 7. Affected Files Summary

| File | Current State | Required Changes |
|---|---|---|
| `kiwoom_adapter/client.py` | Mostly KIS-derived | Fix E1 (is_paper), E2 (POST retry), E3 (dead constants), E10 (token field) |
| `kiwoom_adapter/orders.py` | KIS fields only | Replace all fields with Kiwoom equivalents; add `cancel_kr()`, `get_order_status()` |
| `kiwoom_adapter/portfolio.py` | KIS fields only | Replace endpoint + all params + all response fields |
| `kiwoom_adapter/market_data.py` | KIS fields only | Replace endpoint + params + response field |
| `backend/brokers/kiwoom.py` | All NotImplementedError | Implement all 6 methods; remove COM comment; add factory |
| `backend/brokers/semantic_mapper.py` | TBD placeholders | Confirm field names; implement `map_status()` |
| `kiwoom_adapter/__init__.py` | Does not exist | Create with public exports |

---

## 8. Recommended Next Work

### Immediate (P0): API Documentation Acquisition

Before writing any code, the correct Kiwoom OpenAPI+ field names must be obtained from the official Kiwoom API documentation. The implementation must NOT be guessed from KIS field names.

Minimum information needed:
1. Order placement endpoint path, request fields, response fields
2. Order cancellation endpoint and fields
3. Order status inquiry endpoint and fields
4. Balance inquiry endpoint, request params, response fields
5. Spot price endpoint and fields
6. Authentication flow: request fields, token response field names
7. Account number format (to fix the `[:8]`/`[9:]` parsing)
8. Paper trading: separate URL or request parameter?
9. Status codes in responses (confirming `rt_cd` convention)

### After Documentation (P1): Implement Phase 0

Per the migration plan in `docs/KIS_US_ONLY_REFACTOR.md`, Phase 0 work:

1. Fix `client.py`: remove POST retry for order endpoints, fix `is_paper` flag, fix dead constants, verify token field name
2. Rewrite `orders.py` with correct Kiwoom fields; add `cancel_kr()` and `get_order_status()`
3. Rewrite `portfolio.py` with correct Kiwoom endpoint and fields
4. Rewrite `market_data.py` with correct Kiwoom endpoint and fields
5. Update `KiwoomDomesticMapper` with confirmed field names; implement `map_status()`
6. Remove COM comment from `backend/brokers/kiwoom.py`

### After Phase 0: Implement Phase 1

Wire `KiwoomBroker` to the `kiwoom_adapter/` classes. Add `get_kiwoom_broker()` factory. All 6 `BrokerAdapter` methods must be implemented. Run `tests/brokers/test_kiwoom_broker.py` (to be created).

---

## Appendix: Field Name Cross-Reference

For reference when Kiwoom documentation is obtained:

| Concept | KIS Domestic Field | Kiwoom Field | Confirmed? |
|---|---|---|---|
| Account number (main) | `CANO` | Unknown | ✗ |
| Account product code | `ACNT_PRDT_CD` | Unknown | ✗ |
| Ticker symbol (request) | `PDNO` | Unknown | ✗ |
| Order type | `ORD_DVSN` | Unknown | ✗ |
| Order quantity | `ORD_QTY` | Unknown | ✗ |
| Order price | `ORD_UNPR` | Unknown | ✗ |
| Buy/sell indicator | `SLL_BUY_DVSN_CD` | Unknown | ✗ |
| Holding quantity | `hldg_qty` | Unknown | ✗ |
| Avg purchase price | `pchs_avg_pric` | Unknown | ✗ |
| Current price | `prpr` | Unknown | ✗ |
| Evaluated P&L | `evlu_pfls_amt` | Unknown | ✗ |
| Cash balance | `dnca_tot_amt` | Unknown | ✗ |
| Total evaluation | `tot_evlu_amt` | Unknown | ✗ |
| Stock price (market data) | `stck_prpr` | Unknown | ✗ |
| Order ID (response) | `ODNO` | Unknown | ✗ |
| Cumulative filled qty | `tot_ccld_qty` | Unknown | ✗ |
| Average fill price | `avg_prvs` | Unknown | ✗ |
| Order status name | `ord_stts_name` | Unknown | ✗ |
| Access token field | `access_token` | `token`? | ✗ |
