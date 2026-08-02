"""純計算的 Gamma Exposure (GEX) 引擎 —— 不做任何網路 I/O。

只吃數字/numpy 陣列進來，回傳數字出去，方便單獨測試，也方便未來換掉資料源
（例如從 yfinance 換成其他券商 API）時完全不用動這支檔案。

Net GEX 採用業界（SqueezeMetrics / SpotGamma 風格）常見的假設：
「做市商（dealer）長 Call、短 Put」，所以：
    Net GEX = (Call_OI * Call_Gamma - Put_OI * Put_Gamma) * S^2 * 100 * 0.01
這是「現貨每變動 1%」對應的美元 Gamma 曝險（SpotGamma等業界工具常見的標示
慣例）——實測抓到的真bug：先前公式少乘了最後這個 0.01，數字因此比業界慣例
的「每1%GEX」大了100倍，且docstring原本誤標成「每變動$1」（真正的「每變動
$1」公式應該只有一次 spot、不是 spot²：Gamma*OI*100*S，不是Gamma*OI*100*S²）。
正負號跟履約價之間的相對排序不受影響（絕對值倍率一致地跟著縮放），只有
絕對金額被少乘 0.01 而誇大了100倍。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

import numpy as np
from scipy.stats import norm

CONTRACT_MULTIPLIER = 100  # 美股標準期權合約乘數：1口 = 100股

OptionType = Literal["call", "put"]


def _d1(spot: np.ndarray, strike: np.ndarray, time_to_expiry: np.ndarray, risk_free_rate: float, iv: np.ndarray) -> np.ndarray:
    return (
        np.log(spot / strike) + (risk_free_rate + 0.5 * iv**2) * time_to_expiry
    ) / (iv * np.sqrt(time_to_expiry))


def black_scholes_gamma(
    spot: float | np.ndarray,
    strike: float | np.ndarray,
    time_to_expiry_years: float | np.ndarray,
    iv: float | np.ndarray,
    risk_free_rate: float = 0.045,
) -> np.ndarray:
    """Black-Scholes Gamma（Call/Put 公式相同）。向量化，可一次算整條期權鏈。"""
    spot = np.asarray(spot, dtype=float)
    strike = np.asarray(strike, dtype=float)
    time_to_expiry = np.asarray(time_to_expiry_years, dtype=float)
    iv = np.asarray(iv, dtype=float)

    # 已到期或 IV<=0 的合約 Gamma 沒有意義，回傳 0 而不是 NaN/inf，
    # 避免一筆壞資料污染整個向量化加總。
    safe = (time_to_expiry > 0) & (iv > 0) & (spot > 0) & (strike > 0)
    time_safe = np.where(safe, time_to_expiry, 1.0)
    iv_safe = np.where(safe, iv, 1.0)

    d1 = _d1(spot, strike, time_safe, risk_free_rate, iv_safe)
    gamma = norm.pdf(d1) / (spot * iv_safe * np.sqrt(time_safe))
    return np.where(safe, gamma, 0.0)


def black_scholes_delta(
    spot: float | np.ndarray,
    strike: float | np.ndarray,
    time_to_expiry_years: float | np.ndarray,
    iv: float | np.ndarray,
    option_type: OptionType | np.ndarray,
    risk_free_rate: float = 0.045,
) -> np.ndarray:
    """Black-Scholes Delta：Call delta = N(d1)；Put delta = N(d1) - 1。

    給期權策略引擎估算理論勝率用（機率OTM ≈ 1 - |短腿Delta|，業界常見的
    Probability of Profit 近似算法）。option_type 可以是單一 "call"/"put"
    字串（會自動 broadcast），也可以是跟 strike/iv 等長的陣列。
    """
    spot = np.asarray(spot, dtype=float)
    strike = np.asarray(strike, dtype=float)
    time_to_expiry = np.asarray(time_to_expiry_years, dtype=float)
    iv = np.asarray(iv, dtype=float)

    safe = (time_to_expiry > 0) & (iv > 0) & (spot > 0) & (strike > 0)
    time_safe = np.where(safe, time_to_expiry, 1.0)
    iv_safe = np.where(safe, iv, 1.0)

    d1 = _d1(spot, strike, time_safe, risk_free_rate, iv_safe)
    call_delta = norm.cdf(d1)
    is_call = np.asarray(option_type) == "call"
    delta = np.where(is_call, call_delta, call_delta - 1.0)
    return np.where(safe, delta, np.nan)


@dataclass
class OptionLeg:
    strike: float
    call_oi: float
    call_iv: float
    put_oi: float
    put_iv: float
    time_to_expiry_years: float  # 每個履約價可能來自不同到期日，Gamma 要用各自的到期時間算


def compute_net_gex_by_strike(legs: Sequence[OptionLeg], spot: float, risk_free_rate: float = 0.045) -> list[dict]:
    """依履約價彙總 Net GEX（同一履約價若橫跨多個到期日，先在呼叫端合併成一筆
    再傳進來，或直接傳多筆同 strike 的 leg 讓這裡自然加總——兩種用法都支援，
    因為這裡只是逐筆算完再用 strike 分組加總，不假設輸入已經去重）。
    """
    if not legs:
        return []

    strikes = np.array([leg.strike for leg in legs], dtype=float)
    call_oi = np.array([leg.call_oi for leg in legs], dtype=float)
    call_iv = np.array([leg.call_iv for leg in legs], dtype=float)
    put_oi = np.array([leg.put_oi for leg in legs], dtype=float)
    put_iv = np.array([leg.put_iv for leg in legs], dtype=float)
    time_arr = np.array([leg.time_to_expiry_years for leg in legs], dtype=float)

    call_gamma = black_scholes_gamma(spot, strikes, time_arr, call_iv, risk_free_rate)
    put_gamma = black_scholes_gamma(spot, strikes, time_arr, put_iv, risk_free_rate)

    # *0.01：現貨每變動「1%」（而不是每變動「1股/1單位」）對應的美元曝險，
    # 業界（SpotGamma等）標示 GEX 的標準慣例——見檔頭 docstring。
    scale = spot**2 * CONTRACT_MULTIPLIER * 0.01
    call_gex = call_oi * call_gamma * scale
    put_gex = put_oi * put_gamma * scale

    # 同一履約價可能來自多個到期日的多筆 leg，這裡用字典依 strike 加總
    agg: dict[float, dict] = {}
    for i in range(len(strikes)):
        k = float(strikes[i])
        row = agg.setdefault(k, {"strike": k, "call_gex": 0.0, "put_gex": 0.0, "call_oi": 0.0, "put_oi": 0.0})
        row["call_gex"] += float(call_gex[i])
        row["put_gex"] += float(put_gex[i])
        row["call_oi"] += float(call_oi[i])
        row["put_oi"] += float(put_oi[i])

    result = []
    for row in agg.values():
        row["net_gex"] = row["call_gex"] - row["put_gex"]
        result.append(row)

    return sorted(result, key=lambda r: r["strike"])


def _find_nearest_zero_crossing(x_values: Sequence[float], y_values: Sequence[float], reference_x: float) -> float | None:
    """在一條由 (x_values, y_values) 描述的曲線上，找離 reference_x 最近的
    那個 y 由負轉正（或正轉負）的交叉點（線性內插）。純數學工具，不管
    x/y 軸實際代表什麼——find_gamma_flip_point 用它在『假設現貨價』軸上找
    交叉，但這支函式本身跟金融公式無關，方便獨立測試內插邏輯本身對不對。
    """
    crossings: list[float] = []
    for i in range(1, len(y_values)):
        prev, curr = y_values[i - 1], y_values[i]
        if prev == 0:
            crossings.append(float(x_values[i - 1]))
        elif (prev < 0) != (curr < 0):
            frac = abs(prev) / (abs(prev) + abs(curr))
            crossings.append(float(x_values[i - 1] + frac * (x_values[i] - x_values[i - 1])))

    if not crossings:
        return None
    return min(crossings, key=lambda x: abs(x - reference_x))


def compute_net_gex_curve(
    legs: Sequence[OptionLeg], spot: float, risk_free_rate: float = 0.045,
    price_range_pct: float = 0.30, grid_points: int = 241,
) -> list[dict]:
    """建立一段以現貨價為中心的假設現貨價格網格，在每一個假設價格點都重新
    計算所有期權的 Gamma（履約價/OI/IV/到期時間維持不變，只有這裡的『假設
    現貨價』會變），加總成那個假設價格下的 Total Net GEX。

    這是業界定義的 Gamma Flip 概念本身：『如果現貨真的走到這個價格，做市商
    淨Gamma部位的正負號會是什麼』——是在『假設現貨價』這個軸上掃描。舊版
    find_gamma_flip_point 是把『現在這個當下、不同履約價各自的GEX貢獻』
    由低到高累加找交叉點，是在『履約價』這個軸上掃描，兩者是完全不同的
    東西：後者容易被彙總多個到期日後、遠離現貨的稀疏舊倉位（LEAPS留下的）
    造成的假交叉誤導，因為那些履約價的 Gamma 是用「現在的現貨價」算的，
    根本沒有模擬「現貨真的走到那裡」的情境。

    回傳依假設現貨價由低到高排序的 [{"spot": S, "net_gex": ...}, ...]。
    """
    if not legs or spot <= 0:
        return []

    strikes = np.array([leg.strike for leg in legs], dtype=float)
    call_oi = np.array([leg.call_oi for leg in legs], dtype=float)
    call_iv = np.array([leg.call_iv for leg in legs], dtype=float)
    put_oi = np.array([leg.put_oi for leg in legs], dtype=float)
    put_iv = np.array([leg.put_iv for leg in legs], dtype=float)
    time_arr = np.array([leg.time_to_expiry_years for leg in legs], dtype=float)

    price_grid = np.linspace(spot * (1 - price_range_pct), spot * (1 + price_range_pct), grid_points)

    curve = []
    for hypothetical_spot in price_grid:
        call_gamma = black_scholes_gamma(hypothetical_spot, strikes, time_arr, call_iv, risk_free_rate)
        put_gamma = black_scholes_gamma(hypothetical_spot, strikes, time_arr, put_iv, risk_free_rate)
        # 跟 compute_net_gex_by_strike 用同一套「每變動1%」的單位慣例（*0.01）
        # ——這裡是找零交叉點，乘上正的常數並不影響交叉點位置，但維持全專案
        # GEX 數值單位一致，之後如果直接顯示這條曲線也不會跟其他地方對不上。
        scale = float(hypothetical_spot) ** 2 * CONTRACT_MULTIPLIER * 0.01
        net_gex = float(np.sum(call_oi * call_gamma * scale - put_oi * put_gamma * scale))
        curve.append({"spot": float(hypothetical_spot), "net_gex": net_gex})
    return curve


def find_gamma_flip_point(
    legs: Sequence[OptionLeg], spot: float, risk_free_rate: float = 0.045,
    price_range_pct: float = 0.30, grid_points: int = 241,
) -> float | None:
    """在假設現貨價格網格上重新計算出的 Total Net GEX 曲線（見
    compute_net_gex_curve）裡，找離現貨最近的那個正負號交叉點——現貨低於
    此點時做市商淨空 Gamma（避險行為傾向放大波動，即「大負GEX區域」）；
    高於此點則淨多 Gamma（避險行為抑制波動）。找不到交叉（例如整條曲線都
    同號）時回傳 None。
    """
    curve = compute_net_gex_curve(legs, spot, risk_free_rate, price_range_pct, grid_points)
    if len(curve) < 2:
        return None

    prices = [row["spot"] for row in curve]
    values = [row["net_gex"] for row in curve]
    return _find_nearest_zero_crossing(prices, values, reference_x=spot)


def gamma_flip_distance_pct(spot: float, gamma_flip: float | None) -> float | None:
    """現貨價距離 Gamma 翻轉點的百分比。正值代表現貨在翻轉點之上（做市商淨多
    Gamma，避險行為抑制波動）；負值代表現貨在翻轉點之下（淨空 Gamma，避險行為
    傾向放大波動）。gamma_flip 為 None（無法計算）時回傳 None。
    """
    if gamma_flip is None or gamma_flip == 0:
        return None
    return (spot - gamma_flip) / gamma_flip * 100


def summarize_zero_dte_contribution(
    total_gex_by_strike: Sequence[dict], zero_dte_gex_by_strike: Sequence[dict],
) -> dict:
    """比較『所有到期日加總』與『僅 0DTE（當日到期）』的 Net GEX 差異。

    0DTE 選擇權收盤後全部歸零，若當日 Net GEX 有一大部分來自 0DTE 合約，
    代表隔天開盤 Gamma 結構可能劇烈改變（俗稱「Gamma 斷崖」）——這是只看
    單一總量數字看不出來的風險，所以額外拆開 0DTE vs. 非0DTE 兩塊。
    """
    total = sum(row["net_gex"] for row in total_gex_by_strike)
    zero_dte = sum(row["net_gex"] for row in zero_dte_gex_by_strike) if zero_dte_gex_by_strike else 0.0
    ex_zero_dte = total - zero_dte
    # 分母接近 0 時佔比沒有意義（可能被除出離譜的數字），回傳 None 讓報告顯示 N/A
    zero_dte_share_pct = (zero_dte / total * 100) if abs(total) > 1e-9 else None
    return {
        "total_net_gex": total,
        "zero_dte_net_gex": zero_dte,
        "ex_zero_dte_net_gex": ex_zero_dte,
        "zero_dte_share_pct": zero_dte_share_pct,
    }
