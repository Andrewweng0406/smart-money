from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from pmcc_research import CallQuote, PMCCLedger, PutQuote, put_wall_breach_signal


ET = ZoneInfo("America/New_York")


def quote(day, expiry, strike, bid, ask, symbol="SPY"):
    return CallQuote(symbol, date.fromisoformat(expiry), strike,
                     datetime.fromisoformat(day).replace(hour=15, minute=45, tzinfo=ET), bid, ask)


def long(day="2024-01-02", bid=32, ask=33):
    return quote(day, "2025-01-17", 70, bid, ask)


def short(day="2024-01-02", bid=2, ask=2.1):
    return quote(day, "2024-02-16", 105, bid, ask)


def put(day="2024-01-02", bid=3, ask=3.1, expiry="2024-03-15", strike=95):
    return PutQuote("SPY", date.fromisoformat(expiry), strike,
                    datetime.fromisoformat(day).replace(hour=15, minute=45, tzinfo=ET), bid, ask)


def test_pmcc_keeps_long_mark_after_short_cycle_and_charges_each_side():
    ledger = PMCCLedger.buy_long(long(), capital=10000)
    ledger.write_short(long(), short(), spot=104.5, call_wall=105)
    assert ledger.liquidation_nav(long(), short())["nav"] == pytest.approx(9883.4)

    ledger.close_short(short("2024-01-03", bid=1.4, ask=1.5))
    mark = ledger.liquidation_nav(long("2024-02-16", bid=34, ask=35))
    assert mark["active_short"] is False
    assert mark["short_cycles"] == 1
    assert mark["nav"] == pytest.approx(10143.4)
    assert ledger.close_long(long("2024-02-20", bid=35, ask=36)) == pytest.approx(10243.4)


def test_pmcc_short_must_be_near_wall_higher_strike_and_earlier_expiry():
    ledger = PMCCLedger.buy_long(long(), capital=10000)
    with pytest.raises(ValueError, match="resistance"):
        ledger.write_short(long(), short(), spot=100, call_wall=105)
    with pytest.raises(ValueError, match="higher strike"):
        ledger.write_short(long(), quote("2024-01-02", "2024-02-16", 65, 2, 2.1),
                           spot=104.5, call_wall=105)
    with pytest.raises(ValueError, match="earlier expiry"):
        ledger.write_short(long(), quote("2024-01-02", "2025-03-21", 105, 2, 2.1),
                           spot=104.5, call_wall=105)


def test_pmcc_short_cannot_be_marked_or_closed_at_expiry_without_assignment_model():
    ledger = PMCCLedger.buy_long(long(), capital=10000)
    ledger.write_short(long(), short(), spot=104.5, call_wall=105)
    with pytest.raises(ValueError, match="short call expired"):
        ledger.liquidation_nav(long("2024-02-16"), short("2024-02-16"))
    with pytest.raises(ValueError, match="short call expired"):
        ledger.close_short(short("2024-02-16"))


def test_pmcc_rejects_lookahead_and_missing_held_leg():
    ledger = PMCCLedger.buy_long(long(), capital=10000)
    ledger.write_short(long("2024-01-03"), short("2024-01-03"), spot=104.5, call_wall=105)
    with pytest.raises(ValueError, match="earlier quote"):
        ledger.liquidation_nav(long("2024-01-02"), short("2024-01-02"))
    with pytest.raises(ValueError, match="Missing exact"):
        ledger.liquidation_nav(long("2024-01-04"))
    with pytest.raises(ValueError, match="follow short open"):
        ledger.close_short(short("2024-01-03"))


def test_pmcc_rolls_short_without_rebuying_long():
    ledger = PMCCLedger.buy_long(long(), capital=10000)
    ledger.write_short(long(), short(), spot=104.5, call_wall=105)
    ledger.close_short(short("2024-01-03", bid=1.4, ask=1.5))
    second = quote("2024-01-04", "2024-03-15", 110, 1.4, 1.5)
    ledger.write_short(long("2024-01-04", bid=34, ask=35), second,
                       spot=109.5, call_wall=110)
    assert ledger.short_cycles == 2
    assert ledger.active_short == second.contract
    with pytest.raises(ValueError, match="Close the short"):
        ledger.close_long(long("2024-01-04"))


