"""Point-in-time option research. Expiry-only accounting, not marked-to-market NAV."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

import db_manager
from data_fetcher import StrikeLegRaw
from options_strategy_engine import OPTIONS_MULTIPLIER, _select_strategy_template, near_call_wall, select_strategy
from strategy_tracker import score_outcome


def capture_chain(symbol, result, expiry, legs, db_path=db_manager.DEFAULT_DB_PATH):
    """Keep received quotes for prospective research; never fabricate historical quotes."""
    observed = datetime.now(timezone.utc).isoformat()
    frame = {
        "symbol": symbol, "observed_at": observed, "expiry": expiry,
        "spot": result.spot, "put_wall": result.put_wall, "call_wall": result.call_wall,
        "alert": result.alert, "total_net_gex": result.zero_dte_summary.get("total_net_gex"),
        "data_usable": all(q is not None and q.get("usable", False)
                           for q in (result.data_quality, result.oi_data_quality)),
        "quote_source": "yfinance_received_indicative", "legs": [asdict(leg) for leg in legs],
    }
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS research_option_chains (
            symbol TEXT NOT NULL, observed_at TEXT NOT NULL, expiry TEXT NOT NULL,
            frame_json TEXT NOT NULL, PRIMARY KEY(symbol, observed_at, expiry))""")
        conn.execute("INSERT INTO research_option_chains VALUES (?, ?, ?, ?)",
                     (symbol, observed, expiry, json.dumps(frame, allow_nan=False)))


def audit_database(db_path):
    with sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        snapshots = [dict(r) for r in conn.execute("SELECT * FROM daily_snapshots")]
        records = [dict(r) for r in conn.execute("SELECT * FROM strategy_recommendations")]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        chain_count = conn.execute("SELECT count(*) FROM research_option_chains").fetchone()[0] if "research_option_chains" in tables else 0
    lookup = {(r["symbol"], r["date"]): r for r in snapshots}
    issues = []
    for r in records:
        source = lookup.get((r["symbol"], r["recommended_date"]))
        if source is None:
            issues.append({"id": r["id"], "issue": "missing_entry_snapshot"})
            continue
        legs = json.loads(r["legs_json"])
        if r["strategy_type"] == "debit" and any(
            (leg["option_type"] == "CALL" and leg["strike_price"] < source["spot"] * 1.03)
            or (leg["option_type"] == "PUT" and leg["strike_price"] > source["spot"] * .97)
            for leg in legs
        ):
            issues.append({"id": r["id"], "issue": "strangle_inconsistent_with_entry_rule"})
    return {
        "daily_snapshots": len(snapshots), "recommendations": len(records),
        "resolved": sum(bool(r["resolved"]) for r in records),
        "full_chain_snapshots": chain_count, "issues": issues,
        "conclusion": "insufficient_for_strategy_validation",
        "limitations": ["Legacy recommendations lack contemporaneous full quotes and forecast provenance",
                        "One settled recommendation cannot establish expected returns"],
    }


def _timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Timestamps must contain a timezone")
    return result


def read_settlements(path: Path):
    """Require as-traded, standard-contract prices with explicit provenance."""
    required = {"symbol", "date", "close", "price_basis", "contract_multiplier", "source", "corporate_action_checked"}
    prices = {}
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if not required <= set(reader.fieldnames or []):
            raise ValueError(f"Settlement CSV missing: {sorted(required - set(reader.fieldnames or []))}")
        for row in reader:
            key = (row["symbol"], row["date"])
            if key in prices:
                raise ValueError(f"Duplicate settlement: {key}")
            date.fromisoformat(row["date"])
            close = float(row["close"])
            if not math.isfinite(close) or close <= 0:
                raise ValueError(f"Invalid settlement close: {key}")
            if (row["price_basis"] != "as_traded_equity_close" or row["contract_multiplier"] != "100"
                or row["corporate_action_checked"].lower() != "yes" or not row["source"].strip()):
                raise ValueError(f"Settlement lacks as-traded/contract evidence: {key}")
            prices[key] = close
    return prices


def _settle(trades, equity, peak, drawdown):
    by_day = {}
    for trade in trades:
        by_day[trade["expiry"]] = by_day.get(trade["expiry"], 0) + trade["net_pnl"]
    for day in sorted(by_day):
        equity += by_day[day]
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak)
    return equity, peak, drawdown


