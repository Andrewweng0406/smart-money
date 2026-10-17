from tsla_put_signal_stock_diagnostic import events_for, summarize


def prices(entry=89):
    rows = [(f"2023-01-{index + 1:02d}", 100.) for index in range(20)]
    rows.extend([
        ("2023-01-21", 90.), ("2023-01-22", entry), ("2023-01-23", 87.),
        ("2023-01-24", 86.), ("2023-01-25", 88.), ("2023-01-26", 85.),
    ])
    return rows


def test_breakout_signal_uses_next_session_and_four_forward_sessions():
    events = events_for(prices(), "rebound", .01)
    assert len(events) == 1
    assert events[0]["signal_date"] == "2023-01-21"
    assert events[0]["entry_date"] == "2023-01-22"
    assert events[0]["minimum_return_pct"] == -4.494
    assert events[0]["final_return_pct"] == -4.494


def test_rebound_filter_rejects_entry_above_threshold():
    assert events_for(prices(92), "rebound", .01) == []
    assert len(events_for(prices(92), "rebound", .03)) == 1


def test_summary_reports_direction_threshold_rates():
    summary = summarize(events_for(prices(), "rebound", .01))
    assert summary["signals"] == 1
    assert summary["down_2pct_rate_pct"] == 100
    assert summary["down_4pct_rate_pct"] == 100
