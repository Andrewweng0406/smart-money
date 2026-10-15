"""Offline rule checks for the cloud-only QuantConnect research script."""

import importlib.util
import sys
import types
from collections import Counter
from datetime import date
from pathlib import Path


PATH = Path(__file__).resolve().parents[1] / "quantconnect" / "tsla_pmcc_research.py"


def load_script():
    stub = types.ModuleType("AlgorithmImports")
    stub.QCAlgorithm = object
    old = sys.modules.get("AlgorithmImports")
    sys.modules["AlgorithmImports"] = stub
    spec = importlib.util.spec_from_file_location("tsla_qc_rules_for_test", PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
        if old is None:
            sys.modules.pop("AlgorithmImports", None)
        else:
            sys.modules["AlgorithmImports"] = old
    return module


def quote(module, symbol, right, expiry, strike, bid, ask, oi=0):
    return module.Quote(symbol, right, date.fromisoformat(expiry), strike, bid, ask, oi)


def test_oi_wall_uses_strike_level_oi_not_price_high():
    m = load_script()
    book = {q.symbol: q for q in (
        quote(m, "c100a", "call", "2021-08-27", 100, 2, 2.1, 600),
        quote(m, "c100b", "call", "2021-09-03", 100, 3, 3.1, 500),
        quote(m, "c105", "call", "2021-08-27", 105, 1, 1.1, 1000),
        quote(m, "p100", "put", "2021-08-27", 100, 1, 1.1, 5000),
    )}
    assert m.oi_resistance(book, 99.5, date(2021, 8, 2)) == 100


def test_weekly_put_is_planned_one_day_before_eligible_execution():
    m = load_script()
    day = date(2021, 8, 2)
    good = quote(m, "p95", "put", "2021-08-06", 95, 1, 1.1)
    too_soon = quote(m, "p96", "put", "2021-08-05", 96, 1, 1.1)
    assert m.put_candidate({"a": good, "b": too_soon}, day, 100) == good


def test_put_uses_only_realized_short_call_premium():
    m = load_script()
    engine = object.__new__(m.TslaPmccResearch)
    engine.mode = "funded"
    engine.session = 10
    engine.counts = Counter()
    long_q = quote(m, "long", "call", "2023-01-20", 80, 22, 22.1)
    short_q = quote(m, "short", "call", "2021-09-03", 105, 3, 3.1)
    put_q = quote(m, "put", "put", "2021-08-06", 95, 1, 1.1)
    book = {q.symbol: q for q in (long_q, short_q, put_q)}
    state = dict(cash=10_000.0, long="long", short="short", put=None,
                 budget=0.0, short_credit=500.0, short_open=1, put_open=0)
    engine.slots = [state]
    engine.pending = [(0, "put", "put", 0.0)]
    engine._execute_pending(date(2021, 8, 3), 100, book)
    assert state["put"] is None
    assert engine.counts["put_unfunded_or_out_of_range"] == 1

    engine._close_short(state, book)
    assert state["budget"] > m.buy_cost(put_q)
    engine.pending = [(0, "put", "put", 0.0)]
    engine._execute_pending(date(2021, 8, 3), 100, book)
    assert state["put"] == "put"
    assert state["budget"] >= 0


def test_roll_checks_short_buyback_before_spending_cash():
    m = load_script()
    engine = object.__new__(m.TslaPmccResearch)
    engine.mode = "funded"
    engine.counts = Counter()
    old = quote(m, "old", "call", "2022-01-21", 80, 10, 10.1)
    new = quote(m, "new", "call", "2023-01-20", 80, 18, 18.1)
    short = quote(m, "short", "call", "2021-11-05", 110, 2, 8)
    book = {q.symbol: q for q in (old, new, short)}
    state = dict(cash=1000.0, long="old", short="short", put=None,
                 budget=0.0, short_credit=200.0, short_open=1, put_open=0)
    engine.slots = [state]
    engine.pending = [(0, "roll", "new", 0.0)]
    engine._execute_pending(date(2021, 10, 1), 100, book)
    assert state["long"] == "old"
    assert state["short"] == "short"
    assert engine.counts["roll_unavailable"] == 1


def test_next_day_gap_invalidates_preselected_put_strike():
    m = load_script()
    engine = object.__new__(m.TslaPmccResearch)
    engine.mode = "funded"
    engine.session = 10
    engine.counts = Counter()
    long_q = quote(m, "long", "call", "2023-01-20", 80, 22, 22.1)
    put_q = quote(m, "put", "put", "2021-08-06", 95, 1, 1.1)
    book = {q.symbol: q for q in (long_q, put_q)}
    state = dict(cash=10_000.0, long="long", short=None, put=None,
                 budget=500.0, short_credit=0.0, short_open=0, put_open=0)
    engine.slots = [state]
    engine.pending = [(0, "put", "put", 0.0)]
    engine._execute_pending(date(2021, 8, 3), 110, book)
    assert state["put"] is None
    assert state["budget"] == 500.0