def marked_portfolio(trades, marks, initial_capital):
    """Conservative closing liquidation NAV from contemporaneous option quotes.

    This describes marks for an expiry-held portfolio; no intraday exit or
    early assignment is simulated. Missing contracts invalidate NAV metrics.
    """
    if not trades:
        return {"status": "no_trades", "daily_nav": [], "max_drawdown_pct": None,
                "annualized_return_pct": None, "sharpe": None}
    if not math.isfinite(initial_capital) or initial_capital <= 0:
        raise ValueError("Invalid initial capital")
    quotes = {}
    for row in marks:
        key = (row["symbol"], row["date"], row["expiry"], float(row["strike"]), row["option_type"].upper())
        if key in quotes:
            raise ValueError(f"Duplicate daily contract quote: {key}")
        stamp = _timestamp(row["observed_at"])
        if stamp.astimezone(ZoneInfo("America/New_York")).date().isoformat() != row["date"]:
            raise ValueError("Quote timestamp and trading date disagree")
        bid, ask = float(row["bid"]), float(row["ask"])
        if not all(math.isfinite(value) for value in (bid, ask)) or bid < 0 or ask <= 0 or bid > ask:
            raise ValueError(f"Invalid daily quote: {key}")
        quotes[key] = (bid, ask, stamp)
    calendar = mcal.get_calendar("NYSE")
    first = min(trade["entry"] for trade in trades)
    last = max(trade["expiry"] for trade in trades)
    sessions = calendar.schedule(start_date=first, end_date=last)
    daily_nav, missing = [], []
    for timestamp, session in sessions.iterrows():
        day = timestamp.date().isoformat()
        completed = sum(trade["net_pnl"] for trade in trades if trade["expiry"] <= day)
        open_pnl = 0.0
        issues = []
        for trade in trades:
            if not trade["entry"] <= day < trade["expiry"]:
                continue
            quantity = trade["quantity"]
            signed_premium = trade["net_premium"] if trade["strategy_type"] == "credit" else -trade["net_premium"]
            liquidation = 0.0
            for leg in trade["legs"]:
                key = (trade["symbol"], day, trade["expiry"], float(leg["strike_price"]), leg["option_type"])
                quote = quotes.get(key)
                if quote is None:
                    issues.append({"symbol": trade["symbol"], "expiry": trade["expiry"],
                                   "strike": leg["strike_price"], "type": leg["option_type"], "reason": "missing_contract_quote"})
                    continue
                bid, ask, observed = quote
                if observed > session.market_close.to_pydatetime() or (
                    day == trade["entry"] and observed < _timestamp(trade["entry_at"])
                ):
                    issues.append({"symbol": trade["symbol"], "expiry": trade["expiry"],
                                   "strike": leg["strike_price"], "type": leg["option_type"], "reason": "quote_time_mismatch"})
                    continue
                liquidation += bid if leg["action"] == "BUY" else -ask
            open_pnl += quantity * ((signed_premium + liquidation) * OPTIONS_MULTIPLIER - trade["costs"] / quantity)
        nav = None if issues else initial_capital + completed + open_pnl
        daily_nav.append({"date": day, "nav": nav, "open_positions": sum(
            trade["quantity"] for trade in trades if trade["entry"] <= day < trade["expiry"])})
        if issues:
            missing.append({"date": day, "contracts": issues})
    if missing:
        return {"status": "incomplete_marks", "daily_nav": daily_nav,
                "missing_sessions": missing, "max_drawdown_pct": None,
                "annualized_return_pct": None, "sharpe": None}
    nav_values = [row["nav"] for row in daily_nav]
    peak = initial_capital
    max_drawdown = 0.0
    for nav in nav_values:
        peak = max(peak, nav)
        max_drawdown = max(max_drawdown, (peak - nav) / peak)
    # Daily NAV can go negative when the assigned short stock or other broker
    # mechanics are not modeled. Reject such a series rather than annualize it.
    if any(nav <= 0 for nav in nav_values):
        return {"status": "invalid_nav", "daily_nav": daily_nav,
                "max_drawdown_pct": None, "annualized_return_pct": None, "sharpe": None}
    daily_returns = [(nav_values[i] / nav_values[i-1] - 1) for i in range(1, len(nav_values))]
    annualized = ((nav_values[-1] / initial_capital) ** (252 / len(daily_returns)) - 1) * 100 if len(daily_returns) >= 252 else None
    mean = sum(daily_returns) / len(daily_returns) if daily_returns else 0.0
    deviation = math.sqrt(sum((value - mean)**2 for value in daily_returns) / (len(daily_returns) - 1)) if len(daily_returns) >= 252 else 0.0
    sharpe = mean / deviation * math.sqrt(252) if deviation > 0 else None
    return {"status": "complete", "daily_nav": daily_nav, "missing_sessions": [],
            "max_drawdown_pct": max_drawdown * 100,
            "annualized_return_pct": annualized, "sharpe": sharpe}


