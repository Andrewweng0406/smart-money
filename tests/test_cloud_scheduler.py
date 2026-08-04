"""cloud_scheduler.py 的排程判斷邏輯是純函式（不做 I/O），用合成時間點測試
就能涵蓋所有邊界情況，不需要真的等到收盤時間或真的執行排程任務。"""

from datetime import datetime
from zoneinfo import ZoneInfo

import cloud_scheduler

US_EASTERN = ZoneInfo("America/New_York")


def _et(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=US_EASTERN)


def test_should_trigger_daily_at_exact_time_on_weekday():
    # 2026-08-04 是週二
    now = _et(2026, 8, 4, 16, 30)
    assert cloud_scheduler.should_trigger_daily(now, last_run_date=None) is True


def test_should_trigger_daily_false_if_already_ran_today():
    now = _et(2026, 8, 4, 16, 30)
    assert cloud_scheduler.should_trigger_daily(now, last_run_date=now.date()) is False


def test_should_trigger_daily_false_on_weekend():
    # 2026-08-08 是週六
    now = _et(2026, 8, 8, 16, 30)
    assert cloud_scheduler.should_trigger_daily(now, last_run_date=None) is False


def test_should_trigger_daily_false_outside_time_window():
    now = _et(2026, 8, 4, 16, 31)
    assert cloud_scheduler.should_trigger_daily(now, last_run_date=None) is False


def test_should_trigger_intraday_on_15_minute_boundary():
    now = _et(2026, 8, 4, 10, 15)
    assert cloud_scheduler.should_trigger_intraday(now, last_run_bucket=None) is True


def test_should_trigger_intraday_false_off_boundary():
    now = _et(2026, 8, 4, 10, 16)
    assert cloud_scheduler.should_trigger_intraday(now, last_run_bucket=None) is False


def test_should_trigger_intraday_false_if_bucket_already_ran():
    now = _et(2026, 8, 4, 10, 15)
    bucket = (now.date(), now.hour, now.minute // cloud_scheduler.INTRADAY_INTERVAL_MINUTES)
    assert cloud_scheduler.should_trigger_intraday(now, last_run_bucket=bucket) is False


def test_should_trigger_intraday_true_in_new_bucket_same_hour():
    now = _et(2026, 8, 4, 10, 30)
    previous_bucket = (now.date(), now.hour, 1)  # 10:15 的 bucket
    assert cloud_scheduler.should_trigger_intraday(now, last_run_bucket=previous_bucket) is True
