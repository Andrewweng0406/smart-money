"""Predeclared $100k TSLA slot-count and cash-reserve sensitivity replay."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from tsla_optionsdx_continuous_replay import FUNDED_VARIANT, replay_year


CONFIGS = {
    "six_slots": (1, 2, 3, 6, 7, 8),
    "three_slots": (1, 3, 7),
    "two_slots": (1, 7),
}
WINDOWS = {
    2020: (date(2020, 1, 2), date(2020, 6, 17)),
    2021: (date(2021, 1, 4), date(2021, 6, 30)),
    2022: (date(2022, 1, 3), date(2022, 5, 10)),
}
RESERVE_PCT = .15


def summarize(row):
    funded = row["variants"][FUNDED_VARIANT]
    return {
        "year": row["year"], "entry_policy": row["entry_policy"],
        "status": row["status"], "window": row["window"], "sessions": row["sessions"],
        "entry_months": row["entry_months"], "slot_capital": row["slot_capital"],
        "reserve_per_slot": row["reserve_per_slot"],
        "long_call_entries": row["counts"].get("entries", 0),
        "call_over_budget": row["counts"].get("call_over_budget", 0),
        "call_reserve_blocked": row["counts"].get("call_reserve_blocked", 0),
        "short_call_entries": row["counts"].get(f"{FUNDED_VARIANT}_short_entries", 0),
        "short_reserve_blocked": row["counts"].get(f"{FUNDED_VARIANT}_short_reserve_blocked", 0),
        "funded_put_entries": row["counts"].get(f"{FUNDED_VARIANT}_put_entries", 0),
        "put_reserve_blocked": row["counts"].get(f"{FUNDED_VARIANT}_put_reserve_blocked", 0),
        "risk_signals_without_funded_put": row["counts"].get("risk_signals_without_funded_put", 0),
        "roll_unavailable": row["counts"].get(f"{FUNDED_VARIANT}_roll_unavailable", 0),
        "account_return_pct": funded["account_return_pct"],
        "max_daily_nav_drawdown_pct": funded["max_daily_nav_drawdown_pct"],
        "worst_slot_cash": row["worst_slot_cash"][FUNDED_VARIANT],
        "negative_cash_slot_sessions": row["negative_cash_slot_sessions"][FUNDED_VARIANT],
        "reserve_breach_slot_sessions": row["reserve_breach_slot_sessions"][FUNDED_VARIANT],
        "funding": row["funding"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("research/raw_options/optionsdx"))
    parser.add_argument("--audit", type=Path, default=Path("research/optionsdx_tsla_multiyear_audit.json"))
    parser.add_argument("--output", type=Path, default=Path("research/tsla_optionsdx_capital_sensitivity.json"))
    parser.add_argument("--years", type=int, nargs="+", default=[2019, 2020, 2021, 2022, 2023])
    args = parser.parse_args()
    audit = json.loads(args.audit.read_text())
    output = {
        "study": "predeclared_slot_count_cash_reserve_sensitivity",
        "capital": 100000, "reserve_pct": RESERVE_PCT,
        "entry_months": CONFIGS,
        "limitations": [
            "Changing slot count also changes entry months; results are not a pure capital effect",
            "2020-2022 windows are partial and not annual returns",
            "Option split deliverables are not modeled; windows do not cross split dates",
            "A 15% cash reserve is a heuristic, not a guarantee against future short-call buyback or assignment",
            "Negative cash or reserve-breach sessions invalidate simple cash-feasibility claims",
            "EOD quotes do not prove executable fills; OI and margin/assignment are unavailable",
            "Parameters were selected after prior research; no untouched holdout",
        ],
        "results": {},
    }
    for year in args.years:
        for config, months in CONFIGS.items():
            for entry in ("month_first", "dip_10"):
                row = replay_year(
                    args.directory, audit, year, entry_policy=entry, put_policy="weekly",
                    funding_policy="realized", risk_signal_policy="break20_low",
                    funding_scope="slot", window=WINDOWS.get(year),
                    entry_months=months, reserve_pct=RESERVE_PCT,
                )
                key = f"{year}_{config}_{entry}"
                output["results"][key] = summarize(row)
                summary = output["results"][key]
                print(f"{key}: entries={summary['long_call_entries']} "
                      f"shorts={summary['short_call_entries']} puts={summary['funded_put_entries']} "
                      f"return={summary['account_return_pct']}% "
                      f"cash_deficit_days={summary['negative_cash_slot_sessions']}", flush=True)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
