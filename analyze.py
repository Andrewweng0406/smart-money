#!/usr/bin/env python3
"""美股期權 GEX（Gamma Exposure）分析腳本。

用法：
    python analyze.py --symbol TSLA
    python analyze.py --symbol NVDA --max-expiries 6 --notify

流程：抓現貨價 + 所有到期日的期權鏈 -> 依履約價彙總 Call/Put OI、成交量、
Net GEX -> 找出 Max Pain / Call Wall / Put Wall -> 畫圖 -> 輸出 Markdown
盤後複習報告 -> （可選）推播到 Telegram。
"""

from __future__ import annotations

import argparse
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import plotly.graph_objects as go

import dashboard_generator
import data_fetcher
import db_manager
import line_formatter
import line_notifier
import macro_calendar
import options_strategy_engine
import pinning_engine
import smart_money
from gex_engine import (
    OptionLeg,
    compute_net_gex_by_strike,
    find_gamma_flip_point,
    gamma_flip_distance_pct,
    summarize_zero_dte_contribution,
)

# 30-45天期權策略引擎要用的到期日範圍：太近權利金太薄、太遠時間效率差，
# 是賣方價差策略的常見慣例（沿用自舊專案 options_strategy_engine.py 的假設）。
STRATEGY_MIN_DTE = 25
STRATEGY_MAX_DTE = 45
STRATEGY_TARGET_DTE = 35

# 異常大單偵測只看現價附近的履約價——實測過的真實現象：彙總多個到期日後，
# 遠價外的 LEAPS 履約價（現價20%以外）常常 OI=0 但當天有一點點成交量，
# ratio 會被算成無限大，把真正靠近現價、有參考價值的異常大單擠出前5名。
UNUSUAL_ACTIVITY_MAX_DISTANCE_PCT = 0.20

# 用 Path(__file__).parent 而不是寫死 ~/Desktop/stock.agent——舊版用
# Path.home()/"Desktop"/"stock.agent" 展開，只解決了「換一台機器、換使用者
# 帳號」的可攜性，卻在「同一台機器上把專案資料夾搬到別的地方」時整個失效
# （實測踩到：專案搬出 ~/Desktop 以解決 macOS TCC 權限問題後，這個常數還是
# 指向已經不存在的舊路徑）。跟 db_manager.DEFAULT_DB_PATH 用同一個慣例，
# 才是真正不受專案位置影響的寫法。
DEFAULT_DASHBOARD_PATH = Path(__file__).parent / "dashboard" / "index.html"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("options_gex")

# 判定「大負 GEX 區域」的門檻：現貨價低於 Gamma 翻轉點時，做市商淨空 Gamma，
# 避險買賣行為會放大而非抑制波動，是常見的高風險訊號。
NEGATIVE_GEX_ALERT_TEXT = "⚠️ 做市商對沖賣壓風險高，注意洗盤與連鎖拋售"


@dataclass
class AnalysisResult:
    symbol: str
    spot: float
    expiries_used: list[str]
    gex_by_strike: list[dict]  # strike, call_gex, put_gex, net_gex, call_oi, put_oi
    volume_by_strike: dict[float, dict]  # strike -> {call_volume, put_volume}
    max_pain: float
    call_wall: float
    put_wall: float
    gamma_flip: float | None
    gamma_flip_distance_pct: float | None
    zero_dte_summary: dict
    alert: str | None
    # Smart Money 指標——都有預設值，是額外的加分欄位，不影響既有呼叫端
    # （包括測試裡手動建構 AnalysisResult 的地方）。
    iv_skew: float | None = None
    put_call_ratio: dict = field(default_factory=dict)
    unusual_activity: list = field(default_factory=list)
    mm_pressure: dict | None = None
    pinning: dict | None = None


