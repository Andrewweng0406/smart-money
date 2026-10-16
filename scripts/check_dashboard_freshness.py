"""Check the public dashboard's completed daily observations after the US close."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal


BASE = "https://tsla-strategy-workbench.yummyapple37.chatgpt.site"
EASTERN = ZoneInfo("America/New_York")


def expected_session(now: datetime) -> str:
    eastern = now.astimezone(EASTERN)
    cutoff = eastern.date() if eastern.hour >= 17 else eastern.date() - timedelta(days=1)
    sessions = mcal.get_calendar("NYSE").valid_days(
        start_date=cutoff - timedelta(days=14), end_date=cutoff,
    )
    if sessions.empty:
        raise ValueError("No recent NYSE session")
    return sessions[-1].date().isoformat()


def check_payloads(price: dict, macro: dict, now: datetime) -> list[str]:
    expected = expected_session(now)
    errors = []
    rows = price.get("rows")
    if price.get("source") != "https://www.nasdaq.com/market-activity/stocks/tsla/historical":
        errors.append("TSLA price source is unexpected")
    if not isinstance(rows, list) or not rows:
        errors.append("TSLA price rows are missing")
    else:
        latest = max((row.get("date", "") for row in rows if isinstance(row, dict)), default="")
        if latest < expected:
            errors.append(f"TSLA close is {latest or 'missing'}; expected {expected}")
    metrics = macro.get("metrics")
    by_id = {metric.get("id"): metric for metric in metrics if isinstance(metric, dict)} if isinstance(metrics, list) else {}
    for key in ("dgs10", "curve", "vix"):
        day = by_id.get(key, {}).get("date", "")
        if day < expected:
            errors.append(f"{key} observation is {day or 'missing'}; expected {expected}")
    today = now.astimezone(EASTERN).date()
    for key, max_age in (("unrate", 60), ("cpi", 75)):
        day = by_id.get(key, {}).get("date", "")
        try:
            age = (today - datetime.fromisoformat(day).date()).days
        except (ValueError, TypeError):
            age = max_age + 1
        if age > max_age:
            errors.append(f"{key} observation is {day or 'missing'} ({age} days old)")
    return errors


def fetch_json(path: str) -> dict:
    for attempt in range(3):
        version = int(datetime.now(EASTERN).timestamp())
        request = Request(
            f"{BASE}{path}?monitor={version}",
            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        try:
            with urlopen(request, timeout=25) as response:
                if response.headers.get_content_type() != "application/json":
                    raise ValueError(f"Unexpected content type for {path}")
                return json.load(response)
        except HTTPError as exc:
            if attempt == 2 or (exc.code != 429 and exc.code < 500):
                raise
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(2)
    raise AssertionError("Unreachable retry state")


def main() -> int:
    now = datetime.now(EASTERN)
    errors = []
    payloads = {}
    for name in ("price", "macro"):
        try:
            payloads[name] = fetch_json(f"/api/{name}")
        except Exception as exc:
            errors.append(f"{name} API unavailable: {exc}")
    if "price" in payloads and "macro" in payloads:
        errors.extend(check_payloads(payloads["price"], payloads["macro"], now))
    elif "price" in payloads:
        expected = expected_session(now)
        rows = payloads["price"].get("rows") or []
        latest = max((row.get("date", "") for row in rows if isinstance(row, dict)), default="")
        if latest < expected:
            errors.append(f"TSLA close is {latest or 'missing'}; expected {expected}")
    print(f"Dashboard check: {now.isoformat(timespec='minutes')} / expected NYSE session {expected_session(now)}")
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print("PASS: public price and macro observations are current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
