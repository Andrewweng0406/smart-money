"""市場狀態分類只用合成輸入，不讀資料庫或行情。"""

import market_context


def test_classify_context_freezes_all_decision_dimensions():
    context = market_context.classify_market_context(
        spot=95.0, put_wall=90.0, call_wall=110.0, gamma_flip=100.0,
        zero_dte_share_pct=60.0, event_risk=True,
        data_quality={"usable": True, "label": "完整"},
    )

    assert context == {
        "gamma_regime": "negative",
        "price_zone": "inside_walls",
        "event_regime": "event_risk",
        "zero_dte_regime": "high",
        "data_regime": "usable",
    }


def test_classify_context_marks_missing_inputs_unknown():
    context = market_context.classify_market_context(
        spot=100.0, put_wall=0.0, call_wall=0.0, gamma_flip=None,
        zero_dte_share_pct=None, event_risk=None, data_quality=None,
    )

    assert context["gamma_regime"] == "unknown"
    assert context["price_zone"] == "unknown"
    assert context["event_regime"] == "unknown"
    assert context["zero_dte_regime"] == "unknown"
    assert context["data_regime"] == "unknown"


def test_context_key_is_stable_and_order_independent():
    context = {
        "price_zone": "above_call_wall", "gamma_regime": "positive",
        "data_regime": "usable", "zero_dte_regime": "normal",
        "event_regime": "normal",
    }

    assert market_context.context_key(context) == (
        "positive", "above_call_wall", "normal", "normal", "usable",
    )
