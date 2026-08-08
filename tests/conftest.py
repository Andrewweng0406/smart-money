"""跨檔案共用的 pytest fixture。"""

from __future__ import annotations

import pytest

import data_fetcher


@pytest.fixture(autouse=True)
def _disable_yfinance_throttle(monkeypatch):
    """data_fetcher._throttle() 是保護真實 yfinance 呼叫的節流機制（見
    data_fetcher.py 的 MIN_REQUEST_INTERVAL_SECONDS 註解），但單元測試裡
    yf.Ticker 幾乎都被 mock 掉了，不是真的網路請求，不需要真的等待——不關掉
    的話，凡是直接呼叫 data_fetcher 真正函式本體（而不是整支函式被
    monkeypatch 掉）的測試，會因為節流累積出沒必要的真實秒數延遲，拖慢
    整個測試套件。設成 0 秒間隔而不是整個跳過 _throttle()，讓節流邏輯本身
    仍然會被執行到（有 bug 一樣測得出來），只是門檻歸零、不會真的睡著。
    """
    monkeypatch.setattr(data_fetcher, "MIN_REQUEST_INTERVAL_SECONDS", 0.0)
