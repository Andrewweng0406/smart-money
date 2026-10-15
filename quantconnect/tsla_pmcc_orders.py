"""TSLA PMCC order backtest for QuantConnect Cloud. Paste this file as main.py.

Daily close signals submit market orders for the next session's open. This is
an order-based experiment, not a promise of executable live option prices.
Parameters: year=2021, mode=funded|pmcc|long, resistance=oi|price.
"""

from AlgorithmImports import *

from collections import Counter, defaultdict, deque
from datetime import date


CAPITAL = 100_000
SLOT_CAP = CAPITAL / 6
ENTRY_MONTHS = (1, 2, 3, 6, 7, 8)


def usable(contract, max_spread):
    bid, ask = float(contract.bid_price), float(contract.ask_price)
    return bid > 0 and ask >= bid and (ask - bid) / ask <= max_spread


def expiry_date(contract):
    return contract.expiry.date()


def choose_long(chain, spot, day, excluded, after=None):
    rows = [c for c in chain if c.right == OptionRight.CALL and usable(c, .10)
            and c.symbol not in excluded and expiry_date(c).month == 1
            and (expiry_date(c) - day).days >= 365
            and (after is None or expiry_date(c) > after)
            and .70 * spot <= float(c.strike) <= .85 * spot]
    return min(rows, key=lambda c: (expiry_date(c), abs(float(c.strike) - .8 * spot)), default=None)


def resistance_level(chain, spot, day, closes, mode):
    if mode == "price":
        return max(closes) if len(closes) == 20 else None
    totals = defaultdict(int)
    for c in chain:
        if (c.right == OptionRight.CALL and 21 <= (expiry_date(c) - day).days <= 45
                and spot <= float(c.strike) <= 1.1 * spot and c.open_interest > 0):
            totals[float(c.strike)] += int(c.open_interest)
    return max(totals, key=lambda strike: (totals[strike], -strike)) if totals else None


def choose_short(chain, day, level, long_expiry, long_strike):
    rows = [c for c in chain if c.right == OptionRight.CALL and usable(c, .25)
            and 21 <= (expiry_date(c) - day).days <= 45
            and expiry_date(c) < long_expiry
            and float(c.strike) >= level and float(c.strike) > long_strike]
    return min(rows, key=lambda c: (abs((expiry_date(c) - day).days - 30),
                                    float(c.strike) - level), default=None)


def choose_put(chain, day, spot):
    # A close-time signal uses a 4-10 DTE quote for a 3-9 DTE next-open fill.
    rows = [c for c in chain if c.right == OptionRight.PUT and usable(c, .25)
            and 4 <= (expiry_date(c) - day).days <= 10
            and .90 * spot <= float(c.strike) <= spot]
    return min(rows, key=lambda c: ((expiry_date(c) - day).days,
                                    abs(float(c.strike) - .95 * spot)), default=None)


