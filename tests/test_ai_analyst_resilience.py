"""Claude API 是報告的加分項——沒設定金鑰或呼叫失敗都不該讓整份報告產不出來。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import anthropic

import ai_analyst


def test_generate_commentary_skips_without_api_key(monkeypatch):
    monkeypatch.setattr(ai_analyst, "ANTHROPIC_API_KEY", "")

    result = ai_analyst.generate_commentary(
        symbol="TSLA", spot=100.0, max_pain=100.0, call_wall=105.0,
        put_wall=95.0, gamma_flip=98.0, alert=None,
    )

    assert result is None


def test_generate_commentary_returns_none_on_api_connection_error(monkeypatch):
    monkeypatch.setattr(ai_analyst, "ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr(ai_analyst, "_fetch_headlines", lambda symbol, limit=5: [])

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = anthropic.APIConnectionError(request=MagicMock())

    with patch("ai_analyst.anthropic.Anthropic", return_value=fake_client):
        result = ai_analyst.generate_commentary(
            symbol="TSLA", spot=100.0, max_pain=100.0, call_wall=105.0,
            put_wall=95.0, gamma_flip=98.0, alert=None,
        )

    assert result is None


def test_generate_commentary_returns_none_on_non_sdk_exception(monkeypatch):
    """實測過的真實案例：.env 裡如果誤留著範例佔位字串（含中文字元）當 API
    Key，httpx 組 header 時會在送出請求前就丟出 UnicodeEncodeError——這不是
    anthropic.APIStatusError / APIConnectionError 的子類別，必須也接得住。
    """
    monkeypatch.setattr(ai_analyst, "ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr(ai_analyst, "_fetch_headlines", lambda symbol, limit=5: [])

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = UnicodeEncodeError("ascii", "你的key", 0, 1, "ordinal not in range")

    with patch("ai_analyst.anthropic.Anthropic", return_value=fake_client):
        result = ai_analyst.generate_commentary(
            symbol="TSLA", spot=100.0, max_pain=100.0, call_wall=105.0,
            put_wall=95.0, gamma_flip=98.0, alert=None,
        )

    assert result is None


def test_generate_commentary_returns_none_on_refusal(monkeypatch):
    monkeypatch.setattr(ai_analyst, "ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr(ai_analyst, "_fetch_headlines", lambda symbol, limit=5: [])

    fake_response = MagicMock()
    fake_response.stop_reason = "refusal"
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response

    with patch("ai_analyst.anthropic.Anthropic", return_value=fake_client):
        result = ai_analyst.generate_commentary(
            symbol="TSLA", spot=100.0, max_pain=100.0, call_wall=105.0,
            put_wall=95.0, gamma_flip=98.0, alert=None,
        )

    assert result is None


def test_fetch_headlines_returns_empty_list_when_yfinance_news_fails(monkeypatch):
    class _RaisingTicker:
        @property
        def news(self):
            raise ConnectionError("down")

    with patch("ai_analyst.yf.Ticker", return_value=_RaisingTicker()):
        headlines = ai_analyst._fetch_headlines("TSLA")

    assert headlines == []
