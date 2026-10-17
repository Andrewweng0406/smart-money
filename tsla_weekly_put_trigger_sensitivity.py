"""Test whether rejecting post-signal rebounds improves weekly put defense."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tsla_optionsdx_continuous_replay import replay_year
from tsla_weekly_put_sensitivity import summarize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("research/raw_options/optionsdx"))
    parser.add_argument("--audit", type=Path, default=Path("research/optionsdx_tsla_multiyear_audit.json"))
    parser.add_argument("--output", type=Path,
                        default=Path("research/tsla_weekly_put_trigger_sensitivity.json"))
    args = parser.parse_args()
    audit = json.loads(args.audit.read_text())
    rows = []
    for entry_policy in ("month_first", "dip_10"):
        for target_ratio in (.90, .95):
            for max_rebound in (0., .01, .02):
                result = replay_year(
                    args.directory, audit, 2023, entry_policy=entry_policy,
                    put_policy="weekly", funding_policy="realized",
                    put_target_ratio=target_ratio, risk_signal_policy="break20_low",
                    funding_scope="slot", put_hold_sessions=4, put_take_profit_pct=1.,
                    put_entry_max_rebound_pct=max_rebound)
                rows.append({
                    "entry_policy": entry_policy, "put_target_ratio": target_ratio,
                    "put_entry_max_rebound_pct": max_rebound,
                    "rebound_rejected_slot_signals": result["counts"].get(
                        "pmcc_premium_put_put_rebound_rejected", 0),
                    **summarize(result),
                })
    output = {
        "study": "2023_weekly_put_entry_confirmation_sensitivity_exploratory",
        "fixed_rules": {"put_hold_sessions": 4, "put_take_profit_pct": 1.},
        "warning": "One complete year and very few independent signals; confirmation rules are exploratory.",
        "rows": rows,
    }
    args.output.write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")
    print(json.dumps(sorted(rows, key=lambda row: (-row["strategy_return_pct"],
                                                   row["strategy_max_drawdown_pct"])), indent=2))


if __name__ == "__main__":
    main()
