from dataclasses import asdict

import pytest

import options_research
from data_fetcher import StrikeLegRaw
from options_strategy_engine import build_credit_spread, select_strategy


def chain():
    return [StrikeLegRaw("2026-11-13", k, 100, .4, 10, 100, .45, 10,
                         2 if k == 105 else 1 if k == 110 else .5,
                         2.1 if k == 105 else 1.1 if k == 110 else .6,
                         2 if k == 95 else 1 if k == 90 else .5,
                         2.1 if k == 95 else 1.1 if k == 90 else .6)
            for k in (85, 90, 95, 105, 110, 115)]


def recommend(**kwargs):
    return select_strategy(chain(), 100, 35 / 365, 90, 110, None, **kwargs)


def test_production_abstains_without_forecast():
    assert recommend(total_net_gex=100).legs is None


@pytest.mark.parametrize("gex", [None, 0, float("nan")])
def test_production_requires_numeric_gamma_state(gex):
    assert recommend(total_net_gex=gex, forecast_vol=.8).legs is None


def test_negative_gamma_does_not_alone_justify_strangle():
    assert recommend(total_net_gex=-100, forecast_vol=.1).legs is None
    assert recommend(total_net_gex=-100, forecast_vol=.8).strategy_type == "debit"


def test_costs_can_remove_model_edge():
    assert recommend(total_net_gex=-100, forecast_vol=.8, fee_per_contract=1000).legs is None


def test_inverted_walls_abstain():
    assert select_strategy(chain(), 100, 35/365, 105, 95, None,
                           total_net_gex=100, forecast_vol=.2).legs is None


def resistance_chain():
    legs = chain()
    legs[3].call_bid, legs[3].call_ask = 3, 3.2
    legs[4].call_bid, legs[4].call_ask = .5, .6
    return legs


def test_resistance_defense_only_sells_call_at_wall():
    legs = resistance_chain()
    result = select_strategy(legs, 104.5, 35/365, 90, 105, None,
                             total_net_gex=100, forecast_vol=.05,
                             setup="resistance_defense")
    assert result.strategy_type == "credit"
    assert [(leg.action, leg.option_type, leg.strike_price) for leg in result.legs] == [
        ("SELL", "CALL", 105), ("BUY", "CALL", 110)]
    for spot, gex in [(100, 100), (105.5, 100), (104.5, -100)]:
        assert select_strategy(legs, spot, 35/365, 90, 105, None,
                               total_net_gex=gex, forecast_vol=.05,
                               setup="resistance_defense").legs is None


def test_impossible_spread_credit_rejected():
    legs = chain()
    legs[1].put_bid = 10
    assert build_credit_spread(legs, 100, "put", 35/365) is None


def test_crossed_quote_rejected():
    legs = chain()
    legs[3].call_bid = 3
    assert select_strategy(legs, 100, 35/365, 90, 110, None,
                           total_net_gex=-100, forecast_vol=.8).legs is None


def frames():
    base = dict(symbol="TEST", expiry="2026-11-13", spot=100,
                put_wall=90, call_wall=110, alert="negative gamma", total_net_gex=-100,
                data_usable=True, forecast_vol=.8, forecast_asof="2026-10-08T19:00:00+00:00",
                legs=[asdict(leg) for leg in chain()])
    return [dict(base, observed_at="2026-10-08T19:30:00+00:00"),
            dict(base, observed_at="2026-10-09T15:00:00+00:00")]


def resistance_frames():
    return [dict(frame, spot=104.5, call_wall=105, total_net_gex=100, alert=None,
                 forecast_vol=.05, legs=[asdict(leg) for leg in resistance_chain()])
            for frame in frames()]


def test_resistance_replay_requires_signal_and_entry_near_wall():
    data = resistance_frames()
    settlements = {("TEST", "2026-11-13"): 100}
    result = options_research.replay(data, settlements, "2026-10-01", variant="resistance_defense",
                                     initial_capital=100000)
    assert result["results"]["holdout"]["trade_count"] == 1
    data[0]["spot"] = 100
    result = options_research.replay(data, settlements, "2026-10-01", variant="resistance_defense",
                                     initial_capital=100000)
    assert result["results"]["holdout"]["trade_count"] == 0
    assert result["results"]["holdout"]["skip_reasons"]["signal_not_near_resistance"] == 1


