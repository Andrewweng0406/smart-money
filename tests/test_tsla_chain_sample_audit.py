from tsla_chain_sample_audit import standard_contract, valid_quote


def test_valid_quote_requires_ordered_finite_bid_ask():
    assert valid_quote({"bid": "0", "ask": "0.05"})
    assert not valid_quote({"bid": "2", "ask": "1"})
    assert not valid_quote({"bid": "", "ask": "1"})
    assert not valid_quote({"bid": "nan", "ask": "1"})
    assert not valid_quote({"bid": "0", "ask": "0"})


def test_standard_contract_matches_root_expiry_side_and_strike():
    row = {"underlying": "TSLA", "style": "A", "expiration": "2024-01-19",
           "type": "call", "strike": "220", "contract": "TSLA240119C00220000"}
    assert standard_contract(row)
    assert not standard_contract({**row, "contract": "TSLA240119P00220000"})
    assert not standard_contract({**row, "style": "E"})
    assert not standard_contract({**row, "contract": "TSLA1240119C00220000"})
