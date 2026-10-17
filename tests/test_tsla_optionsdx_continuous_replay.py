from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from tsla_optionsdx_continuous_replay import (available_premium, choose_weekly_put,
                                              funding_after_short_close,
                                              funding_after_short_write, last_normal_exit_before_expiry,
                                              open_long, premium_can_fund_put, put_risk_signal,
                                              replay_year, roll_long, reserved_cash_available,
                                              short_buyback_cost)


def option(expiry, strike, bid, ask):
    return {"type": "call", "expiration": expiry, "strike": strike,
            "bid": bid, "ask": ask, "size": (1, 1)}


def test_roll_preserves_cash_and_keeps_replacement_open():
    at = datetime(2023, 9, 1, 16, tzinfo=ZoneInfo("America/New_York"))
    old = option(date(2024, 1, 19), 160, 40, 41)
    new = option(date(2025, 1, 17), 160, 44, 45)
    ledger = open_long(old, at, 10000)
    book = {(row["expiration"], row["strike"], "call"): row for row in (old, new)}
    replacement = roll_long(ledger, book, 200, at.date(), at)
    assert replacement is not None
    assert ledger.closed
    assert replacement.long_contract[1] == new["expiration"]
    assert replacement.cash == pytest.approx(10000 - 4101.65 + 3998.35 - 4501.65)


def test_roll_does_not_close_old_contract_when_new_one_is_unaffordable():
    at = datetime(2023, 9, 1, 16, tzinfo=ZoneInfo("America/New_York"))
    old = option(date(2024, 1, 19), 160, 40, 41)
    new = option(date(2025, 1, 17), 160, 99, 100)
    ledger = open_long(old, at, 5000)
    book = {(row["expiration"], row["strike"], "call"): row for row in (old, new)}
    assert roll_long(ledger, book, 200, at.date(), at) is None
    assert not ledger.closed


def test_incomplete_year_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Reject incomplete source year"):
        replay_year(tmp_path, {"years": {"2022": {"complete_market_year": False}}}, 2022)


def test_partial_window_cannot_cross_unmodeled_stock_split(tmp_path):
    with pytest.raises(ValueError, match="Window crosses unmodeled option split"):
        replay_year(tmp_path, {"years": {"2022": {"complete_market_year": False}}}, 2022,
                    window=(date(2022, 8, 24), date(2022, 8, 25)))


def test_weekly_put_selects_nearest_eligible_expiry():
    from tsla_optionsdx_multiyear_replay import choose_put

    day = date(2023, 8, 1)
    near = {"type": "put", "expiration": date(2023, 8, 4), "strike": 190.,
            "bid": 4., "ask": 4.2, "size": (1, 1)}
    farther = {**near, "expiration": date(2023, 8, 11)}
    term = {**near, "expiration": date(2023, 9, 15)}
    book = {(row["expiration"], row["strike"], "put"): row
            for row in (near, farther, term)}
    assert choose_weekly_put(book, day, 200) == near
    assert choose_put(book, day, 200) == term


def test_weekly_put_target_changes_strike_without_changing_expiry():
    day = date(2023, 8, 1)
    book = {}
    for strike in (180., 190., 196.):
        row = {"type": "put", "expiration": date(2023, 8, 4), "strike": strike,
               "bid": 4., "ask": 4.2, "size": (1, 1)}
        book[(row["expiration"], strike, "put")] = row
    assert choose_weekly_put(book, day, 200, .90)["strike"] == 180.
    assert choose_weekly_put(book, day, 200, .95)["strike"] == 190.
    assert choose_weekly_put(book, day, 200, .98)["strike"] == 196.


def test_weekly_put_exits_before_expiry_or_early_close():
    assert not last_normal_exit_before_expiry(date(2023, 8, 2), date(2023, 8, 4))
    assert last_normal_exit_before_expiry(date(2023, 8, 3), date(2023, 8, 4))
    assert last_normal_exit_before_expiry(date(2023, 11, 22), date(2023, 11, 24))


def test_premium_funding_cannot_spend_unrealized_short_credit_in_realized_mode():
    gross = funding_after_short_write(0, 200, "gross")
    realized = funding_after_short_write(0, 200, "realized")
    assert premium_can_fund_put(gross, 150)
    assert not premium_can_fund_put(realized, 150)
    assert funding_after_short_close(gross - 150, 200, 100, "gross") == -50
    assert funding_after_short_close(realized, 200, 100, "realized") == 100


