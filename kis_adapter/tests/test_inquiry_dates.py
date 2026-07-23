"""Unit tests for kis_adapter.dates.inquiry_date_range.

The KIS single-symbol order-inquiry TRs take YYYYMMDD date bounds. An empty
range collapses to "today only", which the Quick Trade reconciliation sweep
relies on NOT happening (see docs / kis_adapter/dates.py). These tests pin the
format and window semantics without depending on the wall-clock date.
"""
from datetime import datetime, timedelta, timezone

from kis_adapter.dates import INQUIRE_LOOKBACK_DAYS, inquiry_date_range


def _parse(s):
    return datetime.strptime(s, "%Y%m%d").date()


def test_returns_two_8digit_yyyymmdd_strings():
    start, end = inquiry_date_range()
    assert isinstance(start, str) and isinstance(end, str)
    assert len(start) == 8 and len(end) == 8
    # Round-trips as a real calendar date.
    _parse(start)
    _parse(end)


def test_default_span_is_lookback_days():
    start, end = inquiry_date_range()
    assert (_parse(end) - _parse(start)).days == INQUIRE_LOOKBACK_DAYS


def test_start_not_after_end():
    start, end = inquiry_date_range()
    assert start <= end


def test_end_is_today_kst():
    _, end = inquiry_date_range()
    today_kst = datetime.now(timezone(timedelta(hours=9))).date()
    assert _parse(end) == today_kst


def test_zero_lookback_is_same_day():
    start, end = inquiry_date_range(0)
    assert start == end


def test_negative_lookback_clamped_to_same_day():
    start, end = inquiry_date_range(-5)
    assert start == end


def test_custom_lookback_span():
    start, end = inquiry_date_range(7)
    assert (_parse(end) - _parse(start)).days == 7
