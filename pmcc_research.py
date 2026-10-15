"""Research-only ledger for a long call, short calls, and optional long put.

This models quoted option cash flows, not early assignment or stock delivery.
Every held option must be closed before expiry; a missing quote invalidates NAV.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")
MULTIPLIER = 100


def put_wall_breach_signal(*, spot: float, put_wall: float, observed_at: datetime,
                           execution_at: datetime) -> bool:
    """Research trigger using a completed observation before the execution quote."""
    if observed_at.tzinfo is None or execution_at.tzinfo is None or observed_at >= execution_at:
        raise ValueError("Put risk signal must precede execution")
    if not all(math.isfinite(x) and x > 0 for x in (spot, put_wall)):
        raise ValueError("Invalid put risk signal inputs")
    return spot < put_wall


@dataclass(frozen=True)
class CallQuote:
    symbol: str
    expiry: date
    strike: float
    observed_at: datetime
    bid: float
    ask: float

    def __post_init__(self):
        if not self.symbol or self.observed_at.tzinfo is None:
            raise ValueError("Quote requires symbol and timezone-aware timestamp")
        if not all(math.isfinite(value) for value in (self.strike, self.bid, self.ask)):
            raise ValueError("Non-finite option quote")
        if self.strike <= 0 or self.bid < 0 or self.ask <= 0 or self.bid > self.ask:
            raise ValueError("Invalid call bid/ask or strike")
        if self.expiry < self.session_date:
            raise ValueError("Expired call quote")

    @property
    def session_date(self) -> date:
        return self.observed_at.astimezone(EASTERN).date()

    @property
    def contract(self) -> tuple[str, date, float]:
        return self.symbol, self.expiry, self.strike


@dataclass(frozen=True)
class PutQuote:
    symbol: str
    expiry: date
    strike: float
    observed_at: datetime
    bid: float
    ask: float

    def __post_init__(self):
        if not self.symbol or self.observed_at.tzinfo is None:
            raise ValueError("Quote requires symbol and timezone-aware timestamp")
        if not all(math.isfinite(value) for value in (self.strike, self.bid, self.ask)):
            raise ValueError("Non-finite option quote")
        if self.strike <= 0 or self.bid < 0 or self.ask <= 0 or self.bid > self.ask:
            raise ValueError("Invalid put bid/ask or strike")
        if self.expiry < self.session_date:
            raise ValueError("Expired put quote")

    @property
    def session_date(self) -> date:
        return self.observed_at.astimezone(EASTERN).date()

    @property
    def contract(self) -> tuple[str, date, float]:
        return self.symbol, self.expiry, self.strike


@dataclass
class PMCCLedger:
    long_contract: tuple[str, date, float]
    long_open_date: date
    starting_capital: float
    cash: float
    quantity: int
    fee_per_contract: float
    slippage_per_share: float
    last_event_at: datetime
    active_short: tuple[str, date, float] | None = None
    short_cycles: int = 0
    closed: bool = False
    active_put: tuple[str, date, float] | None = None
    put_quantity: int = 0
    put_cycles: int = 0
    short_open_at: datetime | None = None
    put_open_at: datetime | None = None

    @classmethod
    def buy_long(cls, quote: CallQuote, *, capital: float, quantity: int = 1,
                 fee_per_contract: float = .65, slippage_per_share: float = .01) -> PMCCLedger:
        if not math.isfinite(capital) or capital <= 0 or isinstance(quantity, bool) or quantity < 1 or int(quantity) != quantity:
            raise ValueError("Invalid PMCC capital or quantity")
        if not all(math.isfinite(x) and x >= 0 for x in (fee_per_contract, slippage_per_share)):
            raise ValueError("Invalid PMCC costs")
        if quote.session_date >= quote.expiry:
            raise ValueError("Long call must outlive its purchase date")
        debit = quantity * ((quote.ask + slippage_per_share) * MULTIPLIER + fee_per_contract)
        if debit > capital:
            raise ValueError("Insufficient cash for long call")
        return cls(quote.contract, quote.session_date, capital, capital - debit,
                   quantity, fee_per_contract, slippage_per_share, quote.observed_at)

    def _check_long(self, quote: CallQuote):
        if self.closed or quote.contract != self.long_contract:
            raise ValueError("Long call contract mismatch or closed position")
        if not self.long_open_date <= quote.session_date < self.long_contract[1]:
            raise ValueError("Long call quote outside holding interval")

    def _check_active_expiries(self, session_date: date):
        if self.active_short is not None and session_date >= self.active_short[1]:
            raise ValueError("Active short call expired; assignment is not modeled")
        if self.active_put is not None and session_date >= self.active_put[1]:
            raise ValueError("Active long put expired; exercise is not modeled")

    def buy_put(self, long_quote: CallQuote, put_quote: PutQuote, *, quantity: int | None = None):
        self._check_long(long_quote)
        self._check_active_expiries(long_quote.session_date)
        if self.active_put is not None:
            raise ValueError("Close the current long put before buying another")
        if not isinstance(put_quote, PutQuote) or put_quote.symbol != self.long_contract[0]:
            raise ValueError("Long put must match the call underlying")
        if put_quote.observed_at != long_quote.observed_at or long_quote.observed_at < self.last_event_at:
            raise ValueError("Long put needs a contemporaneous, chronological call quote")
        if put_quote.session_date >= put_quote.expiry:
            raise ValueError("Long put must outlive its purchase date")
        quantity = self.quantity if quantity is None else quantity
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise ValueError("Invalid long put quantity")
        debit = quantity * ((put_quote.ask + self.slippage_per_share) * MULTIPLIER
                            + self.fee_per_contract)
        if debit > self.cash:
            raise ValueError("Insufficient cash for long put")
        self.cash -= debit
        self.active_put = put_quote.contract
        self.put_quantity = quantity
        self.put_cycles += 1
        self.put_open_at = put_quote.observed_at
        self.last_event_at = put_quote.observed_at

    def close_put(self, quote: PutQuote):
        if self.closed or not isinstance(quote, PutQuote) or quote.contract != self.active_put:
            raise ValueError("Long put contract mismatch or no active put")
        if quote.observed_at < self.last_event_at or quote.observed_at <= self.put_open_at:
            raise ValueError("Put close must follow put open")
        self._check_active_expiries(quote.session_date)
        if quote.session_date >= quote.expiry:
            raise ValueError("Long put must be closed before expiry; exercise is not modeled")
        if quote.bid > self.slippage_per_share:
            self.cash += self.put_quantity * ((quote.bid - self.slippage_per_share) * MULTIPLIER
                                              - self.fee_per_contract)
        self.active_put = None
        self.put_quantity = 0
        self.put_open_at = None
        self.last_event_at = quote.observed_at

    def write_short(self, long_quote: CallQuote, short_quote: CallQuote, *, spot: float,
                    call_wall: float, resistance_band_pct: float = .01):
        self._check_long(long_quote)
        self._check_active_expiries(long_quote.session_date)
        if self.active_short is not None:
            raise ValueError("Close the current short call before writing another")
        if long_quote.observed_at != short_quote.observed_at:
            raise ValueError("PMCC legs need contemporaneous quotes")
        if long_quote.observed_at < self.last_event_at:
            raise ValueError("Cannot write a short call using an earlier quote")
        if short_quote.symbol != self.long_contract[0] or not (
            self.long_contract[2] < short_quote.strike and
            short_quote.expiry < self.long_contract[1] and
            short_quote.session_date < short_quote.expiry
        ):
            raise ValueError("Short call must be higher strike and earlier expiry")
        if not all(math.isfinite(x) for x in (spot, call_wall, resistance_band_pct)) or spot <= 0 or resistance_band_pct <= 0:
            raise ValueError("Invalid resistance inputs")
        if not (0 <= call_wall - spot <= spot * resistance_band_pct and short_quote.strike >= call_wall):
            raise ValueError("Short call is not being written at resistance")
        if short_quote.bid <= self.slippage_per_share:
            raise ValueError("Short call has no executable bid")
        self.cash += self.quantity * ((short_quote.bid - self.slippage_per_share) * MULTIPLIER
                                      - self.fee_per_contract)
        self.active_short = short_quote.contract
        self.short_cycles += 1
        self.short_open_at = short_quote.observed_at
        self.last_event_at = short_quote.observed_at

    def close_short(self, quote: CallQuote):
        if self.closed or quote.contract != self.active_short:
            raise ValueError("Short call contract mismatch or no active short")
        if quote.observed_at < self.last_event_at or quote.observed_at <= self.short_open_at:
            raise ValueError("Short close must follow short open")
        self._check_active_expiries(quote.session_date)
        if quote.session_date >= quote.expiry:
            raise ValueError("Short call must be closed before expiry; assignment is not modeled")
        self.cash -= self.quantity * ((quote.ask + self.slippage_per_share) * MULTIPLIER
                                      + self.fee_per_contract)
        self.active_short = None
        self.short_open_at = None
        self.last_event_at = quote.observed_at

    def liquidation_nav(self, long_quote: CallQuote, short_quote: CallQuote | None = None,
                        put_quote: PutQuote | None = None) -> dict:
        self._check_long(long_quote)
        self._check_active_expiries(long_quote.session_date)
        if long_quote.observed_at < self.last_event_at:
            raise ValueError("Cannot value PMCC using an earlier quote")
        long_proceeds = self.quantity * ((long_quote.bid - self.slippage_per_share) * MULTIPLIER
                                         - self.fee_per_contract)
        short_cost = 0.0
        if self.active_short is not None:
            if short_quote is None or short_quote.contract != self.active_short:
                raise ValueError("Missing exact active short-call quote")
            if long_quote.observed_at != short_quote.observed_at or short_quote.session_date >= short_quote.expiry:
                raise ValueError("Short-call quote time mismatch or expired short")
            short_cost = self.quantity * ((short_quote.ask + self.slippage_per_share) * MULTIPLIER
                                          + self.fee_per_contract)
        elif short_quote is not None:
            raise ValueError("Unexpected short-call quote")
        put_proceeds = 0.0
        if self.active_put is not None:
            if not isinstance(put_quote, PutQuote) or put_quote.contract != self.active_put:
                raise ValueError("Missing exact active long-put quote")
            if put_quote.observed_at != long_quote.observed_at or put_quote.session_date >= put_quote.expiry:
                raise ValueError("Long-put quote time mismatch or expired put")
            if put_quote.bid > self.slippage_per_share:
                put_proceeds = self.put_quantity * ((put_quote.bid - self.slippage_per_share) * MULTIPLIER
                                                    - self.fee_per_contract)
        elif put_quote is not None:
            raise ValueError("Unexpected long-put quote")
        nav = self.cash + long_proceeds + put_proceeds - short_cost
        return {"date": long_quote.session_date.isoformat(), "nav": nav,
                "net_pnl": nav - self.starting_capital,
                "active_short": self.active_short is not None, "short_cycles": self.short_cycles,
                "active_put": self.active_put is not None, "put_cycles": self.put_cycles}

    def close_long(self, quote: CallQuote) -> float:
        self._check_long(quote)
        self._check_active_expiries(quote.session_date)
        if self.active_short is not None:
            raise ValueError("Close the short call before the long call")
        if self.active_put is not None:
            raise ValueError("Close the long put before the long call")
        if quote.observed_at < self.last_event_at:
            raise ValueError("Cannot close long call using an earlier quote")
        self.cash += self.quantity * ((quote.bid - self.slippage_per_share) * MULTIPLIER
                                      - self.fee_per_contract)
        self.closed = True
        self.last_event_at = quote.observed_at
        return self.cash
