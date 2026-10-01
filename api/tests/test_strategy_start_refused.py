"""The app does not pretend to run a strategy (docs/STRATEGY_START_AUDIT.md, A).

``POST /api/strategies/start`` used to set ``status="running"`` and add the id
to a Redis set nothing read. The worker never heard of it: no signals, no
orders, no ``strategy_runs`` row — so the screen said "running" and the 4-week
paper-trading clock never started. Start is now refused and changes nothing;
stop still clears a row left "running" by the old behaviour.
"""
import inspect

from api.models import Strategy, StrategyLog, User
from api.routers import strategies
from api.tests.test_compat_strategy import _create_strategy


def _start(client, headers, sid):
    res = client.post(f"/api/strategies/start?id={sid}", headers=headers)
    assert res.status_code == 200
    return res.json()


def _row(db_session, sid):
    db_session.expire_all()
    return db_session.get(Strategy, sid)


def test_start_is_refused_with_the_reason(client, auth_headers, db_session, seed_user):
    user, _ = seed_user
    s = _create_strategy(db_session, user)

    body = _start(client, auth_headers, s.id)

    assert body["code"] == -1
    assert body["msg"] == strategies.START_UNAVAILABLE


def test_a_refused_start_changes_nothing(client, auth_headers, db_session, seed_user):
    user, _ = seed_user
    s = _create_strategy(db_session, user)
    before = _row(db_session, s.id).updated_at

    _start(client, auth_headers, s.id)

    row = _row(db_session, s.id)
    assert row.status == "stopped"
    assert row.updated_at == before
    assert db_session.query(StrategyLog).filter_by(strategy_id=s.id).count() == 0


def test_a_missing_or_foreign_strategy_is_still_not_found(client, auth_headers, db_session, seed_user):
    other = User(email="other@example.com", password_hash="x")
    db_session.add(other)
    db_session.commit()
    theirs = _create_strategy(db_session, other)

    assert _start(client, auth_headers, 999999)["msg"] == "Strategy not found"
    assert _start(client, auth_headers, theirs.id)["msg"] == "Strategy not found"


def test_stop_clears_a_row_the_old_start_left_running(client, auth_headers, db_session, seed_user):
    user, _ = seed_user
    s = _create_strategy(db_session, user, status="running")

    res = client.post(f"/api/strategies/stop?id={s.id}", headers=auth_headers)

    assert res.json()["data"]["status"] == "stopped"
    assert _row(db_session, s.id).status == "stopped"


def test_the_unread_redis_set_is_gone():
    """``running_strategies`` was written and never read; nothing replaces it."""
    src = inspect.getsource(strategies)
    for gone in ("import redis", "_get_redis", ".sadd(", ".srem(", '"running_strategies"'):
        assert gone not in src, gone
