"""cloud_scheduler.py 的排程判斷邏輯是純函式（不做 I/O），用合成時間點測試
就能涵蓋所有邊界情況，不需要真的等到收盤時間或真的執行排程任務。"""

from datetime import date, datetime
from unittest.mock import MagicMock
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


def test_should_trigger_daily_false_on_market_holiday():
    now = _et(2026, 12, 25, 16, 30)
    assert cloud_scheduler.should_trigger_daily(now, last_run_date=None) is False


def test_should_trigger_daily_uses_early_close_time():
    now = _et(2026, 11, 27, 13, 30)
    assert cloud_scheduler.should_trigger_daily(now, last_run_date=None) is True


def test_should_trigger_daily_catches_up_after_exact_schedule_minute():
    now = _et(2026, 8, 4, 16, 31)
    assert cloud_scheduler.should_trigger_daily(now, last_run_date=None) is True


def test_should_trigger_daily_false_before_analysis_time():
    now = _et(2026, 8, 4, 16, 29)
    assert cloud_scheduler.should_trigger_daily(now, last_run_date=None) is False


def test_find_missing_daily_snapshots_checks_every_symbol(monkeypatch, tmp_path):
    def fake_recent(symbol, limit, db_path):
        return [{"date": "2026-08-04"}] if symbol == "TSLA" else [{"date": "2026-08-03"}]

    monkeypatch.setattr(cloud_scheduler.db_manager, "get_recent_snapshots", fake_recent)

    missing = cloud_scheduler.find_missing_daily_snapshots(
        ["TSLA", "SOXL"], date(2026, 8, 4), db_path=tmp_path / "history.db",
    )

    assert missing == ["SOXL"]


def test_run_daily_analysis_alerts_once_for_missing_snapshots(monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_scheduler.run_watchlist, "load_watchlist", lambda path: ["TSLA", "SOXL"])
    monkeypatch.setattr(cloud_scheduler, "_run_job", lambda args: True)
    monkeypatch.setattr(cloud_scheduler, "find_missing_daily_snapshots", lambda *a, **k: ["SOXL"])
    send_mock = MagicMock()
    monkeypatch.setattr(cloud_scheduler.telegram_notifier, "send_text_report", send_mock)
    state_path = tmp_path / "scheduler_state.json"

    first = cloud_scheduler.run_daily_analysis(
        date(2026, 8, 4), db_path=tmp_path / "history.db", state_path=state_path,
    )
    second = cloud_scheduler.run_daily_analysis(
        date(2026, 8, 4), db_path=tmp_path / "history.db", state_path=state_path,
    )

    assert first is False
    assert second is False
    send_mock.assert_called_once()
    assert "SOXL" in send_mock.call_args.args[0]


def test_run_daily_analysis_does_not_alert_when_all_snapshots_exist(monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_scheduler.run_watchlist, "load_watchlist", lambda path: ["TSLA", "SOXL"])
    monkeypatch.setattr(cloud_scheduler, "_run_job", lambda args: True)
    monkeypatch.setattr(cloud_scheduler, "find_missing_daily_snapshots", lambda *a, **k: [])
    send_mock = MagicMock()
    monkeypatch.setattr(cloud_scheduler.telegram_notifier, "send_text_report", send_mock)

    result = cloud_scheduler.run_daily_analysis(
        date(2026, 8, 4), db_path=tmp_path / "history.db",
        state_path=tmp_path / "scheduler_state.json",
    )

    assert result is True
    send_mock.assert_not_called()


def test_run_daily_analysis_skips_job_when_snapshots_already_complete(monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_scheduler.run_watchlist, "load_watchlist", lambda path: ["TSLA", "SOXL"])
    monkeypatch.setattr(cloud_scheduler, "find_missing_daily_snapshots", lambda *a, **k: [])
    run_mock = MagicMock(return_value=True)
    monkeypatch.setattr(cloud_scheduler, "_run_job", run_mock)

    result = cloud_scheduler.run_daily_analysis(
        date(2026, 8, 4), db_path=tmp_path / "history.db",
        state_path=tmp_path / "scheduler_state.json",
    )

    assert result is True
    run_mock.assert_not_called()


def test_should_trigger_intraday_summary_at_exact_time_on_weekday():
    now = _et(2026, 8, 4, 10, 0)
    assert cloud_scheduler.should_trigger_intraday_summary(now, last_run_date=None) is True


def test_should_trigger_intraday_summary_false_if_already_ran_today():
    now = _et(2026, 8, 4, 10, 0)
    assert cloud_scheduler.should_trigger_intraday_summary(now, last_run_date=now.date()) is False


def test_should_trigger_intraday_summary_false_on_weekend():
    now = _et(2026, 8, 8, 10, 0)
    assert cloud_scheduler.should_trigger_intraday_summary(now, last_run_date=None) is False


def test_should_trigger_intraday_summary_false_outside_time_window():
    now = _et(2026, 8, 4, 10, 1)
    assert cloud_scheduler.should_trigger_intraday_summary(now, last_run_date=None) is False


def test_should_trigger_intraday_on_15_minute_boundary():
    now = _et(2026, 8, 4, 10, 15)
    assert cloud_scheduler.should_trigger_intraday(now, last_run_bucket=None) is True


def test_should_trigger_intraday_false_during_premarket():
    now = _et(2026, 8, 4, 8, 45)
    assert cloud_scheduler.should_trigger_intraday(now, last_run_bucket=None) is False


def test_should_trigger_intraday_true_at_exact_regular_open():
    now = _et(2026, 8, 4, 9, 30)
    assert cloud_scheduler.should_trigger_intraday(now, last_run_bucket=None) is True


def test_should_trigger_intraday_true_at_exact_close():
    now = _et(2026, 8, 4, 16, 0)
    assert cloud_scheduler.should_trigger_intraday(now, last_run_bucket=None) is True


def test_should_trigger_intraday_false_after_close():
    now = _et(2026, 8, 4, 16, 15)
    assert cloud_scheduler.should_trigger_intraday(now, last_run_bucket=None) is False


def test_should_trigger_intraday_false_on_weekend():
    now = _et(2026, 8, 8, 10, 15)
    assert cloud_scheduler.should_trigger_intraday(now, last_run_bucket=None) is False


def test_should_trigger_intraday_false_on_market_holiday():
    now = _et(2026, 12, 25, 10, 15)
    assert cloud_scheduler.should_trigger_intraday(now, last_run_bucket=None) is False


def test_should_trigger_intraday_respects_early_close():
    now = _et(2026, 11, 27, 13, 15)
    assert cloud_scheduler.should_trigger_intraday(now, last_run_bucket=None) is False


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
