"""The api and the worker ORMs never map the same table name.

They share one database (docker-compose) and both provision with a
non-altering ``create_all``, so a shared name silently goes to whichever
service starts first and breaks the other (``trades`` did — see
``tests/postgres/test_trades_table_ownership.py``).
"""
import api.models  # noqa: F401 - registers the api tables
from api.database import Base as ApiBase
from backend.database.models import Base as WorkerBase


def test_no_table_name_is_mapped_by_both_apps():
    shared = set(ApiBase.metadata.tables) & set(WorkerBase.metadata.tables)
    assert shared == set(), f"tables mapped by both api and worker: {sorted(shared)}"
