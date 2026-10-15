from datetime import date, timedelta

import pytest

from tsla_seasonal_research import strategy_path
from tsla_walkforward_research import (entry_for_month, fold_summary,
                                       paired_year_bootstrap, score, trend_ok)


def test_trend_filter_uses_only_closes_before_entry():
    closes = [100.0] * 200 + [101.0]
    assert not trend_ok(closes, 200)
    assert trend_ok(closes, 201)


def test_trend_dip_skips_month_when_only_current_day_falls():
    closes = [100.0] * 200 + [101.0, 80.0]
    assert entry_for_month(closes, [201], "trend_200_dip_10") == 201
    assert entry_for_month(closes, [202], "trend_200_dip_10") is None


def test_skipped_tranches_stay_in_cash():
    prices = [(date(2024, 1, 2) + timedelta(days=index), close)
              for index, close in enumerate((100.0, 110.0))]
    result = strategy_path(prices, [(0, 1)], cost_bps=0, allocation_slots=6)
    assert result["return_pct"] == pytest.approx(1.667)
    empty = strategy_path(prices, [], allocation_slots=6)
    assert empty["return_pct"] == 0


def test_score_distinguishes_trade_wins_from_positive_account_years():
    rows = [{"return_pct": 1.0, "max_daily_nav_drawdown_pct": 2.0,
             "trade_count": 2, "winning_trades": 1},
            {"return_pct": -3.0, "max_daily_nav_drawdown_pct": 4.0,
             "trade_count": 0, "winning_trades": 0}]
    result = score(rows)
    assert result["positive_years"] == 1
    assert result["trade_win_rate_pct"] == 50
    assert result["participation_pct"] == pytest.approx(16.67)


def test_fold_summary_reports_chosen_years_without_compounding():
    result = fold_summary([{"selected": "dip_10", "selected_return_pct": 5,
                            "month_first_return_pct": 8},
                           {"selected": "dip_10", "selected_return_pct": -2,
                            "month_first_return_pct": -4}])
    assert result["selected_positive_years"] == 1
    assert result["selected_mean_return_pct"] == 1.5
    assert result["selection_counts"] == {"dip_10": 2}


def test_paired_bootstrap_preserves_constant_year_difference():
    result = paired_year_bootstrap([-2.0, -2.0, -2.0], samples=100)
    assert result == {"mean_difference_percentage_points": -2.0,
                      "descriptive_95pct_interval": [-2.0, -2.0]}
