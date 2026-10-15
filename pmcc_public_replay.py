"""Exploratory annual SPY long-call/PMCC/conditional-put replay.

The public mirror's vendor and exact quote timestamp are unverified. Results
are diagnostics, not executable performance or a live trading recommendation.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import date
from pathlib import Path

from market_calendar import get_market_session
from pmcc_research import CallQuote, PMCCLedger, PutQuote, put_wall_breach_signal
from public_options_research import checked_underlying, days_in_parquet, sha256, standard_contract


CAPITAL = 100_000


def eligible(row):
    bid, ask = row.get("bid"), row.get("ask")
    return (standard_contract(row) and bid is not None and ask is not None
            and all(math.isfinite(x) for x in (bid, ask))
            and 0 <= bid <= ask and ask > 0)


def liquid(row, max_spread):
    return row["bid"] > .01 and (row["ask"] - row["bid"]) / row["ask"] <= max_spread


def choose_long(rows, spot, day):
    candidates = [row for row in rows if row["type"] == "call" and liquid(row, .10)
                  and 365 <= (date.fromisoformat(row["expiration"]) - day).days <= 730
                  and .70 * spot <= row["strike"] <= .85 * spot]
    return min(candidates, key=lambda row: (abs((date.fromisoformat(row["expiration"]) - day).days - 540),
                                            abs(row["strike"] - .80 * spot), row["ask"]), default=None)


def choose_short(rows, day, wall, long_contract):
    candidates = [row for row in rows if row["type"] == "call" and liquid(row, .25)
                  and 21 <= (date.fromisoformat(row["expiration"]) - day).days <= 45
                  and date.fromisoformat(row["expiration"]) < long_contract[1]
                  and row["strike"] >= wall and row["strike"] > long_contract[2]]
    return min(candidates, key=lambda row: (abs((date.fromisoformat(row["expiration"]) - day).days - 30),
                                            row["strike"] - wall), default=None)


def choose_put(rows, day, signal_spot):
    candidates = [row for row in rows if row["type"] == "put" and liquid(row, .25)
                  and 45 <= (date.fromisoformat(row["expiration"]) - day).days <= 90
                  and .90 * signal_spot <= row["strike"] <= signal_spot]
    return min(candidates, key=lambda row: (abs((date.fromisoformat(row["expiration"]) - day).days - 60),
                                            abs(row["strike"] - .95 * signal_spot), row["ask"]), default=None)


def walls(rows, spot, day):
    nearby = [row for row in rows if 21 <= (date.fromisoformat(row["expiration"]) - day).days <= 45
              and isinstance(row.get("open_interest"), (int, float)) and row["open_interest"] > 0]
    calls = [row for row in nearby if row["type"] == "call" and spot <= row["strike"] <= 1.05 * spot]
    puts = [row for row in nearby if row["type"] == "put" and .95 * spot <= row["strike"] <= spot]
    call_wall = max(calls, key=lambda row: (row["open_interest"], -row["strike"]))["strike"] if calls else None
    put_wall = max(puts, key=lambda row: (row["open_interest"], row["strike"]))["strike"] if puts else None
    return call_wall, put_wall


def quote(row, at):
    option_class = CallQuote if row["type"] == "call" else PutQuote
    return option_class("SPY", date.fromisoformat(row["expiration"]), row["strike"],
                        at, row["bid"], row["ask"])


def marked_quote(book, contract, option_type, at):
    row = book.get((contract[1].isoformat(), contract[2], option_type))
    if row is None:
        raise ValueError(f"Missing {option_type} mark for {contract} on {at.date()}")
    return quote(row, at)


def metrics(nav):
    peak = CAPITAL
    drawdown = 0.0
    for value in nav:
        peak = max(peak, value)
        drawdown = max(drawdown, 1 - value / peak)
    return {"final_nav": round(nav[-1], 2), "net_pnl": round(nav[-1] - CAPITAL, 2),
            "account_return_pct": round(100 * (nav[-1] / CAPITAL - 1), 3),
            "max_daily_nav_drawdown_pct": round(100 * drawdown, 3)}


def replay_year(path, underlying, independent_prices):
    from collections import Counter

    valid_days = set()
    for day, _ in days_in_parquet(path):
        if get_market_session(date.fromisoformat(day)) is not None:
            valid_days.add(day)
    prices, audit = checked_underlying(underlying, independent_prices, valid_days)
    last_day = max(valid_days)
    ledgers = None
    nav = {name: [] for name in ("long_call", "pmcc", "pmcc_put")}
    dates = []
    counts = Counter()
    short_open_index = put_open_index = None
    prior_call_signal = None
    prior_put_wall = None
    prior_risk_signal = None
    session_index = -1
    for day_str, raw in days_in_parquet(path):
        day = date.fromisoformat(day_str)
        session = get_market_session(day)
        if session is None:
            counts["non_session_days"] += 1
            continue
        session_index += 1
        at = session.market_close
        spot = prices[day_str]["close"]
        rows = [row for row in raw if eligible(row)]
        book = {(row["expiration"], row["strike"], row["type"]): row for row in rows}
        if ledgers is None:
            long_row = choose_long(rows, spot, day)
            if long_row is None:
                return {"status": "invalid", "reason": f"No eligible long call on {day_str}"}
            initial_quote = quote(long_row, at)
            ledgers = {name: PMCCLedger.buy_long(initial_quote, capital=CAPITAL)
                       for name in nav}
            first_day = day_str
            long_entry = {"expiry": long_row["expiration"], "strike": long_row["strike"],
                          "ask": long_row["ask"],
                          "initial_debit": round(CAPITAL - ledgers["long_call"].cash, 2)}
        long_contract = ledgers["long_call"].long_contract
        long_quote = marked_quote(book, long_contract, "call", at)

        if ledgers["pmcc"].active_short is not None and spot >= ledgers["pmcc"].active_short[2]:
            counts["short_itm_observation_days"] += 1
        if ledgers["pmcc"].active_short is not None and (session_index - short_open_index >= 5 or day_str == last_day):
            for name in ("pmcc", "pmcc_put"):
                held = ledgers[name].active_short
                ledgers[name].close_short(marked_quote(book, held, "call", at))
            short_open_index = None
        if ledgers["pmcc_put"].active_put is not None and (session_index - put_open_index >= 20 or day_str == last_day):
            held = ledgers["pmcc_put"].active_put
            ledgers["pmcc_put"].close_put(marked_quote(book, held, "put", at))
            put_open_index = None

        if day_str != last_day and prior_call_signal and ledgers["pmcc"].active_short is None:
            signal_spot, wall = prior_call_signal
            short_row = (choose_short(rows, day, wall, long_contract)
                         if 0 <= wall - spot <= .01 * spot else None)
            if short_row is not None:
                short_quote = quote(short_row, at)
                for name in ("pmcc", "pmcc_put"):
                    ledgers[name].write_short(long_quote, short_quote, spot=signal_spot, call_wall=wall)
                short_open_index = session_index
                counts["short_entries"] += 1
        if day_str != last_day and prior_risk_signal and ledgers["pmcc_put"].active_put is None:
            signal_spot, signal_wall, signal_at = prior_risk_signal
            if put_wall_breach_signal(spot=signal_spot, put_wall=signal_wall,
                                      observed_at=signal_at, execution_at=at):
                put_row = choose_put(rows, day, signal_spot)
                if put_row is not None:
                    ledgers["pmcc_put"].buy_put(long_quote, quote(put_row, at))
                    put_open_index = session_index
                    counts["put_entries"] += 1

        if day_str == last_day:
            for ledger in ledgers.values():
                ledger.close_long(long_quote)
            dates.append(day_str)
            for name, ledger in ledgers.items():
                nav[name].append(ledger.cash)
            break

        dates.append(day_str)
        for name, ledger in ledgers.items():
            short_quote = (marked_quote(book, ledger.active_short, "call", at)
                           if ledger.active_short else None)
            put_quote = (marked_quote(book, ledger.active_put, "put", at)
                         if ledger.active_put else None)
            nav[name].append(ledger.liquidation_nav(long_quote, short_quote, put_quote)["nav"])

        call_wall, put_wall = walls(rows, spot, day)
        prior_call_signal = ((spot, call_wall) if call_wall is not None
                             and 0 <= call_wall - spot <= .01 * spot else None)
        prior_risk_signal = ((spot, prior_put_wall, at) if prior_put_wall is not None
                             and spot < prior_put_wall else None)
        prior_put_wall = put_wall

    overlay_pnl = {"short_calls": round(nav["pmcc"][-1] - nav["long_call"][-1], 2),
                   "conditional_puts": round(nav["pmcc_put"][-1] - nav["pmcc"][-1], 2)}
    return {"status": "exploratory_unverified_quotes", "first_day": first_day, "last_day": last_day,
            "sessions": len(nav["long_call"]), "long_entry": long_entry, "price_audit": audit,
            "counts": dict(counts), "variants": {**{name: metrics(values) for name, values in nav.items()},
                                                  "cash": metrics([CAPITAL])},
            "overlay_net_pnl": overlay_pnl,
            "daily_nav": {"dates": dates, **{name: [round(value, 2) for value in values]
                                           for name, values in nav.items()}}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--options", type=Path, nargs="+", required=True)
    parser.add_argument("--underlying", type=Path, required=True)
    parser.add_argument("--independent-prices", type=Path, default=Path("research/market_data/SPY.csv"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = {}
    for path in args.options:
        year = path.stem.rsplit("-", 1)[-1]
        try:
            results[year] = replay_year(path, args.underlying, args.independent_prices)
        except ValueError as error:
            results[year] = {"status": "invalid", "reason": str(error)}
    output = {"study": "exploratory_spy_not_executable_not_live_approved", "capital_per_year": CAPITAL,
              "source": "https://github.com/anahatsingh-ui/options-dataset-hist",
              "source_files": [{"path": str(path), "sha256": sha256(path)} for path in args.options],
              "underlying_sha256": sha256(args.underlying), "results": results,
              "limitations": ["Vendor and exact option quote timestamp unverified",
                              "Rules were designed after inspecting the public mirror; no untouched holdout",
                              "Call/Put Wall is a nearby max-open-interest proxy, not proven support/resistance",
                              "EOD signal and next-session EOD fills assume unverified executable quotes",
                              "American early assignment, ex-dividend, financing and corporate actions not simulated"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")
    print(json.dumps({year: {key: value for key, value in result.items() if key != "daily_nav"}
                      for year, result in results.items()}, indent=2))


if __name__ == "__main__":
    main()
