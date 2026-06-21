# Deploy Gating (audit risk R-D)

## Why
`/.github/workflows/deploy.yml` previously auto-deployed on **every push to `main`** with no
test gate, and its only "health check" was `sleep 10 && docker compose ps` — which never verified
that the trading worker actually booted. A bad commit reached the live host automatically, and a
container that crash-looped on startup would not fail the deploy. This is audit risk **R-D**.

This change is CI/CD-only. It touches no trading, broker, strategy, or runtime code, and does not
change any default (`ENABLE_LIVE_TRADING=false`, `KIS_ENV=paper` remain as-is).

## The gate
`deploy.yml` no longer triggers on `push`. It triggers via `workflow_run` after the
**`Tests (PostgreSQL)`** workflow (`tests.yml`, which runs `pytest backend tests`) completes on
`main`, and the `deploy` job only proceeds when:

```yaml
if: >-
  github.event_name == 'workflow_dispatch' ||
  (github.event_name == 'workflow_run' && github.event.workflow_run.conclusion == 'success')
```

- Push to `main` → tests run → **deploy only if tests pass** (a red suite blocks deploy).
- `workflow_dispatch` → manual deploy still works (escape hatch / hotfix path).

## The post-deploy health probe
After `docker compose up -d --build`, the SSH script:
1. Fails immediately (`exit 1`) if any container is `exited`/`unhealthy`.
2. Polls the **auth-free** `http://localhost:5001/api/metrics` (kis-api) for up to ~120s and
   requires `worker_alive`, `db_ok`, and `redis_ok` to all be `true`. On timeout it dumps
   `docker compose logs --tail=80 kis-api kis-worker` and fails the deploy.

`/api/metrics` is an open route (`backend/api/server.py` `_OPEN_ROUTES`), so the probe needs no
`KIS_API_KEY`.

## Tests
`tests/test_deploy_gating.py` pins the gate, the dispatch escape hatch, and the probe, and asserts
`tests.yml`'s `name:` still matches the string `deploy.yml` gates on (so a future rename fails CI
instead of silently disabling auto-deploy). The test is text-based (no PyYAML dependency) and
needs no DB or network.

## Known limitations / remaining risks
- The gate keys only on `Tests (PostgreSQL)`; the separate `CI — PostgreSQL` (Alembic round-trip)
  workflow is **not** part of the gate.
- `workflow_run` only fires for the workflow file on the default branch, so the gate is active
  once this is merged to `main`.
- The probe depends on the host having `curl` and on the worker heartbeat appearing within the
  timeout window; an unusually slow boot could fail an otherwise-good deploy (fail-safe direction).
- Exited/unhealthy detection is a text grep over `docker compose ps`.

## Rollback
Revert the single commit (`git revert <sha>`) to restore push-triggered deploy and the old
health check, and delete `tests/test_deploy_gating.py` / this doc. No database, migration, or
runtime state is involved — fully reversible.
