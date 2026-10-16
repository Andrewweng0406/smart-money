"""Within-year continuous TSLA LEAPS replay; open positions are marked, not sold."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from market_calendar import get_market_session
from pmcc_research import PMCCLedger
from tsla_optionsdx_multiyear_replay import (
    CAPITAL, ENTRY_MONTHS, choose_long, choose_put, choose_short, liquid,
    held_quote, metrics, quote, year_days,
)

VARIANTS = ("long_call", "pmcc", "pmcc_put", "pmcc_put_reinvest")
FUNDED_VARIANT = "pmcc_premium_put"
ROLL_DTE = 120
SPLIT_DATES = {2020: date(2020, 8, 31), 2022: date(2022, 8, 25)}


def choose_weekly_put(book, day, signal_spot, target_ratio=.95):
    candidates = [row for row in book.values() if row["type"] == "put" and liquid(row, .25)
                  and 3 <= (row["expiration"] - day).days <= 9
                  and .90 * signal_spot <= row["strike"] <= signal_spot]
    return min(candidates, key=lambda row: ((row["expiration"] - day).days,
                                            abs(row["strike"] - target_ratio * signal_spot), row["ask"]),
               default=None)


def last_normal_exit_before_expiry(day, expiry):
    next_day = day + timedelta(days=1)
    while get_market_session(next_day) is None:
        next_day += timedelta(days=1)
    session = get_market_session(next_day)
    return next_day >= expiry or session.market_close.hour < 16


def funding_after_short_write(balance, credit, policy):
    return balance + credit if policy == "gross" else balance


def funding_after_short_close(balance, open_credit, buyback_cost, policy):
    return balance + (open_credit if policy == "realized" else 0) - buyback_cost


def premium_can_fund_put(balance, debit):
    return balance + 1e-8 >= debit


def put_liquidation_proceeds(ledger, put_quote):
    if put_quote.bid <= ledger.slippage_per_share:
        return 0.
    return ledger.put_quantity * ((put_quote.bid - ledger.slippage_per_share) * 100
                                  - ledger.fee_per_contract)


def start_put_trade(state, row, day, session_index, spot, signal_spot):
    state["put_trade"] = {
        "entry_date": day.isoformat(), "entry_session_index": session_index,
        "expiry": row["expiration"].isoformat(), "strike": row["strike"],
        "entry_spot": spot, "signal_spot": signal_spot,
        "debit": state["put_debit"], "min_spot": spot,
        "max_mark_proceeds": 0., "max_mark_date": day.isoformat(),
    }


def update_put_trade(state, ledger, put_quote, day, spot):
    trade = state.get("put_trade")
    if trade is None:
        return
    proceeds = put_liquidation_proceeds(ledger, put_quote)
    trade["min_spot"] = min(trade["min_spot"], spot)
    if proceeds > trade["max_mark_proceeds"]:
        trade["max_mark_proceeds"] = proceeds
        trade["max_mark_date"] = day.isoformat()


def finish_put_trade(state, name, slot, day, session_index, spot, proceeds, reason):
    trade = state.get("put_trade")
    if trade is None:
        return {"date": day.isoformat(), "action": "put_close", "variant": name,
                "slot": slot, "realized_pnl": round(proceeds - state["put_debit"], 2),
                "exit_reason": reason}
    debit = trade["debit"]
    pnl = proceeds - debit
    max_pnl = trade["max_mark_proceeds"] - debit
    event = {
        "date": day.isoformat(), "action": "put_close", "variant": name, "slot": slot,
        "entry_date": trade["entry_date"], "expiry": trade["expiry"],
        "strike": trade["strike"], "signal_spot": round(trade["signal_spot"], 4),
        "entry_spot": round(trade["entry_spot"], 4), "exit_spot": round(spot, 4),
        "min_spot": round(trade["min_spot"], 4), "debit": round(debit, 2),
        "proceeds": round(proceeds, 2), "realized_pnl": round(pnl, 2),
        "return_pct": round(100 * pnl / debit, 3),
        "hold_sessions": session_index - trade["entry_session_index"],
        "max_mark_proceeds": round(trade["max_mark_proceeds"], 2),
        "max_unrealized_pnl": round(max_pnl, 2),
        "max_return_pct": round(100 * max_pnl / debit, 3),
        "max_mark_date": trade["max_mark_date"],
        "underlying_return_pct": round(100 * (spot / trade["entry_spot"] - 1), 3),
        "max_underlying_drawdown_pct": round(100 * (trade["min_spot"] / trade["entry_spot"] - 1), 3),
        "exit_reason": reason,
    }
    state["put_trade"] = None
    return event


def short_buyback_cost(ledger, row):
    ask = row["ask"] if isinstance(row, dict) else row.ask
    return ledger.quantity * ((ask + ledger.slippage_per_share) * 100
                              + ledger.fee_per_contract)


def reserved_cash_available(ledger, book, at, debit, reserve):
    short_cost = 0.
    if ledger.active_short is not None:
        short_cost = short_buyback_cost(
            ledger, held_quote(book, ledger.active_short, "call", at))
    return ledger.cash - debit - short_cost + 1e-8 >= reserve


def available_premium(slots, state, scope):
    if scope == "account":
        return sum(item["funding_balance"] for item in slots if item is not None)
    return state["funding_balance"]


def put_risk_signal(spot, high, low, policy):
    if policy == "break20_low":
        return spot < low
    if policy == "drawdown5":
        return spot <= .95 * high
    raise ValueError(f"Unknown risk signal policy: {policy}")


def open_long(row, at, capital):
    return PMCCLedger.buy_long(quote(row, at), capital=capital)


def roll_long(ledger, book, spot, day, at):
    replacement = choose_long(book, spot, day, "long_january")
    if replacement is None or replacement["expiration"] <= ledger.long_contract[1]:
        return None
    old_quote = held_quote(book, ledger.long_contract, "call", at)
    cash = ledger.cash + ledger.quantity * ((old_quote.bid - ledger.slippage_per_share) * 100
                                          - ledger.fee_per_contract)
    debit = (replacement["ask"] + ledger.slippage_per_share) * 100 + ledger.fee_per_contract
    if cash < debit:
        return None
    ledger.close_long(old_quote)
    return open_long(replacement, at, ledger.cash)


def replay_year(directory, audit, year, entry_policy="month_first", put_policy="term",
                funding_policy="none", put_target_ratio=.95,
                risk_signal_policy="break20_low", funding_scope="slot", window=None,
                entry_months=ENTRY_MONTHS, reserve_pct=0., put_hold_sessions=None,
                put_take_profit_pct=None):
    if put_policy not in ("term", "weekly"):
        raise ValueError(f"Unknown put policy: {put_policy}")
    if funding_policy not in ("none", "gross", "realized"):
        raise ValueError(f"Unknown funding policy: {funding_policy}")
    if not .90 <= put_target_ratio <= 1.0:
        raise ValueError(f"Invalid put target ratio: {put_target_ratio}")
    if risk_signal_policy not in ("break20_low", "drawdown5"):
        raise ValueError(f"Unknown risk signal policy: {risk_signal_policy}")
    if funding_scope not in ("slot", "account"):
        raise ValueError(f"Unknown funding scope: {funding_scope}")
    entry_months = tuple(entry_months)
    if not entry_months or len(set(entry_months)) != len(entry_months) or any(
            month not in ENTRY_MONTHS for month in entry_months):
        raise ValueError(f"Invalid entry months: {entry_months}")
    if not 0 <= reserve_pct < 1:
        raise ValueError(f"Invalid reserve percent: {reserve_pct}")
    if put_hold_sessions is None:
        put_hold_sessions = 4 if put_policy == "weekly" else 20
    if put_hold_sessions < 1:
        raise ValueError(f"Invalid put hold sessions: {put_hold_sessions}")
    if put_take_profit_pct is not None and put_take_profit_pct <= 0:
        raise ValueError(f"Invalid put take profit percent: {put_take_profit_pct}")
    slot_capital = CAPITAL / len(entry_months)
    reserve = slot_capital * reserve_pct
    status = audit["years"].get(str(year))
    if not status or window is None and not status["complete_market_year"]:
        raise ValueError(f"Reject incomplete source year {year}")
    if window is not None:
        start, end = window
        split = SPLIT_DATES.get(year)
        if split is not None and start < split <= end:
            raise ValueError(f"Window crosses unmodeled option split: {split}")
    active_variants = (*VARIANTS, FUNDED_VARIANT) if funding_policy != "none" else VARIANTS
    portfolios = {name: [None] * len(entry_months) for name in active_variants}
    stock = [None] * len(entry_months)
    nav = {name: [] for name in (*active_variants, "stock")}
    counts = Counter()
    events = []
    history = []
    pending = (None, None, None)
    months_seen = set()
    dates = []
    account_budget_min = 0.
    lowest_cash = {name: None for name in active_variants}
    negative_cash_sessions = {name: 0 for name in active_variants}
    reserve_breach_sessions = {name: 0 for name in active_variants}
    for session_index, (day, at, spot, book) in enumerate(year_days(directory, year, window=window)):
        tradable = at <= get_market_session(day).market_close
        first = tradable and day.month not in months_seen
        if tradable:
            months_seen.add(day.month)
        pending_dip, pending_call, pending_put = pending
        for index, month in enumerate(entry_months):
            if portfolios["long_call"][index] is not None or day.month != month or not tradable:
                continue
            enter = first if entry_policy == "month_first" else pending_dip == month
            if not enter:
                continue
            row = choose_long(book, spot, day, "long_january")
            if row is None:
                counts["no_eligible_call"] += 1
                continue
            debit = (row["ask"] + .01) * 100 + .65
            if debit > slot_capital:
                counts["call_over_budget"] += 1
                continue
            if debit > slot_capital - reserve:
                counts["call_reserve_blocked"] += 1
                continue
            for name in active_variants:
                portfolios[name][index] = {"ledger": open_long(row, at, slot_capital),
                                           "short_index": None, "put_index": None,
                                           "put_debit": 0., "put_profit_available": 0.,
                                           "put_trade": None,
                                           "extra": None, "short_credit": 0.,
                                           "funding_balance": 0., "funding_min": 0.,
                                           "short_credits": 0., "short_buybacks": 0.,
                                           "put_debits": 0., "put_proceeds": 0.}
            stock[index] = (slot_capital * (1 - .0005)) / spot
            events.append({"date": day.isoformat(), "action": "entry", "month": month,
                           "expiry": row["expiration"].isoformat(), "debit": round(debit, 2)})
            counts["entries"] += 1
        for name, slots in portfolios.items():
            deferred_puts = []
            for index, state in enumerate(slots):
                if state is None:
                    continue
                ledger = state["ledger"]
                long_mark = held_quote(book, ledger.long_contract, "call", at)
                if ledger.active_put:
                    update_put_trade(state, ledger,
                                     held_quote(book, ledger.active_put, "put", at), day, spot)
                if ledger.active_short and tradable and session_index - state["short_index"] >= 5:
                    before = ledger.cash
                    ledger.close_short(held_quote(book, ledger.active_short, "call", at))
                    if name == FUNDED_VARIANT:
                        cost = before - ledger.cash
                        state["short_buybacks"] += cost
                        state["funding_balance"] = funding_after_short_close(
                            state["funding_balance"], state["short_credit"], cost, funding_policy)
                        state["short_credit"] = 0.
                    state["short_index"] = None
                    counts[f"{name}_short_closes"] += 1
                take_profit = (ledger.active_put and put_take_profit_pct is not None and
                               state["put_trade"]["max_mark_date"] == day.isoformat() and
                               state["put_trade"]["max_mark_proceeds"] >=
                               state["put_debit"] * (1 + put_take_profit_pct))
                close_put = (ledger.active_put and tradable and
                             (session_index - state["put_index"] >= put_hold_sessions
                              or take_profit
                              or put_policy == "weekly" and
                              last_normal_exit_before_expiry(day, ledger.active_put[1])))
                if close_put:
                    put_mark = held_quote(book, ledger.active_put, "put", at)
                    before = ledger.cash
                    ledger.close_put(put_mark)
                    proceeds = ledger.cash - before
                    profit = proceeds - state["put_debit"]
                    if name == FUNDED_VARIANT:
                        state["put_proceeds"] += proceeds
                        state["funding_balance"] += proceeds
                    state["put_profit_available"] += profit
                    events.append(finish_put_trade(state, name, index, day, session_index,
                                                   spot, proceeds, "take_profit" if take_profit else
                                                   "scheduled_or_pre_expiry"))
                    state["put_debit"] = 0.
                    state["put_index"] = None
                    counts[f"{name}_put_closes"] += 1
                if tradable and (ledger.long_contract[1] - day).days <= ROLL_DTE:
                    if ledger.active_short:
                        before = ledger.cash
                        ledger.close_short(held_quote(book, ledger.active_short, "call", at))
                        if name == FUNDED_VARIANT:
                            cost = before - ledger.cash
                            state["short_buybacks"] += cost
                            state["funding_balance"] = funding_after_short_close(
                                state["funding_balance"], state["short_credit"], cost, funding_policy)
                            state["short_credit"] = 0.
                        state["short_index"] = None
                    if ledger.active_put:
                        put_mark = held_quote(book, ledger.active_put, "put", at)
                        before = ledger.cash
                        ledger.close_put(put_mark)
                        proceeds = ledger.cash - before
                        state["put_profit_available"] += proceeds - state["put_debit"]
                        if name == FUNDED_VARIANT:
                            state["put_proceeds"] += proceeds
                            state["funding_balance"] += proceeds
                        events.append(finish_put_trade(state, name, index, day, session_index,
                                                       spot, proceeds, "long_call_roll"))
                        state["put_debit"] = 0.
                        state["put_index"] = None
                    old_expiry = ledger.long_contract[1]
                    replacement = roll_long(ledger, book, spot, day, at)
                    if replacement is None:
                        counts[f"{name}_roll_unavailable"] += 1
                    else:
                        state["ledger"] = ledger = replacement
                        long_mark = held_quote(book, ledger.long_contract, "call", at)
                        counts[f"{name}_rolls"] += 1
                        events.append({"date": day.isoformat(), "action": "roll", "variant": name,
                                       "slot": index, "from": old_expiry.isoformat(),
                                       "to": ledger.long_contract[1].isoformat()})
                if name != "long_call" and tradable and pending_call and ledger.active_short is None:
                    signal_spot, resistance = pending_call
                    if 0 <= resistance - spot <= .01 * spot:
                        row = choose_short(book, day, resistance, ledger.long_contract)
                        if row is not None:
                            if reserve and ledger.cash + ledger.quantity * (
                                    (row["bid"] - ledger.slippage_per_share) * 100
                                    - ledger.fee_per_contract) - short_buyback_cost(ledger, row) < reserve:
                                counts[f"{name}_short_reserve_blocked"] += 1
                            else:
                                before = ledger.cash
                                ledger.write_short(long_mark, quote(row, at), spot=signal_spot,
                                                   call_wall=resistance)
                                if name == FUNDED_VARIANT:
                                    credit = ledger.cash - before
                                    state["short_credit"] = credit
                                    state["short_credits"] += credit
                                    state["funding_balance"] = funding_after_short_write(
                                        state["funding_balance"], credit, funding_policy)
                                state["short_index"] = session_index
                                counts[f"{name}_short_entries"] += 1
                if name in ("pmcc_put", "pmcc_put_reinvest", FUNDED_VARIANT) and tradable and pending_put and ledger.active_put is None:
                    row = (choose_weekly_put(book, day, pending_put, put_target_ratio) if put_policy == "weekly"
                           else choose_put(book, day, pending_put))
                    if row is not None:
                        debit = (row["ask"] + ledger.slippage_per_share) * 100 + ledger.fee_per_contract
                        if name == FUNDED_VARIANT and funding_scope == "account":
                            deferred_puts.append((index, state, ledger, long_mark, row, debit))
                        elif name == FUNDED_VARIANT and not premium_can_fund_put(
                                available_premium(slots, state, funding_scope), debit):
                            counts["risk_signals_without_funded_put"] += 1
                        elif reserve and not reserved_cash_available(ledger, book, at, debit, reserve):
                            counts[f"{name}_put_reserve_blocked"] += 1
                        else:
                            before = ledger.cash
                            try:
                                ledger.buy_put(long_mark, quote(row, at))
                            except ValueError as error:
                                if "Insufficient cash" not in str(error):
                                    raise
                                counts[f"{name}_put_over_budget"] += 1
                            else:
                                state["put_debit"] = before - ledger.cash
                                if name == FUNDED_VARIANT:
                                    state["put_debits"] += state["put_debit"]
                                    state["funding_balance"] -= state["put_debit"]
                                state["put_index"] = session_index
                                start_put_trade(state, row, day, session_index, spot, pending_put)
                                counts[f"{name}_put_entries"] += 1
                if name == "pmcc_put_reinvest" and tradable and state["extra"] is None:
                    row = choose_long(book, spot, day, "long_january")
                    if row is not None:
                        debit = (row["ask"] + .01) * 100 + .65
                        if state["put_profit_available"] >= debit and ledger.cash >= debit:
                            ledger.cash -= debit
                            state["put_profit_available"] -= debit
                            state["extra"] = open_long(row, at, debit)
                            counts["extra_calls_from_realized_put_profit"] += 1
                            events.append({"date": day.isoformat(), "action": "put_profit_call",
                                           "slot": index, "debit": round(debit, 2)})
                extra = state["extra"]
                if extra is not None and tradable and (extra.long_contract[1] - day).days <= ROLL_DTE:
                    replacement = roll_long(extra, book, spot, day, at)
                    if replacement is not None:
                        state["extra"] = replacement
                        counts["extra_call_rolls"] += 1
                if name == FUNDED_VARIANT:
                    state["funding_min"] = min(state["funding_min"], state["funding_balance"])
            if name == FUNDED_VARIANT and funding_scope == "account":
                for index, state, ledger, long_mark, row, debit in deferred_puts:
                    available = available_premium(slots, state, funding_scope)
                    if not premium_can_fund_put(available, debit):
                        counts["risk_signals_without_funded_put"] += 1
                        continue
                    if reserve and not reserved_cash_available(ledger, book, at, debit, reserve):
                        counts["pmcc_premium_put_put_reserve_blocked"] += 1
                        continue
                    before = ledger.cash
                    try:
                        ledger.buy_put(long_mark, quote(row, at))
                    except ValueError as error:
                        if "Insufficient cash" not in str(error):
                            raise
                        counts["pmcc_premium_put_put_over_budget"] += 1
                        continue
                    state["put_debit"] = before - ledger.cash
                    state["put_debits"] += state["put_debit"]
                    state["funding_balance"] -= state["put_debit"]
                    state["funding_min"] = min(state["funding_min"], state["funding_balance"])
                    state["put_index"] = session_index
                    start_put_trade(state, row, day, session_index, spot, pending_put)
                    counts["pmcc_premium_put_put_entries"] += 1
            if name == FUNDED_VARIANT:
                account_budget_min = min(account_budget_min, sum(
                    state["funding_balance"] for state in slots if state is not None))
        dates.append(day.isoformat())
        for name, slots in portfolios.items():
            total = 0.
            for state in slots:
                if state is None:
                    total += slot_capital
                    continue
                ledger = state["ledger"]
                lowest_cash[name] = (ledger.cash if lowest_cash[name] is None else
                                     min(lowest_cash[name], ledger.cash))
                if ledger.cash < -1e-8:
                    negative_cash_sessions[name] += 1
                long_mark = held_quote(book, ledger.long_contract, "call", at)
                short_mark = held_quote(book, ledger.active_short, "call", at) if ledger.active_short else None
                if reserve and ledger.cash - (short_buyback_cost(ledger, short_mark)
                                               if short_mark else 0) < reserve - 1e-8:
                    reserve_breach_sessions[name] += 1
                put_mark = held_quote(book, ledger.active_put, "put", at) if ledger.active_put else None
                total += ledger.liquidation_nav(long_mark, short_mark, put_mark)["nav"]
                extra = state["extra"]
                if extra is not None:
                    total += extra.liquidation_nav(held_quote(book, extra.long_contract, "call", at))["nav"]
            nav[name].append(total)
        nav["stock"].append(sum(slot_capital if shares is None else shares * spot * (1 - .0005)
                                for shares in stock))
        if len(history) >= 20:
            high, low = max(history[-20:]), min(history[-20:])
            pending = (day.month if spot <= .90 * high else None,
                       (spot, high) if 0 <= high - spot <= .01 * spot else None,
                       spot if put_risk_signal(spot, high, low, risk_signal_policy) else None)
        else:
            pending = (None, None, None)
        history.append(spot)
    if not dates:
        raise ValueError("No market sessions")
    open_positions = {name: sum(state is not None for state in slots)
                      for name, slots in portfolios.items()}
    funding = None
    if funding_policy != "none":
        funded = [state for state in portfolios[FUNDED_VARIANT] if state is not None]
        funding = {"policy": funding_policy, "scope": funding_scope,
                   "short_credits": round(sum(state["short_credits"] for state in funded), 2),
                   "short_buybacks": round(sum(state["short_buybacks"] for state in funded), 2),
                   "put_debits": round(sum(state["put_debits"] for state in funded), 2),
                   "put_proceeds": round(sum(state["put_proceeds"] for state in funded), 2),
                   "year_end_budget_balance": round(sum(state["funding_balance"] for state in funded), 2),
                   "worst_slot_budget_deficit": round(min((state["funding_min"] for state in funded),
                                                         default=0), 2),
                   "worst_account_budget_deficit": round(account_budget_min, 2),
                   "year_end_open_short_calls": sum(state["ledger"].active_short is not None
                                                    for state in funded)}
    return {"year": year, "entry_policy": entry_policy, "put_policy": put_policy,
            "capital": CAPITAL, "entry_months": entry_months,
            "slot_capital": round(slot_capital, 2), "reserve_pct": reserve_pct,
            "reserve_per_slot": round(reserve, 2),
            "worst_slot_cash": {name: round(value, 2) if value is not None else None
                                for name, value in lowest_cash.items()},
            "negative_cash_slot_sessions": negative_cash_sessions,
            "reserve_breach_slot_sessions": reserve_breach_sessions,
            "window": {"start": window[0].isoformat(), "end": window[1].isoformat()} if window else None,
            "put_target_ratio": put_target_ratio,
            "risk_signal_policy": risk_signal_policy,
            "funding_policy": funding_policy, "funding": funding,
            "funding_scope": funding_scope,
            "put_hold_sessions": put_hold_sessions,
            "put_take_profit_pct": put_take_profit_pct,
            "sessions": len(dates),
            "status": ("partial_contiguous_window_bid_marked_not_annual_return" if window else
                       "exploratory_open_positions_marked_at_bid_not_realized"),
            "rule": {"roll_at_or_below_dte": ROLL_DTE, "call_maturity": "earliest January >=365 DTE",
                     "put_dte": "3-9" if put_policy == "weekly" else "45-90",
                     "weekly_put_strike_target_pct_of_signal_spot": round(put_target_ratio * 100, 2),
                     "put_max_hold_sessions": put_hold_sessions,
                     "put_take_profit_pct": put_take_profit_pct,
                     "put_risk_signal": risk_signal_policy,
                     "put_profit_extra_call": "one per slot, only after cumulative realized put profit covers ask debit"},
            "counts": dict(counts), "open_positions_year_end": open_positions,
            "variants": {name: metrics(values) for name, values in nav.items()},
            "events": events,
            "daily_nav": {"dates": dates, **{name: [round(value, 2) for value in values]
                                               for name, values in nav.items()}}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("research/raw_options/optionsdx"))
    parser.add_argument("--audit", type=Path, default=Path("research/optionsdx_tsla_multiyear_audit.json"))
    parser.add_argument("--output", type=Path, default=Path("research/tsla_optionsdx_continuous.json"))
    parser.add_argument("--put-policy", choices=("term", "weekly"), default="term")
    parser.add_argument("--funding-policy", choices=("none", "gross", "realized"), default="none")
    parser.add_argument("--put-target-ratio", type=float, default=.95)
    parser.add_argument("--years", type=int, nargs="+", default=[2019, 2023])
    parser.add_argument("--window-start", type=date.fromisoformat)
    parser.add_argument("--window-end", type=date.fromisoformat)
    parser.add_argument("--risk-signal-policy", choices=("break20_low", "drawdown5"),
                        default="break20_low")
    parser.add_argument("--funding-scope", choices=("slot", "account"), default="slot")
    parser.add_argument("--entry-months", type=int, nargs="+", default=list(ENTRY_MONTHS))
    parser.add_argument("--reserve-pct", type=float, default=0.)
    parser.add_argument("--put-hold-sessions", type=int)
    parser.add_argument("--put-take-profit-pct", type=float)
    args = parser.parse_args()
    if (args.window_start is None) != (args.window_end is None):
        parser.error("--window-start and --window-end must be given together")
    window = (args.window_start, args.window_end) if args.window_start else None
    if window and len(args.years) != 1:
        parser.error("A contiguous window requires exactly one --years value")
    audit = json.loads(args.audit.read_text())
    results = {}
    for year in args.years:
        for entry in ("month_first", "dip_10"):
            results[f"{year}_{entry}"] = replay_year(args.directory, audit, year, entry,
                                                     args.put_policy, args.funding_policy,
                                                     args.put_target_ratio, args.risk_signal_policy,
                                                     args.funding_scope, window=window,
                                                     entry_months=args.entry_months,
                                                     reserve_pct=args.reserve_pct,
                                                     put_hold_sessions=args.put_hold_sessions,
                                                     put_take_profit_pct=args.put_take_profit_pct)
    output = {"study": ("contiguous_partial_window_exploratory" if window else
                        "within_year_continuous_long_call_exploratory"),
              "put_policy": args.put_policy,
              "funding_policy": args.funding_policy,
              "put_target_ratio": args.put_target_ratio,
              "risk_signal_policy": args.risk_signal_policy,
              "funding_scope": args.funding_scope,
              "entry_months": args.entry_months, "reserve_pct": args.reserve_pct,
              "put_hold_sessions": args.put_hold_sessions,
              "put_take_profit_pct": args.put_take_profit_pct,
              "capital": CAPITAL, "source": "user-provided optionsDX EOD quotes",
              "limitations": ["Window returns are not annual returns or comparable to complete years" if window else
                              "Only 2019 and 2023 complete years; no cross-year outcome observed",
                              "Open year-end options are bid-marked, not realized profits",
                              "Price resistance is not OI wall; no OI supplied",
                              "EOD standing quotes do not guarantee fills; early assignment and margin omitted",
                              "Rules selected after prior research; no untouched holdout"],
              "results": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")
    print(json.dumps({key: {k: v for k, v in value.items() if k in
                            ("status", "variants", "counts", "funding")}
                      for key, value in results.items()}, indent=2))


if __name__ == "__main__":
    main()
