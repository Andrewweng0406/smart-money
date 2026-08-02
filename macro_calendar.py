"""財報與總經事件日期警示工具。

注意：macro_events.json 裡的日期是範例佔位值，使用者需要自行到 Federal
Reserve 官網 (https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm)
或 BLS 官網查最新公布的 FOMC/CPI 日期，定期更新 macro_events.json。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

try:
    import yfinance as yf
except ImportError:  # 沒安裝 yfinance 時，離線純計算功能仍可正常使用。
    yf = None


def _as_date(value: Any) -> date | None:
    """將 yfinance 常見的 datetime、Timestamp 或 ISO 字串轉為 date。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    if hasattr(value, "date"):
        converted = value.date()
        return converted if isinstance(converted, date) else None
    return None


def get_next_earnings_date(symbol: str) -> date | None:
    """從 yfinance 取得最早的未來財報候選日期。"""
    try:
        if yf is None:
            return None

        ticker = yf.Ticker(symbol)
        today = datetime.now().date()
        candidates: list[Any] = []

        calendar = ticker.calendar
        if isinstance(calendar, dict):
            raw_dates = calendar.get("Earnings Date", calendar.get("earningsDate", []))
            candidates.extend(raw_dates if isinstance(raw_dates, (list, tuple)) else [raw_dates])

        # 舊版 yfinance 可能只在 earnings_dates DataFrame 的 index 提供日期。
        if not candidates:
            earnings_dates = ticker.earnings_dates
            if earnings_dates is not None:
                candidates.extend(list(earnings_dates.index))

        normalized = [parsed for value in candidates if (parsed := _as_date(value)) is not None]
        future_dates = [candidate for candidate in normalized if candidate >= today]
        # 莊家做盤視角：只取最近的未來財報，因為越接近事件，IV 與對沖量越快升高。
        return min(future_dates) if future_dates else None
    except Exception:
        # 財報來源的網路、版本或資料格式錯誤不應中斷主分析流程。
        return None


def load_macro_events(path: str = "macro_events.json") -> list[dict]:
    """載入至少含 name 與 YYYY-MM-DD date 的總經事件。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        events = payload["events"]
        if not isinstance(events, list):
            return []

        # 設定檔是人工維護入口，只接受最小必要形狀，避免錯誤事件進入警示流程。
        valid_events = []
        for event in events:
            if not isinstance(event, dict):
                continue
            if not isinstance(event.get("name"), str) or not isinstance(event.get("date"), str):
                continue
            datetime.strptime(event["date"], "%Y-%m-%d")
            valid_events.append(event)
        return valid_events
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return []


def compute_days_until(event_date: date, today: date | None = None) -> int:
    """計算事件日距指定日期（預設今天）的日曆天數。"""
    reference_date = today if today is not None else datetime.now().date()
    return (event_date - reference_date).days


def build_warning_text(event_name: str, days_until: int) -> str | None:
    """在事件進入三日風險窗時建立固定格式警示。"""
    # 莊家做盤視角：事件前三天 IV 常開始升溫，Gamma/Delta 對沖可能放大現貨波幅。
    if 0 <= days_until <= 3:
        return (
            f"⚠️ 【高波動預警】距離 {event_name} 僅剩 {days_until} 天，"
            "IV 預期飆升，做市商對沖引發的波幅將放大！"
        )
    return None


def get_calendar_warnings(
    symbol: str, macro_events_path: str = "macro_events.json"
) -> list[str]:
    """整合下一次財報與人工維護的總經事件警示。"""
    try:
        warnings: list[str] = []

        earnings_date = get_next_earnings_date(symbol)
        if earnings_date is not None:
            warning = build_warning_text(
                f"{symbol} 財報", compute_days_until(earnings_date)
            )
            if warning is not None:
                warnings.append(warning)

        for event in load_macro_events(macro_events_path):
            event_date = datetime.strptime(event["date"], "%Y-%m-%d").date()
            warning = build_warning_text(
                event["name"], compute_days_until(event_date)
            )
            if warning is not None:
                warnings.append(warning)

        return warnings
    except Exception:
        # 日曆是可選增強功能，任何意外都應優雅降級為沒有警示。
        return []
