import decision_engine


def test_unusable_oi_forces_low_confidence_observation():
    result = decision_engine.build_decision_brief(
        spot=100, put_wall=90, call_wall=110, gamma_flip=95,
        total_net_gex=1,
        oi_data_quality={"usable": False, "reason": "OI 僅為成交量的 1%"},
    )

    assert result["action"] == "觀望"
    assert result["confidence"] == "低"
    assert "OI" in result["why"][0]


def test_imminent_event_overrides_otherwise_quiet_setup():
    result = decision_engine.build_decision_brief(
        spot=100, put_wall=90, call_wall=110, gamma_flip=95,
        total_net_gex=1, calendar_warnings=["明天公布 CPI"],
    )

    assert result["action"] == "事件前觀望"
    assert result["confidence"] == "低"
    assert "CPI" in result["why"][0]


def test_positive_gamma_near_call_wall_identifies_range_upper_edge():
    result = decision_engine.build_decision_brief(
        spot=108, put_wall=90, call_wall=110, gamma_flip=100,
        total_net_gex=1,
    )

    assert result["action"] == "區間上緣，避免追價"
    assert result["confidence"] == "中"
    assert "$110" in result["upside_trigger"]
    assert "$100" in result["downside_trigger"]


def test_negative_gamma_prefers_defense_and_two_sided_triggers():
    result = decision_engine.build_decision_brief(
        spot=95, put_wall=90, call_wall=110, gamma_flip=100,
        total_net_gex=-1,
    )

    assert result["action"] == "防守，等待方向確認"
    assert "放大" in result["summary"]
    assert "$110" in result["upside_trigger"]
    assert "$90" in result["downside_trigger"]


def test_missing_or_inverted_levels_are_not_presented_as_actionable():
    result = decision_engine.build_decision_brief(
        spot=100, put_wall=120, call_wall=90, gamma_flip=None,
        total_net_gex=1,
    )

    assert result["action"] == "觀望"
    assert result["confidence"] == "低"
    assert "結構" in result["summary"]
