"""Small, prespecified stock-proxy rule comparison for TSLA seasonal tranches.

No candidate prices options or simulates a long put. These historical years
have already been inspected, so the forward folds are not pristine holdouts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

from tsla_seasonal_research import CAPITAL, MONTHS, dip_entry, load_prices, strategy_path


CANDIDATES = ("month_first", "month_last", "dip_5", "dip_10", "dip_15",
              "trend_200_first", "trend_200_dip_10")
FIRST_YEAR = 2016
FIRST_TEST_YEAR = 2021
COST_BPS = 5


def trend_ok(closes, index, *, length=200):
    if index < length:
        return False
    return closes[index - 1] > sum(closes[index - length:index]) / length


def entry_for_month(closes, indices, candidate):
    if candidate == "month_first":
        return indices[0]
    if candidate == "month_last":
        return indices[-1]
    if candidate.startswith("dip_"):
        threshold = int(candidate.split("_")[1]) / 100
        return dip_entry(closes, indices, drawdown=threshold)[0]
    if candidate == "trend_200_first":
        return indices[0] if trend_ok(closes, indices[0]) else None
    if candidate == "trend_200_dip_10":
        for index in indices:
            if trend_ok(closes, index) and dip_entry(closes, [index], drawdown=.10)[1]:
                return index
        return indices[-1] if trend_ok(closes, indices[-1]) else None
    raise ValueError(f"Unknown candidate: {candidate}")


def candidate_year(prices, by_year_month, year, candidate):
    closes = [close for _, close in prices]
    spring_exit = by_year_month[(year, 6)][-1]
    winter_exit = by_year_month[(year, 12)][-1]
    first_index = by_year_month[(year, 1)][0]
    trades, entries = [], []
    for month in MONTHS:
        entry = entry_for_month(closes, by_year_month[(year, month)], candidate)
        if entry is None:
            entries.append({"month": month, "entry": None})
            continue
        exit_index = spring_exit if month <= 3 else winter_exit
        trades.append((entry - first_index, exit_index - first_index))
        entries.append({"month": month, "entry": prices[entry][0].isoformat()})
    year_prices = prices[first_index:winter_exit + 1]
    performance = strategy_path(year_prices, trades, cost_bps=COST_BPS, allocation_slots=6)
    year_closes = [close for _, close in year_prices]
    fee = COST_BPS / 10_000
    trade_returns = [(year_closes[exit_index] * (1 - fee) / (year_closes[entry] * (1 + fee)) - 1)
                     for entry, exit_index in trades]
    return {"year": year, **performance, "trade_count": len(trades),
            "winning_trades": sum(value > 0 for value in trade_returns),
            "entries": entries}


def score(rows):
    returns = [row["return_pct"] for row in rows]
    total_trades = sum(row["trade_count"] for row in rows)
    winning_trades = sum(row["winning_trades"] for row in rows)
    return {"years": len(rows), "positive_years": sum(value > 0 for value in returns),
            "mean_account_return_pct": round(sum(returns) / len(returns), 3),
            "median_account_return_pct": round(median(returns), 3),
            "worst_account_return_pct": min(returns),
            "worst_daily_nav_drawdown_pct": max(row["max_daily_nav_drawdown_pct"] for row in rows),
            "trades": total_trades, "winning_trades": winning_trades,
            "trade_win_rate_pct": round(100 * winning_trades / total_trades, 2) if total_trades else None,
            "participation_pct": round(100 * total_trades / (6 * len(rows)), 2)}


def training_rank(summary):
    return (summary["positive_years"], summary["median_account_return_pct"],
            -summary["worst_daily_nav_drawdown_pct"], summary["mean_account_return_pct"])


def fold_summary(folds):
    chosen_returns = [fold["selected_return_pct"] for fold in folds]
    fixed_returns = [fold["month_first_return_pct"] for fold in folds]
    return {"folds": folds,
            "selected_positive_years": sum(value > 0 for value in chosen_returns),
            "fixed_positive_years": sum(value > 0 for value in fixed_returns),
            "selected_mean_return_pct": round(sum(chosen_returns) / len(chosen_returns), 3),
            "fixed_mean_return_pct": round(sum(fixed_returns) / len(fixed_returns), 3),
            "selected_worst_return_pct": min(chosen_returns),
            "selection_counts": dict(Counter(fold["selected"] for fold in folds))}


def paired_year_bootstrap(differences, *, samples=10_000, seed=42):
    rng = random.Random(seed)
    count = len(differences)
    if not count or samples < 100:
        raise ValueError("Bootstrap needs paired years and at least 100 samples")
    means = sorted(sum(differences[rng.randrange(count)] for _ in range(count)) / count
                   for _ in range(samples))
    return {"mean_difference_percentage_points": round(sum(differences) / count, 3),
            "descriptive_95pct_interval": [round(means[int(.025 * samples)], 3),
                                           round(means[int(.975 * samples) - 1], 3)]}


def replay(prices):
    by_year_month = defaultdict(list)
    for index, (day, _) in enumerate(prices):
        by_year_month[(day.year, day.month)].append(index)
    last_date = prices[-1][0]
    years = [year for year in range(FIRST_YEAR, last_date.year + 1)
             if all(by_year_month[(year, month)] for month in range(1, 13))
             and (year < last_date.year or (last_date.month == 12 and last_date.day >= 28))]
    rows = {candidate: {year: candidate_year(prices, by_year_month, year, candidate)
                        for year in years} for candidate in CANDIDATES}
    full = {candidate: score(list(annual.values())) for candidate, annual in rows.items()}
    no_2020 = {candidate: score([result for year, result in annual.items() if year != 2020])
               for candidate, annual in rows.items()}
    recent = {candidate: score([result for year, result in annual.items() if year >= FIRST_TEST_YEAR])
              for candidate, annual in rows.items()}
    best_retrospective = max(CANDIDATES, key=lambda candidate: training_rank(full[candidate]))
    paired_uncertainty = {candidate: paired_year_bootstrap([
        rows[candidate][year]["return_pct"] - rows["month_first"][year]["return_pct"]
        for year in years]) for candidate in CANDIDATES if candidate != "month_first"}
    folds = []
    trade_win_folds = []
    drawdown_folds = []
    for test_year in [year for year in years if year >= FIRST_TEST_YEAR]:
        training = {candidate: score([annual[year] for year in years if year < test_year])
                    for candidate, annual in rows.items()}
        eligible_win = [candidate for candidate in CANDIDATES
                        if training[candidate]["participation_pct"] >= 80]
        eligible_risk = [candidate for candidate in CANDIDATES
                         if training[candidate]["participation_pct"] >= 50
                         and training[candidate]["mean_account_return_pct"] > 0]
        selections = (
            (folds, max(CANDIDATES, key=lambda candidate: training_rank(training[candidate]))),
            (trade_win_folds, max(eligible_win, key=lambda candidate: (
                training[candidate]["trade_win_rate_pct"],
                training[candidate]["median_account_return_pct"]))),
            (drawdown_folds, min(eligible_risk, key=lambda candidate: (
                training[candidate]["worst_daily_nav_drawdown_pct"],
                -training[candidate]["median_account_return_pct"]))))
        for target, selected in selections:
            target.append({"test_year": test_year, "training_years": [year for year in years if year < test_year],
                           "selected": selected, "selected_return_pct": rows[selected][test_year]["return_pct"],
                           "selected_trades": rows[selected][test_year]["trade_count"],
                           "month_first_return_pct": rows["month_first"][test_year]["return_pct"]})
    return {"years": years, "candidates": list(CANDIDATES), "annual": rows,
            "full_period": full, "without_2020": no_2020, "recent_2021_2025": recent,
            "paired_year_uncertainty_vs_month_first": paired_uncertainty,
            "retrospective_max_annual_win_candidate": best_retrospective,
            "walk_forward": fold_summary(folds),
            "trade_win_walk_forward": fold_summary(trade_win_folds),
            "drawdown_walk_forward": fold_summary(drawdown_folds)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prices", type=Path, default=Path("research/market_data/TSLA.csv"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.prices.open("rb") as handle:
        source_hash = hashlib.file_digest(handle, "sha256").hexdigest()
    output = {"study": "TSLA_stock_proxy_candidate_comparison_not_option_backtest",
              "source_file": str(args.prices), "source_sha256": source_hash,
              "rules": {"capital": CAPITAL, "tranche_capital": CAPITAL / 6,
                        "entry_months": MONTHS, "cost_bps_per_side": COST_BPS,
                        "dip_lookback_sessions": 20, "trend_sma_sessions": 200,
                        "spring_exit": "last June close", "summer_exit": "last December close",
                        "selection": "annual wins; trade win rate with >=80% participation; or lowest drawdown with >=50% participation and positive training mean"},
              **replay(load_prices(args.prices)),
              "limitations": ["Stock closes only, not LEAPS calls, puts, or PMCC option P&L",
                              "Candidate years have been inspected; forward folds are chronological but not pristine holdouts",
                              "Fixed six-tranche budget and zero-yield cash; trend filter can abstain",
                              "Annual account resets; results are not compounded"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"full_period": output["full_period"],
                      "retrospective_max_annual_win_candidate": output["retrospective_max_annual_win_candidate"],
                      "walk_forward": output["walk_forward"],
                      "trade_win_walk_forward": output["trade_win_walk_forward"],
                      "drawdown_walk_forward": output["drawdown_walk_forward"]}, indent=2))


if __name__ == "__main__":
    main()
