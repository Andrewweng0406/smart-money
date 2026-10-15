from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal
import pytest

import volatility_research as vr


def prices(count=1400):
    schedule = mcal.get_calendar("NYSE").schedule(start_date="2015-01-01", end_date="2021-12-31").iloc[:count]
    index = [d.date().isoformat() for d in schedule.index]
    generator = np.random.default_rng(7)
    return pd.DataFrame({"adj_close": 100*np.exp(np.cumsum(generator.normal(0, .02, len(index)))),
                         "available_at": [t.isoformat() for t in schedule.market_close]}, index=index)


def test_origin_forecast_never_uses_future_returns():
    data = prices(200)
    before = vr.build_origins(data)
    altered = data.copy()
    altered.loc[altered.index[100]:, "adj_close"] *= 2
    after = vr.build_origins(altered)
    for model in vr.MODELS:
        np.testing.assert_allclose(before.loc[before.origin < data.index[100], model],
                                   after.loc[after.origin < data.index[100], model])
    assert before.iloc[0].target_end == data.index[81]


def test_missing_session_is_rejected_instead_of_bridged():
    with pytest.raises(ValueError, match="Missing sessions"):
        vr.build_origins(prices(200).drop(prices(200).index[80]))


def test_nonoverlapping_targets_do_not_reuse_returns():
    chosen = vr.nonoverlapping(vr.build_origins(prices(200)))
    for i in range(1, len(chosen)):
        assert chosen.iloc[i].origin >= chosen.iloc[i-1].target_end


def test_holdout_prices_cannot_change_training_selection():
    data = prices()
    boundaries = ["2018-01-01", "2020-01-01"]
    before = vr.evaluate_forecasts(data, boundaries)
    changed = data.copy()
    future = changed.index >= "2018-01-01"
    changed.loc[future, "adj_close"] *= np.exp(np.linspace(0, 3, sum(future)))
    after = vr.evaluate_forecasts(changed, boundaries)
    assert before["folds"][0]["selected_on_training"] == after["folds"][0]["selected_on_training"]
    assert before["folds"][0]["training_scales"] == after["folds"][0]["training_scales"]
    assert before["folds"][0]["training_qlike"] == after["folds"][0]["training_qlike"]


def test_forecast_requires_completed_session():
    data = prices(200)
    observed = pd.Timestamp(data.iloc[100].available_at).to_pydatetime()
    before = vr.forecast_at(data, observed)
    changed = data.copy()
    changed.loc[changed.index[101]:, "adj_close"] *= 100
    assert vr.forecast_at(changed, observed) == before
    assert before["forecast_asof"] == data.iloc[100].available_at
    intraday = observed.replace(hour=15)
    assert vr.forecast_at(data, intraday)["forecast_asof"] == data.iloc[99].available_at


def test_forecast_rejects_invalid_prices():
    data = prices(200)
    data.iloc[80, data.columns.get_loc("adj_close")] = float("nan")
    with pytest.raises(ValueError, match="Invalid"):
        vr.forecast_at(data, datetime(2022, 1, 1, tzinfo=timezone.utc))


def test_bootstrap_is_reproducible_and_small_samples_abstain():
    assert vr.paired_interval([1]*5) is None
    assert vr.paired_interval(np.arange(20)) == vr.paired_interval(np.arange(20))


def test_qlike_perfect_prediction_zero():
    np.testing.assert_allclose(vr.qlike([.1, .2], [.1, .2]), 0)


def test_naive_asof_rejected():
    with pytest.raises(ValueError, match="timezone"):
        vr.prepare_prices(pd.DataFrame(), "TSLA", datetime(2026, 1, 1))


def test_prepare_prices_drops_unfinished_session():
    index = pd.to_datetime(["2026-10-08", "2026-10-09"])
    history = pd.DataFrame({"Close": [100, 101], "Adj Close": [99, 100]}, index=index)
    prepared, audit = vr.prepare_prices(history, "TEST", datetime(2026, 10, 9, 15, tzinfo=timezone.utc))
    assert list(prepared.index) == ["2026-10-08"]
    assert audit["contract_settlement_ready"] is False


def test_short_listing_history_is_not_live_approved():
    result = vr.evaluate_forecasts(prices(84), ["2015-01-01", "2016-01-01"])
    assert result["live_approved"] is False
    assert result["status"] == "insufficient_samples"


def test_cached_prices_require_matching_checksum(tmp_path):
    import json
    path = tmp_path / "TEST.csv"
    prices(200).rename_axis("date").to_csv(path)
    path.with_suffix(".manifest.json").write_text(json.dumps({"sha256": "wrong"}))
    with pytest.raises(ValueError, match="checksum"):
        vr.load_prices(tmp_path, "TEST")
