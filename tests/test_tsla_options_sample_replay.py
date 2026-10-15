from datetime import date

import pytest

from tsla_options_sample_replay import choose_long, choose_put, choose_short, walls


DAY = date(2022, 8, 31)


def option(side, expiry, strike, bid=10.0, ask=10.5, oi=1):
    return {"type": side, "expiration": date.fromisoformat(expiry), "strike": strike,
            "bid": bid, "ask": ask, "open_interest": oi}


def test_long_call_expiry_policies_are_explicit():
    rows = [option("call", "2023-01-20", 220), option("call", "2024-01-19", 220),
            option("call", "2024-03-15", 220)]
    assert choose_long(rows, 275, DAY, "jan2023")["expiration"] == date(2023, 1, 20)
    assert choose_long(rows, 275, DAY, "jan2024")["expiration"] == date(2024, 1, 19)
    assert choose_long(rows, 275, DAY)["expiration"] == date(2024, 3, 15)
    with pytest.raises(ValueError, match="Unknown long-call policy"):
        choose_long(rows, 275, DAY, "unknown")


def test_resistance_wall_uses_nearby_open_interest():
    rows = [option("call", "2022-09-30", 280, oi=25),
            option("call", "2022-09-30", 290, oi=5),
            option("put", "2022-09-30", 270, oi=20),
            option("put", "2022-09-30", 260, oi=50)]
    assert walls(rows, 275, DAY) == (280, 270)
    long_contract = ("TSLA", date(2024, 1, 19), 220)
    assert choose_short(rows, DAY, 280, long_contract)["strike"] == 280


def test_put_selection_rejects_wide_spread():
    rows = [option("put", "2022-11-18", 260, bid=1, ask=3),
            option("put", "2022-11-18", 265, bid=10, ask=11)]
    assert choose_put(rows, DAY, 275)["strike"] == 265
