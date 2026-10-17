from tsla_put_regime_gate import RULES, passes, percentile_rank


def item(**updates):
    row = {"rebound_pct": .5, "daily_return_pct": -1., "vix": 20.,
           "vix_percentile_252": 50.}
    row.update(updates)
    return row


def test_full_gate_accepts_confirmed_non_capitulation_entry():
    assert passes(item(), RULES["full_gate"])


def test_full_gate_rejects_rebound_capitulation_and_expensive_vix():
    assert not passes(item(rebound_pct=1.1), RULES["full_gate"])
    assert not passes(item(daily_return_pct=-3.1), RULES["full_gate"])
    assert not passes(item(vix=31.), RULES["full_gate"])
    assert not passes(item(vix_percentile_252=85.), RULES["full_gate"])


def test_percentile_rank_is_observation_based():
    assert percentile_rank([10., 20., 30., 40.], 30.) == 75.
