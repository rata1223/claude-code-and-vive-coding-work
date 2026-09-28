"""Issue #178 — say when the equity number is incomplete.

``KISBroker.get_balance().total_eval_krw`` feeds the MDD kill switch and, since
P0-03, the automatic flatten. It may read low: fields that are absent read as
0, and USD cash is not part of it. None of the field names has been confirmed
against a live account, so the number itself is left alone — adding USD cash
could double-count it if the US evaluation already includes it, and an
overstated total hides a drawdown. What changes is that the reading now says
when it is incomplete (``equity_verified``), and logs the field names KIS did
send so one paper-trading session can settle #178.

No network: the portfolio client is a stub.
"""
import logging

import pytest

import backend.brokers.kis as kis
from backend.brokers.kis import KISBroker
from backend.brokers.models import Balance
from backend.execution.circuit_breaker import ConsecutiveFailureBreaker

FULL_KR = {"dnca_tot_amt": "1000000", "tot_evlu_amt": "1500000"}
FULL_US = {"frcr_dncl_amt_2": "0", "tot_evlu_amt": "100"}


class _Portfolio:
    def __init__(self, kr, us):
        self._kr, self._us = kr, us

    def get_kr_balance(self):
        return {"positions": [], "summary": self._kr}

    def get_us_balance(self):
        return {"positions": [], "summary": self._us}


def _broker(kr, us):
    b = KISBroker.__new__(KISBroker)
    b._breaker = ConsecutiveFailureBreaker(threshold=5, cooldown_minutes=10)
    b._portfolio = _Portfolio(kr, us)
    b._get_fx = lambda: 1300.0
    return b


@pytest.fixture(autouse=True)
def _fresh_log_memory(monkeypatch):
    monkeypatch.setattr(kis, "_reported_missing", set())


class TestTheReadingSaysWhenItIsIncomplete:
    def test_a_complete_response_without_usd_cash_is_verified(self):
        bal = _broker(FULL_KR, FULL_US).get_balance()
        assert bal.total_eval_krw == 1_500_000 + 100 * 1300
        assert bal.equity_verified is True

    def test_a_missing_us_evaluation_is_unverified_and_the_number_is_unchanged(self):
        us = {"frcr_dncl_amt_2": "0"}                    # no tot_evlu_amt
        bal = _broker(FULL_KR, us).get_balance()
        assert bal.total_eval_krw == 1_500_000           # same as before this change
        assert bal.equity_verified is False

    def test_a_missing_kr_field_is_unverified(self):
        kr = {"dnca_tot_amt": "1000000"}
        assert _broker(kr, FULL_US).get_balance().equity_verified is False

    def test_usd_cash_held_makes_it_unverified_without_adding_it(self):
        """Left out of the total, and whether the US evaluation includes it is
        unconfirmed — so it is flagged, not added."""
        us = {"frcr_dncl_amt_2": "250", "tot_evlu_amt": "100"}
        bal = _broker(FULL_KR, us).get_balance()
        assert bal.total_eval_krw == 1_500_000 + 100 * 1300
        assert bal.cash_usd == 250
        assert bal.equity_verified is False


class TestTheGapIsLoggedForDiagnosis:
    def test_the_log_names_the_fields_kis_sent_and_no_values(self, caplog):
        us = {"frcr_dncl_amt_2": "0", "ovrs_tot_pfls": "12345.67"}
        with caplog.at_level(logging.WARNING, logger="backend.brokers.kis"):
            _broker(FULL_KR, us).get_balance()
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "tot_evlu_amt" in text and "ovrs_tot_pfls" in text
        assert "12345.67" not in text                    # balances are not logged

    def test_each_gap_is_logged_once(self, caplog):
        us = {"frcr_dncl_amt_2": "0"}
        b = _broker(FULL_KR, us)
        with caplog.at_level(logging.WARNING, logger="backend.brokers.kis"):
            for _ in range(3):
                b.get_balance()
        assert sum("#178" in r.getMessage() for r in caplog.records) == 1


class TestTheRouterPassesItOn:
    def _router(self, kr_bal, us_bal):
        from backend.brokers.router import MarketRouter
        r = MarketRouter.__new__(MarketRouter)

        class B:
            def __init__(self, result):
                self._result = result

            def get_balance(self):
                if isinstance(self._result, Exception):
                    raise self._result
                return self._result

        r._kr, r._us = B(kr_bal), B(us_bal)
        return r

    def test_an_unverified_leg_makes_the_sum_unverified(self):
        ok = Balance(1.0, 0.0, 10.0)
        bad = Balance(1.0, 0.0, 10.0, equity_verified=False)
        assert self._router(ok, ok).get_balance().equity_verified is True
        assert self._router(ok, bad).get_balance().equity_verified is False

    def test_a_failed_leg_makes_the_sum_unverified(self):
        ok = Balance(1.0, 0.0, 10.0)
        bal = self._router(ok, RuntimeError("down")).get_balance()
        assert bal.total_eval_krw == 10.0 and bal.equity_verified is False

    def test_a_stub_leg_does_not(self):
        ok = Balance(1.0, 0.0, 10.0)
        assert self._router(ok, NotImplementedError()).get_balance().equity_verified is True
