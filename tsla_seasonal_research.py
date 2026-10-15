"""Stock-price-only test of a prespecified TSLA seasonal dip schedule.

This does not price LEAPS, protective puts, or PMCC short calls.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import defaultdict
from datetime import date
from pathlib import Path


CAPITAL = 100_000
MONTHS = (1, 2, 3, 6, 7, 8)
ROUND_TRIP_SIDE_COST_BPS = 5


def load_prices(path: Path) -> list[tuple[date, float]]:
    prices = []
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            if row["symbol"] != "TSLA":
                raise ValueError("Expected TSLA price rows")
            day, close = date.fromisoformat(row["date"]), float(row["close"])
            if not math.isfinite(close) or close <= 0 or (prices and day <= prices[-1][0]):
                raise ValueError("Invalid or unordered TSLA close")
            prices.append((day, close))
    if not prices:
        raise ValueError("No TSLA prices")
    return prices


def dip_entry(closes: list[float], month_indices: list[int], *, lookback: int = 20,
              drawdown: float = .10) -> tuple[int, bool]:
    """Previous close triggers a purchase at the next session's close."""
    if not month_indices or lookback < 2 or not 0 < drawdown < 1:
        raise ValueError("Invalid month or dip rule")
    for index in month_indices:
        if index < lookback + 1:
            continue
        previous_close = closes[index - 1]
        prior_high = max(closes[index - lookback - 1:index - 1])
        if previous_close <= (1 - drawdown) * prior_high:
            return index, True
    return month_indices[-1], False


def window_return(closes, start_index, end_index):
    return closes[end_index] / closes[start_index] - 1


def strategy_path(prices, trades, *, cost_bps=ROUND_TRIP_SIDE_COST_BPS,
                  allocation_slots: int | None = None):
    side_cost = cost_bps / 10_000
    closes = [price for _, price in prices]
    slots = len(trades) if allocation_slots is None else allocation_slots
    if slots < len(trades) or (trades and slots == 0):
        raise ValueError("Insufficient allocation slots")
    allocation = CAPITAL / slots if slots else 0
    nav = []
    for index, _ in enumerate(prices):
        value = CAPITAL
        for entry, exit_index in trades:
            if index < entry:
                continue
            mark_index = min(index, exit_index)
            sale_value = allocation * closes[mark_index] * (1 - side_cost) / (closes[entry] * (1 + side_cost))
            value += sale_value - allocation
        nav.append(value)
    peak, max_drawdown = CAPITAL, 0.0
    for value in nav:
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, 1 - value / peak)
    return {"return_pct": round(100 * (nav[-1] / CAPITAL - 1), 3),
            "max_daily_nav_drawdown_pct": round(100 * max_drawdown, 3),
            "ending_nav": round(nav[-1], 2)}


def summarize(values):
    if not values:
        return {"observations": 0, "positive_observations": 0, "mean_pct": None,
                "median_pct": None, "min_pct": None, "max_pct": None}
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    median = (ordered[midpoint] if len(ordered) % 2 else
              (ordered[midpoint - 1] + ordered[midpoint]) / 2)
    return {"observations": len(values), "positive_observations": sum(value > 0 for value in values),
            "mean_pct": round(100 * sum(values) / len(values), 3),
            "median_pct": round(100 * median, 3),
            "min_pct": round(100 * ordered[0], 3),
            "max_pct": round(100 * ordered[-1], 3)}


