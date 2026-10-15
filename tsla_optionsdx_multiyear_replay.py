"""Exploratory TSLA seasonal PMCC/conditional-put replay on complete optionsDX years.

This is a price-resistance alternative, not an open-interest wall strategy.
EOD standing quotes are hypothetical fills; assignment and financing are omitted.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from market_calendar import get_market_session
from optionsdx_tsla_audit import good_quote, records
from pmcc_research import CallQuote, PMCCLedger, PutQuote


EASTERN = ZoneInfo("America/New_York")
CAPITAL = 100_000
SLOT_CAPITAL = CAPITAL / 6
ENTRY_MONTHS = (1, 2, 3, 6, 7, 8)
VARIANTS = ("long_call", "pmcc", "pmcc_put")


def sizes(value):
    try:
        bid_size, ask_size = value.split("x")
        return int(bid_size.strip()), int(ask_size.strip())
    except (AttributeError, ValueError):
        return 0, 0


def year_days(directory, year, *, window=None):
    if window is None:
        files = sorted(directory.glob(f"tsla_eod_{year}??.txt"))
        if len(files) != 12 or any(path.stat().st_size == 0 for path in files):
            raise ValueError(f"Incomplete monthly files for {year}")
    else:
        start, end = window
        if start.year != year or end.year != year or start > end:
            raise ValueError(f"Invalid contiguous window for {year}")
        files = [directory / f"tsla_eod_{year}{month:02d}.txt"
                 for month in range(start.month, end.month + 1)]
        if any(not path.is_file() or path.stat().st_size == 0 for path in files):
            raise ValueError(f"Missing monthly file within {start} to {end}")

    def check_gap(previous, current):
        if window is None:
            return
        expected = start if previous is None else previous + timedelta(days=1)
        while get_market_session(expected) is None:
            expected += timedelta(days=1)
        if expected != current:
            raise ValueError(f"Missing market session within window: {expected}")

    previous_day = None
    for path in files:
        day = None
        book = {}
        spot = None
        at = None
        with path.open(encoding="utf-8-sig", newline="") as stream:
            for raw in records(stream):
                quote_day = date.fromisoformat(raw["QUOTE_DATE"])
                if window is not None and (quote_day < start or quote_day > end
                                           or get_market_session(quote_day) is None):
                    continue
                timestamp = datetime.fromtimestamp(int(raw["QUOTE_UNIXTIME"]), EASTERN)
                if quote_day.year != year or quote_day.month != int(path.stem[-2:]):
                    raise ValueError(f"Wrong quote date in {path.name}: {quote_day}")
                if day is not None and quote_day != day:
                    if quote_day <= day or (previous_day and quote_day <= previous_day):
                        raise ValueError(f"Nonchronological quotes in {path.name}")
                    check_gap(previous_day, day)
                    yield day, at, spot, book
                    previous_day = day
                    book, spot = {}, None
                if quote_day != day and get_market_session(quote_day) is None:
                    raise ValueError(f"Non-market quote date: {quote_day}")
                day = quote_day
                at = timestamp
                current_spot = float(raw["UNDERLYING_LAST"])
                if spot is None:
                    spot = current_spot
                elif abs(current_spot / spot - 1) > .001:
                    raise ValueError(f"Inconsistent underlying spot on {day}")
                expiry = date.fromisoformat(raw["EXPIRE_DATE"])
                strike = float(raw["STRIKE"])
                for side in ("call", "put"):
                    prefix = "C" if side == "call" else "P"
                    bid, ask = raw[f"{prefix}_BID"], raw[f"{prefix}_ASK"]
                    if not good_quote(bid, ask):
                        continue
                    key = (expiry, strike, side)
                    row = {"type": side, "expiration": expiry, "strike": strike,
                           "bid": float(bid), "ask": float(ask),
                           "size": sizes(raw[f"{prefix}_SIZE"])}
                    if key in book and book[key] != row:
                        raise ValueError(f"Conflicting duplicate option {key} on {day}")
                    book[key] = row
        if day is not None:
            if previous_day and day <= previous_day:
                raise ValueError(f"Nonchronological monthly file: {path.name}")
            check_gap(previous_day, day)
            yield day, at, spot, book
            previous_day = day
    if window is not None and previous_day != end:
        raise ValueError(f"Missing final market session within window: {end}")


def liquid(row, max_spread):
    return (row["bid"] > .01 and row["size"][0] > 0 and row["size"][1] > 0
            and (row["ask"] - row["bid"]) / row["ask"] <= max_spread)


def choose_long(book, spot, day, maturity):
    candidates = [row for row in book.values() if row["type"] == "call" and liquid(row, .10)
                  and row["expiration"].month == 1 and row["expiration"].year >= day.year + 1
                  and .70 * spot <= row["strike"] <= .85 * spot
                  and (maturity == "next_january" and row["expiration"].year == day.year + 1
                       or maturity == "long_january" and (row["expiration"] - day).days >= 365)]
    return min(candidates, key=lambda row: (row["expiration"],
                                            abs(row["strike"] - .80 * spot), row["ask"]), default=None)


def choose_short(book, day, resistance, long_contract):
    candidates = [row for row in book.values() if row["type"] == "call" and liquid(row, .25)
                  and 21 <= (row["expiration"] - day).days <= 45
                  and row["expiration"] < long_contract[1]
                  and row["strike"] >= resistance and row["strike"] > long_contract[2]]
    return min(candidates, key=lambda row: (abs((row["expiration"] - day).days - 30),
                                            row["strike"] - resistance), default=None)


def choose_put(book, day, signal_spot):
    candidates = [row for row in book.values() if row["type"] == "put" and liquid(row, .25)
                  and 45 <= (row["expiration"] - day).days <= 90
                  and .90 * signal_spot <= row["strike"] <= signal_spot]
    return min(candidates, key=lambda row: (abs((row["expiration"] - day).days - 60),
                                            abs(row["strike"] - .95 * signal_spot), row["ask"]), default=None)


def quote(row, at):
    cls = CallQuote if row["type"] == "call" else PutQuote
    return cls("TSLA", row["expiration"], row["strike"], at, row["bid"], row["ask"])


def held_quote(book, contract, side, at):
    row = book.get((contract[1], contract[2], side))
    if row is None:
        raise ValueError(f"Missing held {side} quote on {at.date()}: {contract}")
    return quote(row, at)


def metrics(nav):
    peak = CAPITAL
    max_dd = 0.0
    for value in nav:
        peak = max(peak, value)
        max_dd = max(max_dd, 1 - value / peak)
    return {"final_nav": round(nav[-1], 2), "account_return_pct": round(100 * (nav[-1] / CAPITAL - 1), 3),
            "max_daily_nav_drawdown_pct": round(100 * max_dd, 3)}


def replay_year(directory, audit, year, entry_policy, maturity):
    status = audit["years"].get(str(year))
    if not status or not status["complete_market_year"]:
        raise ValueError(f"Reject incomplete source year {year}")
    slots = [None] * 6
    stock_cash = [SLOT_CAPITAL] * 6
    nav = {name: [] for name in (*VARIANTS, "stock", "cash")}
    counts = Counter()
    entries = []
    dates = []
    history = []
    pending_dip = pending_call = pending_put = None
    seen_tradable_months = set()
    session_index = -1
    for day, at, spot, book in year_days(directory, year):
        session_index += 1
        session = get_market_session(day)
        tradable = at <= session.market_close
        if not tradable:
            counts["after_market_close_vendor_quote_days"] += 1
        month_first = tradable and day.month not in seen_tradable_months
        if tradable:
            seen_tradable_months.add(day.month)
        # Exit on the last market session of the cycle month, not calendar month-end.
        next_session_day = day
        while True:
            next_session_day += timedelta(days=1)
            if get_market_session(next_session_day) is not None:
                break
        cycle_exit = day.month in (6, 12) and next_session_day.month != day.month
        for slot_index, month in enumerate(ENTRY_MONTHS):
            if slots[slot_index] is not None or day.month != month or not tradable:
                continue
            enter = month_first if entry_policy == "month_first" else bool(pending_dip and pending_dip[0] == month)
            if not enter:
                continue
            long_row = choose_long(book, spot, day, maturity)
            if long_row is None:
                counts["no_eligible_long_call"] += 1
                continue
            try:
                ledgers = {name: PMCCLedger.buy_long(quote(long_row, at), capital=SLOT_CAPITAL)
                           for name in VARIANTS}
            except ValueError as error:
                if "Insufficient cash" not in str(error):
                    raise
                counts["long_call_over_slot_budget"] += 1
                continue
            slots[slot_index] = {"ledgers": ledgers, "entered": day, "closed": False,
                                 "short_open_index": None, "put_open_index": None,
                                 "stock_shares": (SLOT_CAPITAL * (1 - .0005)) / spot}
            entries.append({"month": month, "date": day.isoformat(),
                            "long_expiry": long_row["expiration"].isoformat(),
                            "long_dte": (long_row["expiration"] - day).days,
                            "long_debit": round(SLOT_CAPITAL - ledgers["long_call"].cash, 2)})
            counts["long_entries"] += 1
        for slot_index, state in enumerate(slots):
            if state is None or state["closed"]:
                continue
            ledgers = state["ledgers"]
            long_contract = ledgers["long_call"].long_contract
            long_mark = held_quote(book, long_contract, "call", at)
            if ledgers["pmcc"].active_short and spot >= ledgers["pmcc"].active_short[2]:
                counts["short_itm_observation_days"] += 1
            should_exit = cycle_exit and ((slot_index < 3 and day.month == 6)
                                          or (slot_index >= 3 and day.month == 12))
            if ledgers["pmcc"].active_short and ((tradable and session_index - state["short_open_index"] >= 5)
                                                    or should_exit):
                for name in ("pmcc", "pmcc_put"):
                    ledgers[name].close_short(held_quote(book, ledgers[name].active_short, "call", at))
                state["short_open_index"] = None
            if ledgers["pmcc_put"].active_put and ((tradable and session_index - state["put_open_index"] >= 20)
                                                     or should_exit):
                ledgers["pmcc_put"].close_put(held_quote(book, ledgers["pmcc_put"].active_put, "put", at))
                state["put_open_index"] = None
            if should_exit:
                for ledger in ledgers.values():
                    ledger.close_long(long_mark)
                stock_cash[slot_index] = state["stock_shares"] * spot * (1 - .0005)
                state["closed"] = True
                continue
            if tradable and pending_call and ledgers["pmcc"].active_short is None:
                signal_spot, resistance = pending_call
                if 0 <= resistance - spot <= .01 * spot:
                    row = choose_short(book, day, resistance, long_contract)
                    if row is not None:
                        for name in ("pmcc", "pmcc_put"):
                            ledgers[name].write_short(long_mark, quote(row, at), spot=signal_spot,
                                                      call_wall=resistance)
                        state["short_open_index"] = session_index
                        counts["short_entries"] += 1
            if tradable and pending_put and ledgers["pmcc_put"].active_put is None:
                row = choose_put(book, day, pending_put)
                if row is not None:
                    try:
                        ledgers["pmcc_put"].buy_put(long_mark, quote(row, at))
                    except ValueError as error:
                        if "Insufficient cash" not in str(error):
                            raise
                        counts["put_over_slot_budget"] += 1
                    else:
                        state["put_open_index"] = session_index
                        counts["put_entries"] += 1
        dates.append(day.isoformat())
        for name in VARIANTS:
            total = 0.0
            for state in slots:
                if state is None:
                    total += SLOT_CAPITAL
                    continue
                ledger = state["ledgers"][name]
                if state["closed"]:
                    total += ledger.cash
                    continue
                long_mark = held_quote(book, ledger.long_contract, "call", at)
                short_mark = held_quote(book, ledger.active_short, "call", at) if ledger.active_short else None
                put_mark = held_quote(book, ledger.active_put, "put", at) if ledger.active_put else None
                total += ledger.liquidation_nav(long_mark, short_mark, put_mark)["nav"]
            nav[name].append(total)
        nav["stock"].append(sum(stock_cash[index] if state is None or state["closed"]
                                else state["stock_shares"] * spot * (1 - .0005)
                                for index, state in enumerate(slots)))
        nav["cash"].append(CAPITAL)
        if len(history) >= 20:
            previous_high = max(history[-20:])
            previous_low = min(history[-20:])
            pending_dip = (day.month, spot) if spot <= .90 * previous_high else None
            pending_call = (spot, previous_high) if 0 <= previous_high - spot <= .01 * spot else None
            pending_put = spot if spot < previous_low else None
        else:
            pending_dip = pending_call = pending_put = None
        history.append(spot)
    if not dates or any(state and not state["closed"] for state in slots):
        raise ValueError(f"Unclosed positions or no dates in {year}")
    return {"year": year, "status": "exploratory_price_resistance_not_oi_wall", "sessions": len(dates),
            "entry_policy": entry_policy, "maturity": maturity, "entries": entries,
            "counts": dict(counts), "variants": {name: metrics(values) for name, values in nav.items()},
            "overlay_net_pnl": {"short_calls": round(nav["pmcc"][-1] - nav["long_call"][-1], 2),
                                "conditional_puts": round(nav["pmcc_put"][-1] - nav["pmcc"][-1], 2)},
            "daily_nav": {"dates": dates, **{name: [round(value, 2) for value in values]
                                               for name, values in nav.items()}}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("research/raw_options/optionsdx"))
    parser.add_argument("--audit", type=Path, default=Path("research/optionsdx_tsla_multiyear_audit.json"))
    parser.add_argument("--years", type=int, nargs="+", default=[2019, 2023])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    audit = json.loads(args.audit.read_text())
    results = {}
    for year in args.years:
        for entry in ("month_first", "dip_10"):
            for maturity in ("next_january", "long_january"):
                key = f"{year}_{entry}_{maturity}"
                try:
                    results[key] = replay_year(args.directory, audit, year, entry, maturity)
                except ValueError as error:
                    results[key] = {"status": "invalid", "reason": str(error)}
    output = {"study": "exploratory_tsla_optionsdx_price_resistance_not_oi_wall",
              "source": "user-provided optionsDX TSLA EOD files", "capital": CAPITAL,
              "results": results,
              "limitations": ["No open interest; 20-day price highs/lows replace OI walls as a different strategy",
                              "Only complete 2019 and 2023 years are eligible from the supplied archives",
                              "EOD standing bid/ask does not prove fills, especially on early-close dates",
                              "American early assignment, financing, taxes and split-adjusted deliverables omitted",
                              "Rules were formulated after stock and 2022-option research; no untouched holdout"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")
    print(json.dumps({key: {k: v for k, v in value.items() if k != "daily_nav"}
                      for key, value in results.items()}, indent=2))


if __name__ == "__main__":
    main()
