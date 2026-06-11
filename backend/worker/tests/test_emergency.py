"""
Unit tests for StaleDataWatchdog — TASK 3-2D.

StaleDataWatchdog (backend/worker/emergency.py) is the only OHLCV data-quality
check that currently exists in the codebase. It detects whether the last candle
in a DataFrame is older than a configurable max age. It has no Redis/DB/broker
dependencies, so these tests run fully in isolation.

Covered:
  1. Fresh data is not flagged stale
  2. Old data is flagged stale
  3. Timezone-naive timestamps are treated as UTC
  4. Malformed input (empty DataFrame) fails open — returns False, no exception
  5. check_all() returns only the stale symbols from a dict of DataFrames
  6. check_all() handles an empty input dict
"""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from backend.worker.emergency import StaleDataWatchdog


def _df_with_last_ts(ts, tz_aware=True, n=5):
    if not tz_aware:
        ts = ts.replace(tzinfo=None)
    idx = pd.date_range(end=ts, periods=n, freq="h")
    return pd.DataFrame({"Close": range(n)}, index=idx)


class TestIsStale:
    def test_fresh_data_is_not_stale(self):
        watchdog = StaleDataWatchdog(max_age_hours=2.0)
        df = _df_with_last_ts(datetime.now(timezone.utc) - timedelta(minutes=30))
        assert watchdog.is_stale(df) is False

    def test_old_data_is_stale(self):
        watchdog = StaleDataWatchdog(max_age_hours=2.0)
        df = _df_with_last_ts(datetime.now(timezone.utc) - timedelta(hours=3))
        assert watchdog.is_stale(df) is True

    def test_tz_naive_index_assumed_utc(self):
        watchdog = StaleDataWatchdog(max_age_hours=2.0)
        df = _df_with_last_ts(datetime.now(timezone.utc) - timedelta(hours=3), tz_aware=False)
        assert watchdog.is_stale(df) is True

    def test_malformed_input_fails_open(self):
        """Exception path (e.g. empty df) must not raise — returns False (fail-open)."""
        watchdog = StaleDataWatchdog(max_age_hours=2.0)
        assert watchdog.is_stale(pd.DataFrame()) is False


class TestCheckAll:
    def test_returns_only_stale_symbols(self):
        watchdog = StaleDataWatchdog(max_age_hours=2.0)
        fresh = _df_with_last_ts(datetime.now(timezone.utc) - timedelta(minutes=10))
        stale = _df_with_last_ts(datetime.now(timezone.utc) - timedelta(hours=5))
        result = watchdog.check_all({"FRESH": fresh, "STALE": stale})
        assert result == ["STALE"]

    def test_empty_input_returns_empty_list(self):
        watchdog = StaleDataWatchdog(max_age_hours=2.0)
        assert watchdog.check_all({}) == []
