"""Cross-year test of requiring continued downside before weekly put entry."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tsla_optionsdx_continuous_replay import replay_year


def summarize(result):
    pmcc = result["variants"]["pmcc"]
    hedged = result["variants"]["pmcc_put"]
    trades = [event for event in result["events"]
              if event.get("action") == "put_close" and event.get("variant") == "pmcc_put"]
    signals = {(trade["entry_date"], trade["expiry"], trade["strike"]) for trade in trades}
    return {
        "slot_trades": len(trades), "independent_signals": len(signals),
        "put_pnl": round(sum(trade["realized_pnl"] for trade in trades), 2),
        "strategy_return_pct": hedged["account_return_pct"],
        "return_delta_vs_pmcc_pp": round(hedged["account_return_pct"] - pmcc["account_return_pct"], 3),
        "strategy_max_drawdown_pct": hedged["max_daily_nav_drawdown_pct"],
        "drawdown_delta_vs_pmcc_pp": round(hedged["max_daily_nav_drawdown_pct"] -
                                             pmcc["max_daily_nav_drawdown_pct"], 3),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("research/raw_options/optionsdx"))
    parser.add_argument("--audit", type=Path, default=Path("research/optionsdx_tsla_multiyear_audit.json"))
    parser.add_argument("--output", type=Path,
                        default=Path("research/tsla_weekly_put_regime_sensitivity.json"))
    args = parser.parse_args()
    audit = json.loads(args.audit.read_text())
    rows = []
    for year in (2019, 2023):
        for entry_policy in ("month_first", "dip_10"):
            for target_ratio in (.90, .95):
                for max_daily_return in (-.02, -.03, -.04):
                    result = replay_year(
                        args.directory, audit, year, entry_policy=entry_policy,
                        put_policy="weekly", put_target_ratio=target_ratio,
                        put_hold_sessions=4, put_take_profit_pct=1.,
                        put_entry_max_daily_return_pct=max_daily_return)
                    rows.append({
                        "year": year, "entry_policy": entry_policy,
                        "put_target_ratio": target_ratio,
                        "put_entry_max_daily_return_pct": max_daily_return,
                        **summarize(result),
                    })
    output = {
        "study": "cross_year_weekly_put_continued_downside_filter_exploratory",
        "funding": "unconstrained diagnostic; not the premium-funded implementation",
        "fixed_rules": {"put_hold_sessions": 4, "put_take_profit_pct": 1.},
        "warning": "Post-hoc regime research on only two complete years; no holdout.",
        "rows": rows,
    }
    args.output.write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
