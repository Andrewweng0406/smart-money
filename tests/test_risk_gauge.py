"""risk_gauge.py 測試——純函式，全部用合成輸入，不碰 I/O。

這個模組回答的是「今天有多危險」，不是「會漲還跌」。GEX 的理論基礎是
做市商對沖行為對**波動大小**的影響（二階矩），拿它預測方向是用它最弱的
那一面——測試也照這個定位寫：驗風險分數與因子拆解，不驗方向。
"""

from __future__ import annotations

import risk_gauge


def _factor_names(assessment: dict) -> set[str]:
    return {f["name"] for f in assessment["factors"]}


def test_positive_gamma_quiet_day_is_low_risk():
    a = risk_gauge.assess_risk(spot=110.0, gamma_flip=100.0, total_net_gex=5.0)

    assert a["negative_gamma"] is False
    assert a["risk_score"] < 25
    assert a["risk_label"] == "低"


def test_negative_gamma_adds_risk_points():
    a = risk_gauge.assess_risk(spot=90.0, gamma_flip=100.0, total_net_gex=5.0)

    assert a["negative_gamma"] is True
    assert a["risk_score"] == risk_gauge.NEGATIVE_GAMMA_POINTS
    assert "負 Gamma" in _factor_names(a)


def test_zero_dte_concentration_adds_risk():
    a = risk_gauge.assess_risk(
        spot=110.0, gamma_flip=100.0, total_net_gex=5.0, zero_dte_share_pct=70.0,
    )

    assert a["risk_score"] == risk_gauge.ZERO_DTE_POINTS
    assert any("0DTE" in name for name in _factor_names(a))


def test_zero_dte_below_threshold_adds_nothing():
    a = risk_gauge.assess_risk(
        spot=110.0, gamma_flip=100.0, total_net_gex=5.0, zero_dte_share_pct=10.0,
    )

    assert a["risk_score"] == 0


def test_imminent_event_adds_risk():
    a = risk_gauge.assess_risk(
        spot=110.0, gamma_flip=100.0, total_net_gex=5.0,
        calendar_warnings=["⚠️ TSLA 財報 2 天後公布"],
    )

    assert a["risk_score"] == risk_gauge.EVENT_POINTS
    assert any("事件" in name for name in _factor_names(a))


def test_dealer_pressure_alert_adds_risk():
    a = risk_gauge.assess_risk(
        spot=110.0, gamma_flip=100.0, total_net_gex=5.0,
        alert="⚠️ 做市商對沖賣壓風險高",
    )

    assert a["risk_score"] == risk_gauge.ALERT_POINTS


def test_put_skew_warns_against_naked_short_puts():
    a = risk_gauge.assess_risk(
        spot=110.0, gamma_flip=100.0, total_net_gex=5.0, iv_skew=0.12,
    )

    assert a["risk_score"] == risk_gauge.IV_SKEW_POINTS
    assert any("Put" in item for item in a["avoid"])


def test_call_skew_warns_against_naked_short_calls():
    a = risk_gauge.assess_risk(
        spot=110.0, gamma_flip=100.0, total_net_gex=5.0, iv_skew=-0.12,
    )

    assert any("Call" in item for item in a["avoid"])


def test_mild_skew_produces_no_warning():
    a = risk_gauge.assess_risk(
        spot=110.0, gamma_flip=100.0, total_net_gex=5.0, iv_skew=0.01,
    )

    assert a["risk_score"] == 0
    assert a["avoid"] == []


def test_pinning_in_positive_gamma_dampens_risk():
    """正 Gamma + 高 Pinning 是被壓抑的區間環境，風險應該往下調。"""
    baseline = risk_gauge.assess_risk(
        spot=110.0, gamma_flip=100.0, total_net_gex=5.0, zero_dte_share_pct=70.0,
    )
    damped = risk_gauge.assess_risk(
        spot=110.0, gamma_flip=100.0, total_net_gex=5.0, zero_dte_share_pct=70.0,
        pinning={"score": 75, "label": "高"},
    )

    assert damped["risk_score"] < baseline["risk_score"]


