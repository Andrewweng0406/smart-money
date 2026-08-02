import pytest

from strategy_tracker import StrategyOutcome, score_outcome, summarize_track_record


def test_bull_put_spread_safe_settlement_is_win():
    legs = [
        {"action": "SELL", "option_type": "PUT", "strike_price": 100.0},
        {"action": "BUY", "option_type": "PUT", "strike_price": 95.0},
    ]

    result = score_outcome(
        legs=legs,
        net_premium=1.5,
        strategy_type="credit",
        settlement_spot=105.0,
        max_loss=3.5,
    )

    assert result == StrategyOutcome(
        outcome="WIN", realized_pnl=1.5, max_loss_hit=False
    )


def test_bull_put_spread_below_both_strikes_hits_max_loss():
    legs = [
        {"action": "SELL", "option_type": "PUT", "strike_price": 100.0},
        {"action": "BUY", "option_type": "PUT", "strike_price": 95.0},
    ]

    result = score_outcome(
        legs=legs,
        net_premium=1.5,
        strategy_type="credit",
        settlement_spot=90.0,
        max_loss=3.5,
    )

    assert result.outcome == "LOSS"
    assert result.realized_pnl == pytest.approx(-3.5)
    assert result.max_loss_hit is True


@pytest.mark.parametrize("settlement_spot", [85.0, 115.0])
def test_iron_condor_loses_when_either_side_is_fully_breached(settlement_spot):
    legs = [
        {"action": "BUY", "option_type": "PUT", "strike_price": 90.0},
        {"action": "SELL", "option_type": "PUT", "strike_price": 95.0},
        {"action": "SELL", "option_type": "CALL", "strike_price": 105.0},
        {"action": "BUY", "option_type": "CALL", "strike_price": 110.0},
    ]

    result = score_outcome(
        legs=legs,
        net_premium=2.0,
        strategy_type="credit",
        settlement_spot=settlement_spot,
        max_loss=3.0,
    )

    assert result.outcome == "LOSS"
    assert result.realized_pnl == pytest.approx(-3.0)
    assert result.max_loss_hit is True


def test_long_strangle_expires_worthless_and_loses_full_debit():
    legs = [
        {"action": "BUY", "option_type": "PUT", "strike_price": 90.0},
        {"action": "BUY", "option_type": "CALL", "strike_price": 110.0},
    ]

    result = score_outcome(
        legs=legs,
        net_premium=4.0,
        strategy_type="debit",
        settlement_spot=100.0,
        max_loss=4.0,
    )

    assert result == StrategyOutcome(
        outcome="LOSS", realized_pnl=-4.0, max_loss_hit=True
    )


@pytest.mark.parametrize("settlement_spot", [75.0, 125.0])
def test_long_strangle_wins_on_large_move_through_either_side(settlement_spot):
    legs = [
        {"action": "BUY", "option_type": "PUT", "strike_price": 90.0},
        {"action": "BUY", "option_type": "CALL", "strike_price": 110.0},
    ]

    result = score_outcome(
        legs=legs,
        net_premium=4.0,
        strategy_type="debit",
        settlement_spot=settlement_spot,
        max_loss=4.0,
    )

    assert result.outcome == "WIN"
    assert result.realized_pnl == pytest.approx(11.0)
    assert result.max_loss_hit is False


def test_summarize_track_record_empty_list():
    assert summarize_track_record([]) == {
        "total_count": 0,
        "win_count": 0,
        "win_rate_pct": 0.0,
        "total_pnl": 0.0,
        "by_strategy": {},
    }


def test_summarize_track_record_single_strategy():
    records = [
        {"strategy_name": "Bull Put Spread", "outcome": "WIN", "realized_pnl": 1.5},
        {"strategy_name": "Bull Put Spread", "outcome": "WIN", "realized_pnl": 1.0},
        {"strategy_name": "Bull Put Spread", "outcome": "LOSS", "realized_pnl": -3.5},
    ]

    assert summarize_track_record(records) == {
        "total_count": 3,
        "win_count": 2,
        "win_rate_pct": 66.7,
        "total_pnl": -1.0,
        "by_strategy": {
            "Bull Put Spread": {
                "count": 3,
                "win_rate_pct": 66.7,
                "total_pnl": -1.0,
            }
        },
    }


def test_summarize_track_record_mixed_strategies():
    records = [
        {"strategy_name": "Iron Condor", "outcome": "WIN", "realized_pnl": 2.0},
        {"strategy_name": "Iron Condor", "outcome": "LOSS", "realized_pnl": -3.0},
        {"strategy_name": "Long Strangle", "outcome": "WIN", "realized_pnl": 8.0},
        {"strategy_name": "Long Strangle", "outcome": "WIN", "realized_pnl": 5.0},
    ]

    assert summarize_track_record(records) == {
        "total_count": 4,
        "win_count": 3,
        "win_rate_pct": 75.0,
        "total_pnl": 12.0,
        "by_strategy": {
            "Iron Condor": {
                "count": 2,
                "win_rate_pct": 50.0,
                "total_pnl": -1.0,
            },
            "Long Strangle": {
                "count": 2,
                "win_rate_pct": 100.0,
                "total_pnl": 13.0,
            },
        },
    }
