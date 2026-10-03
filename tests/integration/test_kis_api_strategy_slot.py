"""kis-api runs one strategy at a time (B1, docs/STRATEGY_START_B_DESIGN.md).

The worker builds a tracker per run, so two runs would trade the same account's
symbols independently. ``start_strategy`` refuses while any run occupies the
slot — active, or stopped but with the worker's stop not yet recorded — and the
refusal is in kis-api itself, not only in the app proxy (whose list check covers
the newest 50 rows and is not atomic with the start).
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api import server as srv
from backend.database.models import Base, StrategyRun

KEY = "slot-test-key"


class _FakeRedis:
    def __init__(self):
        self.published = []

    def publish(self, channel, payload):
        self.published.append((channel, payload))


@pytest.fixture()
def factory(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    fac = sessionmaker(bind=eng)
    monkeypatch.setattr(srv, "_db_factory", fac)
    monkeypatch.setattr(srv, "_redis", _FakeRedis())
    monkeypatch.setattr(srv, "_API_KEY", KEY)
    monkeypatch.setattr(srv, "_strategy_start_calls", [])
    srv.app.config.update(TESTING=True)
    yield fac
    eng.dispose()


def _start(name="house"):
    return srv.app.test_client().post(
        "/api/strategies/start", headers={"X-API-Key": KEY},
        json={"name": name, "strategy_type": "indicator", "config": {"universe": ["SPY"]}})


def _row(fac, *, active, stopped_ago=None, started_ago=timedelta(days=1)):
    now = datetime.utcnow()
    with fac() as db:
        run = StrategyRun(name="r", strategy_type="indicator", config="{}", is_active=active,
                          started_at=now - started_ago,
                          stopped_at=(now - stopped_ago) if stopped_ago is not None else None)
        db.add(run)
        db.commit()
        return run.id


def test_an_empty_slot_starts(factory):
    res = _start()
    assert res.status_code == 201
    assert srv._redis.published and srv._redis.published[0][0] == "strategy:start"


def test_an_active_run_blocks_a_second_start(factory):
    busy = _row(factory, active=True)
    res = _start()
    assert res.status_code == 409 and res.get_json()["run_id"] == busy
    assert srv._redis.published == []
    with factory() as db:
        assert db.query(StrategyRun).count() == 1, "no row written"


def test_a_stop_not_yet_recorded_by_the_worker_still_blocks(factory):
    busy = _row(factory, active=False)
    assert _start().status_code == 409
    assert _start().get_json()["run_id"] == busy


def test_a_recorded_stop_frees_the_slot(factory):
    _row(factory, active=False, stopped_ago=timedelta(hours=1))
    assert _start().status_code == 201


def test_an_old_occupying_run_behind_fifty_newer_rows_still_blocks(factory):
    """The list endpoint (and so the app's early check) shows only 50 rows."""
    busy = _row(factory, active=True, started_ago=timedelta(days=100))
    for i in range(55):
        _row(factory, active=False, stopped_ago=timedelta(days=1),
             started_ago=timedelta(days=50 - i * 0.1))
    listed = {r["id"] for r in srv.app.test_client().get(
        "/api/strategies", headers={"X-API-Key": KEY}).get_json()}
    assert busy not in listed
    res = _start()
    assert res.status_code == 409 and res.get_json()["run_id"] == busy


def test_the_list_reports_stopped_at(factory):
    _row(factory, active=False, stopped_ago=timedelta(hours=2))
    (row,) = srv.app.test_client().get("/api/strategies", headers={"X-API-Key": KEY}).get_json()
    assert row["stopped_at"] is not None and row["is_active"] is False


# ── the run's environment is the server's, recorded at start ───────────────

import json  # noqa: E402

from backend.database.models import Order  # noqa: E402


@pytest.mark.parametrize("server_env, client_env", [("paper", "real"), ("real", "paper"),
                                                    ("paper", None)])
def test_the_start_is_stamped_with_the_servers_environment(factory, monkeypatch,
                                                           server_env, client_env):
    monkeypatch.setenv("KIS_ENV", server_env)
    config = {"universe": ["SPY"]}
    if client_env:
        config["kis_env"] = client_env          # a caller cannot choose it
    res = srv.app.test_client().post(
        "/api/strategies/start", headers={"X-API-Key": KEY},
        json={"name": "house", "strategy_type": "indicator", "config": config})
    assert res.status_code == 201
    with factory() as db:
        stored = json.loads(db.query(StrategyRun).one().config)
    assert stored == {"universe": ["SPY"], "kis_env": server_env}
    published = json.loads(srv._redis.published[0][1])
    assert published["config"]["kis_env"] == server_env, "the worker sees the same stamp"


def test_a_non_object_config_is_refused(factory):
    res = srv.app.test_client().post(
        "/api/strategies/start", headers={"X-API-Key": KEY},
        json={"name": "house", "strategy_type": "indicator", "config": ["SPY"]})
    assert res.status_code == 400
    with factory() as db:
        assert db.query(StrategyRun).count() == 0


def test_the_list_reports_environment_and_filled_orders(factory):
    with factory() as db:
        a = StrategyRun(name="a", strategy_type="indicator", config='{"kis_env": "paper"}',
                        is_active=False, started_at=datetime.utcnow() - timedelta(days=2),
                        stopped_at=datetime.utcnow())
        b = StrategyRun(name="b", strategy_type="indicator", config="{}", is_active=True)
        db.add_all([a, b])
        db.flush()
        for n, filled in enumerate((1, 2, 0)):
            db.add(Order(broker_order_id=f"x{n}", symbol="SPY", side="buy", qty=2, price=1.0,
                         filled_qty=filled, status="filled", market="US", strategy_run_id=a.id))
        db.commit()
        ids = (a.id, b.id)
    rows = {r["id"]: r for r in srv.app.test_client().get(
        "/api/strategies", headers={"X-API-Key": KEY}).get_json()}
    assert (rows[ids[0]]["kis_env"], rows[ids[0]]["filled_orders"]) == ("paper", 2)
    assert (rows[ids[1]]["kis_env"], rows[ids[1]]["filled_orders"]) == (None, 0)
