"""TSLA PMCC quote replay for QuantConnect Cloud (paste as main.py).

This is a research ledger, not LEAN order execution. Project parameters:
year=2021, mode=funded|pmcc|long, resistance=oi|price.
"""

from AlgorithmImports import *

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date


CAPITAL = 100_000.0
SLOT_CASH = CAPITAL / 6
ENTRY_MONTHS = (1, 2, 3, 6, 7, 8)
EXPECTED_SESSIONS = {2019: 252, 2020: 253, 2021: 252, 2022: 251, 2023: 250}
FEE = 0.65
SLIPPAGE = 0.01


@dataclass(frozen=True)
class Quote:
    symbol: object
    right: str
    expiry: date
    strike: float
    bid: float
    ask: float
    oi: int


def liquid(q, spread):
    return (q.bid > SLIPPAGE and q.ask >= q.bid and q.ask > 0
            and (q.ask - q.bid) / q.ask <= spread)


def long_candidate(book, spot, day, later_than=None):
    rows = [q for q in book.values() if q.right == "call" and liquid(q, .10)
            and q.expiry.month == 1 and q.expiry.year >= day.year + 1
            and (q.expiry - day).days >= 365 and .70 * spot <= q.strike <= .85 * spot
            and (later_than is None or q.expiry > later_than)]
    return min(rows, key=lambda q: (q.expiry, abs(q.strike - .80 * spot), q.ask), default=None)


def oi_resistance(book, spot, day):
    totals = defaultdict(int)
    for q in book.values():
        if (q.right == "call" and 21 <= (q.expiry - day).days <= 45
                and spot <= q.strike <= 1.10 * spot and q.oi > 0):
            totals[q.strike] += q.oi
    return max(totals, key=lambda strike: (totals[strike], -strike)) if totals else None


def short_candidate(book, day, resistance, long_quote):
    rows = [q for q in book.values() if q.right == "call" and liquid(q, .25)
            and 21 <= (q.expiry - day).days <= 45 and q.expiry < long_quote.expiry
            and q.strike >= resistance and q.strike > long_quote.strike]
    return min(rows, key=lambda q: (abs((q.expiry - day).days - 30),
                                    q.strike - resistance, q.ask), default=None)


def put_candidate(book, day, signal_spot):
    # Planning precedes execution by one trading day; target 3-9 DTE at execution.
    rows = [q for q in book.values() if q.right == "put" and liquid(q, .25)
            and 4 <= (q.expiry - day).days <= 10
            and .90 * signal_spot <= q.strike <= signal_spot]
    return min(rows, key=lambda q: ((q.expiry - day).days,
                                    abs(q.strike - .95 * signal_spot), q.ask), default=None)


def buy_cost(q):
    return (q.ask + SLIPPAGE) * 100 + FEE


def sell_proceeds(q):
    return max(0.0, (q.bid - SLIPPAGE) * 100 - FEE)


