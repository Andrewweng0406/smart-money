from datetime import date, datetime
from zoneinfo import ZoneInfo

import market_calendar


US_EASTERN = ZoneInfo("America/New_York")


def _et(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=US_EASTERN)


def test_regular_session_uses_standard_stock_market_hours():
    session = market_calendar.get_market_session(date(2026, 8, 3))

    assert session is not None
    assert session.market_open == _et(2026, 8, 3, 9, 30)
    assert session.market_close == _et(2026, 8, 3, 16, 0)


def test_full_market_holiday_has_no_session():
    assert market_calendar.get_market_session(date(2026, 12, 25)) is None


def test_black_friday_uses_early_close():
    session = market_calendar.get_market_session(date(2026, 11, 27))

    assert session is not None
    assert session.market_close == _et(2026, 11, 27, 13, 0)


def test_market_hours_respect_early_close_boundary():
    assert market_calendar.is_market_hours(_et(2026, 11, 27, 13, 0)) is True
    assert market_calendar.is_market_hours(_et(2026, 11, 27, 13, 1)) is False


def test_daily_analysis_time_is_thirty_minutes_after_actual_close():
    assert market_calendar.daily_analysis_time(date(2026, 8, 3)) == _et(2026, 8, 3, 16, 30)
    assert market_calendar.daily_analysis_time(date(2026, 11, 27)) == _et(2026, 11, 27, 13, 30)
    assert market_calendar.daily_analysis_time(date(2026, 12, 25)) is None
