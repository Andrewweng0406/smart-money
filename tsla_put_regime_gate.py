"""Build and evaluate a three-state TSLA long-put regime gate."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from datetime import datetime
from pathlib import Path


RULES = {
    "price_confirmation": {"max_rebound": .01},
    "no_capitulation": {"max_rebound": .01, "min_daily_return": -.03},
    "vix_below_30": {"max_rebound": .01, "max_vix": 30.},
    "vix_below_80th_percentile": {"max_rebound": .01, "max_vix_percentile": 80.},
    "full_gate": {"max_rebound": .01, "min_daily_return": -.03,
                  "max_vix": 30., "max_vix_percentile": 80.},
}


def load_tsla(path, snapshot=None):
    with path.open(encoding="utf-8", newline="") as stream:
        rows = [(row["date"], float(row["close"])) for row in csv.DictReader(stream)]
    if snapshot and snapshot.is_file():
        additions = json.loads(snapshot.read_text())["rows"]
        known = {day for day, _ in rows}
        rows.extend((row["date"], float(row["close"])) for row in additions
                    if row["date"] not in known)
    return sorted(rows)


def load_vix(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return {datetime.strptime(row["DATE"], "%m/%d/%Y").date().isoformat(): float(row["CLOSE"])
                for row in csv.DictReader(stream)}


def percentile_rank(history, value):
    return 100 * sum(item <= value for item in history) / len(history)


def features(prices, vix, signal_index):
    entry_index = signal_index + 1
    signal_day, signal_spot = prices[signal_index]
    entry_day, entry_spot = prices[entry_index]
    returns = [math.log(prices[index][1] / prices[index - 1][1])
               for index in range(entry_index - 19, entry_index + 1)]
    vix_days = [vix[day] for day, _ in prices[max(0, entry_index - 251):entry_index + 1]
                if day in vix]
    current_vix = vix.get(entry_day)
    return {
        "signal_date": signal_day, "entry_date": entry_day,
        "signal_spot": signal_spot, "entry_spot": entry_spot,
        "rebound_pct": 100 * (entry_spot / signal_spot - 1),
        "daily_return_pct": 100 * (entry_spot / prices[entry_index - 1][1] - 1),
        "rv20_pct": 100 * statistics.stdev(returns) * math.sqrt(252),
        "vix": current_vix,
        "vix_percentile_252": percentile_rank(vix_days, current_vix)
        if current_vix is not None and len(vix_days) >= 20 else None,
    }


def passes(item, rule):
    return (item["rebound_pct"] <= 100 * rule["max_rebound"] and
            ("min_daily_return" not in rule or
             item["daily_return_pct"] >= 100 * rule["min_daily_return"]) and
            ("max_vix" not in rule or item["vix"] is not None and item["vix"] < rule["max_vix"]) and
            ("max_vix_percentile" not in rule or item["vix_percentile_252"] is not None and
             item["vix_percentile_252"] < rule["max_vix_percentile"]))


def evaluate(prices, vix, rule, hold_sessions=4):
    events = []
    next_available = 20
    for signal_index in range(20, len(prices) - hold_sessions - 1):
        if signal_index < next_available:
            continue
        signal_spot = prices[signal_index][1]
        if signal_spot >= min(spot for _, spot in prices[signal_index - 20:signal_index]):
            continue
        item = features(prices, vix, signal_index)
        if not passes(item, rule):
            continue
        entry_index = signal_index + 1
        forward = [spot for _, spot in prices[entry_index + 1:entry_index + hold_sessions + 1]]
        item["minimum_return_pct"] = 100 * (min(forward) / item["entry_spot"] - 1)
        item["final_return_pct"] = 100 * (forward[-1] / item["entry_spot"] - 1)
        events.append({key: round(value, 3) if isinstance(value, float) else value
                       for key, value in item.items()})
        next_available = entry_index + hold_sessions + 1
    return events


def summary(events):
    if not events:
        return {"signals": 0}
    minimums = [event["minimum_return_pct"] for event in events]
    finals = [event["final_return_pct"] for event in events]
    return {
        "signals": len(events),
        "down_2pct_rate_pct": round(100 * sum(value <= -2 for value in minimums) / len(events), 2),
        "down_4pct_rate_pct": round(100 * sum(value <= -4 for value in minimums) / len(events), 2),
        "median_minimum_return_pct": round(statistics.median(minimums), 3),
        "median_final_return_pct": round(statistics.median(finals), 3),
    }


def current_gate(prices, vix):
    latest_index = len(prices) - 1
    latest_day, latest_spot = prices[latest_index]
    latest_features = features(prices, vix, latest_index - 1)
    prior_signal = prices[latest_index - 1][1] < min(
        spot for _, spot in prices[latest_index - 21:latest_index - 1])
    if not prior_signal:
        state, reason = "WAIT", "前一交易日未跌破先前 20 日低點"
    elif not passes(latest_features, RULES["full_gate"]):
        state, reason = "BLOCK", "價格確認或 VIX 成本條件未通過"
    else:
        state, reason = "ALLOW", "跌破延續且市場波動成本條件通過"
    return {"as_of": latest_day, "spot": latest_spot, "state": state, "reason": reason,
            "features": {key: round(value, 3) if isinstance(value, float) else value
                         for key, value in latest_features.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prices", type=Path, default=Path("research/market_data/TSLA.csv"))
    parser.add_argument("--snapshot", type=Path, default=Path("tsla_strategy/price_snapshot.json"))
    parser.add_argument("--vix", type=Path, default=Path("research/market_data/VIX_History.csv"))
    parser.add_argument("--output", type=Path, default=Path("research/tsla_put_regime_gate.json"))
    args = parser.parse_args()
    prices, vix = load_tsla(args.prices, args.snapshot), load_vix(args.vix)
    results, all_events = {}, {}
    for name, rule in RULES.items():
        events = evaluate(prices, vix, rule)
        all_events[name] = events
        results[name] = {
            "all": summary(events),
            "2015_2020": summary([event for event in events if event["entry_date"][:4] <= "2020"]),
            "2021_2025": summary([event for event in events if "2021" <= event["entry_date"][:4] <= "2025"]),
            "2026": summary([event for event in events if event["entry_date"].startswith("2026")]),
        }
    output = {
        "study": "tsla_put_three_state_regime_gate_stock_proxy",
        "rules": RULES, "current": current_gate(prices, vix), "results": results,
        "events": all_events,
        "sources": {"tsla": "local adjusted TSLA daily close plus Nasdaq snapshot",
                    "vix": "Cboe VIX_History.csv"},
        "limitations": ["Stock-direction proxy, not option P&L", "VIX is SPX implied volatility, not TSLA IV",
                        "EOD features imply an EOD hypothetical decision", "No untouched holdout"],
    }
    args.output.write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"current": output["current"], "results": results}, indent=2))


if __name__ == "__main__":
    main()