def replay(frames, prices, split_date, *, initial_capital=10000.0, risk_fraction=.01,
           fee_per_contract=.65, slippage_per_share=.01, variant="guarded",
           total_risk_fraction=.10, symbol_risk_fraction=.03, marks=None):
    """Use previous session signals and entry-date bid/ask; reserve full defined risk.

    prices maps (symbol, expiry date) to actual unadjusted closes. Split purges
    trades whose holding interval crosses the holdout boundary. No tuning on holdout.
    """
    if variant not in {"guarded", "legacy", "resistance_defense"}:
        raise ValueError("Unknown variant")
    if not math.isfinite(initial_capital) or initial_capital <= 0 or not 0 < risk_fraction <= .05:
        raise ValueError("Capital must be positive; per-trade risk must be in (0, 5%]")
    if not all(math.isfinite(x) and x >= 0 for x in (fee_per_contract, slippage_per_share)):
        raise ValueError("Invalid costs")
    if not 0 < symbol_risk_fraction <= total_risk_fraction <= 1:
        raise ValueError("Invalid portfolio risk limits")
    boundary = date.fromisoformat(split_date)
    ordered = sorted(frames, key=lambda f: (_timestamp(f["observed_at"]), f["symbol"], f["expiry"]))
    calendar = mcal.get_calendar("NYSE")
    results = {}
    for partition in ("train", "holdout"):
        equity = initial_capital
        active, trades, skipped, previous = [], [], [], {}
        peak, drawdown, reserved_peak = equity, 0.0, 0.0
        used = set()
        for frame in ordered:
            timestamp = _timestamp(frame["observed_at"])
            entry = timestamp.astimezone(ZoneInfo("America/New_York")).date()
            expiry = date.fromisoformat(frame["expiry"])
            symbol = frame["symbol"]
            key = (symbol, frame["expiry"])
            state = previous.get(symbol)
            if state is None:
                state = {"day": entry, "latest": frame, "signal": None}
                previous[symbol] = state
            elif state["day"] < entry:
                state.update(day=entry, signal=state["latest"], latest=frame)
            else:
                state["latest"] = frame
            signal = state["signal"]
            matured = [trade for trade in active if date.fromisoformat(trade["expiry"]) < entry]
            equity, peak, drawdown = _settle(matured, equity, peak, drawdown)
            active = [trade for trade in active if date.fromisoformat(trade["expiry"]) >= entry]
            if signal is None or _timestamp(signal["observed_at"]) >= timestamp:
                continue
            if partition == "train" and not entry < expiry < boundary:
                continue
            if partition == "holdout" and not boundary <= entry < expiry:
                continue
            if key in used:
                continue
            signal_day = _timestamp(signal["observed_at"]).astimezone(ZoneInfo("America/New_York")).date()
            sessions = calendar.valid_days(start_date=signal_day, end_date=entry)
            if len(sessions) != 2 or sessions[0].date() != signal_day or sessions[-1].date() != entry:
                skipped.append("missing_previous_session_signal")
                continue
            schedule = calendar.schedule(start_date=entry, end_date=entry)
            if schedule.empty or not schedule.iloc[0]["market_open"].to_pydatetime() <= timestamp <= schedule.iloc[0]["market_close"].to_pydatetime():
                skipped.append("quote_outside_session")
                continue
            legs = [StrikeLegRaw(**leg) for leg in frame["legs"]]
            if any(leg.expiry != frame["expiry"] for leg in legs):
                raise ValueError("Frame and contract expiry mismatch")
            tte = (expiry - entry).days / 365
            if not 25 <= (expiry - entry).days <= 45:
                skipped.append("outside_dte_window")
                continue
            if variant == "resistance_defense" and not near_call_wall(signal["spot"], signal["call_wall"]):
                skipped.append("signal_not_near_resistance")
                continue
            kwargs = {}
            if variant in {"guarded", "resistance_defense"}:
                forecast = signal.get("forecast_vol")
                forecast_asof = signal.get("forecast_asof")
                if forecast is not None and (forecast_asof is None or _timestamp(forecast_asof) > _timestamp(signal["observed_at"])):
                    raise ValueError("Forecast must have point-in-time provenance")
                kwargs = dict(total_net_gex=signal.get("total_net_gex"), forecast_vol=forecast,
                              data_usable=signal.get("data_usable", False) and frame.get("data_usable", False),
                              fee_per_contract=fee_per_contract, slippage_per_share=slippage_per_share,
                              setup="resistance_defense" if variant == "resistance_defense" else "gamma_template")
            selector = select_strategy if variant != "legacy" else _select_strategy_template
            rec = selector(legs, frame["spot"], tte, signal["put_wall"], signal["call_wall"], signal.get("alert"), **kwargs)
            if rec.legs is None:
                skipped.append(rec.rationale)
                continue
            settlement = prices.get((symbol, expiry.isoformat()))
            if settlement is None:
                skipped.append("missing_exact_expiry_close")
                continue
            if not math.isfinite(settlement) or settlement <= 0:
                raise ValueError("Invalid settlement price")
            costs = len(rec.legs) * (2 * fee_per_contract + slippage_per_share * OPTIONS_MULTIPLIER)
            risk = rec.max_loss * OPTIONS_MULTIPLIER + costs
            reserved = sum(trade["reserved_risk"] for trade in active)
            symbol_reserved = sum(trade["reserved_risk"] for trade in active if trade["symbol"] == symbol)
            quantity = max(0, math.floor(min(equity * risk_fraction,
                                            equity * total_risk_fraction - reserved,
                                            equity * symbol_risk_fraction - symbol_reserved) / risk))
            if quantity == 0:
                skipped.append("capital_or_risk_budget")
                continue
            outcome = score_outcome([asdict(leg) for leg in rec.legs], rec.net_premium, rec.strategy_type, settlement, rec.max_loss)
            trade = {"symbol": symbol, "entry": entry.isoformat(), "expiry": expiry.isoformat(),
                     "strategy": rec.strategy_name, "quantity": quantity,
                     "entry_at": timestamp.isoformat(), "strategy_type": rec.strategy_type,
                     "net_premium": rec.net_premium, "legs": [asdict(leg) for leg in rec.legs],
                     "net_pnl": quantity * (outcome.realized_pnl * OPTIONS_MULTIPLIER - costs),
                     "reserved_risk": quantity * risk, "costs": quantity * costs}
            used.add(key)
            active.append(trade)
            trades.append(trade)
            reserved_peak = max(reserved_peak, reserved + quantity * risk)
        equity, peak, drawdown = _settle(active, equity, peak, drawdown)
        profits = sum(max(0, t["net_pnl"]) for t in trades)
        losses = -sum(min(0, t["net_pnl"]) for t in trades)
        risk_days = sum(t["reserved_risk"] * (date.fromisoformat(t["expiry"]) - date.fromisoformat(t["entry"])).days for t in trades)
        results[partition] = {
            "trades": trades, "trade_count": len(trades), "skipped_count": len(skipped),
            "skip_reasons": {reason: skipped.count(reason) for reason in sorted(set(skipped))},
            "net_pnl": equity - initial_capital, "return_pct": (equity / initial_capital - 1) * 100,
            "profit_factor": profits / losses if losses else None,
            "win_rate_pct": 100 * sum(t["net_pnl"] > 0 for t in trades) / len(trades) if trades else None,
            "settled_equity_drawdown_pct": drawdown * 100, "peak_reserved_risk": reserved_peak,
            "net_pnl_per_risk_dollar_day": (equity - initial_capital) / risk_days if risk_days else None,
            "minimum_trade_count_met": len(trades) >= 100,
            "sufficient_for_validation": False,
        }
        if marks is not None:
            results[partition]["mark_to_market"] = marked_portfolio(trades, marks, initial_capital)
    return {"variant": variant, "split_date": split_date, "results": results,
            "limitations": ["Expiry payoff research; no daily option marks, Sharpe, or true NAV drawdown",
                            "No early assignment, pin risk, dividends, or corporate-action modeling",
                            "Forecast model and thresholds are experimental; sample count alone does not validate them",
                            "Quotes must be contemporaneous, standard 100-share contracts; received yfinance quotes are indicative",
                            "Forecast provenance timestamps are required but cannot verify external model training"]}


