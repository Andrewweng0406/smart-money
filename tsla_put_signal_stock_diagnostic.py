"""Long-history stock proxy diagnostic for weekly TSLA put entry signals."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path


CONFIGS = {
    "baseline_next_session": ("rebound", None),
    "max_rebound_0pct": ("rebound", 0.),
    "max_rebound_0_5pct": ("rebound", .005),
    "max_rebound_1pct": ("rebound", .01),
    "max_rebound_2pct": ("rebound", .02),
    "continued_down_2pct": ("daily_return", -.02),
    "continued_down_3pct": ("daily_return", -.03),
    "continued_down_4pct": ("daily_return", -.04),
}


def load_prices(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return [(row["date"], float(row["close"])) for row in csv.DictReader(stream)]


def summarize(events):
    if not events:
        return {"signals": 0}
    minimums = [event["minimum_return_pct"] for event in events]
    finals = [event["final_return_pct"] for event in events]
    return {
        "signals": len(events),
        "any_decline_rate_pct": round(100 * sum(value < 0 for value in minimums) / len(events), 2),
        "down_2pct_rate_pct": round(100 * sum(value <= -2 for value in minimums) / len(events), 2),
        "down_4pct_rate_pct": round(100 * sum(value <= -4 for value in minimums) / len(events), 2),
        "median_minimum_return_pct": round(statistics.median(minimums), 3),
        "median_final_return_pct": round(statistics.median(finals), 3),
        "mean_final_return_pct": round(statistics.mean(finals), 3),
    }


def events_for(prices, mode, threshold, hold_sessions=4):
    events = []
    next_available = 20
    for signal_index in range(20, len(prices) - hold_sessions - 1):
        if signal_index < next_available:
            continue
        signal_date, signal_spot = prices[signal_index]
        prior_low = min(spot for _, spot in prices[signal_index - 20:signal_index])
        if signal_spot >= prior_low:
            continue
        entry_index = signal_index + 1
        entry_date, entry_spot = prices[entry_index]
        daily_return = entry_spot / signal_spot - 1
        accepted = (threshold is None or
                    mode == "rebound" and daily_return <= threshold or
                    mode == "daily_return" and daily_return <= threshold)
        if not accepted:
            continue
        forward = [spot for _, spot in prices[entry_index + 1:entry_index + hold_sessions + 1]]
        events.append({
            "signal_date": signal_date, "entry_date": entry_date,
            "entry_spot": round(entry_spot, 4),
            "entry_day_return_pct": round(100 * daily_return, 3),
            "minimum_return_pct": round(100 * (min(forward) / entry_spot - 1), 3),
            "final_return_pct": round(100 * (forward[-1] / entry_spot - 1), 3),
        })
        next_available = entry_index + hold_sessions + 1
    return events


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prices", type=Path, default=Path("research/market_data/TSLA.csv"))
    parser.add_argument("--output", type=Path,
                        default=Path("research/tsla_put_signal_stock_diagnostic.json"))
    args = parser.parse_args()
    prices = load_prices(args.prices)
    results = {}
    all_events = {}
    for name, (mode, threshold) in CONFIGS.items():
        events = events_for(prices, mode, threshold)
        all_events[name] = events
        results[name] = {
            "all": summarize(events),
            "2015_2020": summarize([event for event in events if event["entry_date"][:4] <= "2020"]),
            "2021_2026": summarize([event for event in events if event["entry_date"][:4] >= "2021"]),
            "by_year": {str(year): summarize([event for event in events
                                               if event["entry_date"].startswith(str(year))])
                        for year in range(2015, 2027)},
        }
    output = {
        "study": "stock_proxy_for_weekly_put_direction_only",
        "range": {"start": prices[0][0], "end": prices[-1][0], "sessions": len(prices)},
        "signal": "close below prior 20-session low; execute next session; non-overlapping four-session holds",
        "limitations": [
            "Stock direction only; no option premium, implied volatility, spread, or funding constraints",
            "Thresholds were researched after viewing option-year results; split is descriptive, not untouched",
            "2026 is incomplete",
        ],
        "results": results,
        "events": all_events,
    }
    args.output.write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")
    print(json.dumps({name: value for name, value in results.items()}, indent=2))


if __name__ == "__main__":
    main()
