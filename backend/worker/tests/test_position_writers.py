"""P0-09 — every ``positions`` write goes through the helpers in
``backend/database/models.py``.

``tests/postgres/test_position_upsert_db.py`` proves the helpers hold up under
real concurrency. This proves nobody walks around them: the three writers were
SELECT-then-INSERT copies of one another, and a fourth written the same way
would pass every SQLite test — SQLite has no concurrent writers.

Asserted on the source. In production modules that import the ``Position``
model from ``backend.database.models`` (aliases included), these are refused:

* constructing the model — the only use of a new row is to ``add`` it, which
  is the duplicate-key race (use ``upsert_position``/``insert_position_if_missing``);
* a Core ``insert``/``update``/``delete`` of it — the helpers are the one place
  that owns the ``ON CONFLICT`` target;
* reading one row with ``query(Position)…first()/one()/one_or_none()`` in a
  function that then writes a row (assigns ``qty``/``avg_price`` or calls
  ``delete``) — read-modify-write without the row lock (use ``lock_position``).

The reconciler's updates fetch the row by id with ``db.get`` inside the same
transaction after reading the table with ``.all()``; it skips symbols with open
orders and ``_reconcile_lock`` serialises its runs, so it is left as it is.
"""
import ast
import os

import pytest

from backend.worker.tests.test_trading_day_key import _repo_root

_MODELS = "backend.database.models"
_SKIP_DIRS = {".git", "node_modules", "tests", "__pycache__",
              "quantdinger", "frontend", "mobile"}
_ONE_ROW = {"first", "one", "one_or_none"}


def _model_names(tree) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == _MODELS:
            names |= {a.asname or a.name for a in node.names if a.name == "Position"}
    return names


def _queries_model(node, names) -> bool:
    while isinstance(node, (ast.Call, ast.Attribute)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "query" and node.args
                and isinstance(node.args[0], ast.Name) and node.args[0].id in names):
            return True
        node = node.func if isinstance(node, ast.Call) else node.value
    return False


def _writes_a_row(fn) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Attribute) and t.attr in ("qty", "avg_price")
                for t in node.targets):
            return True
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "delete" and node.args):
            return True
    return False


def bypasses(src: str) -> list[int]:
    tree = ast.parse(src)
    names = _model_names(tree)
    if not names:
        return []
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name) and f.id in names:
            hits.append(node.lineno)                                   # Position(...)
        elif ((getattr(f, "id", None) or getattr(f, "attr", None)) in ("insert", "update", "delete")
              and node.args and isinstance(node.args[0], ast.Name) and node.args[0].id in names):
            hits.append(node.lineno)                                   # insert(Position)
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or not _writes_a_row(fn):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _ONE_ROW and _queries_model(node.func.value, names)):
                hits.append(node.lineno)                               # query(Position).first() → write
    return sorted(set(hits))


def _production_modules() -> list[str]:
    found = []
    for root, dirs, files in os.walk(_repo_root()):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            if not name.endswith(".py") or name.startswith("test_"):
                continue
            path = os.path.join(root, name)
            if path.endswith(os.path.join("backend", "database", "models.py")):
                continue   # the helpers themselves
            found.append(path)
    return found


def test_no_production_code_writes_positions_around_the_helpers():
    hits, scanned = [], 0
    for path in _production_modules():
        try:
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            found = bypasses(src)
        except (OSError, SyntaxError):
            continue
        if _model_names(ast.parse(src)):
            scanned += 1
        hits += [f"{os.path.relpath(path, _repo_root())}:{n}" for n in found]
    assert scanned >= 3, "scan found fewer modules than the known writers — scan is broken"
    assert hits == []


@pytest.mark.parametrize("src", [
    "from backend.database.models import Position as DBPosition\ndb.add(DBPosition(symbol='A'))",
    "from backend.database.models import Position\nrow = Position(symbol='A')\ndb.add(row)",
    "from backend.database.models import Position\nsess.execute(insert(Position).values())",
    "from backend.database.models import Position as P\nsess.execute(sa.update(P))",
    "from backend.database.models import Position as DBPosition\n"
    "def f(db):\n"
    "    row = db.query(DBPosition).filter(DBPosition.symbol == 'A').first()\n"
    "    row.qty = 3\n",
    "from backend.database.models import Position as DBPosition\n"
    "def f(db):\n"
    "    row = (db.query(DBPosition)\n        .filter(DBPosition.symbol == 'A')\n        .one_or_none())\n"
    "    db.delete(row)\n",
])
def test_the_guard_sees_each_kind_of_bypass(src):
    assert bypasses(src)


@pytest.mark.parametrize("src", [
    # the broker dataclass of the same name
    "from backend.brokers.models import Position\nps.append(Position(symbol='A'))",
    # a read-only lookup
    "from backend.database.models import Position as DBPosition\n"
    "def f(db):\n    return db.query(DBPosition).filter(DBPosition.symbol == 'A').first()\n",
    # the helpers
    "from backend.database.models import lock_position, upsert_position\n"
    "def f(db):\n    row = lock_position(db, 'A', 'kis')\n    row.qty = 3\n"
    "    upsert_position(db, symbol='A', broker='kis', qty=1, avg_price=1.0, market='KR')\n",
    # reading the table, then a row by id (the reconciler)
    "from backend.database.models import Position as DBPosition\n"
    "def f(db):\n    rows = db.query(DBPosition).all()\n    row = db.get(DBPosition, 1)\n    row.qty = 2\n",
])
def test_the_guard_ignores_lookalikes(src):
    assert bypasses(src) == []