def walk_forward(frames, prices, boundaries, **kwargs):
    """Expanding training, disjoint holdouts; only training selects a candidate.

    Candidates and minimum sample requirements are fixed before evaluation.
    Select cash if training cannot support selection. This does not optimize
    parameters or use holdout performance to choose the winning rule.
    """
    if len(boundaries) < 2 or boundaries != sorted(set(boundaries)):
        raise ValueError("Provide at least two strictly increasing fold boundaries")
    folds = []
    for start, end in zip(boundaries, boundaries[1:]):
        date.fromisoformat(start)
        date.fromisoformat(end)
        subset = [frame for frame in frames
                  if _timestamp(frame["observed_at"]).astimezone(ZoneInfo("America/New_York")).date().isoformat() < end
                  and frame["expiry"] < end]
        reports = {variant: replay(subset, prices, start, variant=variant, **kwargs)
                   for variant in ("legacy", "guarded")}
        eligible = {variant: report["results"]["train"] for variant, report in reports.items()
                    if report["results"]["train"]["minimum_trade_count_met"]
                    and report["results"]["train"]["net_pnl"] > 0}
        selected = max(eligible, key=lambda v: eligible[v]["net_pnl_per_risk_dollar_day"]) if eligible else "cash"
        folds.append({"start": start, "end_exclusive": end, "selected_using_training_only": selected,
                      "candidate_reports": reports,
                      "selected_holdout": reports[selected]["results"]["holdout"] if selected != "cash" else {"trade_count": 0, "net_pnl": 0}})
    return {"folds": folds, "selection_metric": "training_net_pnl_per_risk_dollar_day",
            "limitations": ["Holding intervals crossing any fold boundary are excluded",
                            "Each fold starts with the same capital; fold returns are not compounded",
                            "Neither candidate nor the selection procedure is validated for live trading"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=str(db_manager.DEFAULT_DB_PATH))
    parser.add_argument("--frames", type=Path, help="JSONL point-in-time chain snapshots")
    parser.add_argument("--prices", type=Path, help="Verified as-traded expiry close CSV; see research data contract")
    parser.add_argument("--marks", type=Path, help="Daily option contract bid/ask CSV from historical_options.py")
    parser.add_argument("--split-date")
    parser.add_argument("--fold-boundaries", nargs="+", help="Chronological expanding-window holdout boundaries")
    parser.add_argument("--export-frames", type=Path, help="Export prospectively captured chains to JSONL")
    parser.add_argument("--capital", type=float, default=10000)
    parser.add_argument("--risk-fraction", type=float, default=.01)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.export_frames:
        with sqlite3.connect(f"file:{Path(args.db_path).resolve()}?mode=ro", uri=True) as conn:
            rows = conn.execute("SELECT frame_json FROM research_option_chains ORDER BY observed_at, symbol, expiry").fetchall()
        args.export_frames.parent.mkdir(parents=True, exist_ok=True)
        args.export_frames.write_text("".join(row[0] + "\n" for row in rows))
        print(json.dumps({"exported_frames": len(rows), "path": str(args.export_frames)}))
        return
    if args.frames:
        if not args.prices or not (args.split_date or args.fold_boundaries):
            parser.error("--frames requires --prices and either --split-date or --fold-boundaries")
        frames = [json.loads(line) for line in args.frames.read_text().splitlines() if line.strip()]
        prices = read_settlements(args.prices)
        if args.marks:
            with args.marks.open(newline="") as handle:
                marks = list(csv.DictReader(handle))
        else:
            marks = None
        if args.fold_boundaries:
            report = walk_forward(frames, prices, args.fold_boundaries, initial_capital=args.capital,
                                  risk_fraction=args.risk_fraction, marks=marks)
        else:
            report = {v: replay(frames, prices, args.split_date, initial_capital=args.capital,
                                risk_fraction=args.risk_fraction, variant=v, marks=marks) for v in ("legacy", "guarded")}
    else:
        report = audit_database(args.db_path)
    output = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n")
    print(output)


if __name__ == "__main__":
    main()