def test_put_cost_and_mark_are_independent_of_short_call():
    plain = PMCCLedger.buy_long(long(), capital=10000)
    hedged = PMCCLedger.buy_long(long(), capital=10000)
    hedged.buy_put(long(), put())
    plain.write_short(long(), short(), spot=104.5, call_wall=105)
    hedged.write_short(long(), short(), spot=104.5, call_wall=105)
    assert hedged.liquidation_nav(long(), short(), put())["nav"] < plain.liquidation_nav(long(), short())["nav"]

    plain_mark = plain.liquidation_nav(long("2024-01-03", bid=25),
                                       short("2024-01-03", bid=1.4, ask=1.5))["nav"]
    hedged_mark = hedged.liquidation_nav(long("2024-01-03", bid=25),
                                         short("2024-01-03", bid=1.4, ask=1.5),
                                         put("2024-01-03", bid=12, ask=12.2))["nav"]
    assert hedged_mark > plain_mark
    assert hedged.active_put == put().contract
    assert hedged.put_quantity == 1


def test_put_requires_exact_mark_and_is_closed_before_expiry():
    ledger = PMCCLedger.buy_long(long(), capital=10000)
    ledger.buy_put(long(), put())
    with pytest.raises(ValueError, match="Missing exact"):
        ledger.liquidation_nav(long())
    with pytest.raises(ValueError, match="time mismatch"):
        ledger.liquidation_nav(long("2024-01-03"), put_quote=put())
    with pytest.raises(ValueError, match="Close the long put"):
        ledger.close_long(long("2024-01-03"))
    with pytest.raises(ValueError, match="long put expired"):
        ledger.close_put(put("2024-03-15"))
    ledger.close_put(put("2024-01-03", bid=2, ask=2.1))
    assert ledger.close_long(long("2024-01-04")) < 10000


def test_put_can_be_added_after_call_and_rolled_independently():
    ledger = PMCCLedger.buy_long(long(), capital=10000)
    ledger.buy_put(long("2024-01-03"), put("2024-01-03"))
    with pytest.raises(ValueError, match="Close the current"):
        ledger.buy_put(long("2024-01-04"), put("2024-01-04"))
    ledger.close_put(put("2024-01-04", bid=2.5, ask=2.6))
    next_put = put("2024-01-05", expiry="2024-04-19", strike=90)
    ledger.buy_put(long("2024-01-05"), next_put)
    assert ledger.put_cycles == 2
    assert ledger.active_put == next_put.contract


def test_put_rejects_wrong_underlying_and_unfunded_purchase():
    ledger = PMCCLedger.buy_long(long(), capital=3500)
    other = PutQuote("QQQ", put().expiry, 95, put().observed_at, 3, 3.1)
    with pytest.raises(ValueError, match="underlying"):
        ledger.buy_put(long(), other)
    with pytest.raises(ValueError, match="Insufficient cash"):
        ledger.buy_put(long(), put())


def test_put_wall_signal_uses_prior_observation_only():
    signal_at = datetime(2024, 1, 2, 15, 45, tzinfo=ET)
    execute_at = datetime(2024, 1, 3, 15, 45, tzinfo=ET)
    assert put_wall_breach_signal(spot=94, put_wall=95, observed_at=signal_at,
                                  execution_at=execute_at)
    assert not put_wall_breach_signal(spot=96, put_wall=95, observed_at=signal_at,
                                      execution_at=execute_at)
    with pytest.raises(ValueError, match="precede execution"):
        put_wall_breach_signal(spot=94, put_wall=95, observed_at=execute_at,
                               execution_at=execute_at)


def test_expired_put_cannot_be_skipped_by_later_short_call():
    ledger = PMCCLedger.buy_long(long(), capital=10000)
    ledger.buy_put(long(), put(expiry="2024-01-05"))
    with pytest.raises(ValueError, match="long put expired"):
        ledger.write_short(long("2024-01-08"), short("2024-01-08"),
                           spot=104.5, call_wall=105)


def test_distinct_legs_can_close_on_same_observation():
    ledger = PMCCLedger.buy_long(long(), capital=10000)
    ledger.buy_put(long(), put())
    ledger.write_short(long(), short(), spot=104.5, call_wall=105)
    ledger.close_short(short("2024-01-03", bid=1.4, ask=1.5))
    ledger.close_put(put("2024-01-03", bid=2.5, ask=2.6))
    assert ledger.close_long(long("2024-01-03", bid=31, ask=32)) > 0