class TslaPmccResearch(QCAlgorithm):
    def initialize(self):
        self.year = int(self.get_parameter("year", 2021))
        self.mode = self.get_parameter("mode", "funded")
        self.resistance_mode = self.get_parameter("resistance", "oi")
        if self.mode not in ("funded", "pmcc", "long"):
            raise ValueError("mode must be funded, pmcc or long")
        if self.resistance_mode not in ("oi", "price"):
            raise ValueError("resistance must be oi or price")
        self.set_start_date(self.year, 1, 1)
        self.set_end_date(self.year, 12, 31)
        self.set_cash(CAPITAL)
        self.settings.daily_precise_end_time = True
        equity = self.add_equity("TSLA", Resolution.DAILY,
                                 data_normalization_mode=DataNormalizationMode.RAW)
        self.equity = equity.symbol
        option = self.add_option("TSLA", Resolution.DAILY)
        option.set_filter(lambda universe: universe.strikes(-100, 100).expiration(0, 900))
        self.option_symbol = option.symbol
        self.set_benchmark(self.equity)
        self.slots = [dict(cash=SLOT_CASH, long=None, short=None, put=None,
                           budget=0.0, short_credit=0.0, short_open=0, put_open=0)
                      for _ in ENTRY_MONTHS]
        self.closes = []
        self.pending = []
        self.counts = Counter()
        self.attempted_months = set()
        self.session = 0
        self.last_nav = CAPITAL
        self.peak_nav = CAPITAL
        self.max_drawdown = 0.0
        self.first_day = None
        self.last_day = None
        self.set_runtime_statistic("TSLA sessions", "0")
        self.set_runtime_statistic("Quote NAV", "$100,000")
        self.set_runtime_statistic("Open long calls", "0")
        self.log(f"TSLA_RESEARCH_START year={self.year} mode={self.mode} resistance={self.resistance_mode}; no LEAN orders")

    def _quote(self, book, symbol):
        q = book.get(symbol)
        if q is None or q.bid < 0 or q.ask <= 0 or q.bid > q.ask:
            raise ValueError(f"Missing/invalid held contract quote: {symbol} on {self.time}")
        return q

    def _close_short(self, state, book):
        cost = buy_cost(self._quote(book, state["short"]))
        state["cash"] -= cost
        state["budget"] += state["short_credit"] - cost
        state["short"] = None
        state["short_credit"] = 0.0
        self.counts["short_closes"] += 1

    def _close_put(self, state, book):
        proceeds = sell_proceeds(self._quote(book, state["put"]))
        state["cash"] += proceeds
        state["budget"] += proceeds
        state["put"] = None
        self.counts["put_closes"] += 1

    def _manage_exits(self, day, book):
        for state in self.slots:
            if state["short"] is not None:
                q = self._quote(book, state["short"])
                if self.session - state["short_open"] >= 5 or (q.expiry - day).days <= 2:
                    self._close_short(state, book)
            if state["put"] is not None:
                q = self._quote(book, state["put"])
                if self.session - state["put_open"] >= 4 or (q.expiry - day).days <= 2:
                    self._close_put(state, book)
            if state["long"] is not None:
                q = self._quote(book, state["long"])
                if (q.expiry - day).days <= 2:
                    if state["short"] is not None:
                        self._close_short(state, book)
                    if state["put"] is not None:
                        self._close_put(state, book)
                    state["cash"] += sell_proceeds(q)
                    state["long"] = None
                    self.counts["forced_long_exits"] += 1

    def _execute_pending(self, day, spot, book):
        for slot, action, symbol, signal in self.pending:
            state = self.slots[slot]
            q = book.get(symbol)
            if q is None or not liquid(q, .10 if action in ("entry", "roll") else .25):
                self.counts[f"{action}_missing_execution_quote"] += 1
                continue
            if action == "entry" and state["long"] is None:
                cost = buy_cost(q)
                if (.70 * spot <= q.strike <= .85 * spot
                        and (q.expiry - day).days >= 365 and cost <= state["cash"]):
                    state["cash"] -= cost
                    state["long"] = symbol
                    self.counts["long_entries"] += 1
                else:
                    self.counts["entry_over_budget"] += 1
            elif action == "roll" and state["long"] is not None:
                old = self._quote(book, state["long"])
                projected_cash = state["cash"] + sell_proceeds(old)
                if state["short"] is not None:
                    projected_cash -= buy_cost(self._quote(book, state["short"]))
                if state["put"] is not None:
                    projected_cash += sell_proceeds(self._quote(book, state["put"]))
                if (q.expiry <= old.expiry or projected_cash < buy_cost(q)
                        or not .70 * spot <= q.strike <= .85 * spot):
                    self.counts["roll_unavailable"] += 1
                    continue
                if state["short"] is not None:
                    self._close_short(state, book)
                if state["put"] is not None:
                    self._close_put(state, book)
                state["cash"] += sell_proceeds(old) - buy_cost(q)
                state["long"] = symbol
                self.counts["long_rolls"] += 1
            elif action == "short" and self.mode != "long" and state["long"] is not None and state["short"] is None:
                long_q = self._quote(book, state["long"])
                if (21 <= (q.expiry - day).days <= 45 and q.expiry < long_q.expiry
                        and q.strike > long_q.strike and q.strike >= signal
                        and 0 <= signal - spot <= .01 * spot):
                    credit = sell_proceeds(q)
                    state["cash"] += credit
                    state["short_credit"] = credit
                    state["short"] = symbol
                    state["short_open"] = self.session
                    self.counts["short_entries"] += 1
            elif action == "put" and self.mode == "funded" and state["long"] is not None and state["put"] is None:
                cost = buy_cost(q)
                if (3 <= (q.expiry - day).days <= 9
                        and .90 * spot <= q.strike <= spot and cost <= state["cash"]
                        and cost <= state["budget"] + 1e-8):
                    state["cash"] -= cost
                    state["budget"] -= cost
                    state["put"] = symbol
                    state["put_open"] = self.session
                    self.counts["funded_put_entries"] += 1
                else:
                    self.counts["put_unfunded_or_out_of_range"] += 1
        self.pending = []

    def _plan(self, day, spot, book):
        plan = []
        if day.month in ENTRY_MONTHS and day.month not in self.attempted_months:
            self.attempted_months.add(day.month)
            slot = ENTRY_MONTHS.index(day.month)
            q = long_candidate(book, spot, day)
            if q is not None:
                plan.append((slot, "entry", q.symbol, 0.0))
            else:
                self.counts["entry_no_candidate"] += 1
        if len(self.closes) < 20:
            return plan
        high, low = max(self.closes[-20:]), min(self.closes[-20:])
        price_resistance = high if 0 <= high - spot <= .01 * spot else None
        oi_level = oi_resistance(book, spot, day) if self.resistance_mode == "oi" else None
        resistance = oi_level if self.resistance_mode == "oi" else price_resistance
        if self.resistance_mode == "oi" and oi_level is None:
            self.counts["missing_oi_wall_days"] += 1
        risk = spot < low
        for slot, state in enumerate(self.slots):
            if state["long"] is None:
                continue
            long_q = self._quote(book, state["long"])
            if (long_q.expiry - day).days <= 120:
                replacement = long_candidate(book, spot, day, long_q.expiry)
                if replacement is not None:
                    plan.append((slot, "roll", replacement.symbol, 0.0))
                else:
                    self.counts["roll_no_candidate"] += 1
            if self.mode != "long" and state["short"] is None and resistance is not None:
                if 0 <= resistance - spot <= .01 * spot:
                    q = short_candidate(book, day, resistance, long_q)
                    if q is not None:
                        plan.append((slot, "short", q.symbol, resistance))
            if self.mode == "funded" and state["put"] is None and risk:
                self.counts["risk_signals"] += 1
                q = put_candidate(book, day, spot)
                if q is not None:
                    plan.append((slot, "put", q.symbol, 0.0))
        return plan

    def _mark(self, book):
        nav = 0.0
        for state in self.slots:
            nav += state["cash"]
            if state["long"] is not None:
                nav += sell_proceeds(self._quote(book, state["long"]))
            if state["short"] is not None:
                nav -= buy_cost(self._quote(book, state["short"]))
            if state["put"] is not None:
                nav += sell_proceeds(self._quote(book, state["put"]))
        self.last_nav = nav
        self.peak_nav = max(self.peak_nav, nav)
        self.max_drawdown = max(self.max_drawdown, 1 - nav / self.peak_nav)
        self.plot("Research NAV", "Quote replay", nav)
        self.set_runtime_statistic("Quote NAV", f"${nav:,.2f}")
        self.set_runtime_statistic("Open long calls", str(sum(
            state["long"] is not None for state in self.slots)))

    def on_data(self, slice):
        bar = slice.bars.get(self.equity)
        chain = slice.option_chains.get(self.option_symbol)
        if bar is None:
            return
        day = bar.time.date()
        if day.year != self.year:
            return
        if chain is None:
            self.counts["sessions_without_chain"] += 1
            if any(state["long"] for state in self.slots):
                raise ValueError(f"Missing TSLA option chain on held day {day}")
            return
        book = {}
        for contract in chain:
            right = "call" if contract.right == OptionRight.CALL else "put"
            expiry = contract.expiry.date()
            q = Quote(contract.symbol, right, expiry, float(contract.strike),
                      float(contract.bid_price), float(contract.ask_price),
                      int(contract.open_interest))
            book[q.symbol] = q
        if not book:
            raise ValueError(f"Empty TSLA option chain on {day}")
        self.session += 1
        self.first_day = self.first_day or day
        self.last_day = day
        self.counts["sessions_with_chain"] += 1
        self.set_runtime_statistic("TSLA sessions", str(self.counts["sessions_with_chain"]))
        if self.first_day == day or self.last_day.month != getattr(self, "progress_month", None):
            self.progress_month = day.month
            self.log(f"TSLA_DATA_PROGRESS date={day} sessions={self.counts['sessions_with_chain']} contracts={len(book)}")
        self._manage_exits(day, book)
        self._execute_pending(day, float(bar.close), book)
        self._mark(book)
        self.pending = self._plan(day, float(bar.close), book)
        self.closes.append(float(bar.close))

    def on_end_of_algorithm(self):
        if self.counts["sessions_with_chain"] < 200:
            raise ValueError(f"Insufficient TSLA option-chain days: {self.counts}")
        expected = EXPECTED_SESSIONS.get(self.year)
        if expected is not None and self.counts["sessions_with_chain"] != expected:
            raise ValueError(f"Incomplete {self.year} sessions: expected {expected}, got {self.counts['sessions_with_chain']}")
        if self.counts["sessions_without_chain"]:
            raise ValueError(f"Missing TSLA option-chain sessions: {self.counts}")
        if self.resistance_mode == "oi" and self.counts["missing_oi_wall_days"] >= self.counts["sessions_with_chain"] - 20:
            raise ValueError("Open-interest wall unavailable on almost all signal days")
        result = dict(year=self.year, mode=self.mode, resistance=self.resistance_mode,
                      first_day=str(self.first_day), last_day=str(self.last_day),
                      quote_nav=round(self.last_nav, 2),
                      quote_return_pct=round(100 * (self.last_nav / CAPITAL - 1), 3),
                      max_quote_drawdown_pct=round(100 * self.max_drawdown, 3),
                      counts=dict(self.counts),
                      open_longs=sum(s["long"] is not None for s in self.slots),
                      open_shorts=sum(s["short"] is not None for s in self.slots),
                      open_puts=sum(s["put"] is not None for s in self.slots))
        self.set_runtime_statistic("Quote return", f"{result['quote_return_pct']:+.2f}%")
        self.log(f"TSLA_PMCC_RESEARCH_RESULT {result}")
