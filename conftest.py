"""Repository-wide test isolation for process globals."""
import pytest


@pytest.fixture(autouse=True)
def _isolate_fill_write_failure():
    """A test that makes a fill write fail latches the worker's process-level
    flag and closes ``SAFE_MODE`` (P1-10). Undo both after that test only, so
    the rest of the session runs as it did before the flag existed."""
    try:
        from backend.worker import recovery
    except Exception:          # suites that do not import the worker
        yield
        return
    gate = recovery.SAFE_MODE
    saved = (gate._can_trade, gate._reason, gate._cause)
    yield
    if recovery._fill_write_failure is not None:
        recovery._fill_write_failure = None
        gate._can_trade, gate._reason, gate._cause = saved
