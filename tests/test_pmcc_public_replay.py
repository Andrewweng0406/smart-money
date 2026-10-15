from datetime import date

from pmcc_public_replay import choose_long, choose_put, choose_short, metrics, walls


def row(option_type, expiry, strike, bid=2, ask=2.1, oi=100):
    return {"type": option_type, "expiration": expiry, "strike": strike,
            "bid": bid, "ask": ask, "open_interest": oi}


def test_long_short_and_put_use_distinct_expiries_and_strikes():
    day = date(2024, 1, 2)
    long = choose_long([row("call", "2025-06-20", 80, bid=25, ask=25.5),
                        row("call", "2024-02-16", 80, bid=4, ask=4.1)], 100, day)
    assert long["expiration"] == "2025-06-20"
    short = choose_short([row("call", "2024-02-02", 105),
                          row("call", "2025-06-20", 105)], day, 105,
                         ("SPY", date(2025, 6, 20), 80))
    assert short["expiration"] == "2024-02-02"
    put = choose_put([row("put", "2024-03-15", 95),
                      row("put", "2024-02-02", 95)], day, 100)
    assert put["expiration"] == "2024-03-15"


def test_walls_are_nearby_oi_levels_not_future_expiry():
    call, put = walls([row("call", "2024-02-02", 105, oi=200),
                       row("call", "2024-02-02", 104, oi=100),
                       row("put", "2024-02-02", 95, oi=300),
                       row("put", "2024-02-02", 90, oi=1000),
                       row("call", "2025-01-17", 102, oi=1000)], 100,
                      date(2024, 1, 2))
    assert (call, put) == (105, 95)


def test_account_drawdown_uses_daily_nav():
    result = metrics([102000, 98000, 101000])
    assert result["net_pnl"] == 1000
    assert result["max_daily_nav_drawdown_pct"] == 3.922