def test_replay_delays_entry_and_deducts_costs():
    result = options_research.replay(frames(), {("TEST", "2026-11-13"): 150},
                                    "2026-10-01", initial_capital=100000)
    holdout = result["results"]["holdout"]
    assert holdout["trade_count"] == 1
    trade = holdout["trades"][0]
    assert trade["entry"] == "2026-10-09"
    assert trade["quantity"] == 2
    assert trade["costs"] == pytest.approx(9.2)
    assert trade["net_pnl"] == pytest.approx(8150.8)
    assert holdout["sufficient_for_validation"] is False


def test_replay_does_not_fallback_to_latest_spot():
    result = options_research.replay(frames(), {}, "2026-10-01", initial_capital=100000)
    assert result["results"]["holdout"]["trade_count"] == 0


def test_replay_purges_boundary_crossing_trades():
    result = options_research.replay(frames(), {("TEST", "2026-11-13"): 150},
                                    "2026-10-15", initial_capital=100000)
    assert all(r["trade_count"] == 0 for r in result["results"].values())


def test_future_dated_forecast_rejected():
    data = frames()
    data[0]["forecast_asof"] = "2026-10-09T20:00:00+00:00"
    with pytest.raises(ValueError, match="provenance"):
        options_research.replay(data, {}, "2026-10-01")


def test_missing_session_not_counted_as_next_day():
    data = frames()
    data[1]["observed_at"] = "2026-10-13T15:00:00+00:00"
    result = options_research.replay(data, {("TEST", "2026-11-13"): 150}, "2026-10-01")
    assert result["results"]["holdout"]["trade_count"] == 0


def test_oversized_trade_does_not_use_fractional_contracts():
    result = options_research.replay(frames(), {("TEST", "2026-11-13"): 150}, "2026-10-01")
    assert result["results"]["holdout"]["trade_count"] == 0


def test_capture_preserves_chain_in_isolated_database(tmp_path):
    from types import SimpleNamespace
    result = SimpleNamespace(spot=100, put_wall=90, call_wall=110, alert=None,
                             zero_dte_summary={"total_net_gex": 100},
                             data_quality={"usable": True}, oi_data_quality={"usable": True})
    path = tmp_path / "research.db"
    options_research.capture_chain("TEST", result, "2026-11-13", chain(), path)
    import sqlite3
    import json
    with sqlite3.connect(path) as conn:
        stored = json.loads(conn.execute("SELECT frame_json FROM research_option_chains").fetchone()[0])
    assert stored["legs"] == [asdict(leg) for leg in chain()]
    assert stored["quote_source"] == "yfinance_received_indicative"


def test_walk_forward_keeps_cash_when_training_is_insufficient():
    result = options_research.walk_forward(frames(), {("TEST", "2026-11-13"): 150},
                                           ["2026-10-01", "2026-12-01"], initial_capital=100000)
    assert result["folds"][0]["selected_using_training_only"] == "cash"
    assert result["folds"][0]["selected_holdout"]["net_pnl"] == 0


def test_settled_drawdown_groups_same_date_positions():
    trades = [{"expiry": "2026-11-13", "net_pnl": -500},
              {"expiry": "2026-11-13", "net_pnl": 500}]
    assert options_research._settle(trades, 10000, 10000, 0) == (10000, 10000, 0)


def test_total_risk_budget_blocks_overlapping_trade():
    data = frames()
    data.extend([dict(frame, symbol="OTHER") for frame in frames()])
    result = options_research.replay(data, {("TEST", "2026-11-13"): 150, ("OTHER", "2026-11-13"): 150},
                                    "2026-10-01", initial_capital=100000,
                                    total_risk_fraction=.01, symbol_risk_fraction=.01)
    assert result["results"]["holdout"]["trade_count"] == 1


def test_multiple_expiries_share_previous_session_signal():
    data = frames()
    additional = dict(data[1], expiry="2026-11-20")
    additional["legs"] = [dict(leg, expiry="2026-11-20") for leg in data[1]["legs"]]
    data.append(additional)
    result = options_research.replay(data, {("TEST", "2026-11-13"): 150, ("TEST", "2026-11-20"): 150},
                                    "2026-10-01", initial_capital=100000)
    assert result["results"]["holdout"]["trade_count"] == 2


