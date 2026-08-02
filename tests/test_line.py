"""line_notifier.py + line_formatter.py 測試——line_notifier 用 mock 的
requests.post（不打真實網路），line_formatter 是純函式，直接測邏輯即可。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import line_formatter
import line_notifier

# ---------- line_formatter: detect_extreme_event ----------


def test_detect_extreme_event_death_loop_has_highest_priority():
    """死亡Loop優先序最高——就算同時也符合牆位突破，也只回傳死亡Loop。"""
    mm_pressure = {"is_death_loop_alert": True, "alert_text": "..."}
    event = line_formatter.detect_extreme_event(
        spot=1000.0, call_wall=110.0, put_wall=90.0, total_net_gex=-1.0, mm_pressure=mm_pressure,
    )
    assert event == line_formatter.DEATH_LOOP


def test_detect_extreme_event_call_wall_breach():
    event = line_formatter.detect_extreme_event(
        spot=115.0, call_wall=110.0, put_wall=90.0, total_net_gex=1.0, mm_pressure=None,
    )
    assert event == line_formatter.CALL_WALL_BREACH


def test_detect_extreme_event_put_wall_breach():
    event = line_formatter.detect_extreme_event(
        spot=85.0, call_wall=110.0, put_wall=90.0, total_net_gex=1.0, mm_pressure=None,
    )
    assert event == line_formatter.PUT_WALL_BREACH


def test_detect_extreme_event_negative_gex():
    event = line_formatter.detect_extreme_event(
        spot=100.0, call_wall=110.0, put_wall=90.0, total_net_gex=-1.0, mm_pressure=None,
    )
    assert event == line_formatter.NEGATIVE_GEX


def test_detect_extreme_event_none_when_nothing_triggered():
    event = line_formatter.detect_extreme_event(
        spot=100.0, call_wall=110.0, put_wall=90.0, total_net_gex=1.0,
        mm_pressure={"is_death_loop_alert": False},
    )
    assert event is None


def test_detect_extreme_event_handles_none_mm_pressure():
    """mm_pressure 是 None（Smart Money 指標計算失敗時的預設值）不該讓判斷邏輯出錯。"""
    event = line_formatter.detect_extreme_event(
        spot=100.0, call_wall=110.0, put_wall=90.0, total_net_gex=1.0, mm_pressure=None,
    )
    assert event is None


# ---------- line_formatter: format_line_alert ----------


def test_format_line_alert_death_loop_matches_expected_wording():
    text = line_formatter.format_line_alert("TSLA", 210.5, line_formatter.DEATH_LOOP)
    lines = text.split("\n")
    assert lines[0] == "🚨 【莊家洗盤警示 - TSLA】"
    assert lines[1] == "價格：$210.50"
    assert lines[2].startswith("重點：")
    assert lines[3].startswith("建議：")
    assert len(lines) == 4  # 標題+價格+重點+建議，固定4行（含標題不超過3句內文）


def test_format_line_alert_call_wall_breach_includes_wall_price():
    text = line_formatter.format_line_alert("TSLA", 315.0, line_formatter.CALL_WALL_BREACH, call_wall=310.0)
    assert "310" in text


def test_format_line_alert_put_wall_breach_includes_wall_price():
    text = line_formatter.format_line_alert("TSLA", 285.0, line_formatter.PUT_WALL_BREACH, put_wall=290.0)
    assert "290" in text


def test_format_line_alert_negative_gex_does_not_require_wall_prices():
    text = line_formatter.format_line_alert("TSLA", 300.0, line_formatter.NEGATIVE_GEX)
    assert "TSLA" in text


def test_format_line_alert_raises_on_unknown_event_code():
    import pytest
    with pytest.raises(ValueError):
        line_formatter.format_line_alert("TSLA", 300.0, "not_a_real_event")


# ---------- line_formatter: build_line_alert (整合) ----------


def test_build_line_alert_returns_none_when_no_extreme_event():
    result = line_formatter.build_line_alert(
        symbol="TSLA", spot=300.0, call_wall=330.0, put_wall=290.0,
        total_net_gex=1.0, mm_pressure=None,
    )
    assert result is None


def test_build_line_alert_returns_text_when_triggered():
    result = line_formatter.build_line_alert(
        symbol="TSLA", spot=335.0, call_wall=330.0, put_wall=290.0,
        total_net_gex=1.0, mm_pressure=None,
    )
    assert result is not None
    assert "TSLA" in result
    assert "330" in result


# ---------- line_notifier ----------


def test_send_line_alert_skips_without_token(monkeypatch):
    monkeypatch.setattr(line_notifier, "LINE_CHANNEL_ACCESS_TOKEN", "")
    assert line_notifier.send_line_alert("測試訊息") is False


def test_send_line_alert_returns_true_on_success(monkeypatch):
    monkeypatch.setattr(line_notifier, "LINE_CHANNEL_ACCESS_TOKEN", "fake-token")
    fake_response = MagicMock(status_code=200)

    with patch("line_notifier.requests.post", return_value=fake_response) as mock_post:
        result = line_notifier.send_line_alert("測試訊息")

    assert result is True
    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args.kwargs
    assert call_kwargs["headers"]["Authorization"] == "Bearer fake-token"
    assert call_kwargs["json"]["messages"][0] == {"type": "text", "text": "測試訊息"}
    assert mock_post.call_args.args[0] == line_notifier.LINE_BROADCAST_URL


def test_send_line_alert_returns_false_on_non_200_status(monkeypatch):
    """實測情境：Token失效時 API 會回傳401/400而不是拋出連線例外——這裡要用
    狀態碼判斷失敗，不能只接住例外。
    """
    monkeypatch.setattr(line_notifier, "LINE_CHANNEL_ACCESS_TOKEN", "fake-token")
    fake_response = MagicMock(status_code=401, text="Invalid access token")

    with patch("line_notifier.requests.post", return_value=fake_response):
        result = line_notifier.send_line_alert("測試訊息")

    assert result is False


def test_send_line_alert_returns_false_on_network_error(monkeypatch):
    monkeypatch.setattr(line_notifier, "LINE_CHANNEL_ACCESS_TOKEN", "fake-token")

    with patch("line_notifier.requests.post", side_effect=ConnectionError("網路斷線")):
        result = line_notifier.send_line_alert("測試訊息")  # 不應該拋出例外

    assert result is False
