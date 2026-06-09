"""
Unit tests for build_scheduler() — TASK 3-1D.

Verifies that the market session scheduler registers all expected jobs with
correct timezone configurations. This is the core of the "KRX and US sessions
separated correctly" and "DST handled correctly" requirements.

build_scheduler() only registers CronTrigger callbacks — it does not connect
to Redis, DB, or KIS — so these tests run fully in isolation.

Covered:
  1. All 5 expected jobs are registered with correct IDs
  2. KR session uses Asia/Seoul timezone (KST — no DST observed)
  3. US session uses America/New_York timezone (APScheduler resolves DST:
     09:30 ET → 22:30 KST summer / 23:30 KST winter)
  4. KR and US sessions are separated (different timezones)
  5. periodic_reconcile has max_instances=1 (no overlapping runs)
"""
import pytest

from backend.worker.scheduler import build_scheduler


_EXPECTED_JOB_IDS = {"kr_session", "us_session", "risk_reset", "equity_snapshot", "periodic_reconcile"}


class TestSchedulerJobRegistration:
    def test_all_expected_jobs_registered(self):
        """build_scheduler() must register exactly the 5 documented jobs."""
        s = build_scheduler()
        assert {j.id for j in s.get_jobs()} == _EXPECTED_JOB_IDS

    def test_kr_session_uses_kst_timezone(self):
        """KRX session job must use Asia/Seoul so it fires at 09:05 KST on all days."""
        s = build_scheduler()
        kr_job = s.get_job("kr_session")
        assert str(kr_job.trigger.timezone) == "Asia/Seoul"

    def test_us_session_uses_eastern_timezone_for_dst(self):
        """US session must use America/New_York so APScheduler handles DST automatically.

        09:30 ET = 22:30 KST during EDT (summer) and 23:30 KST during EST (winter).
        A UTC-fixed timezone would drift by one hour on DST transition dates.
        """
        s = build_scheduler()
        us_job = s.get_job("us_session")
        assert str(us_job.trigger.timezone) == "America/New_York"

    def test_kr_and_us_sessions_separated_by_different_timezones(self):
        """KRX and US sessions must use distinct timezone configs — session separation."""
        s = build_scheduler()
        kr_tz = str(s.get_job("kr_session").trigger.timezone)
        us_tz = str(s.get_job("us_session").trigger.timezone)
        assert kr_tz != us_tz

    def test_periodic_reconcile_max_instances_one(self):
        """Concurrent reconciliation runs would cause position double-correction."""
        s = build_scheduler()
        job = s.get_job("periodic_reconcile")
        assert job.max_instances == 1
