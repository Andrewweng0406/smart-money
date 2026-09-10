"""美股正式交易時段的單一真相來源。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal


US_EASTERN = ZoneInfo("America/New_York")
DAILY_ANALYSIS_DELAY = timedelta(minutes=30)
_NYSE = mcal.get_calendar("NYSE")


@dataclass(frozen=True)
class MarketSession:
    market_open: datetime
    market_close: datetime


@lru_cache(maxsize=512)
def get_market_session(trading_date: date) -> MarketSession | None:
    """回傳指定日期的正式交易時段；休市日回傳 None。"""
    schedule = _NYSE.schedule(start_date=trading_date, end_date=trading_date)
    if schedule.empty:
        return None

    row = schedule.iloc[0]
    return MarketSession(
        market_open=row["market_open"].to_pydatetime().astimezone(US_EASTERN),
        market_close=row["market_close"].to_pydatetime().astimezone(US_EASTERN),
    )


def is_market_trading_day(trading_date: date) -> bool:
    """判斷指定日期是否有正式交易 session。"""
    return get_market_session(trading_date) is not None


def is_market_hours(now: datetime) -> bool:
    """判斷時間點是否位於當日實際正式交易時段內。"""
    now_et = now.astimezone(US_EASTERN)
    session = get_market_session(now_et.date())
    return session is not None and session.market_open <= now_et <= session.market_close


def daily_analysis_time(trading_date: date) -> datetime | None:
    """回傳當日實際收盤後 30 分鐘；休市日沒有執行時間。"""
    session = get_market_session(trading_date)
    return session.market_close + DAILY_ANALYSIS_DELAY if session else None