class TslaPmccOrders(QCAlgorithm):
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
        self.settings.seed_initial_prices = True
        self.underlying = self.add_equity(
            "TSLA", Resolution.DAILY,
            data_normalization_mode=DataNormalizationMode.RAW).symbol
        option = self.add_option("TSLA", Resolution.DAILY)
        option.set_filter(lambda u: u.strikes(-100, 100).expiration(0, 900))
        self.option_symbol = option.symbol
        self.set_benchmark(self.underlying)
        self.slots = [dict(long=None, short=None, put=None, pending=None, entered=False,
                           short_credit=0., budget=0., short_day=None, put_day=None)
                      for _ in ENTRY_MONTHS]
        self.closes = deque(maxlen=20)
        self.stats = Counter()
        self.session = 0
        self.last_month = None
        self.set_runtime_statistic("TSLA chain days", "0")
        self.set_runtime_statistic("Option fills", "0")
        self.log(f"TSLA_PMCC_ORDERS_START year={self.year} mode={self.mode} resistance={self.resistance_mode}")

    def _reserved_symbols(self):
        result = set()
        for state in self.slots:
            for leg in ("long", "short", "put"):
                if state[leg] is not None:
                    result.add(state[leg])
            if state["pending"] is not None:
                result.add(state["pending"][1])
        return result

    def _submit(self, slot, action, symbol, quantity):
        state = self.slots[slot]
        if state["pending"] is not None:
            return
        state["pending"] = (action, symbol)
        ticket = self.market_on_open_order(symbol, quantity, tag=f"pmcc:{slot}:{action}")
        if ticket.status == OrderStatus.INVALID:
            state["pending"] = None
            self.stats[f"{action}_rejected"] += 1
            self.log(f"ORDER_REJECTED {action} slot={slot} symbol={symbol}")

    def on_order_event(self, event):
        tag = event.ticket.tag if event.ticket else ""
        if not tag.startswith("pmcc:"):
            if event.status == OrderStatus.FILLED:
                self.stats["external_or_assignment_fills"] += 1
                self.log(f"UNTRACKED_FILL {event}")
            return
        _, slot_text, action = tag.split(":")
        slot = int(slot_text)
        state = self.slots[slot]
        if event.status in (OrderStatus.INVALID, OrderStatus.CANCELED):
            state["pending"] = None
            self.stats[f"{action}_rejected"] += 1
            self.log(f"ORDER_FAILED {action} slot={slot} event={event}")
            return
        if event.status != OrderStatus.FILLED:
            return
        state["pending"] = None
        cash_flow = -float(event.fill_quantity) * float(event.fill_price) * 100
        fee = float(event.order_fee.value.amount)
        net = cash_flow - fee
        if action == "buy_long":
            state["long"] = event.symbol
            state["entered"] = True
        elif action == "sell_long":
            state["long"] = None
        elif action == "sell_short":
            state["short"] = event.symbol
            state["short_credit"] = net
            state["short_day"] = self.session
        elif action == "buy_short":
            state["short"] = None
            state["budget"] = max(0., state["budget"] + state["short_credit"] + net)
            state["short_credit"] = 0.
        elif action == "buy_put":
            state["put"] = event.symbol
            state["budget"] = max(0., state["budget"] + net)
            state["put_day"] = self.session
        elif action == "sell_put":
            state["put"] = None
            state["budget"] += net
        self.stats[f"{action}_fills"] += 1
        self.set_runtime_statistic("Option fills", str(sum(
            count for name, count in self.stats.items() if name.endswith("_fills"))))
        self.log(f"FILL {action} slot={slot} qty={event.fill_quantity} price={event.fill_price} fee={fee}")

    def on_assignment_order_event(self, event):
        self.stats["assignments"] += 1
        self.log(f"OPTION_ASSIGNMENT {event}")

    def _check_positions(self):
        for slot, state in enumerate(self.slots):
            for leg, direction in (("long", 1), ("short", -1), ("put", 1)):
                symbol = state[leg]
                if symbol is None:
                    continue
                if int(self.portfolio[symbol].quantity) != direction:
                    raise ValueError(
                        f"Position mismatch/assignment slot={slot} leg={leg} "
                        f"symbol={symbol} qty={self.portfolio[symbol].quantity}; "
                        "inspect Orders before using results")

    def on_data(self, data):
        bar = data.bars.get(self.underlying)
        if bar is None or self.time.year != self.year:
            return
        chain = data.option_chains.get(self.option_symbol)
        contracts = list(chain) if chain is not None else []
        if not contracts:
            self.stats["missing_chain_days"] += 1
            return
        day = self.time.date()
        spot = float(bar.close)
        self.session += 1
        self.stats["chain_days"] += 1
        self.set_runtime_statistic("TSLA chain days", str(self.stats["chain_days"]))
        if self.last_month != day.month:
            self.last_month = day.month
            self.log(f"DATA_PROGRESS date={day} contracts={len(contracts)} equity={self.portfolio.total_portfolio_value}")
        self._check_positions()
        reserved = self._reserved_symbols()
        level = resistance_level(contracts, spot, day, self.closes, self.resistance_mode)
        risk = len(self.closes) == 20 and spot < min(self.closes)
        entry_low = len(self.closes) == 20 and spot <= 1.03 * min(self.closes)
        for slot, month in enumerate(ENTRY_MONTHS):
            state = self.slots[slot]
            if state["pending"] is not None:
                continue
            if state["short"] is not None:
                c = next((x for x in contracts if x.symbol == state["short"]), None)
                if c is None:
                    self.stats["missing_held_quote"] += 1
                elif self.session - state["short_day"] >= 5 or (expiry_date(c) - day).days <= 2:
                    self._submit(slot, "buy_short", state["short"], 1)
                    continue
            if state["put"] is not None:
                c = next((x for x in contracts if x.symbol == state["put"]), None)
                if c is None:
                    self.stats["missing_held_quote"] += 1
                elif self.session - state["put_day"] >= 4 or (expiry_date(c) - day).days <= 2:
                    self._submit(slot, "sell_put", state["put"], -1)
                    continue
            if state["long"] is None:
                if state["entered"] or (day.month == month and (entry_low or day.day >= 24)):
                    c = choose_long(contracts, spot, day, reserved)
                    if c is not None and float(c.ask_price) * 100 <= SLOT_CAP:
                        self._submit(slot, "buy_long", c.symbol, 1)
                        reserved.add(c.symbol)
                    else:
                        self.stats["no_affordable_long"] += 1
                continue
            long_c = next((x for x in contracts if x.symbol == state["long"]), None)
            if long_c is None:
                self.stats["missing_held_quote"] += 1
                continue
            if (expiry_date(long_c) - day).days <= 120:
                if state["short"] is None and state["put"] is None:
                    replacement = choose_long(contracts, spot, day, reserved,
                                              after=expiry_date(long_c))
                    if replacement is not None and float(replacement.ask_price) * 100 <= SLOT_CAP:
                        # Exit first; replacement entry is attempted after the exit fills.
                        self._submit(slot, "sell_long", state["long"], -1)
                        continue
            if self.mode != "long" and state["short"] is None and level is not None:
                if 0 <= level - spot <= .01 * spot:
                    c = choose_short(contracts, day, level, expiry_date(long_c), float(long_c.strike))
                    if c is not None and c.symbol not in reserved:
                        self._submit(slot, "sell_short", c.symbol, -1)
                        reserved.add(c.symbol)
                        continue
            if self.mode == "funded" and state["put"] is None and risk:
                c = choose_put(contracts, day, spot)
                if c is not None and c.symbol not in reserved:
                    cost = float(c.ask_price) * 100
                    if cost <= state["budget"] and cost <= float(self.portfolio.cash):
                        self._submit(slot, "buy_put", c.symbol, 1)
                        reserved.add(c.symbol)
                    else:
                        self.stats["unfunded_put_signals"] += 1
        self.closes.append(spot)

    def on_end_of_algorithm(self):
        self.log(f"TSLA_PMCC_ORDERS_RESULT year={self.year} mode={self.mode} "
                 f"resistance={self.resistance_mode} "
                 f"equity={self.portfolio.total_portfolio_value} stats={dict(self.stats)}")
        if self.stats["chain_days"] < 200:
            raise ValueError(f"Insufficient TSLA chain days: {self.stats['chain_days']}")
        if self.stats["buy_long_fills"] == 0:
            raise ValueError("No long Call filled; inspect no_affordable_long and order rejections")