def test_invalid_premium_funding_policy_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Unknown funding policy"):
        replay_year(tmp_path, {}, 2023, funding_policy="borrow")


def test_invalid_put_target_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Invalid put target ratio"):
        replay_year(tmp_path, {}, 2023, put_target_ratio=.85)


def test_invalid_put_exit_rules_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="Invalid put hold sessions"):
        replay_year(tmp_path, {}, 2023, put_hold_sessions=0)
    with pytest.raises(ValueError, match="Invalid put take profit percent"):
        replay_year(tmp_path, {}, 2023, put_take_profit_pct=0)
    with pytest.raises(ValueError, match="Invalid put entry max rebound percent"):
        replay_year(tmp_path, {}, 2023, put_entry_max_rebound_pct=.11)
    with pytest.raises(ValueError, match="Invalid put entry max daily return percent"):
        replay_year(tmp_path, {}, 2023, put_entry_max_daily_return_pct=.01)


def test_put_risk_signals_use_observed_high_and_low():
    assert put_risk_signal(94, 100, 90, "drawdown5")
    assert not put_risk_signal(94, 100, 90, "break20_low")
    assert put_risk_signal(89, 100, 90, "break20_low")
    with pytest.raises(ValueError, match="Unknown risk signal policy"):
        put_risk_signal(94, 100, 90, "future_low")


def test_invalid_funding_scope_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Unknown funding scope"):
        replay_year(tmp_path, {}, 2023, funding_scope="borrow")


def test_account_premium_pool_can_cover_a_slot_that_cannot_self_fund():
    first = {"funding_balance": 20.}
    second = {"funding_balance": 180.}
    slots = [first, second]
    assert not premium_can_fund_put(available_premium(slots, first, "slot"), 150)
    assert premium_can_fund_put(available_premium(slots, first, "account"), 150)


def test_configurable_slots_and_entry_reserve(monkeypatch, tmp_path):
    from tsla_optionsdx_continuous_replay import __name__ as module_name

    day = date(2023, 1, 3)
    at = datetime(2023, 1, 3, 16, tzinfo=ZoneInfo("America/New_York"))
    long = option(date(2025, 1, 17), 800, 290, 300)
    book = {(long["expiration"], long["strike"], "call"): long}
    monkeypatch.setattr(f"{module_name}.year_days",
                        lambda *args, **kwargs: iter([(day, at, 1000., book)]))
    audit = {"years": {"2023": {"complete_market_year": True}}}
    three = replay_year(tmp_path, audit, 2023, entry_months=(1, 3, 7), reserve_pct=.15)
    two = replay_year(tmp_path, audit, 2023, entry_months=(1, 7), reserve_pct=.15)
    assert three["counts"]["call_reserve_blocked"] == 1
    assert three["counts"].get("entries", 0) == 0
    assert two["counts"]["entries"] == 1
    assert two["slot_capital"] == 50000
    assert two["reserve_per_slot"] == 7500
    assert two["negative_cash_slot_sessions"]["pmcc"] == 0


def test_reserve_validation_and_current_short_liability(tmp_path):
    with pytest.raises(ValueError, match="Invalid entry months"):
        replay_year(tmp_path, {}, 2023, entry_months=(1, 1))
    with pytest.raises(ValueError, match="Invalid reserve percent"):
        replay_year(tmp_path, {}, 2023, reserve_pct=1)
    at = datetime(2023, 9, 1, 16, tzinfo=ZoneInfo("America/New_York"))
    ledger = open_long(option(date(2025, 1, 17), 80, 19, 20), at, 10000)
    short = option(date(2023, 9, 15), 110, 2, 2.1)
    ledger.active_short = ("TSLA", short["expiration"], short["strike"])
    book = {(short["expiration"], short["strike"], "call"): short}
    assert short_buyback_cost(ledger, short) == pytest.approx(211.65)
    assert reserved_cash_available(ledger, book, at, 100, 1000)
    assert not reserved_cash_available(ledger, book, at, 7000, 1000)
