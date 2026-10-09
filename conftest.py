"""Repository-wide test isolation for process globals."""
import pytest


@pytest.fixture(autouse=True)
def _isolate_fill_write_latch():
    """A test that makes a fill write fail latches the worker's ``SAFE_MODE``
    until the process ends (P1-10). Undo it after that test only, so the rest
    of the session runs as it did before the latch existed."""
    try:
        from backend.worker.recovery import SAFE_MODE as gate
    except Exception:          # suites that do not import the worker
        yield
        return
    saved = (gate._can_trade, gate._reason, gate._cause)
    yield
    if gate._latch is not None:
        gate._latch = None
        gate._can_trade, gate._reason, gate._cause = saved
