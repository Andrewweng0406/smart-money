from public_options_research import as_cboe_row, standard_contract


def mirror_row(**changes):
    row = {"contract_id": "SPY230825C00275000", "symbol": "SPY", "expiration": "2023-08-25",
           "strike": 275.0, "type": "call", "date": "2023-08-25",
           "bid": 1.0, "ask": 1.1, "bid_size": 10, "ask_size": 10,
           "open_interest": 100, "implied_volatility": .2}
    row.update(changes)
    return row


def test_public_mirror_accepts_only_standard_contract_identity():
    assert standard_contract(mirror_row())
    assert not standard_contract(mirror_row(contract_id="SPY1230825C00275000"))
    assert not standard_contract(mirror_row(symbol="QQQ"))
    assert not standard_contract(mirror_row(strike=276.0))
    assert not standard_contract(mirror_row(type="other"))


def test_public_mirror_keeps_quote_and_open_interest_fields():
    result = as_cboe_row(mirror_row(), 440.79)
    assert result["bid_1545"] == 1.0
    assert result["ask_1545"] == 1.1
    assert result["open_interest"] == 100
    assert result["active_underlying_price_1545"] == 440.79
