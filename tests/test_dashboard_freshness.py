from datetime import datetime
from zoneinfo import ZoneInfo

from scripts.check_dashboard_freshness import check_payloads, expected_session


ET = ZoneInfo("America/New_York")


def payloads(date: str):
    price = {
        "source": "https://www.nasdaq.com/market-activity/stocks/tsla/historical",
        "rows": [{"date": date, "close": 360}],
    }
    macro = {"metrics": [
        {"id": key, "date": date} for key in ("dgs10", "curve", "vix")
    ] + [
        {"id": "unrate", "date": "2026-09-01"},
        {"id": "cpi", "date": "2026-09-01"},
    ]}
    return price, macro


def test_expected_session_skips_weekends_and_market_holidays():
    assert expected_session(datetime(2026, 10, 19, 16, 0, tzinfo=ET)) == "2026-10-16"
    assert expected_session(datetime(2026, 10, 19, 18, 0, tzinfo=ET)) == "2026-10-19"
    assert expected_session(datetime(2026, 12, 25, 19, 0, tzinfo=ET)) == "2026-12-24"


def test_current_observations_pass():
    now = datetime(2026, 10, 15, 18, 0, tzinfo=ET)
    assert check_payloads(*payloads("2026-10-15"), now) == []


def test_successful_api_with_old_observations_fails():
    now = datetime(2026, 10, 15, 18, 0, tzinfo=ET)
    errors = check_payloads(*payloads("2026-10-12"), now)
    assert any("TSLA close" in error for error in errors)
    assert any("vix observation" in error for error in errors)


def test_missing_monthly_metric_fails():
    now = datetime(2026, 10, 15, 18, 0, tzinfo=ET)
    price, macro = payloads("2026-10-15")
    macro["metrics"] = [metric for metric in macro["metrics"] if metric["id"] != "cpi"]
    assert any("cpi observation" in error for error in check_payloads(price, macro, now))
