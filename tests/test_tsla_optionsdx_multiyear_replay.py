import csv
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from optionsdx_tsla_audit import FIELDS
from tsla_optionsdx_multiyear_replay import (choose_long, choose_put, choose_short,
                                             held_quote, liquid, replay_year, year_days)


def row(side, expiry, strike, bid=10, ask=11, size=(1, 1)):
    return {"type": side, "expiration": date.fromisoformat(expiry), "strike": strike,
            "bid": bid, "ask": ask, "size": size}


def test_maturity_policies_are_distinct_in_summer():
    book = {(date(2024, 1, 19), 160, "call"): row("call", "2024-01-19", 160),
            (date(2025, 1, 17), 160, "call"): row("call", "2025-01-17", 160)}
    assert choose_long(book, 200, date(2023, 8, 1), "next_january")["expiration"].year == 2024
    assert choose_long(book, 200, date(2023, 8, 1), "long_january")["expiration"].year == 2025


def test_entry_quotes_need_two_sided_size_and_spread():
    assert liquid(row("call", "2025-01-17", 160), .10)
    assert not liquid(row("call", "2025-01-17", 160, size=(0, 1)), .10)
    assert not liquid(row("call", "2025-01-17", 160, bid=1, ask=5), .10)


def test_short_and_put_selection_follow_fixed_bands():
    day = date(2023, 8, 1)
    short = row("call", "2023-09-01", 210)
    put = row("put", "2023-10-20", 190)
    book = {(r["expiration"], r["strike"], r["type"]): r for r in (short, put)}
    assert choose_short(book, day, 205, ("TSLA", date(2025, 1, 17), 160)) == short
    assert choose_put(book, day, 200) == put


def test_missing_marks_and_incomplete_years_fail_closed(tmp_path):
    with pytest.raises(ValueError, match="Missing held call quote"):
        held_quote({}, ("TSLA", date(2025, 1, 17), 160), "call",
                   datetime(2023, 8, 1, 16, tzinfo=ZoneInfo("America/New_York")))
    with pytest.raises(ValueError, match="Reject incomplete source year"):
        replay_year(tmp_path, {"years": {"2021": {"complete_market_year": False}}},
                    2021, "month_first", "long_january")
    with pytest.raises(ValueError, match="Incomplete monthly files"):
        list(year_days(tmp_path, 2023))


def write_days(path, days):
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for day in days:
            at = datetime(day.year, day.month, day.day, 16,
                          tzinfo=ZoneInfo("America/New_York"))
            writer.writerow({"QUOTE_UNIXTIME": int(at.timestamp()), "QUOTE_DATE": day.isoformat(),
                             "UNDERLYING_LAST": 100, "EXPIRE_DATE": "2021-01-15",
                             "STRIKE": 80, "C_BID": 20, "C_ASK": 21, "C_SIZE": "1 x 1",
                             "P_BID": 1, "P_ASK": 2, "P_SIZE": "1 x 1"})


def test_contiguous_window_uses_only_observed_sessions(tmp_path):
    write_days(tmp_path / "tsla_eod_202001.txt", [date(2020, 1, 2), date(2020, 1, 3)])
    rows = list(year_days(tmp_path, 2020, window=(date(2020, 1, 2), date(2020, 1, 3))))
    assert [row[0] for row in rows] == [date(2020, 1, 2), date(2020, 1, 3)]


def test_contiguous_window_rejects_missing_market_session(tmp_path):
    write_days(tmp_path / "tsla_eod_202001.txt", [date(2020, 1, 2), date(2020, 1, 6)])
    with pytest.raises(ValueError, match="Missing market session within window: 2020-01-03"):
        list(year_days(tmp_path, 2020, window=(date(2020, 1, 2), date(2020, 1, 6))))
