import json
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import macro_calendar
from macro_calendar import (
    build_warning_text,
    compute_days_until,
    get_calendar_warnings,
    get_next_earnings_date,
    load_macro_events,
)


def test_compute_days_until_with_future_and_past_dates():
    today = date(2026, 8, 1)

    assert compute_days_until(date(2026, 8, 4), today=today) == 3
    assert compute_days_until(date(2026, 7, 30), today=today) == -2


def test_build_warning_text_for_zero_to_three_days():
    assert build_warning_text("FOMC利率決議", 0) == (
        "⚠️ 【高波動預警】距離 FOMC利率決議 僅剩 0 天，"
        "IV 預期飆升，做市商對沖引發的波幅將放大！"
    )
    assert build_warning_text("CPI數據公布", 3) == (
        "⚠️ 【高波動預警】距離 CPI數據公布 僅剩 3 天，"
        "IV 預期飆升，做市商對沖引發的波幅將放大！"
    )


def test_build_warning_text_ignores_outside_warning_window():
    assert build_warning_text("FOMC利率決議", -1) is None
    assert build_warning_text("FOMC利率決議", 4) is None


def test_load_macro_events_reads_valid_json(tmp_path: Path):
    path = tmp_path / "events.json"
    expected = [{"name": "測試事件", "date": "2026-08-03"}]
    path.write_text(json.dumps({"events": expected}), encoding="utf-8")

    assert load_macro_events(str(path)) == expected


def test_load_macro_events_returns_empty_for_missing_or_invalid_file(tmp_path: Path):
    assert load_macro_events(str(tmp_path / "missing.json")) == []

    invalid = tmp_path / "invalid.json"
    invalid.write_text("not json", encoding="utf-8")
    assert load_macro_events(str(invalid)) == []


def test_get_next_earnings_date_returns_none_when_yfinance_fails(monkeypatch):
    broken_yf = SimpleNamespace(Ticker=lambda symbol: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(macro_calendar, "yf", broken_yf)

    assert get_next_earnings_date("TSLA") is None


def test_get_next_earnings_date_selects_earliest_future_candidate(monkeypatch):
    today = datetime.now().date()
    fake_ticker = SimpleNamespace(
        calendar={"Earnings Date": [today + timedelta(days=8), today + timedelta(days=3)]}
    )
    monkeypatch.setattr(
        macro_calendar, "yf", SimpleNamespace(Ticker=lambda symbol: fake_ticker)
    )

    assert get_next_earnings_date("TSLA") == today + timedelta(days=3)


def test_get_calendar_warnings_combines_earnings_and_macro_events(monkeypatch):
    today = datetime.now().date()
    monkeypatch.setattr(
        macro_calendar, "get_next_earnings_date", lambda symbol: today + timedelta(days=1)
    )
    monkeypatch.setattr(
        macro_calendar,
        "load_macro_events",
        lambda path: [
            {"name": "CPI數據公布", "date": (today + timedelta(days=2)).isoformat()},
            {"name": "遠期事件", "date": (today + timedelta(days=10)).isoformat()},
        ],
    )

    warnings = get_calendar_warnings("TSLA", "unused.json")

    assert warnings == [
        "⚠️ 【高波動預警】距離 TSLA 財報 僅剩 1 天，IV 預期飆升，做市商對沖引發的波幅將放大！",
        "⚠️ 【高波動預警】距離 CPI數據公布 僅剩 2 天，IV 預期飆升，做市商對沖引發的波幅將放大！",
    ]
