"""Research-only TSLA option replay from the audited 2022 H2 EOD sample.

The vendor does not provide 2022 quote timestamps. Date-bucket marks and
assumed EOD executions cannot establish contemporaneous or executable fills.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import date
from pathlib import Path

from market_calendar import get_market_session
from pmcc_research import CallQuote, PMCCLedger, PutQuote
from tsla_chain_sample_audit import JAN_2023, JAN_2024, standard_contract, valid_quote


CAPITAL = 100_000
FIRST_DAY = date(2022, 8, 31)
LAST_DAY = date(2022, 12, 30)


def load_day(path):
    rows = []
    spot = None
    with path.open(newline="") as handle:
        for raw in csv.DictReader(handle):
            if raw["underlying"] != "TSLA":
                continue
            if spot is None and raw["underlying_close"]:
                spot = float(raw["underlying_close"])
            if not standard_contract(raw) or not valid_quote(raw):
                continue
            rows.append({"contract": raw["contract"], "expiration": date.fromisoformat(raw["expiration"]),
                         "type": raw["type"], "strike": float(raw["strike"]),
                         "bid": float(raw["bid"]), "ask": float(raw["ask"]),
                         "open_interest": int(raw["open_interest"] or 0)})
    if spot is None or not rows:
        raise ValueError(f"Missing TSLA spot or contracts: {path.name}")
    return spot, rows


def liquid(row, max_spread):
    return row["bid"] > .01 and (row["ask"] - row["bid"]) / row["ask"] <= max_spread


def choose_long(rows, spot, day, policy="nearest_540d"):
    target = {"jan2023": JAN_2023, "jan2024": JAN_2024}.get(policy)
    if policy not in ("nearest_540d", "jan2023", "jan2024"):
        raise ValueError(f"Unknown long-call policy: {policy}")
    candidates = [row for row in rows if row["type"] == "call" and liquid(row, .10)
                  and ((row["expiration"] == target) if target else
                       365 <= (row["expiration"] - day).days <= 730)
                  and .70 * spot <= row["strike"] <= .85 * spot]
    return min(candidates, key=lambda row: (abs((row["expiration"] - day).days - 540) if not target else 0,
                                            abs(row["strike"] - .80 * spot), row["ask"]), default=None)


def walls(rows, spot, day):
    nearby = [row for row in rows if 21 <= (row["expiration"] - day).days <= 45
              and row["open_interest"] > 0]
    calls = [row for row in nearby if row["type"] == "call" and spot <= row["strike"] <= 1.05 * spot]
    puts = [row for row in nearby if row["type"] == "put" and .95 * spot <= row["strike"] <= spot]
    call_wall = max(calls, key=lambda row: (row["open_interest"], -row["strike"]))["strike"] if calls else None
    put_wall = max(puts, key=lambda row: (row["open_interest"], row["strike"]))["strike"] if puts else None
    return call_wall, put_wall


def choose_short(rows, day, wall, long_contract):
    candidates = [row for row in rows if row["type"] == "call" and liquid(row, .25)
                  and 21 <= (row["expiration"] - day).days <= 45
                  and row["expiration"] < long_contract[1]
                  and row["strike"] >= wall and row["strike"] > long_contract[2]]
    return min(candidates, key=lambda row: (abs((row["expiration"] - day).days - 30),
                                            row["strike"] - wall), default=None)


def choose_put(rows, day, signal_spot):
    candidates = [row for row in rows if row["type"] == "put" and liquid(row, .25)
                  and 45 <= (row["expiration"] - day).days <= 90
                  and .90 * signal_spot <= row["strike"] <= signal_spot]
    return min(candidates, key=lambda row: (abs((row["expiration"] - day).days - 60),
                                            abs(row["strike"] - .95 * signal_spot), row["ask"]), default=None)


def quote(row, at):
    cls = CallQuote if row["type"] == "call" else PutQuote
    return cls("TSLA", row["expiration"], row["strike"], at, row["bid"], row["ask"])


def marked(book, contract, side, at):
    row = book.get((contract[1], contract[2], side))
    if row is None:
        raise ValueError(f"Missing {side} mark for {contract} on {at.date()}")
    return quote(row, at)


def summary(nav):
    peak = CAPITAL
    max_dd = 0.0
    for value in nav:
        peak = max(peak, value)
        max_dd = max(max_dd, 1 - value / peak)
    return {"final_nav": round(nav[-1], 2), "net_pnl": round(nav[-1] - CAPITAL, 2),
            "account_return_pct": round(100 * (nav[-1] / CAPITAL - 1), 3),
            "max_daily_nav_drawdown_pct": round(100 * max_dd, 3)}


def replay(sample_dir, policy="nearest_540d"):
    paths = sorted((sample_dir / "day_by_date").glob("*_options.csv"))
    paths = [path for path in paths if FIRST_DAY <= date.fromisoformat(path.name[:10]) <= LAST_DAY]
    if len(paths) != 85 or paths[0].name[:10] != FIRST_DAY.isoformat() or paths[-1].name[:10] != LAST_DAY.isoformat():
        raise ValueError("Expected complete 85-session August 31 to December 30 sample")
    names = ("long_call", "pmcc", "pmcc_put")
    nav = {name: [] for name in names}
    ledgers = None
    counts = Counter()
    prior_call_signal = None
    prior_put_wall = None
    prior_risk_signal = None
    short_open_index = put_open_index = None
    daily_dates = []
    put_debits = put_credits = 0.0
    put_entry_dates = []
    put_exit_dates = []
    for index, path in enumerate(paths):
        day = date.fromisoformat(path.name[:10])
        session = get_market_session(day)
        if session is None:
            raise ValueError(f"Non-trading session in sample: {day}")
        at = session.market_close  # Date-bucket label only; vendor quote times are unavailable.
        spot, rows = load_day(path)
        book = {(row["expiration"], row["strike"], row["type"]): row for row in rows}
        if ledgers is None:
            long_row = choose_long(rows, spot, day, policy)
            if long_row is None:
                raise ValueError(f"No liquid long call on {day}")
            ledgers = {name: PMCCLedger.buy_long(quote(long_row, at), capital=CAPITAL) for name in names}
            long_entry = {"date": day.isoformat(), "expiry": long_row["expiration"].isoformat(),
                          "strike": long_row["strike"], "ask": long_row["ask"],
                          "debit": round(CAPITAL - ledgers["long_call"].cash, 2),
                          "entry_dte": (long_row["expiration"] - day).days}
        long_contract = ledgers["long_call"].long_contract
        long_quote = marked(book, long_contract, "call", at)
        if ledgers["pmcc"].active_short and spot >= ledgers["pmcc"].active_short[2]:
            counts["short_itm_observation_days"] += 1
        if ledgers["pmcc"].active_short and (index - short_open_index >= 5 or day == LAST_DAY):
            for name in ("pmcc", "pmcc_put"):
                ledgers[name].close_short(marked(book, ledgers[name].active_short, "call", at))
            short_open_index = None
        if ledgers["pmcc_put"].active_put and (index - put_open_index >= 20 or day == LAST_DAY):
            before = ledgers["pmcc_put"].cash
            ledgers["pmcc_put"].close_put(marked(book, ledgers["pmcc_put"].active_put, "put", at))
            put_credits += ledgers["pmcc_put"].cash - before
            put_exit_dates.append(day.isoformat())
            put_open_index = None
        if day != LAST_DAY and prior_call_signal and ledgers["pmcc"].active_short is None:
            counts["prior_resistance_signal_days"] += 1
            signal_spot, wall = prior_call_signal
            if 0 <= wall - spot <= .01 * spot:
                counts["entry_day_still_near_resistance"] += 1
                short_row = choose_short(rows, day, wall, long_contract)
                if short_row is not None:
                    for name in ("pmcc", "pmcc_put"):
                        ledgers[name].write_short(long_quote, quote(short_row, at), spot=signal_spot,
                                                  call_wall=wall)
                    short_open_index = index
                    counts["short_entries"] += 1
        if day != LAST_DAY and prior_risk_signal and ledgers["pmcc_put"].active_put is None:
            counts["prior_put_risk_signal_days"] += 1
            signal_spot = prior_risk_signal
            put_row = choose_put(rows, day, signal_spot)
            if put_row is not None:
                before = ledgers["pmcc_put"].cash
                ledgers["pmcc_put"].buy_put(long_quote, quote(put_row, at))
                put_debits += before - ledgers["pmcc_put"].cash
                put_entry_dates.append(day.isoformat())
                put_open_index = index
                counts["put_entries"] += 1
        daily_dates.append(day.isoformat())
        if day == LAST_DAY:
            for ledger in ledgers.values():
                ledger.close_long(long_quote)
            for name, ledger in ledgers.items():
                nav[name].append(ledger.cash)
            break
        for name, ledger in ledgers.items():
            short_quote = marked(book, ledger.active_short, "call", at) if ledger.active_short else None
            put_quote = marked(book, ledger.active_put, "put", at) if ledger.active_put else None
            nav[name].append(ledger.liquidation_nav(long_quote, short_quote, put_quote)["nav"])
        call_wall, put_wall = walls(rows, spot, day)
        prior_call_signal = (spot, call_wall) if call_wall is not None and 0 <= call_wall - spot <= .01 * spot else None
        prior_risk_signal = spot if prior_put_wall is not None and spot < prior_put_wall else None
        prior_put_wall = put_wall
    return {"status": "exploratory_date_bucket_quotes_not_executable", "long_call_policy": policy,
            "first_day": FIRST_DAY.isoformat(),
            "last_day": LAST_DAY.isoformat(), "sessions": len(daily_dates), "capital": CAPITAL,
            "long_entry": long_entry, "counts": dict(counts),
            "put_cashflows": {"total_debits": round(put_debits, 2),
                              "total_credits": round(put_credits, 2),
                              "net_pnl": round(put_credits - put_debits, 2),
                              "entry_dates": put_entry_dates, "exit_dates": put_exit_dates},
            "variants": {**{name: summary(values) for name, values in nav.items()},
                         "cash": summary([CAPITAL])},
            "overlay_net_pnl": {"short_calls": round(nav["pmcc"][-1] - nav["long_call"][-1], 2),
                                "conditional_puts": round(nav["pmcc_put"][-1] - nav["pmcc"][-1], 2)},
            "daily_nav": {"dates": daily_dates, **{name: [round(value, 2) for value in values]
                                               for name, values in nav.items()}}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-dir", type=Path, default=Path("/tmp/options_sample_2022H2"))
    parser.add_argument("--long-expiry", choices=("nearest_540d", "jan2023", "jan2024"),
                        default="nearest_540d")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = replay(args.sample_dir, args.long_expiry)
    result["source"] = "https://historicaldata.net/file/options_sample_2022H2.zip"
    result["limitations"] = ["Single four-month post-split episode; no win-rate estimate",
                             "2022 source quote_time blank; assumed EOD fills are not verified",
                             "Call/Put Walls are maximum-open-interest proxies, not proven levels",
                             "American early assignment and financing not simulated",
                             "Entry rules borrowed from prior SPY diagnostic; not an untouched holdout"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "daily_nav"}, indent=2))


if __name__ == "__main__":
    main()
