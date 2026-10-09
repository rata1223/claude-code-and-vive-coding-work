"""Repository-wide test isolation for process globals."""
import pytest


@pytest.fixture(autouse=True)
def _isolate_fill_write_latch():
    """A test that makes a fill write fail latches the worker's ``SAFE_MODE``
    until the process ends, and may mark the audit database down (P1-10). Put
    both back after that test only, so the rest of the session runs as it did
    before the latch existed."""
    try:
        from backend.worker import recovery
    except Exception:          # suites that do not import the worker
        yield
        return
    gate = recovery.SAFE_MODE
    saved = (gate._can_trade, gate._reason, gate._cause, gate._latch)
    audit_down = recovery._audit_down
    yield
    if gate._latch is not saved[3]:
        gate._can_trade, gate._reason, gate._cause, gate._latch = saved
    recovery._audit_down = audit_down
