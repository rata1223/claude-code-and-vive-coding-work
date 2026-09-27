"""Issue #164 — every ``DailyRiskState`` write goes through the row lock.

The Postgres suite (``tests/postgres/test_risk_row_lock.py``) proves the lock
does its job. This proves nobody walks around it. The four writers were
copy-pasted from one another, and a fifth written the same way — ``sess.get``
then assign then commit — would reintroduce the lost update without failing a
single behavioural test on SQLite, which ignores ``FOR UPDATE`` anyway.

Asserted on the source, like ``test_trading_day_key``'s ``date.today()`` guard.
"""
import ast
import os

from backend.worker.tests.test_trading_day_key import _repo_root

_FIELDS = {"kill_switch", "kill_reason", "daily_pnl", "weekly_pnl", "peak_equity"}
_MODELS = "backend.database.models"
_SKIP_DIRS = {".git", "node_modules", "tests", "__pycache__",
              "quantdinger", "frontend", "mobile"}


def _production_modules() -> list[str]:
    """Production modules that import from ``backend.database.models``.

    That import is the way in to the risk row; modules without it are left
    out, which keeps unrelated objects that happen to have a ``daily_pnl``
    attribute (the live pipeline's in-memory state) out of scope.
    """
    found = []
    for root, dirs, files in os.walk(_repo_root()):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            if not name.endswith(".py") or name.startswith("test_"):
                continue
            path = os.path.join(root, name)
            if path.endswith(os.path.join("backend", "database", "models.py")):
                continue   # the helpers themselves
            tree = _parse(path)
            if tree is not None and any(
                    isinstance(n, ast.ImportFrom) and n.module == _MODELS
                    for n in ast.walk(tree)):
                found.append(path)
    assert found, "found no modules importing backend.database.models — scan is broken"
    return sorted(found)


def _parse(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return ast.parse(fh.read(), filename=path)
    except (OSError, SyntaxError):
        return None


def _outermost_functions(node):
    """Top-level functions and methods. Closures belong to their outer function:
    ``_write_db`` locks the row and its nested ``_apply`` writes it."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield child
        elif isinstance(child, ast.ClassDef):
            yield from _outermost_functions(child)


def _rooted_at_self(expr) -> bool:
    while isinstance(expr, ast.Attribute):
        expr = expr.value
    return isinstance(expr, ast.Name) and expr.id == "self"


def _called_names(func) -> set[str]:
    names = set()
    for n in ast.walk(func):
        if isinstance(n, ast.Call):
            f = n.func
            names.add(f.id if isinstance(f, ast.Name)
                      else f.attr if isinstance(f, ast.Attribute) else "")
    return names


def unlocked_writes(path: str) -> list[str]:
    """``path:line function`` for each risk-row field assigned without a lock.

    A write is an assignment to one of the row's fields on anything but
    ``self`` (the tracker's own attributes share the names). It is locked when
    the enclosing function calls ``lock_risk_row``/``lock_risk_rows`` or a
    local ``_lock_*`` wrapper around them. Direct construction of the model is
    reported on its own — creation must be the helper's ``ON CONFLICT`` insert.
    """
    tree = _parse(path)
    if tree is None:
        return []
    out = []
    for func in _outermost_functions(tree):
        locked = any(c.startswith(("lock_risk_row", "_lock_")) for c in _called_names(func))
        for n in ast.walk(func):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                    and n.func.id == "DailyRiskState":
                out.append(f"{path}:{n.lineno} {func.name} constructs DailyRiskState")
            if locked or not isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                continue
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            for t in targets:
                for a in ast.walk(t):
                    if isinstance(a, ast.Attribute) and a.attr in _FIELDS \
                            and not _rooted_at_self(a.value):
                        out.append(f"{path}:{a.lineno} {func.name} writes .{a.attr}")
    return out


def test_every_risk_row_write_holds_the_row_lock():
    offenders = [o for p in _production_modules() for o in unlocked_writes(p)]
    assert offenders == [], (
        "these write DailyRiskState without lock_risk_row(s) — a concurrent "
        f"writer's decision would be silently overwritten (#164): {offenders}"
    )


def test_the_scan_covers_every_known_writer():
    """A scan that matches nothing is the easiest green there is. The five
    writers must all be inside it, each found writing a field."""
    scanned = {os.path.relpath(p, _repo_root()) for p in _production_modules()}
    for path in ("backend/quant/risk/engine.py", "api/routers/risk.py",
                 "backend/worker/heartbeat.py", "backend/worker/runner.py",
                 "backend/risk/kill_switch.py"):
        assert path in scanned, path


def test_the_guard_can_actually_see_a_violation(tmp_path):
    """The pre-#164 shapes, one per writer style, must each be reported; the
    locked shapes and ``self`` attributes must not."""
    sample = tmp_path / "offender.py"
    sample.write_text(
        "from backend.database.models import DailyRiskState, lock_risk_row\n"
        "def unlocked(sess, day):\n"
        "    row = sess.get(DailyRiskState, day)\n"
        "    if row is None:\n"
        "        row = DailyRiskState(trade_date=day)\n"
        "    row.kill_switch = True\n"
        "def via_helper(db):\n"
        "    for row in _halted_rows(db):\n"
        "        row.kill_reason = None\n"
        "def locked(sess, day):\n"
        "    row, _ = lock_risk_row(sess, day)\n"
        "    row.kill_switch = True\n"
        "class T:\n"
        "    def own(self):\n"
        "        self.kill_switch = True\n"
        "        self._tracker.peak_equity = 1.0\n",
        encoding="utf-8",
    )
    found = unlocked_writes(str(sample))
    assert sorted(f.split(" ", 1)[1] for f in found) == [
        "unlocked constructs DailyRiskState",
        "unlocked writes .kill_switch",
        "via_helper writes .kill_reason",
    ]
