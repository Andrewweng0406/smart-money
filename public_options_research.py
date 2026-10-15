"""Exploratory SPY replay from an independently checked public Parquet mirror.

The mirror does not document a verifiable quote timestamp or original vendor.
Its results are research diagnostics, never production or investment evidence.
Requires pyarrow in the Python environment used to run this script.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import Counter
from datetime import date, datetime
from itertools import groupby
from pathlib import Path

from historical_options import attach_forecasts, import_cboe
from market_calendar import get_market_session
from options_research import marked_portfolio, replay


SOURCE = "public_mirror_eod_time_unverified"
FIELDS = ("contract_id", "symbol", "expiration", "strike", "type", "bid", "ask", "bid_size",
          "ask_size", "open_interest", "date", "implied_volatility")


def standard_contract(row):
    if row.get("symbol") != "SPY":
        return False
    expiry = str(row["expiration"])
    strike = float(row["strike"])
    option_type = {"call": "C", "put": "P"}.get(str(row["type"]).lower())
    if option_type is None or not math.isfinite(strike) or strike <= 0:
        return False
    encoded = round(strike * 1000)
    if abs(strike * 1000 - encoded) > 1e-6:
        return False
    try:
        expected = "SPY" + date.fromisoformat(expiry).strftime("%y%m%d") + option_type + f"{encoded:08d}"
    except ValueError:
        return False
    return row["contract_id"] == expected


def as_cboe_row(row, spot):
    return {"underlying_symbol": row["symbol"], "quote_date": row["date"], "root": "SPY",
            "expiration": row["expiration"], "strike": row["strike"],
            "option_type": "C" if row["type"] == "call" else "P",
            "bid_1545": row["bid"], "ask_1545": row["ask"],
            "bid_size_1545": row["bid_size"], "ask_size_1545": row["ask_size"],
            "active_underlying_price_1545": spot,
            "implied_volatility_1545": row["implied_volatility"],
            "open_interest": row["open_interest"]}


def days_in_parquet(path):
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    table = pq.read_table(path, columns=list(FIELDS))
    table = table.take(pc.sort_indices(table, sort_keys=[("date", "ascending")]))
    days = table["date"].to_pylist()
    offset = 0
    for day, group in groupby(days):
        count = sum(1 for _ in group)
        yield day, table.slice(offset, count).to_pylist()
        offset += count


def checked_underlying(path, yahoo_csv, option_days):
    import pyarrow.parquet as pq

    rows = pq.read_table(path).to_pylist()
    by_day = {}
    for row in rows:
        day = row["date"]
        if day in by_day:
            raise ValueError(f"Duplicate underlying close: {day}")
        by_day[day] = row
    with yahoo_csv.open(newline="") as handle:
        independent = {row["date"]: float(row["close"]) for row in csv.DictReader(handle)}
    differences = []
    for day in option_days:
        row = by_day.get(day)
        close = row and row["close"]
        if not row or row["symbol"] != "SPY" or row["split_coefficient"] != 1 or not close or not math.isfinite(close):
            raise ValueError(f"Missing or adjusted underlying close: {day}")
        if day not in independent:
            raise ValueError(f"Missing independent price check: {day}")
        difference = abs(close - independent[day])
        if difference > .01:
            raise ValueError(f"Underlying close fails independent check: {day}, difference={difference}")
        differences.append(difference)
    return by_day, {"checked_days": len(differences), "max_close_difference": max(differences, default=None),
                    "no_splits_on_option_days": True}


def sha256(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--options", type=Path, nargs="+", required=True, help="SPY yearly Parquet files")
    parser.add_argument("--underlying", type=Path, required=True)
    parser.add_argument("--independent-prices", type=Path, default=Path("research/market_data/SPY.csv"))
    parser.add_argument("--forecast-validation", type=Path, default=Path("research/volatility_validation.json"))
    parser.add_argument("--price-directory", type=Path, default=Path("research/market_data"))
    parser.add_argument("--split-date", default="2024-01-01")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    import pyarrow.parquet as pq

    option_days, non_session_rows, seen_days = set(), {}, set()
    for path in args.options:
        from collections import Counter as DayCounter
        day_counts = DayCounter(pq.read_table(path, columns=["date"])["date"].to_pylist())
        overlap = seen_days & day_counts.keys()
        if overlap:
            raise ValueError(f"Duplicate trading dates across files: {sorted(overlap)[:3]}")
        seen_days.update(day_counts)
        for day, count in day_counts.items():
            if get_market_session(date.fromisoformat(day)) is None:
                non_session_rows[day] = count
            else:
                option_days.add(day)
    prices, price_audit = checked_underlying(args.underlying, args.independent_prices, option_days)
    validation = json.loads(args.forecast_validation.read_text())
    frames, histories = [], {}
    totals, rejected = Counter(), Counter()
    standard_rejected = 0
    for path in args.options:
        for day, raw in days_in_parquet(path):
            if day in non_session_rows:
                continue
            accepted = [as_cboe_row(row, prices[day]["close"]) for row in raw if standard_contract(row)]
            standard_rejected += len(raw) - len(accepted)
            daily, report = import_cboe(accepted, {"SPY"}, quote_minutes_before_close=0,
                                         quote_source=SOURCE)
            frames.extend(attach_forecasts(daily, args.price_directory, validation, histories))
            totals.update(report["counts"])
            rejected.update(report["rejections"])
    settlements = {("SPY", day): prices[day]["close"] for day in option_days}
    replay_frames = [frame for frame in frames if frame["expiry"] <= max(option_days)]
    results = {variant: replay(replay_frames, settlements, args.split_date, variant=variant,
                               initial_capital=100000) for variant in ("legacy", "guarded", "resistance_defense")}
    contracts = {}
    for report in results.values():
        for partition in report["results"].values():
            for trade in partition["trades"]:
                for leg in trade["legs"]:
                    key = (trade["expiry"], float(leg["strike_price"]), leg["option_type"])
                    contracts.setdefault(key, []).append((trade["entry"], trade["expiry"]))
    marks = []
    for path in args.options:
        for day, raw in days_in_parquet(path):
            if day in non_session_rows:
                continue
            session = get_market_session(date.fromisoformat(day))
            observed_at = session.market_close.isoformat()
            for row in raw:
                key = (row["expiration"], float(row["strike"]), str(row["type"]).upper())
                if key not in contracts or not standard_contract(row):
                    continue
                if not any(entry <= day < expiry for entry, expiry in contracts[key]):
                    continue
                bid, ask = row["bid"], row["ask"]
                if bid is None or ask is None or not all(math.isfinite(x) for x in (bid, ask)) or bid < 0 or ask <= 0 or bid > ask:
                    continue
                marks.append({"symbol": "SPY", "date": day, "observed_at": observed_at,
                              "expiry": row["expiration"], "strike": row["strike"],
                              "option_type": key[2], "bid": bid, "ask": ask})
    summaries = {}
    for variant, report in results.items():
        summaries[variant] = {}
        for name, partition in report["results"].items():
            marked = marked_portfolio(partition["trades"], marks, 100000)
            summaries[variant][name] = {key: value for key, value in partition.items() if key != "trades"}
            summaries[variant][name]["mark_to_market"] = {key: value for key, value in marked.items()
                                                          if key != "daily_nav"}
    output = {"study": "exploratory_unverified_public_mirror_not_live_approved",
              "source": "https://github.com/anahatsingh-ui/options-dataset-hist",
              "source_files": [{"path": str(path), "sha256": sha256(path)} for path in args.options],
              "underlying_file": {"path": str(args.underlying), "sha256": sha256(args.underlying)},
              "option_sessions": len(option_days), "date_range": [min(option_days), max(option_days)],
              "input_rows": totals["input_rows"] + standard_rejected + sum(non_session_rows.values()),
              "standard_contract_rejections": standard_rejected, "counts": dict(totals),
              "quote_rejections": dict(rejected), "frames": len(frames),
              "eligible_settled_frames": len(replay_frames),
              "non_session_rows_quarantined": non_session_rows,
              "forecasts_attached": sum("forecast_vol" in frame for frame in frames),
              "underlying_audit": price_audit, "split_date": args.split_date,
              "results": summaries,
              "limitations": ["Original options vendor and exact EOD quote timestamp cannot be independently verified",
                              "The public mirror contains example-like rows on a market holiday; these were quarantined",
                              "The mirror's in_the_money field is incorrect and was ignored",
                              "Greeks/IV are vendor-derived and not independently validated across the full period",
                              "American early assignment, dividends, exercise, and broker financing are not simulated",
                              "Only SPY is available; TSLA/SOXL strategy profitability cannot be inferred"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")
    print(json.dumps({key: value for key, value in output.items() if key != "results"}, indent=2))
    print(json.dumps({v: {p: {"trades": d["trade_count"], "net_pnl": d["net_pnl"],
                               "mark_status": d["mark_to_market"]["status"]} for p, d in partitions.items()}
                      for v, partitions in summaries.items()}, indent=2))


if __name__ == "__main__":
    main()
