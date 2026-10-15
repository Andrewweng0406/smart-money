"""Offline selection and accounting checks for the QC order-based script."""

import importlib.util
import sys
import types
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import pytest


PATH = Path(__file__).resolve().parents[1] / "quantconnect" / "tsla_pmcc_orders.py"


def load_script():
    stub = types.ModuleType("AlgorithmImports")
    stub.QCAlgorithm = object
    stub.OptionRight = types.SimpleNamespace(CALL="call", PUT="put")
    stub.OrderStatus = types.SimpleNamespace(FILLED="filled", INVALID="invalid", CANCELED="canceled")
    previous = sys.modules.get("AlgorithmImports")
    sys.modules["AlgorithmImports"] = stub
    spec = importlib.util.spec_from_file_location("tsla_qc_orders_for_test", PATH)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop("AlgorithmImports", None)
        else:
            sys.modules["AlgorithmImports"] = previous
    return module


def contract(symbol, right, expiry, strike, bid, ask, oi=0):
    return types.SimpleNamespace(
        symbol=symbol, right=right, expiry=datetime.fromisoformat(expiry),
        strike=strike, bid_price=bid, ask_price=ask, open_interest=oi)


def fill(module, action, qty, price, fee=0):
    return types.SimpleNamespace(
        ticket=types.SimpleNamespace(tag=f"pmcc:0:{action}"),
        status=module.OrderStatus.FILLED, symbol=action,
        fill_quantity=qty, fill_price=price,
        order_fee=types.SimpleNamespace(value=types.SimpleNamespace(amount=fee)))


def test_long_and_resistance_selection():
    m = load_script()
    day = date(2021, 1, 4)
    long = contract("long", "call", "2022-01-21", 80, 20, 20.5)
    near = contract("near", "call", "2021-12-17", 80, 18, 18.5)
    assert m.choose_long([near, long], 100, day, set()) == long
    wall1 = contract("wall1", "call", "2021-02-05", 105, 2, 2.1, 600)
    wall2 = contract("wall2", "call", "2021-02-12", 105, 2, 2.1, 500)
    wall3 = contract("wall3", "call", "2021-02-05", 110, 1, 1.1, 1000)
    assert m.resistance_level([wall1, wall2, wall3], 100, day, [], "oi") == 105


def test_put_budget_is_available_only_after_short_close():
    m = load_script()
    engine = object.__new__(m.TslaPmccOrders)
    engine.slots = [dict(long="long", short=None, put=None, pending=None,
                         entered=True, short_credit=0., budget=0.,
                         short_day=None, put_day=None)]
    engine.stats = Counter()
    engine.session = 10
    engine.set_runtime_statistic = lambda *_: None
    engine.log = lambda *_: None
    state = engine.slots[0]
    engine.on_order_event(fill(m, "sell_short", -1, 5.0, .65))
    assert state["short_credit"] == 499.35
    assert state["budget"] == 0
    engine.on_order_event(fill(m, "buy_short", 1, 2.0, .65))
    assert state["budget"] == pytest.approx(298.70)
    engine.on_order_event(fill(m, "buy_put", 1, 1.0, .65))
    assert state["budget"] == pytest.approx(198.05)


def test_put_selector_leaves_one_day_for_next_open():
    m = load_script()
    day = date(2021, 8, 2)
    good = contract("good", "put", "2021-08-06", 95, 1, 1.1)
    expiring = contract("expiring", "put", "2021-08-05", 95, 1, 1.1)
    assert m.choose_put([expiring, good], day, 100) == good