def replay(prices):
    closes = [close for _, close in prices]
    by_year_month = defaultdict(list)
    for index, (day, _) in enumerate(prices):
        by_year_month[(day.year, day.month)].append(index)
    last_date = prices[-1][0]
    complete_years = [year for year in sorted({day.year for day, _ in prices})
                      if all(by_year_month[(year, month)] for month in range(1, 13))
                      and (year < last_date.year or (last_date.month == 12 and last_date.day >= 28))]
    annual = []
    trigger_forward_returns = []
    for year in complete_years:
        spring_exit = by_year_month[(year, 6)][-1]
        winter_exit = by_year_month[(year, 12)][-1]
        dip_trades, fixed_trades, entries = [], [], []
        for month in MONTHS:
            indices = by_year_month[(year, month)]
            entry, triggered = dip_entry(closes, indices)
            exit_index = spring_exit if month <= 3 else winter_exit
            dip_trades.append((entry, exit_index))
            fixed_trades.append((indices[0], exit_index))
            entries.append({"month": month, "dip_entry": prices[entry][0].isoformat(),
                            "dip_triggered": triggered, "fixed_entry": prices[indices[0]][0].isoformat(),
                            "exit": prices[exit_index][0].isoformat(),
                            "forward_20_session_stock_pct": (round(100 * window_return(closes, entry, entry + 20), 3)
                                                             if triggered and entry + 20 <= winter_exit else None)})
            if triggered and entry + 20 <= winter_exit:
                trigger_forward_returns.append(window_return(closes, entry, entry + 20))
        year_prices = prices[by_year_month[(year, 1)][0]:winter_exit + 1]
        offset = by_year_month[(year, 1)][0]
        dip_result = strategy_path(year_prices, [(entry - offset, exit_index - offset)
                                                  for entry, exit_index in dip_trades])
        fixed_result = strategy_path(year_prices, [(entry - offset, exit_index - offset)
                                                    for entry, exit_index in fixed_trades])
        april_june = window_return(closes, by_year_month[(year, 4)][0], spring_exit)
        august_december = window_return(closes, by_year_month[(year, 8)][-1], winter_exit)
        annual.append({"year": year, "windows": {"april_to_june_pct": round(100 * april_june, 3),
                                                  "late_august_to_december_pct": round(100 * august_december, 3)},
                       "dip_schedule": dip_result, "first_session_schedule": fixed_result,
                       "dip_minus_fixed_pct_points": round(dip_result["return_pct"] - fixed_result["return_pct"], 3),
                       "dip_triggers": sum(entry["dip_triggered"] for entry in entries),
                       "entries": entries})
    return {"complete_years": complete_years, "annual": annual,
            "summary": {"april_to_june": summarize([row["windows"]["april_to_june_pct"] / 100 for row in annual]),
                        "late_august_to_december": summarize([row["windows"]["late_august_to_december_pct"] / 100 for row in annual]),
                        "dip_schedule": summarize([row["dip_schedule"]["return_pct"] / 100 for row in annual]),
                        "first_session_schedule": summarize([row["first_session_schedule"]["return_pct"] / 100 for row in annual]),
                        "dip_minus_fixed": summarize([row["dip_minus_fixed_pct_points"] / 100 for row in annual]),
                        "trigger_forward_20_sessions": summarize(trigger_forward_returns)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prices", type=Path, default=Path("research/market_data/TSLA.csv"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.prices.open("rb") as handle:
        source_hash = hashlib.file_digest(handle, "sha256").hexdigest()
    output = {"study": "TSLA_stock_price_seasonality_not_option_backtest",
              "source_file": str(args.prices), "source_sha256": source_hash,
              "rules": {"months": MONTHS, "prior_sessions_for_high": 20,
                        "dip_threshold_pct": 10, "execution": "next session close after prior-close signal",
                        "fallback": "last session of month", "cost_bps_per_side": ROUND_TRIP_SIDE_COST_BPS,
                        "capital": CAPITAL, "tranche_capital": CAPITAL / 6,
                        "spring_exit": "last June session", "summer_exit": "last December session"},
              **replay(load_prices(args.prices)),
              "limitations": ["Split-adjusted TSLA stock closes, not historical option quotes",
                              "No LEAPS premium, implied volatility, put payoff, short call or assignment modeled",
                              "The seasonal rule was specified after viewing the broad TSLA history; no untouched holdout",
                              "Five basis points per side is a stock-proxy cost assumption, not option execution cost"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"complete_years": output["complete_years"], "summary": output["summary"]}, indent=2))


if __name__ == "__main__":
    main()