def marked_trades_and_quotes():
    import pandas_market_calendars as mcal
    entry, expiry = "2026-10-09", "2026-11-13"
    trade = {"symbol": "TEST", "entry": entry, "expiry": expiry,
             "entry_at": "2026-10-09T15:00:00+00:00", "quantity": 2,
             "strategy_type": "debit", "net_premium": 4.2, "costs": 9.2,
             "net_pnl": 8150.8,
             "legs": [{"action": "BUY", "option_type": "CALL", "strike_price": 105},
                      {"action": "BUY", "option_type": "PUT", "strike_price": 95}]}
    schedule = mcal.get_calendar("NYSE").schedule(start_date=entry, end_date=expiry)
    quotes = []
    for day, session in schedule.iterrows():
        if day.date().isoformat() == expiry:
            continue
        observed = (session.market_close - __import__("pandas").Timedelta(minutes=15)).isoformat()
        for leg in trade["legs"]:
            quotes.append({"symbol": "TEST", "date": day.date().isoformat(), "observed_at": observed,
                           "expiry": expiry, "strike": leg["strike_price"],
                           "option_type": leg["option_type"], "bid": 2, "ask": 2.1})
    return [trade], quotes


def test_daily_marked_nav_includes_entry_spread_and_expiry_pnl():
    trades, quotes = marked_trades_and_quotes()
    report = options_research.marked_portfolio(trades, quotes, 100000)
    assert report["status"] == "complete"
    assert report["daily_nav"][0]["nav"] == pytest.approx(99950.8)
    assert report["daily_nav"][-1]["nav"] == pytest.approx(108150.8)
    assert report["max_drawdown_pct"] == pytest.approx(.0492)
    assert report["annualized_return_pct"] is None
    assert report["sharpe"] is None


def test_missing_daily_contract_invalidates_nav_statistics():
    trades, quotes = marked_trades_and_quotes()
    quotes.pop(3)
    report = options_research.marked_portfolio(trades, quotes, 100000)
    assert report["status"] == "incomplete_marks"
    assert report["max_drawdown_pct"] is None
    assert report["daily_nav"][1]["nav"] is None
    assert report["missing_sessions"][0]["contracts"][0]["reason"] == "missing_contract_quote"


def test_duplicate_or_stale_quotes_rejected():
    trades, quotes = marked_trades_and_quotes()
    with pytest.raises(ValueError, match="Duplicate"):
        options_research.marked_portfolio(trades, quotes + [dict(quotes[0])], 100000)
    quotes[0]["observed_at"] = "2026-10-09T14:00:00+00:00"
    report = options_research.marked_portfolio(trades, quotes, 100000)
    assert report["status"] == "incomplete_marks"
    assert report["missing_sessions"][0]["contracts"][0]["reason"] == "quote_time_mismatch"


def test_replay_passes_position_legs_to_marked_portfolio():
    data = frames()
    trade_result = options_research.replay(data, {("TEST", "2026-11-13"): 150},
                                           "2026-10-01", initial_capital=100000, marks=[])
    holdout = trade_result["results"]["holdout"]
    assert holdout["trade_count"] == 1
    assert holdout["mark_to_market"]["status"] == "incomplete_marks"
    assert holdout["mark_to_market"]["max_drawdown_pct"] is None


def test_settlement_csv_rejects_adjusted_prices(tmp_path):
    path = tmp_path / "settlements.csv"
    path.write_text("symbol,date,close,price_basis,contract_multiplier,source,corporate_action_checked\n"
                    "TEST,2026-11-13,150,split_adjusted,100,provider,yes\n")
    with pytest.raises(ValueError, match="as-traded"):
        options_research.read_settlements(path)


def test_settlement_csv_requires_contract_review_and_unique_dates(tmp_path):
    path = tmp_path / "settlements.csv"
    header = "symbol,date,close,price_basis,contract_multiplier,source,corporate_action_checked\n"
    row = "TEST,2026-11-13,150,as_traded_equity_close,100,exchange,yes\n"
    path.write_text(header + row)
    assert options_research.read_settlements(path) == {("TEST", "2026-11-13"): 150}
    path.write_text(header + row + row)
    with pytest.raises(ValueError, match="Duplicate"):
        options_research.read_settlements(path)
