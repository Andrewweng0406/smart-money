"""測試 Yahoo Finance 斷線／回傳異常資料時，data_fetcher 是否優雅降級
（回傳空結果或明確的例外），而不是讓 NaN/exception 一路往上炸穿。
"""

from __future__ import annotations

import math
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import data_fetcher


def test_get_spot_price_falls_back_to_fast_info_when_history_fails():
    fake_ticker = MagicMock()
    fake_ticker.history.side_effect = ConnectionError("network down")
    fake_ticker.fast_info = {"lastPrice": 123.45}

    with patch("data_fetcher.yf.Ticker", return_value=fake_ticker):
        price = data_fetcher.get_spot_price("TSLA")

    assert price == 123.45


def test_get_spot_price_raises_clear_error_when_totally_unavailable():
    fake_ticker = MagicMock()
    fake_ticker.history.side_effect = ConnectionError("network down")
    fake_ticker.fast_info = {}  # .get("lastPrice") -> None

    with patch("data_fetcher.yf.Ticker", return_value=fake_ticker):
        with pytest.raises(RuntimeError):
            data_fetcher.get_spot_price("TSLA")


def test_is_market_trading_day_true_when_reference_date_matches_last_bar():
    fake_hist = pd.DataFrame({"Close": [100.0]}, index=pd.to_datetime(["2026-08-03"]))
    fake_ticker = MagicMock()
    fake_ticker.history.return_value = fake_hist

    with patch("data_fetcher.yf.Ticker", return_value=fake_ticker):
        assert data_fetcher.is_market_trading_day(date(2026, 8, 3)) is True


def test_is_market_trading_day_false_on_holiday_where_last_bar_is_stale():
    # 上一根日K是週五（8/1），但目標日期是週一(8/3)遇到假日，SPY還沒有新的一根。
    fake_hist = pd.DataFrame({"Close": [100.0]}, index=pd.to_datetime(["2026-07-31"]))
    fake_ticker = MagicMock()
    fake_ticker.history.return_value = fake_hist

    with patch("data_fetcher.yf.Ticker", return_value=fake_ticker):
        assert data_fetcher.is_market_trading_day(date(2026, 8, 3)) is False


def test_is_market_trading_day_false_when_history_empty():
    fake_ticker = MagicMock()
    fake_ticker.history.return_value = pd.DataFrame()

    with patch("data_fetcher.yf.Ticker", return_value=fake_ticker):
        assert data_fetcher.is_market_trading_day(date(2026, 8, 3)) is False


def test_is_market_trading_day_false_when_query_fails():
    fake_ticker = MagicMock()
    fake_ticker.history.side_effect = ConnectionError("network down")

    with patch("data_fetcher.yf.Ticker", return_value=fake_ticker):
        assert data_fetcher.is_market_trading_day(date(2026, 8, 3)) is False


def test_get_close_price_on_date_returns_that_days_close():
    fake_hist = pd.DataFrame({"Close": [305.5]}, index=pd.to_datetime(["2026-07-15"]))
    fake_ticker = MagicMock()
    fake_ticker.history.return_value = fake_hist

    with patch("data_fetcher.yf.Ticker", return_value=fake_ticker):
        price = data_fetcher.get_close_price_on_date("TSLA", "2026-07-15")

    assert price == 305.5


def test_get_close_price_on_date_returns_none_when_no_data():
    fake_ticker = MagicMock()
    fake_ticker.history.return_value = pd.DataFrame()

    with patch("data_fetcher.yf.Ticker", return_value=fake_ticker):
        assert data_fetcher.get_close_price_on_date("TSLA", "2026-07-15") is None


def test_get_close_price_on_date_returns_none_on_failure():
    fake_ticker = MagicMock()
    fake_ticker.history.side_effect = ConnectionError("network down")

    with patch("data_fetcher.yf.Ticker", return_value=fake_ticker):
        assert data_fetcher.get_close_price_on_date("TSLA", "2026-07-15") is None


class _RaisingOptionsTicker:
    """單獨開一個小類別而不是在 MagicMock 上動態掛 property——MagicMock
    的 property 是掛在共用的類別物件上，會污染同一個行程裡其他測試用到
    的 MagicMock 實例。
    """

    @property
    def options(self):
        raise ConnectionError("down")


def test_get_all_expiries_returns_empty_list_on_api_failure():
    with patch("data_fetcher.yf.Ticker", return_value=_RaisingOptionsTicker()):
        result = data_fetcher.get_all_expiries("TSLA")

    assert result == []


def test_get_option_chain_legs_returns_empty_list_on_api_failure():
    fake_ticker = MagicMock()
    fake_ticker.option_chain.side_effect = ConnectionError("down")

    with patch("data_fetcher.yf.Ticker", return_value=fake_ticker):
        result = data_fetcher.get_option_chain_legs("TSLA", "2026-01-01")

    assert result == []


