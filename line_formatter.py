"""白話文極端警報轉換器——純計算層，不做任何I/O。

把專業的 GEX/Smart Money 數據轉成一般散戶看得懂的3句以內警示文字，而且
只在真的很嚴重的時候才觸發（跟 Markdown 報告裡「現貨低於Gamma翻轉點」這種
每天都可能出現的例行警示刻意分開——那個訊號太常見，全部拿來發LINE會變成
「狼來了」，使用者很快就會學會忽略。這裡只挑三種真正具體、稀有的極端事件：
現貨突破 Call/Put Wall（價格層面的具體事實）、整體 Net GEX 由正轉負
（做市商避險立場整體翻轉，不是「離翻轉點多近」這種局部/常見的狀態）、
散戶死亡Loop警示（smart_money.py 已經是嚴格門檻才會觸發的訊號）。
"""

from __future__ import annotations

from typing import Optional

CALL_WALL_BREACH = "call_wall_breach"
PUT_WALL_BREACH = "put_wall_breach"
NEGATIVE_GEX = "negative_gex"
DEATH_LOOP = "death_loop"


def detect_extreme_event(
    spot: float, call_wall: float, put_wall: float, total_net_gex: float,
    mm_pressure: Optional[dict],
) -> Optional[str]:
    """判斷是否觸發「重大極端事件」，回傳事件代碼；沒有觸發回傳 None。

    優先序（由高到低，一次只回傳最急迫的那一種，避免同時符合多個條件時
    訊息互相打架）：死亡Loop（smart_money.py已經是嚴格門檻+方向性最明確的
    訊號）> 牆位突破（價格層面已經發生的具體事實）> 整體轉負（比較籠統的
    背景風險狀態，急迫性最低）。
    """
    if mm_pressure and mm_pressure.get("is_death_loop_alert"):
        return DEATH_LOOP
    if spot > call_wall:
        return CALL_WALL_BREACH
    if spot < put_wall:
        return PUT_WALL_BREACH
    if total_net_gex < 0:
        return NEGATIVE_GEX
    return None


def format_line_alert(
    symbol: str, spot: float, event: str,
    call_wall: Optional[float] = None, put_wall: Optional[float] = None,
) -> str:
    """把事件代碼組成3句以內的白話警示文字（標題/價格/重點/建議，固定格式）。"""
    if event == DEATH_LOOP:
        title = "莊家洗盤警示"
        point = "散戶正在狂買 Call，做市商避險賣壓極大！"
        advice = "目前處於追跌殺漲區，隨時可能劇烈下洗，千萬不要追高！"
    elif event == CALL_WALL_BREACH:
        title = "壓力位突破警示"
        point = f"價格已經衝破做市商設的壓力關卡 ${call_wall:.0f}！"
        advice = "上方賣壓可能瞬間湧出，追高風險變高，注意隨時拉回。"
    elif event == PUT_WALL_BREACH:
        title = "支撐位跌破警示"
        point = f"價格已經跌破做市商設的支撐關卡 ${put_wall:.0f}！"
        advice = "下方防守可能失守，避險賣壓恐怕連環引發下殺，先別急著抄底。"
    elif event == NEGATIVE_GEX:
        title = "波動放大警示"
        point = "做市商目前偏向順勢操作，不是逆勢穩定盤面。"
        advice = "接下來漲跌容易被追價行為放大，操作務必縮小部位、設好停損。"
    else:
        raise ValueError(f"未知的事件代碼：{event}")

    return (
        f"🚨 【{title} - {symbol}】\n"
        f"價格：${spot:.2f}\n"
        f"重點：{point}\n"
        f"建議：{advice}"
    )


def build_line_alert(
    symbol: str, spot: float, call_wall: float, put_wall: float, total_net_gex: float,
    mm_pressure: Optional[dict],
) -> Optional[str]:
    """一次判斷是否該發 + 組好文字；沒有觸發極端事件回傳 None，呼叫端不用
    另外呼叫 detect_extreme_event() 判斷一次。
    """
    event = detect_extreme_event(spot, call_wall, put_wall, total_net_gex, mm_pressure)
    if event is None:
        return None
    return format_line_alert(symbol, spot, event, call_wall=call_wall, put_wall=put_wall)