def _load_previous_oi_snapshot(symbol: str, db_path: Path | str) -> dict[float, dict] | None:
    """查前一個交易日的逐履約價OI快照，給 detect_unusual_activity 判斷
    「新開倉還是平倉/轉倉」用。找不到（例如系統第一次跑、還沒有歷史資料）
    回傳 None，detect_unusual_activity 會把 likely_opening 都設成 None，
    不會出錯——這是加分項中的加分項，查詢失敗不該讓其他 Smart Money
    指標（IV Skew/PCR）也一起算不出來。
    """
    try:
        today_str = data_fetcher.current_trading_date_str()
        previous_date = db_manager.get_most_recent_oi_snapshot_date(symbol, today_str, db_path=db_path)
        if previous_date is None:
            return None
        return db_manager.get_oi_snapshot(symbol, previous_date, db_path=db_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s 查詢前一日OI快照失敗：%s", symbol, exc)
        return None


def save_oi_snapshot_if_trading_day(
    symbol: str, result: AnalysisResult, date_str: str, db_path: Path | str = db_manager.DEFAULT_DB_PATH,
) -> None:
    """把今天的逐履約價OI存進歷史快照，給明天判斷「新開倉還是平倉/轉倉」
    用。跟 save_snapshot 一樣是加分項，寫入失敗只記警告，呼叫端應該只在
    data_fetcher.is_market_trading_day() 為真時才呼叫這支函式。
    """
    try:
        oi_legs = [
            {"strike": row["strike"], "call_oi": row["call_oi"], "put_oi": row["put_oi"]}
            for row in result.gex_by_strike
        ]
        db_manager.save_oi_snapshot(symbol, date_str, oi_legs, db_path=db_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s OI快照寫入失敗：%s", symbol, exc)


def fetch_and_aggregate(
    symbol: str, max_expiries: int | None, risk_free_rate: float,
    db_path: Path | str = db_manager.DEFAULT_DB_PATH,
) -> AnalysisResult:
    spot = data_fetcher.get_spot_price(symbol)
    expiries = data_fetcher.get_all_expiries(symbol, max_expiries=max_expiries)
    if not expiries:
        raise RuntimeError(f"{symbol} 沒有可用的到期日期權鏈")

    today = datetime.now(timezone.utc).date().isoformat()

    all_legs: list[OptionLeg] = []
    zero_dte_legs: list[OptionLeg] = []  # 當日到期（0DTE）合約另外存一份，算「Gamma 斷崖」風險用
    all_raw_legs: list[data_fetcher.StrikeLegRaw] = []  # 給 Smart Money 指標用，需要 volume/IV 原始值
    volume_by_strike: dict[float, dict] = defaultdict(lambda: {"call_volume": 0.0, "put_volume": 0.0})

    for expiry in expiries:
        tte = data_fetcher.time_to_expiry_years(expiry)
        raw_legs = data_fetcher.get_option_chain_legs(symbol, expiry)
        is_zero_dte = expiry == today
        all_raw_legs.extend(raw_legs)
        for raw in raw_legs:
            leg = OptionLeg(
                strike=raw.strike, call_oi=raw.call_oi, call_iv=raw.call_iv,
                put_oi=raw.put_oi, put_iv=raw.put_iv, time_to_expiry_years=tte,
            )
            all_legs.append(leg)
            if is_zero_dte:
                zero_dte_legs.append(leg)
            volume_by_strike[raw.strike]["call_volume"] += raw.call_volume
            volume_by_strike[raw.strike]["put_volume"] += raw.put_volume

    gex_by_strike = compute_net_gex_by_strike(all_legs, spot=spot, risk_free_rate=risk_free_rate)
    if not gex_by_strike:
        raise RuntimeError(f"{symbol} 期權鏈彙總後沒有有效資料")

    zero_dte_gex_by_strike = (
        compute_net_gex_by_strike(zero_dte_legs, spot=spot, risk_free_rate=risk_free_rate)
        if zero_dte_legs else []
    )

    max_pain = _calculate_max_pain(gex_by_strike)
    call_wall = max(gex_by_strike, key=lambda r: r["call_oi"])["strike"]
    put_wall = max(gex_by_strike, key=lambda r: r["put_oi"])["strike"]
    gamma_flip = find_gamma_flip_point(all_legs, spot=spot, risk_free_rate=risk_free_rate)
    flip_distance_pct = gamma_flip_distance_pct(spot, gamma_flip)
    zero_dte_summary = summarize_zero_dte_contribution(gex_by_strike, zero_dte_gex_by_strike)

    alert = None
    if spot < put_wall or (gamma_flip is not None and spot < gamma_flip):
        alert = NEGATIVE_GEX_ALERT_TEXT

    try:
        smart_legs = _aggregate_smart_money_legs(all_raw_legs)
        iv_skew = smart_money.compute_iv_skew(smart_legs, spot)
        put_call_ratio = smart_money.compute_put_call_ratio(smart_legs)
        # PCR/IV Skew 本來就該看全部履約價（或 compute_iv_skew 自己控制的OTM
        # 範圍），但異常大單偵測只篩選現價附近的——太遠的履約價 OI=0 很正常
        # （根本沒人在意那個履約價），不該被當成「異常」冒出來。
        nearby_legs = [
            leg for leg in smart_legs if abs(leg.strike - spot) / spot <= UNUSUAL_ACTIVITY_MAX_DISTANCE_PCT
        ]
        previous_oi_by_strike = _load_previous_oi_snapshot(symbol, db_path)
        unusual_activity = smart_money.detect_unusual_activity(
            nearby_legs, previous_oi_by_strike=previous_oi_by_strike,
        )
        # in_negative_gamma 要單純看「現貨是否低於 Gamma 翻轉點」，不能沿用
        # alert（alert 也會因為單純跌破 Put Wall 而觸發，即使現貨仍在
        # Gamma Flip 之上、做市商其實還是淨多 Gamma）——這是實測抓到的真bug：
        # 混用會讓莊家壓力分數平白多加50分，並可能誤觸「死亡Loop」警示。
        in_negative_gamma = gamma_flip is not None and spot < gamma_flip
        mm_pressure = smart_money.compute_market_maker_pressure_score(
            spot, put_wall, in_negative_gamma=in_negative_gamma, legs=smart_legs,
        )
    except Exception as exc:  # noqa: BLE001
        # Smart Money 指標是報告的加分項，計算失敗不該影響核心的 GEX/Wall/Max Pain 結果。
        logger.warning("%s Smart Money 指標計算失敗：%s", symbol, exc)
        iv_skew, put_call_ratio, unusual_activity, mm_pressure = None, {}, [], None

    try:
        # 只有確認 gamma_flip 存在且現貨真的在其之上，才算「確認正Gamma」——
        # gamma_flip 算不出來時保守回傳 False（未確認），不要沿用
        # in_negative_gamma 的預設值（那個變數在 gamma_flip 為 None 時預設
        # False，若直接取反會誤把「無法判斷」當成「確認正Gamma」，讓
        # Pinning 的必要條件在資料不足時被錯誤地判定成立）。
        in_positive_gamma = gamma_flip is not None and spot >= gamma_flip
        pinning = pinning_engine.compute_pinning_analysis(
            gex_by_strike, spot, max_pain, call_wall, put_wall, in_positive_gamma,
        )
    except Exception as exc:  # noqa: BLE001
        # Pinning 判斷是報告的加分項，計算失敗不該影響核心的 GEX/Wall/Max Pain 結果。
        logger.warning("%s Pinning 判斷計算失敗：%s", symbol, exc)
        pinning = None

    return AnalysisResult(
        symbol=symbol, spot=spot, expiries_used=expiries, gex_by_strike=gex_by_strike,
        volume_by_strike=dict(volume_by_strike), max_pain=max_pain, call_wall=call_wall,
        put_wall=put_wall, gamma_flip=gamma_flip, gamma_flip_distance_pct=flip_distance_pct,
        zero_dte_summary=zero_dte_summary, alert=alert,
        iv_skew=iv_skew, put_call_ratio=put_call_ratio,
        unusual_activity=unusual_activity, mm_pressure=mm_pressure, pinning=pinning,
    )


def _aggregate_smart_money_legs(raw_legs: list[data_fetcher.StrikeLegRaw]) -> list[SimpleNamespace]:
    """把跨到期日的原始期權鏈合併成 smart_money.py 要的單一履約價清單——
    OI/成交量直接加總（跟 GEX 彙總邏輯一致，代表「這個履約價across所有
    考慮中的到期日」的總量）；IV 則用「有效樣本」（排除資料清洗後變成0的
    雜訊值，見 data_fetcher.IV_SANE_MIN/MAX）取平均，避免 0 把偏斜判斷拉低。

    time_to_expiry_years 同樣取簡單平均（同一履約價跨到期日混在一起時，
    沒有單一「正確」的到期時間，用平均值當 Delta 加權成交量計算的近似
    代表值）——StrikeLegRaw 本身有 expiry 字串，用
    data_fetcher.time_to_expiry_years() 換算成年化時間（純日期運算，不是
    網路 I/O，可以放心在這個彙總函式裡呼叫）。
    """
    accum: dict[float, dict] = {}
    for raw in raw_legs:
        row = accum.setdefault(raw.strike, {
            "call_oi": 0.0, "put_oi": 0.0, "call_volume": 0.0, "put_volume": 0.0,
            "call_iv_samples": [], "put_iv_samples": [], "tte_samples": [],
        })
        row["call_oi"] += raw.call_oi
        row["put_oi"] += raw.put_oi
        row["call_volume"] += raw.call_volume
        row["put_volume"] += raw.put_volume
        if raw.call_iv > 0:
            row["call_iv_samples"].append(raw.call_iv)
        if raw.put_iv > 0:
            row["put_iv_samples"].append(raw.put_iv)
        row["tte_samples"].append(data_fetcher.time_to_expiry_years(raw.expiry))

    legs = []
    for strike, row in accum.items():
        call_iv = sum(row["call_iv_samples"]) / len(row["call_iv_samples"]) if row["call_iv_samples"] else 0.0
        put_iv = sum(row["put_iv_samples"]) / len(row["put_iv_samples"]) if row["put_iv_samples"] else 0.0
        avg_tte = sum(row["tte_samples"]) / len(row["tte_samples"]) if row["tte_samples"] else 0.0
        legs.append(SimpleNamespace(
            strike=strike, call_oi=row["call_oi"], call_iv=call_iv, call_volume=row["call_volume"],
            put_oi=row["put_oi"], put_iv=put_iv, put_volume=row["put_volume"],
            time_to_expiry_years=avg_tte,
        ))
    return legs


def _calculate_max_pain(gex_by_strike: list[dict]) -> float:
    """Max Pain：找出讓所有未平倉期權「到期內在價值總和」最小的履約價 ——
    理論上這是做市商避險成本最低、因此有誘因把股價釘在附近的價位。
    對每個候選履約價 S，計算：
        sum(call_oi_k * max(0, S-k)) + sum(put_oi_k * max(0, k-S))
    取讓這個總和最小的 S。
    """
    strikes = [row["strike"] for row in gex_by_strike]
    call_oi = {row["strike"]: row["call_oi"] for row in gex_by_strike}
    put_oi = {row["strike"]: row["put_oi"] for row in gex_by_strike}

    best_strike, best_loss = strikes[0], float("inf")
    for candidate in strikes:
        loss = sum(call_oi[k] * max(0.0, candidate - k) for k in strikes)
        loss += sum(put_oi[k] * max(0.0, k - candidate) for k in strikes)
        if loss < best_loss:
            best_strike, best_loss = candidate, loss
    return best_strike


def compute_strategy_recommendation(symbol: str, result: AnalysisResult) -> options_strategy_engine.StrategyRecommendation | None:
    """找一個 25~45 天後到期的到期日、抓它的期權鏈（含 bid/ask），依當前的
    GEX 狀態選一個策略建議。這是報告的加分項——找不到合適到期日、期權鏈
    查詢失敗、或安全墊範圍內沒有合適履約價，都優雅跳過，不影響報告其餘部分。
    """
    try:
        strategy_expiry = data_fetcher.get_expiry_by_dte(
            symbol, min_days=STRATEGY_MIN_DTE, max_days=STRATEGY_MAX_DTE, target_days=STRATEGY_TARGET_DTE,
        )
        if strategy_expiry is None:
            logger.info("%s 找不到 %d~%d 天內的到期日，略過策略建議", symbol, STRATEGY_MIN_DTE, STRATEGY_MAX_DTE)
            return None

        strategy_legs = data_fetcher.get_option_chain_legs(symbol, strategy_expiry)
        if not strategy_legs:
            logger.info("%s %s 期權鏈查詢失敗或無資料，略過策略建議", symbol, strategy_expiry)
            return None

        tte = data_fetcher.time_to_expiry_years(strategy_expiry)
        strategy = options_strategy_engine.select_strategy(
            strategy_legs, result.spot, tte, result.put_wall, result.call_wall, result.alert,
        )
        strategy.expiry_date = strategy_expiry  # select_strategy()只知道年化到期時間，這裡補上實際日期給追蹤記分板用
        return strategy
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s 策略建議計算失敗：%s", symbol, exc)
        return None


def save_strategy_recommendation_if_trackable(
    symbol: str, strategy: options_strategy_engine.StrategyRecommendation | None, date_str: str,
) -> None:
    """策略建議如果有結構化履約價（legs 不是 None，代表不是「今天找不到合適
    履約價/到期日」這種退化情況），就存進歷史資料庫，等到期後給
    strategy_resolver.py 結算——這是「策略追蹤記分板」的寫入端，讓策略引擎
    的規則式判斷從「感覺合理」變成「可以驗證勝率/損益的紀錄」。跟其他加分項
    一樣，任何失敗只記警告，不影響報告本身。
    """
    if strategy is None or strategy.legs is None or strategy.expiry_date is None:
        return
    try:
        legs_as_dicts = [
            {"action": leg.action, "option_type": leg.option_type, "strike_price": leg.strike_price}
            for leg in strategy.legs
        ]
        db_manager.save_strategy_recommendation(
            symbol=symbol,
            recommended_date=date_str,
            strategy_name=strategy.strategy_name,
            strategy_type=strategy.strategy_type,
            legs=legs_as_dicts,
            net_premium=strategy.net_premium,
            max_loss=strategy.max_loss,
            expiry_date=strategy.expiry_date,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s 策略追蹤紀錄寫入失敗：%s", symbol, exc)


def send_line_alert_if_extreme(result: AnalysisResult) -> None:
    """判斷這次分析有沒有觸發「重大極端事件」（現貨突破Call/Put Wall、整體
    Net GEX轉負、散戶死亡Loop），有的話推播一則白話文警示到 LINE——這是
    比 Telegram 每日報告更稀有、更急迫的第二通道，不該每天都響。
    """
    try:
        total_net_gex = result.zero_dte_summary["total_net_gex"]
        alert_text = line_formatter.build_line_alert(
            result.symbol, result.spot, result.call_wall, result.put_wall,
            total_net_gex, result.mm_pressure,
        )
        if alert_text:
            line_notifier.send_line_alert(alert_text)
    except Exception as exc:  # noqa: BLE001
        # LINE 警報是額外的加分通道，判斷/推播失敗不該影響 Telegram 或報告本身。
        logger.warning("%s LINE 極端警報判斷失敗：%s", result.symbol, exc)


def build_chart(result: AnalysisResult, output_path: Path) -> None:
    """畫互動式 Net GEX 長條圖：藍色=正GEX（做市商多Gamma，避險行為抑制波動）、
    紅色=負GEX（做市商空Gamma，避險行為放大波動）；虛線標示現貨價；
    標示 Net GEX 絕對值最大的 3 個履約價（潛在支撐/壓力牆）。
    """
    strikes = [row["strike"] for row in result.gex_by_strike]
    net_gex = [row["net_gex"] for row in result.gex_by_strike]

    # 診斷色：正/負用 diverging 配色（藍/紅），對色弱使用者也能區分正負。
    colors = ["#2a78d6" if v >= 0 else "#e34948" for v in net_gex]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=strikes, y=net_gex, marker_color=colors,
        hovertemplate="Strike %{x}<br>Net GEX %{y:$,.0f}<extra></extra>",
        name="Net GEX",
    ))

    fig.add_vline(
        x=result.spot, line_dash="dash", line_color="#5a5a56",
        annotation_text=f"Spot ${result.spot:.2f}", annotation_position="top",
    )

    # 標示 Net GEX 絕對值最大的 3 個履約價（潛在的 Option Wall）
    # 圖表上的文字全用英文 —— kaleido 底層用 headless Chromium 截圖，
    # 曾實測中文標題只顯示「/」（缺 CJK 字型），英文則不受字型影響、
    # 保證在任何機器上都能正常渲染成 PNG。
    top3 = sorted(result.gex_by_strike, key=lambda r: abs(r["net_gex"]), reverse=True)[:3]
    for row in top3:
        fig.add_annotation(
            x=row["strike"], y=row["net_gex"], text=f"Wall ${row['strike']:.0f}",
            showarrow=True, arrowhead=2, yshift=10,
        )

    fig.update_layout(
        title=f"{result.symbol} Gamma Exposure by Strike (Spot ${result.spot:.2f})",
        xaxis_title="Strike Price", yaxis_title="Net GEX (USD per 1% spot move)",
        plot_bgcolor="#fcfcfb", paper_bgcolor="#fcfcfb",
        font_color="#1a1a19", showlegend=False,
        xaxis=dict(showgrid=True, gridcolor="#e8e7e3"),
        yaxis=dict(showgrid=True, gridcolor="#e8e7e3", zerolinecolor="#c9c8c3"),
    )

    fig.write_html(str(output_path.with_suffix(".html")))
    fig.write_image(str(output_path.with_suffix(".png")), width=1280, height=720, scale=2)
    logger.info("圖表已輸出：%s / %s", output_path.with_suffix(".html"), output_path.with_suffix(".png"))


def _fmt_ratio(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "N/A"


_PINNING_REGIME_LABEL = {
    "PINNING": "🧲 Pinning（磁吸區間）",
    "BREAKOUT": "🚀 Breakout（突破區間）",
    "NEUTRAL": "🔄 Neutral（中性觀望）",
}


def _build_pinning_lines(pinning: dict | None) -> list[str]:
    """把 pinning_engine.compute_pinning_analysis() 的結果轉成 Markdown
    區塊。pinning 為 None（加分項計算失敗或期權鏈為空）時回傳空列表，
    整段直接從報告消失，不留下半殘的標題或 N/A 表格。
    """
    if pinning is None:
        return []

    regime_text = _PINNING_REGIME_LABEL.get(pinning["regime"], pinning["regime"])
    max_pain_note = "（與 Max Pain 重合，訊號較一致）" if pinning["pin_strike_matches_max_pain"] else "（與 Max Pain 不同，兩個訊號互相參考）"

    return [
        "## Pinning 釘價效應判斷", "",
        f"- 目前狀態：**{regime_text}**",
        f"- Pin Strike（未平倉量最集中的履約價）：${pinning['pin_strike']:.0f}{max_pain_note}",
        f"- 現貨距離 Pin Strike：{pinning['distance_pct']:.2f}%",
        f"- Pin Strike 未平倉量集中度：{pinning['oi_concentration_pct']:.1f}%（占全鏈總未平倉量）",
        f"- 正 Gamma 區（Pinning 的必要條件）：{'是' if pinning['in_positive_gamma'] else '否'}",
        f"- Pinning 分數：{pinning['score']} / 100（{pinning['label']}）",
        "",
    ]


def build_markdown_report(
    result: AnalysisResult, report_path: Path, ai_commentary: str | None = None,
    strategy: options_strategy_engine.StrategyRecommendation | None = None,
    macro_warnings: list[str] | None = None,
) -> None:
    flip_pct = result.gamma_flip_distance_pct
    flip_pct_text = f"（現貨距離翻轉點 {flip_pct:+.1f}%）" if flip_pct is not None else ""

    zdte = result.zero_dte_summary
    zdte_share = zdte["zero_dte_share_pct"]
    zdte_share_text = f"{zdte_share:.1f}%" if zdte_share is not None else "N/A"

    lines = [f"# {result.symbol} 盤後複習報告 — {datetime.now():%Y-%m-%d}", ""]

    # 總經/財報預警放在報告最頂端——這是「開始看籌碼細節之前」最該先看到
    # 的東西，跟策略建議刻意放在最後（行動建議）的順序邏輯相反。
    if macro_warnings:
        lines += macro_warnings + [""]

    lines += [
        f"- 當日現貨收盤價：${result.spot:.2f}",
        f"- Max Pain（最大痛點履約價）：${result.max_pain:.0f}",
        f"- Call Wall（潛在壓力）：${result.call_wall:.0f}",
        f"- Put Wall（潛在支撐）：${result.put_wall:.0f}",
        f"- Gamma 翻轉點：{'$' + format(result.gamma_flip, '.0f') if result.gamma_flip is not None else 'N/A'}{flip_pct_text}",
        f"- 使用到期日：{', '.join(result.expiries_used)}",
        "",
    ]

    lines += _build_pinning_lines(result.pinning)

    lines += [
        "## 0DTE 與整體 GEX 差異",
        "",
        f"- 全部到期日 Net GEX 總和：{zdte['total_net_gex']:,.0f}",
        f"- 僅 0DTE（當日到期）Net GEX：{zdte['zero_dte_net_gex']:,.0f}",
        f"- 排除 0DTE 後 Net GEX：{zdte['ex_zero_dte_net_gex']:,.0f}",
        f"- 0DTE 佔總 Net GEX 比例：{zdte_share_text}"
        + ("（比例高代表收盤後 Gamma 結構可能劇烈改變，俗稱『Gamma 斷崖』）" if zdte_share is not None and abs(zdte_share) >= 50 else ""),
        "",
    ]

    if result.alert:
        lines += [f"## {result.alert}", ""]

    pcr = result.put_call_ratio or {}
    lines += [
        "## 聰明資金指標", "",
        f"- Put/Call 成交量比：{_fmt_ratio(pcr.get('volume_ratio'))}",
        f"- Put/Call 未平倉量比：{_fmt_ratio(pcr.get('oi_ratio'))}",
        f"- IV Skew（OTM Put IV − OTM Call IV）：{f'{result.iv_skew:+.1%}' if result.iv_skew is not None else 'N/A'}",
        "",
    ]
    if result.unusual_activity:
        lines += ["**異常大單（成交量遠超未平倉量，疑似當日新建倉）：**", ""]
        for item in result.unusual_activity[:5]:
            ratio_text = "∞" if item["ratio"] == float("inf") else f"{item['ratio']:.1f}x"
            # likely_opening：跟前一交易日OI比較後的判斷（None代表沒有前一天
            # 資料可比較，不確定是新開倉還是平倉/轉倉，不瞎猜）。
            opening_note = {
                True: "，OI較前一交易日增加，比較像新開倉",
                False: "，OI較前一交易日持平或下降，比較像平倉/轉倉",
                None: "",
            }[item.get("likely_opening")]
            lines.append(
                f"- ${item['strike']:.0f} {item['side'].upper()}：成交量 {item['volume']:,.0f} / "
                f"OI {item['oi']:,.0f}（量能是OI的 {ratio_text}{opening_note}）"
            )
        lines.append("")

    if result.mm_pressure:
        lines += [
            "## 莊家收割壓力評分", "",
            f"- 壓力分數：{result.mm_pressure['score']} / 100（{result.mm_pressure['label']}）",
            f"- Delta加權Call成交量：{result.mm_pressure['delta_adjusted_call_volume']:,.0f}"
            "（比原始張數更能反映實質方向性曝險，深度價外的量能會被大幅折算）",
            "",
        ]
        if result.mm_pressure.get("is_death_loop_alert"):
            lines += [f"## {result.mm_pressure['alert_text']}", ""]

    if ai_commentary:
        lines += ["## AI 盤後籌碼與新聞綜合評語", "", ai_commentary, ""]

    lines += ["## Net GEX 前 5 大履約價", "", "| Strike | Net GEX | Call OI | Put OI |", "|---|---|---|---|"]
    top5 = sorted(result.gex_by_strike, key=lambda r: abs(r["net_gex"]), reverse=True)[:5]
    for row in top5:
        lines.append(f"| ${row['strike']:.0f} | {row['net_gex']:,.0f} | {row['call_oi']:,.0f} | {row['put_oi']:,.0f} |")

    # 策略建議放在報告最後——是「看完籌碼面全貌之後」的行動建議，適合當結尾。
    if strategy:
        lines += ["", "## 建議期權策略", "", f"**{strategy.strategy_name}**", "", strategy.rationale, ""]
        lines += strategy.detail_lines

    report_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("報告已輸出：%s", report_path)


def get_macro_warnings(symbol: str) -> list[str]:
    """包一層 macro_calendar.get_calendar_warnings——那支函式本身已經處理過
    所有已知的例外情況並保證回傳 list，這裡多包一層 try/except 純粹是
    防禦性寫法：萬一 macro_calendar 未來改版不小心漏接了某個例外，也不該
    讓總經日曆這個加分項拖垮整份報告。
    """
    try:
        return macro_calendar.get_calendar_warnings(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s 總經事件日曆查詢失敗：%s", symbol, exc)
        return []


def build_dashboard_data(
    result: AnalysisResult, ai_commentary: str | None,
    strategy: options_strategy_engine.StrategyRecommendation | None, macro_warnings: list[str],
) -> dict:
    """把 AnalysisResult 轉成 dashboard_generator.generate_dashboard() 要的
    資料字典——集中在這支函式，往後 dashboard 資料格式有變動只需要改這裡。
    """
    return {
        "symbol": result.symbol, "spot": result.spot, "gex_by_strike": result.gex_by_strike,
        "max_pain": result.max_pain, "call_wall": result.call_wall, "put_wall": result.put_wall,
        "gamma_flip": result.gamma_flip, "gamma_flip_distance_pct": result.gamma_flip_distance_pct,
        "iv_skew": result.iv_skew, "put_call_ratio": result.put_call_ratio,
        "unusual_activity": result.unusual_activity, "mm_pressure": result.mm_pressure,
        "pinning": result.pinning,
        "ai_commentary": ai_commentary, "strategy_name": strategy.strategy_name if strategy else None,
        "macro_warnings": macro_warnings, "alert": result.alert,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="美股期權 GEX 分析與盤後報告")
    parser.add_argument("--symbol", default="TSLA", help="標的代號，預設 TSLA")
    parser.add_argument("--max-expiries", type=int, default=8, help="最多抓取幾個到期日（預設 8，設 0 代表全部抓）")
    parser.add_argument("--risk-free-rate", type=float, default=0.045, help="無風險利率，預設 0.045")
    parser.add_argument("--output-dir", default="reports", help="報告與圖表輸出目錄，預設 ./reports")
    parser.add_argument("--notify", action="store_true", help="產出後推播 Markdown 摘要與圖表 PNG 到 Telegram")
    parser.add_argument("--no-ai", action="store_true", help="跳過 Claude AI 綜合評語（即使有設定 ANTHROPIC_API_KEY）")
    parser.add_argument("--dashboard-path", default=str(DEFAULT_DASHBOARD_PATH), help="HTML儀表板輸出路徑，預設專案目錄下的 dashboard/index.html")
    parser.add_argument("--no-dashboard", action="store_true", help="跳過 HTML 儀表板產生")
    parser.add_argument(
        "--watch", action="store_true",
        help="只執行一次盤中即時異常檢查就結束（等同 python intraday_watcher.py --symbol <symbol>），不跑完整日報流程",
    )
    args = parser.parse_args()

    if args.watch:
        import intraday_watcher
        intraday_watcher.run_watch_cycle([args.symbol], notify=args.notify)
        return

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        max_expiries = None if args.max_expiries == 0 else args.max_expiries
        result = fetch_and_aggregate(args.symbol, max_expiries=max_expiries, risk_free_rate=args.risk_free_rate)
    except Exception as exc:  # noqa: BLE001
        # Yahoo Finance 斷線、標的下市、期權鏈整組失效等情況都會走到這裡——
        # 排程工具（launchd/cron）看到的是清楚的錯誤訊息，不是原始 traceback，
        # 也不會留下殘缺的報告或圖檔。
        logger.error("分析 %s 失敗，本次排程略過：%s", args.symbol, exc)
        if args.notify:
            import telegram_notifier
            telegram_notifier.send_failure_notice(args.symbol, str(exc))
        raise SystemExit(1) from exc

    date_tag = datetime.now().strftime("%Y%m%d")
    chart_path = output_dir / f"gex_chart_{args.symbol}_{date_tag}"
    report_path = output_dir / f"daily_report_{args.symbol}_{date_tag}.md"

    strategy = compute_strategy_recommendation(args.symbol, result)

    # 只有「今天美股真的有開盤交易」才寫進歷史資料庫——launchd 的 Weekday
    # 過濾只能排除週六週日，排不掉感恩節這類平日休市日，如果排程照樣執行
    # 分析（yfinance 會回傳上一個交易日的舊資料），寫進去會留下一筆「其實
    # 沒有新資料」的假紀錄，汙染 backtester.py 依星期幾配對的統計，也會讓
    # 策略追蹤記分板收到重複/過期的建議。報告本身照常產生（手動測試不該
    # 被這個檔案擋下來），只是不寫入歷史資料庫。
    if data_fetcher.is_market_trading_day():
        trading_date_str = data_fetcher.current_trading_date_str()
        try:
            db_manager.save_snapshot(result, trading_date_str)
        except Exception as exc:  # noqa: BLE001
            # 歷史資料庫是回頭比對訊號準不準的加分項，寫入失敗（例如磁碟權限問題）
            # 不該讓當天的報告產不出來。
            logger.warning("寫入歷史資料庫失敗：%s", exc)
        save_strategy_recommendation_if_trackable(args.symbol, strategy, trading_date_str)
        save_oi_snapshot_if_trading_day(args.symbol, result, trading_date_str)
    else:
        logger.info("今天不是美股交易日，跳過歷史資料庫寫入與策略追蹤紀錄")

    macro_warnings = get_macro_warnings(args.symbol)

    ai_commentary = None
    if not args.no_ai:
        import ai_analyst
        ai_commentary = ai_analyst.generate_commentary(
            symbol=args.symbol, spot=result.spot, max_pain=result.max_pain,
            call_wall=result.call_wall, put_wall=result.put_wall,
            gamma_flip=result.gamma_flip, alert=result.alert,
        )

    build_chart(result, chart_path)
    build_markdown_report(result, report_path, ai_commentary=ai_commentary, strategy=strategy, macro_warnings=macro_warnings)

    if not args.no_dashboard:
        try:
            dashboard_data = build_dashboard_data(result, ai_commentary, strategy, macro_warnings)
            dashboard_generator.generate_dashboard(dashboard_data, Path(args.dashboard_path))
            logger.info("HTML 儀表板已輸出：%s", args.dashboard_path)
        except Exception as exc:  # noqa: BLE001
            # 儀表板是加分項，產生失敗（例如磁碟空間、路徑權限）不該讓當天
            # 的 Markdown 報告跟 Telegram 推播連帶失敗。
            logger.warning("HTML 儀表板產生失敗：%s", exc)

    if result.alert:
        logger.warning(result.alert)

    if macro_warnings:
        for warning in macro_warnings:
            logger.warning(warning)

    if args.notify:
        import telegram_notifier
        telegram_notifier.send_daily_report(
            symbol=args.symbol, report_path=report_path,
            png_path=chart_path.with_suffix(".png"),
        )
        send_line_alert_if_extreme(result)

    # 結算到期的策略建議（策略追蹤記分板）——單一標的排程模式（./run.sh
    # TSLA）跟 watchlist 模式一樣，都該有這一步，不然只跑單一標的的使用者
    # 永遠不會有 /scorecard 資料。加分項，失敗只記警告。
    try:
        import strategy_resolver
        resolved = strategy_resolver.resolve_pending(args.symbol)
        if resolved and args.notify:
            import telegram_notifier
            telegram_notifier.send_text_report(strategy_resolver.build_resolution_summary_text(args.symbol, resolved))
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s 策略追蹤記分板結算失敗：%s", args.symbol, exc)


if __name__ == "__main__":
    main()
