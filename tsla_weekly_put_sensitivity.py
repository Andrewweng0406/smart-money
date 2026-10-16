"""Run a small, predeclared sensitivity grid for premium-funded weekly TSLA puts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tsla_optionsdx_continuous_replay import FUNDED_VARIANT, replay_year


def summarize(result):
    trades = [event for event in result["events"]
              if event.get("action") == "put_close" and event.get("variant") == FUNDED_VARIANT]
    unique = {}
    for trade in trades:
        unique[(trade["entry_date"], trade["expiry"], trade["strike"])] = trade
    independent = list(unique.values())
    debit = sum(trade["debit"] for trade in trades)
    proceeds = sum(trade["proceeds"] for trade in trades)
    return {
        "slot_trades": len(trades), "independent_signals": len(independent),
        "winning_slot_trades": sum(trade["realized_pnl"] > 0 for trade in trades),
        "put_debit": round(debit, 2), "put_proceeds": round(proceeds, 2),
        "put_pnl": round(proceeds - debit, 2),
        "take_profit_exits": sum(trade["exit_reason"] == "take_profit" for trade in trades),
        "strategy_return_pct": result["variants"][FUNDED_VARIANT]["account_return_pct"],
        "strategy_max_drawdown_pct": result["variants"][FUNDED_VARIANT]["max_daily_nav_drawdown_pct"],
        "pmcc_return_pct": result["variants"]["pmcc"]["account_return_pct"],
        "pmcc_max_drawdown_pct": result["variants"]["pmcc"]["max_daily_nav_drawdown_pct"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("research/raw_options/optionsdx"))
    parser.add_argument("--audit", type=Path, default=Path("research/optionsdx_tsla_multiyear_audit.json"))
    parser.add_argument("--output", type=Path,
                        default=Path("research/tsla_weekly_put_sensitivity.json"))
    args = parser.parse_args()
    audit = json.loads(args.audit.read_text())
    rows = []
    for entry_policy in ("month_first", "dip_10"):
        for target_ratio in (.90, .95, .98):
            for hold_sessions in (2, 4):
                for take_profit_pct in (None, 1.):
                    result = replay_year(
                        args.directory, audit, 2023, entry_policy=entry_policy,
                        put_policy="weekly", funding_policy="realized",
                        put_target_ratio=target_ratio, risk_signal_policy="break20_low",
                        funding_scope="slot", put_hold_sessions=hold_sessions,
                        put_take_profit_pct=take_profit_pct)
                    rows.append({
                        "entry_policy": entry_policy, "put_target_ratio": target_ratio,
                        "put_hold_sessions": hold_sessions,
                        "put_take_profit_pct": take_profit_pct, **summarize(result),
                    })
    output = {
        "study": "2023_weekly_put_rule_sensitivity_exploratory",
        "warning": "Only one complete year and at most a few independent hedge signals; not an optimization claim.",
        "rows": rows,
    }
    args.output.write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")
    print(json.dumps(sorted(rows, key=lambda row: (-row["strategy_return_pct"],
                                                   row["strategy_max_drawdown_pct"]))[:8], indent=2))


if __name__ == "__main__":
    main()