def test_get_option_chain_legs_sanitizes_nan_and_insane_values():
    """實測過的真實現象：Yahoo 對近到期、無流動性合約常回傳 NaN 或近乎 0 的
    impliedVolatility——這些必須被清成 0，不能讓 NaN 流進 Gamma 計算。
    """
    calls = pd.DataFrame({
        "strike": [100.0, 105.0],
        "openInterest": [float("nan"), 50.0],
        "impliedVolatility": [0.00001, 0.4],  # 第一筆是「假的近零 IV」雜訊
        "volume": [10.0, 20.0],
        "bid": [1.0, 2.0],
        "ask": [1.1, 2.1],
    })
    puts = pd.DataFrame({
        "strike": [100.0],
        "openInterest": [-5.0],  # 負值同樣是異常資料
        "impliedVolatility": [float("inf")],
        "volume": [5.0],
        "bid": [0.5],
        "ask": [0.6],
    })

    fake_chain = MagicMock()
    fake_chain.calls = calls
    fake_chain.puts = puts
    fake_ticker = MagicMock()
    fake_ticker.option_chain.return_value = fake_chain

    with patch("data_fetcher.yf.Ticker", return_value=fake_ticker):
        legs = data_fetcher.get_option_chain_legs("TSLA", "2026-01-01")

    by_strike = {leg.strike: leg for leg in legs}
    assert by_strike[100.0].call_oi == 0.0  # NaN -> 0
    assert by_strike[100.0].call_iv == 0.0  # 近零 IV 雜訊 -> 0
    assert by_strike[100.0].put_oi == 0.0  # 負值 -> 0
    assert by_strike[100.0].put_iv == 0.0  # inf -> 0
    assert not math.isnan(by_strike[100.0].call_oi)
    assert by_strike[105.0].call_oi == 50.0


# ---------- _throttle (rate limiting) ----------

def test_throttle_sleeps_when_called_too_soon_after_previous(monkeypatch):
    """兩次呼叫間隔小於 MIN_REQUEST_INTERVAL_SECONDS 時，應該補足睡眠時間
    到剛好滿足間隔——用假的 time.monotonic()/time.sleep() 驗證邏輯本身，
    不需要真的等待。
    """
    monkeypatch.setattr(data_fetcher, "MIN_REQUEST_INTERVAL_SECONDS", 0.5)
    monkeypatch.setattr(data_fetcher, "_last_request_at", 100.0)

    fake_now = [100.1]  # 距上次呼叫只過了 0.1 秒，小於 0.5 秒門檻
    monkeypatch.setattr(data_fetcher.time, "monotonic", lambda: fake_now[0])
    sleep_calls = []
    monkeypatch.setattr(data_fetcher.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    data_fetcher._throttle()

    assert len(sleep_calls) == 1
    assert sleep_calls[0] == pytest.approx(0.4, abs=1e-9)


def test_throttle_does_not_sleep_when_interval_already_satisfied(monkeypatch):
    monkeypatch.setattr(data_fetcher, "MIN_REQUEST_INTERVAL_SECONDS", 0.5)
    monkeypatch.setattr(data_fetcher, "_last_request_at", 100.0)

    fake_now = [100.9]  # 距上次呼叫已經過了 0.9 秒，超過 0.5 秒門檻
    monkeypatch.setattr(data_fetcher.time, "monotonic", lambda: fake_now[0])
    sleep_calls = []
    monkeypatch.setattr(data_fetcher.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    data_fetcher._throttle()

    assert sleep_calls == []


def test_throttle_updates_last_request_timestamp(monkeypatch):
    monkeypatch.setattr(data_fetcher, "MIN_REQUEST_INTERVAL_SECONDS", 0.5)
    monkeypatch.setattr(data_fetcher, "_last_request_at", 0.0)
    monkeypatch.setattr(data_fetcher.time, "monotonic", lambda: 42.0)
    monkeypatch.setattr(data_fetcher.time, "sleep", lambda seconds: None)

    data_fetcher._throttle()

    assert data_fetcher._last_request_at == 42.0


def test_get_spot_price_calls_throttle(monkeypatch):
    """確認節流真的接進實際會打 yfinance 的函式，不是只有 _throttle() 自己
    測過就沒接上——這是實測會踩到的整合缺口，光測 _throttle() 本身測不出來。
    """
    fake_ticker = MagicMock()
    fake_ticker.history.return_value = pd.DataFrame({"Close": [123.45]})
    calls = []
    monkeypatch.setattr(data_fetcher, "_throttle", lambda: calls.append(1))

    with patch("data_fetcher.yf.Ticker", return_value=fake_ticker):
        data_fetcher.get_spot_price("TSLA")

    assert calls == [1]
