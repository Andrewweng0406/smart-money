from datetime import date

import pytest

from tsla_seasonal_research import dip_entry, replay, strategy_path


def test_dip_requires_previous_close_and_executes_next_session():
    closes = [100.0] * 20 + [89.0, 92.0, 95.0]
    index, triggered = dip_entry(closes, [20, 21, 22])
    assert (index, triggered) == (21, True)


def test_no_signal_uses_last_session_of_month():
    closes = [100.0] * 25
    assert dip_entry(closes, [21, 22, 23]) == (23, False)


def test_stock_proxy_charges_both_sides_and_reserves_unused_cash():
    prices = [(date(2024, 1, 2), 100.0), (date(2024, 1, 3), 110.0)]
    result = strategy_path(prices, [(0, 1), (1, 1)], cost_bps=5)
    first_return = 110 * .9995 / (100 * 1.0005) - 1
    second_return = .9995 / 1.0005 - 1
    assert result["return_pct"] == pytest.approx(round(50 * (first_return + second_return), 3))


def test_incomplete_current_year_is_not_included():
    prices = [(date(2024, month, 15), 100.0 + month) for month in range(1, 13)]
    prices += [(date(2025, 1, 15), 120.0)]
    result = replay(prices)
    assert result["complete_years"] == [2024]
    assert len(result["annual"]) == 1