def test_pinning_does_not_dampen_in_negative_gamma():
    """負 Gamma 下的 Pinning 不該給人安全感——對沖是順勢的，釘不住。"""
    a = risk_gauge.assess_risk(
        spot=90.0, gamma_flip=100.0, total_net_gex=5.0,
        pinning={"score": 75, "label": "高"},
    )

    assert a["risk_score"] == risk_gauge.NEGATIVE_GAMMA_POINTS


def test_risk_score_is_capped_at_100():
    a = risk_gauge.assess_risk(
        spot=90.0, gamma_flip=100.0, total_net_gex=-5.0,
        zero_dte_share_pct=90.0, iv_skew=0.20,
        alert="⚠️ 做市商對沖賣壓風險高",
        calendar_warnings=["⚠️ FOMC 1 天後"],
    )

    assert a["risk_score"] == 100
    assert a["risk_label"] == "極高"


def test_risk_score_never_negative():
    a = risk_gauge.assess_risk(
        spot=110.0, gamma_flip=100.0, total_net_gex=5.0,
        pinning={"score": 90, "label": "高"},
    )

    assert a["risk_score"] >= 0


def test_factors_expose_points_and_reason():
    """每個因子都要能說出貢獻幾分、為什麼——這是透明量化判斷的要求，
    使用者要能自己判斷同不同意每一項，而不是接受一個黑箱分數。"""
    a = risk_gauge.assess_risk(spot=90.0, gamma_flip=100.0, total_net_gex=5.0)

    factor = a["factors"][0]
    assert factor["points"] != 0
    assert factor["why"]


def test_posture_differs_between_regimes():
    positive = risk_gauge.assess_risk(spot=110.0, gamma_flip=100.0, total_net_gex=5.0)
    negative = risk_gauge.assess_risk(spot=90.0, gamma_flip=100.0, total_net_gex=5.0)

    assert positive["posture"] != negative["posture"]
    assert positive["posture"]
    assert negative["posture"]


def test_extreme_risk_posture_says_stand_aside():
    a = risk_gauge.assess_risk(
        spot=90.0, gamma_flip=100.0, total_net_gex=-5.0,
        zero_dte_share_pct=90.0, alert="⚠️ 做市商對沖賣壓風險高",
        calendar_warnings=["⚠️ FOMC 1 天後"],
    )

    assert "觀望" in a["posture"]


def test_missing_gamma_flip_falls_back_to_net_gex():
    """gamma_flip 缺值時退回 net_gex 判斷，且要標明走了退化路徑。"""
    a = risk_gauge.assess_risk(spot=100.0, gamma_flip=None, total_net_gex=-5.0)

    assert a["negative_gamma"] is True
    assert a["regime_source"] == "net_gex_fallback"


def test_regime_definition_matches_signal_tiering():
    """全系統只能有一個「什麼叫負 Gamma」的定義。

    這個 codebase 的獨立審查抓到過 negative_gamma 判斷被誤用的 bug，
    兩套定義並存遲早再出事。
    """
    import signal_tiering

    a = risk_gauge.assess_risk(spot=90.0, gamma_flip=100.0, total_net_gex=5.0)
    regime = signal_tiering.build_regime(spot=90.0, gamma_flip=100.0, total_net_gex=5.0)

    assert a["negative_gamma"] == regime["negative_gamma"]


# ---------- 報告區塊 ----------

def test_build_risk_lines_renders_score_and_factors():
    import analyze

    assessment = risk_gauge.assess_risk(
        spot=90.0, gamma_flip=100.0, total_net_gex=5.0,
        alert="⚠️ 做市商對沖賣壓風險高",
    )

    lines = analyze._build_risk_lines(assessment)
    text = "\n".join(lines)

    assert "風險計量" in text
    assert "45" in text                 # 30（負Gamma）+ 15（警報）
    assert "負 Gamma" in text
    assert "避免裸賣" in text


def test_build_risk_lines_returns_empty_for_none():
    """加分項計算失敗時整段從報告消失，不留半殘標題。"""
    import analyze

    assert analyze._build_risk_lines(None) == []


def test_build_risk_lines_omits_avoid_section_when_nothing_to_avoid():
    import analyze

    assessment = risk_gauge.assess_risk(spot=110.0, gamma_flip=100.0, total_net_gex=5.0)
    text = "\n".join(analyze._build_risk_lines(assessment))

    assert "風險計量" in text
    assert "應避開" not in text
