"""
Regression tests for the deploy gate (audit risk R-D).

These lock in the behaviour that `.github/workflows/deploy.yml`:
  1. only auto-deploys after the "Tests (PostgreSQL)" suite passes,
  2. still allows a manual `workflow_dispatch` escape hatch,
  3. runs a real post-deploy health probe that can fail the deploy.

They are intentionally text-based (no PyYAML dependency — it is not declared in
requirements.txt) so they run anywhere the suite runs, with no DB or network.
The point is to prevent a future edit from silently removing the gate or probe.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_YML = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
TESTS_YML = REPO_ROOT / ".github" / "workflows" / "tests.yml"

# The workflow name deploy.yml gates on. Kept as a module constant so the
# consistency check below pins deploy.yml and tests.yml together.
GATING_WORKFLOW_NAME = "Tests (PostgreSQL)"


def _deploy_text() -> str:
    return DEPLOY_YML.read_text(encoding="utf-8")


def test_deploy_workflow_exists():
    assert DEPLOY_YML.is_file(), f"missing {DEPLOY_YML}"
    assert TESTS_YML.is_file(), f"missing {TESTS_YML}"


def test_deploy_is_gated_on_test_workflow():
    text = _deploy_text()
    # Triggered via workflow_run on the test suite, not a bare push.
    assert "workflow_run:" in text, "deploy must trigger via workflow_run, not push"
    assert f'"{GATING_WORKFLOW_NAME}"' in text, (
        f"deploy must gate on the {GATING_WORKFLOW_NAME!r} workflow"
    )
    # The job must only proceed when that run succeeded.
    assert "conclusion == 'success'" in text, (
        "deploy job must guard on github.event.workflow_run.conclusion == 'success'"
    )


def test_deploy_no_longer_triggers_directly_on_push():
    # Guard against reintroducing an ungated `on: push` trigger. We check that
    # the `on:` block opens with workflow_run rather than push.
    text = _deploy_text()
    on_idx = text.find("\non:")
    assert on_idx != -1, "deploy.yml must declare an `on:` trigger block"
    window = text[on_idx:on_idx + 200]
    assert "push:" not in window, (
        "deploy must not auto-trigger on push; it is gated via workflow_run"
    )


def test_manual_dispatch_escape_hatch_preserved():
    text = _deploy_text()
    assert "workflow_dispatch:" in text, "manual deploy escape hatch must remain"
    assert "github.event_name == 'workflow_dispatch'" in text, (
        "job guard must still allow manual workflow_dispatch deploys"
    )


def test_post_deploy_health_probe_present_and_can_fail():
    text = _deploy_text()
    # Probes the open metrics endpoint for real worker/infra liveness...
    assert "/api/metrics" in text, "health probe must query /api/metrics"
    assert "worker_alive" in text, "health probe must verify the worker booted"
    # ...and must be able to fail the deploy rather than only printing status.
    assert "exit 1" in text, "health probe must fail the deploy on an unhealthy stack"


def test_gating_name_matches_tests_workflow():
    # If tests.yml's `name:` is ever renamed, the workflow_run gate would
    # silently stop firing (deploys would only run via manual dispatch). Pin
    # the two files together so such a rename fails CI loudly instead.
    tests_text = TESTS_YML.read_text(encoding="utf-8")
    first_line = tests_text.splitlines()[0].strip()
    assert first_line == f"name: {GATING_WORKFLOW_NAME}", (
        f"tests.yml name must stay {GATING_WORKFLOW_NAME!r} to match deploy.yml's gate; "
        f"found: {first_line!r}"
    )
    assert f'"{GATING_WORKFLOW_NAME}"' in _deploy_text()
