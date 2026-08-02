"""yfinance 資料存取層 —— 免費、不需登入，抓現貨價與期權鏈。

單一 CLI 腳本一次性執行完就結束，不像常駐服務要保護事件迴圈，所以這裡刻意
用同步 (blocking) 呼叫，不包 asyncio，保持簡單。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

import yfinance as yf

logger = logging.getLogger("options_gex")

# IV 超出這個範圍視為資料雜訊（深度價內/價外、極端近到期合約常見的失真數值），
# 捨棄不用 —— 這是真實市場資料現象，不是程式錯誤。
IV_SANE_MIN = 0.05
IV_SANE_MAX = 5.0


@dataclass
class StrikeLegRaw:
    expiry: str
    strike: float
    call_oi: float
    call_iv: float
    call_volume: float
    put_oi: float
    put_iv: float
    put_volume: float
    # bid/ask 給期權價差策略引擎算真實權利金用（options_strategy_engine.py）；
    # GEX/Smart Money 計算本身用不到，只是跟 OI/IV 共用同一次期權鏈查詢，
    # 不用再多打一次 yfinance。沒有 sane_min/max 限制——0 代表這檔合約完全
    # 沒人報價（無流動性），呼叫端本來就要看到真實的 0，不是被清成別的值。
    call_bid: float = 0.0
    call_ask: float = 0.0
    put_bid: float = 0.0
    put_ask: float = 0.0


def _clean(value, *, sane_min: float | None = None, sane_max: float | None = None) -> float:
    """pandas 缺值是 NaN（float），不是 None，不能用 `value or 0.0` 接（NaN 是
    truthy），一定要明確判斷 isnan，否則 NaN 會一路帶進 Gamma 乘法污染整條曲線。
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(f) or math.isinf(f) or f < 0:
        return 0.0
    if sane_max is not None and f > sane_max:
        return 0.0
    if sane_min is not None and 0 < f < sane_min:
        return 0.0
    return f


def get_spot_price(symbol: str) -> float:
    """優先用最近一根日K的收盤價（『當日收盤價』的定義），抓不到才退回即時報價。"""
    ticker = yf.Ticker(symbol)
    try:
        hist = ticker.history(period="5d", interval="1d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception as exc:  # noqa: BLE001
        logger.warning("抓取 %s 日K收盤價失敗：%s", symbol, exc)

    try:
        price = ticker.fast_info.get("lastPrice")
        if price:
            return float(price)
    except Exception as exc:  # noqa: BLE001
        logger.warning("抓取 %s 即時報價失敗：%s", symbol, exc)

    raise RuntimeError(f"無法取得 {symbol} 的現貨價格")


def get_all_expiries(symbol: str, max_expiries: int | None = None) -> list[str]:
    """回傳所有未來到期日（yyyy-MM-dd），依時間排序；max_expiries 可限制只取
    最近的 N 個到期日（避免像 SPY 這種標的到期日太多、抓一次要打幾十次 API）。
    """
    ticker = yf.Ticker(symbol)
    try:
        expiries = list(ticker.options)
    except Exception as exc:  # noqa: BLE001
        logger.warning("查詢 %s 到期日清單失敗：%s", symbol, exc)
        return []

    today = datetime.now(timezone.utc).date().isoformat()
    future = sorted(e for e in expiries if e >= today)
    if max_expiries is not None:
        future = future[:max_expiries]
    return future


def get_expiry_by_dte(symbol: str, min_days: int, max_days: int, target_days: int) -> str | None:
    """給期權價差策略引擎用——GEX 模塊要的是「最近到期日」（gamma曝險看當下
    最活躍的合約），但賣方價差策略要的是 30-45 天左右的到期日（太近權利金
    太薄、太遠時間效率差），兩者需求不同。

    在 [min_days, max_days] 範圍內找最接近 target_days 的到期日；範圍內沒有
    的話，退而求其次選整個未來到期日清單裡最接近 target_days 的那個（例如
    小型股常常只有月選擇權，剛好卡在範圍外一點點），完全沒有到期日回傳 None。
    """
    ticker = yf.Ticker(symbol)
    try:
        expiries = list(ticker.options)
    except Exception as exc:  # noqa: BLE001
        logger.warning("查詢 %s 到期日清單失敗：%s", symbol, exc)
        return None

    if not expiries:
        return None

    today = datetime.now(timezone.utc).date()
    candidates = []
    for e in expiries:
        try:
            dte = (datetime.strptime(e, "%Y-%m-%d").date() - today).days
        except ValueError:
            continue
        if dte > 0:
            candidates.append((e, dte))

    if not candidates:
        return None

    in_range = [c for c in candidates if min_days <= c[1] <= max_days]
    pool = in_range if in_range else candidates
    best = min(pool, key=lambda c: abs(c[1] - target_days))
    return best[0]


def get_option_chain_legs(symbol: str, expiry: str) -> list[StrikeLegRaw]:
    """取得單一到期日的完整期權鏈，依履約價彙整成 Call/Put 成對的資料列。"""
    ticker = yf.Ticker(symbol)
    try:
        chain = ticker.option_chain(expiry)
    except Exception as exc:  # noqa: BLE001
        logger.warning("查詢 %s %s 期權鏈失敗：%s", symbol, expiry, exc)
        return []

    legs_by_strike: dict[float, StrikeLegRaw] = {}

    def _apply(rows, is_call: bool) -> None:
        for _, row in rows.iterrows():
            strike = _clean(row["strike"])
            leg = legs_by_strike.setdefault(
                strike,
                StrikeLegRaw(
                    expiry=expiry, strike=strike,
                    call_oi=0.0, call_iv=0.0, call_volume=0.0,
                    put_oi=0.0, put_iv=0.0, put_volume=0.0,
                ),
            )
            oi = _clean(row.get("openInterest"))
            iv = _clean(row.get("impliedVolatility"), sane_min=IV_SANE_MIN, sane_max=IV_SANE_MAX)
            volume = _clean(row.get("volume"))
            bid = _clean(row.get("bid"))
            ask = _clean(row.get("ask"))
            if is_call:
                leg.call_oi, leg.call_iv, leg.call_volume = oi, iv, volume
                leg.call_bid, leg.call_ask = bid, ask
            else:
                leg.put_oi, leg.put_iv, leg.put_volume = oi, iv, volume
                leg.put_bid, leg.put_ask = bid, ask

    _apply(chain.calls, is_call=True)
    _apply(chain.puts, is_call=False)

    return list(legs_by_strike.values())


def time_to_expiry_years(expiry: str) -> float:
    """到期日字串轉換成距今的年化時間（Black-Scholes 公式需要的單位）。"""
    expiry_date = datetime.strptime(expiry, "%Y-%m-%d").replace(
        hour=21, minute=0, tzinfo=timezone.utc  # 約當美東收盤時間，避免當天到期算出 0
    )
    now = datetime.now(timezone.utc)
    seconds = max((expiry_date - now).total_seconds(), 3600.0)  # 至少留1小時避免除以極小值
    return seconds / (365.25 * 24 * 3600)
