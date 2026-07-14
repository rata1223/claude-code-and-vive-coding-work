# P5-02A: Compatibility Adapter Phase 1A (Login + Credentials)

> Implements the P1 slice of `docs/P5_ADAPTER_PLAN.md` — Login and Credential compatibility only. TDD: failing tests written first, then the minimum code to pass. No authentication logic, business logic, or database schema was modified.

**Date:** 2026-07-14

---

## Implemented endpoints

| Endpoint | Change |
|---|---|
| `POST /api/auth/login` | Now accepts either `email` (existing/canonical) or `username` (the frontend's actual field name) in the request body. Route logic, DB query, password verification, and token issuance are byte-for-byte unchanged. |
| `POST /api/credentials/create` | Now accepts either the backend-native field names (`app_key`, `app_secret`, `env`) or the frontend's field names (`api_key`, `secret_key`, `enable_demo_trading`). Route logic, encryption, and persistence are byte-for-byte unchanged. |

No other endpoint was touched.

---

## Request mapping

**`POST /api/auth/login`**

| Frontend sends | Backend field | Mechanism |
|---|---|---|
| `username` | `email` | `Field(validation_alias=AliasChoices("email", "username"))` — `email` is tried first, so existing callers using `email` are unaffected; `username` is used only as a fallback. |
| `email` (existing/direct callers) | `email` | Same alias — first choice, unchanged behavior. |
| `password` | `password` | Passthrough, no change. |
| `turnstile_token` | *(none)* | Accepted for wire compatibility; not validated anywhere in the backend (pre-existing no-op, unchanged by this task). |

**`POST /api/credentials/create`**

| Frontend sends | Backend field | Mechanism |
|---|---|---|
| `api_key` | `app_key` | `Field(validation_alias=AliasChoices("app_key", "api_key"))` — `app_key` tried first (direct/backward-compat callers), `api_key` as fallback. |
| `secret_key` | `app_secret` | Same pattern via `AliasChoices("app_secret", "secret_key")`. |
| `enable_demo_trading: bool` | `env: "paper"\|"real"` | `model_validator(mode="after")`: `True` → `"paper"`, `False` → `"real"`, absent → `env` keeps its own explicit value (default `"paper"`, or whatever was sent directly). |
| `passphrase` | *(none)* | No destination field exists (KIS doesn't use one). Pydantic silently ignores unknown keys (no `extra="forbid"` anywhere in this codebase) — accepted without error, not persisted anywhere. Verified by test (`test_passphrase_is_accepted_but_not_persisted_anywhere`). |
| `name`, `exchange_id`, `account_no`, `hts_id` | same names | Passthrough, no change. |
| `app_key`, `app_secret`, `env` (existing/direct callers) | same names | First alias choice in each case — unchanged behavior. |

The backend's own pre-existing, distinct `api_key` field (a separate encrypted column, `api_key_enc`, unrelated to `app_key_enc`) is **not** fed from the frontend's `api_key` value — that value is routed to `app_key` per the mapping above. `CompatCredentialCreate.api_key` is a read-only `@property` that always returns `None`, so it can never be set from wire input at all.

**Self-review + code-review catches, both fixed before this doc was finalized:**
1. An earlier version declared `api_key` as a plain pydantic field with no alias override, which meant it silently fell back to binding from its own field name — since the frontend's payload also happens to contain a key literally named `"api_key"`, both `app_key` *and* `api_key` independently bound to the same value (pydantic resolves each field's alias/name binding separately). That would have encrypted and persisted the same KIS app key into *both* `app_key_enc` and `api_key_enc`, duplicating a secret across two columns — verified directly (`CompatCredentialCreate(...).api_key` returned the app key, not `None`, before the fix).
2. The first fix aliased `api_key` to a sentinel key name (`AliasChoices("__unused_legacy_api_key__")`) that can never match real input — functionally correct, but a subsequent `/code-review` pass flagged it as solving a self-inflicted collision with more machinery than necessary (an extra `Field`/`AliasChoices` construct, a defensive comment block, and a phantom always-null `api_key` parameter in the generated OpenAPI schema). Replaced with a plain read-only `@property` returning `None` — properties aren't part of a pydantic model's field set, so they can't be bound from input at all, making the collision structurally impossible rather than defended against, and the phantom OpenAPI parameter disappears too.

Locked in with a regression assertion (`test_frontend_shape_maps_api_key_and_secret_key_onto_app_columns` asserts `cred.api_key_enc is None`).

---

## Response mapping

**No response shape changes were made in this phase.** Both endpoints' response envelopes are identical to before:
- `POST /api/auth/login` still returns `Resp.ok({"token": ..., "user_id": ..., "email": ...})` on success, `Resp.err("Invalid email or password", code=-1)` on failure — unchanged.
- `POST /api/credentials/create` still returns the masked `_credential_to_dict(cred)` shape (`id`, `name`, `exchange_id`, `env`, `created_at`, and `"****"`-masked `app_key`/`account_no`/`hts_id`) — unchanged.

A response-mapping test class (`TestCredentialResponseMapping`) was included anyway to **lock in** this contract going forward — it asserts the masked shape and that no plaintext secret ever appears in the response body, so a future change can't silently regress this.

---

## Remaining compatibility gaps

Everything else from `docs/P5_ADAPTER_PLAN.md` is explicitly out of scope for this phase and remains open:

- **Strategy sub-resource query/response mismatches** (`id`→`strategy_id`, `items`→`trades`/`positions`/`logs`, equity-curve unwrap), **watchlist array-unwrapping**, **notification `count`→`unread` rename**, **indicator `items`→`indicators` rename** — next in the P1 migration order per the adapter plan, not implemented here.
- **Quick Trade** — still requires the frontend paradigm rework identified in the adapter plan; untouched by this phase.
- **WebSocket wiring**, **AI Analysis / Community marketplace / Billing backend build-out**, all P2/P3 items from `docs/P5_ADAPTER_PLAN.md` — untouched.
- **CI wiring gap** (pre-existing, noted during this task's planning, not fixed here): neither `tests.yml` nor `ci-postgres.yml` installs `requirements-api.txt` or points pytest at an `api/`-rooted test path, so the new `api/tests/` suite added in this phase **will not run in CI today**. It was run and verified manually in this session (isolated venv, `pip install -r requirements-api.txt` + `pytest`). Recommend a follow-up to wire `api/tests/` into one of the existing Postgres-backed CI workflows.
- **Register's dropped fields** (`code`, `referral_code`, `turnstile_token` non-enforcement) — real backend feature gaps (email verification, referral tracking, captcha), not compatibility issues; register already succeeds today without them. Out of scope per the adapter plan's P3 classification.
- **Non-email `username` values get an opaque 422**, not the friendly login-failure envelope (found during `/code-review`, not fixed — see below). `CompatLoginRequest.email` is typed `EmailStr`, so a `username` value that isn't email-shaped fails pydantic validation before the route body ever runs, returning FastAPI's raw 422 error body instead of `Resp.err("Invalid email or password")` (the response wrong-password/unknown-account cases get). In the common case this doesn't matter — `loginForm.username` is populated with the account's email throughout the mobile UI (including via the forgot-password flow) — but a user who types a non-email string directly into that field would see a confusing error instead of a normal "invalid credentials" message. Not a regression (every login 422'd before this task), and fixing it would mean adding error-handling logic to the route beyond pure request/response translation, which is out of this phase's scope — flagged here as a follow-up rather than fixed.

---

## Test results (TDD red → green)

**Before implementation** (`api/compat.py` did not exist yet, routers still used the strict backend-only schemas): 8 of 16 new tests failed exactly as expected — every test exercising the new `username`/`api_key`/`secret_key`/`enable_demo_trading` shapes returned HTTP 422, while tests exercising already-existing behavior (`email`-shaped login, direct `app_key`/`app_secret`-shaped credential creation, unrelated endpoints) already passed.

**After implementation**: all 16 tests pass.

```
api/tests/test_compat_login.py::TestLoginUsernameAlias::test_username_alias_logs_in_successfully PASSED
api/tests/test_compat_login.py::TestLoginUsernameAlias::test_username_alias_without_turnstile_token PASSED
api/tests/test_compat_login.py::TestLoginEmailExisting::test_email_shape_still_logs_in_successfully PASSED
api/tests/test_compat_login.py::TestLoginBackwardCompatibility::test_both_email_and_username_present_prefers_email PASSED
api/tests/test_compat_login.py::TestLoginBackwardCompatibility::test_neither_email_nor_username_present_is_rejected PASSED
api/tests/test_compat_login.py::TestLoginBackwardCompatibility::test_wrong_password_still_rejected_via_username_alias PASSED
api/tests/test_compat_login.py::TestLoginBackwardCompatibility::test_unknown_account_still_rejected_via_username_alias PASSED
api/tests/test_compat_credentials.py::TestCredentialRequestMapping::test_frontend_shape_maps_api_key_and_secret_key_onto_app_columns PASSED
api/tests/test_compat_credentials.py::TestCredentialRequestMapping::test_enable_demo_trading_false_maps_to_real_env PASSED
api/tests/test_compat_credentials.py::TestCredentialRequestMapping::test_passphrase_is_accepted_but_not_persisted_anywhere PASSED
api/tests/test_compat_credentials.py::TestCredentialResponseMapping::test_create_response_masks_sensitive_fields PASSED
api/tests/test_compat_credentials.py::TestCredentialBackwardCompatibility::test_old_backend_native_shape_still_works_unchanged PASSED
api/tests/test_compat_credentials.py::TestRegressionExistingEndpoints::test_register_endpoint_unaffected PASSED
api/tests/test_compat_credentials.py::TestRegressionExistingEndpoints::test_credentials_list_unaffected PASSED
api/tests/test_compat_credentials.py::TestRegressionExistingEndpoints::test_credentials_get_unaffected PASSED
api/tests/test_compat_credentials.py::TestRegressionExistingEndpoints::test_credentials_delete_unaffected PASSED

======================== 16 passed in 5.55s ========================
```

`tests/postgres/` (33 tests) was also run and self-skipped as expected (no `TEST_DATABASE_URL` in this sandbox) — confirmed importable and collectible, no collection errors introduced by the new `api.compat` import.

## Files changed

- **New**: `api/compat.py` (adapter models), `api/tests/__init__.py`, `api/tests/conftest.py`, `api/tests/test_compat_login.py`, `api/tests/test_compat_credentials.py`.
- **Modified** (signature-only, verified via diff): `api/routers/auth.py` (4 lines: one import swap, one type annotation), `api/routers/credentials.py` (5 lines: one import swap, one type annotation). No handler-body line was touched in either file.
