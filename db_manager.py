"""SQLite 歷史快照資料庫——每天分析完後把當日的籌碼面關鍵指標存一筆，之後
才有辦法回頭比對『這些訊號準不準』，不然每天算出來的數字都是各自獨立、
沒有歷史可以對照。

只做最小可用的寫入 + 查詢，不做報表/回測邏輯——那是之後有需要再加的功能，
現在先把資料存下來即可。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

# 本機跑就用專案目錄下的 history.db；雲端部署（例如 Railway）沒有固定的
# 「專案目錄」概念，資料庫要放在掛載的 Volume 裡才能在重新部署後還留著，
# 所以留一個 DB_PATH 環境變數可以覆蓋——本機開發沒設這個變數，行為完全不變。
DEFAULT_DB_PATH = Path(os.environ["DB_PATH"]) if os.environ.get("DB_PATH") else Path(__file__).parent / "history.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_snapshots (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    spot REAL NOT NULL,
    max_pain REAL NOT NULL,
    call_wall REAL NOT NULL,
    put_wall REAL NOT NULL,
    gamma_flip REAL,
    gamma_flip_distance_pct REAL,
    total_net_gex REAL NOT NULL,
    zero_dte_net_gex REAL NOT NULL,
    zero_dte_share_pct REAL,
    alert TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (symbol, date)
)
"""

# 策略追蹤記分板——每次 analyze.py 產生一個「有結構化履約價」的策略建議
# 就存一筆，到期後由 strategy_resolver.py 結算，回填 outcome/realized_pnl。
# 沒有這張表的話，策略引擎每天都在「推薦」，但沒有人知道這些推薦到期後
# 到底是賺是賠——這張表就是讓「規則式策略選擇」變成「可驗證的績效」。
_STRATEGY_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    recommended_date TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    strategy_type TEXT NOT NULL,
    legs_json TEXT NOT NULL,
    net_premium REAL NOT NULL,
    max_loss REAL NOT NULL,
    expiry_date TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0,
    settlement_spot REAL,
    outcome TEXT,
    realized_pnl REAL,
    max_loss_hit INTEGER,
    resolved_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (symbol, recommended_date, strategy_name)
)
"""

# 逐履約價的 OI 歷史快照——只有存下「昨天」的 OI，才能判斷「今天的異常大單
# 是新開倉還是平倉/轉倉」（單靠當天的 volume/OI 比例分不出這兩種情況，
# 這是審查抓出的訊號品質問題之一）。只存最小必要欄位，不是完整的期權鏈
# 存檔——歷史 IV/成交量沒有拿來跟「昨天」比較的用途，不需要一起存。
_OI_SNAPSHOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS oi_snapshots (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    strike REAL NOT NULL,
    call_oi REAL NOT NULL,
    put_oi REAL NOT NULL,
    PRIMARY KEY (symbol, date, strike)
)
"""


@contextmanager
def _connect(db_path: Path | str = DEFAULT_DB_PATH):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(_SCHEMA)
        conn.execute(_STRATEGY_SCHEMA)
        conn.execute(_OI_SNAPSHOT_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def save_snapshot(result, date_str: str, db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """存一筆當日快照。同一 symbol+date 重複執行會直接覆蓋（INSERT OR REPLACE），
    這樣同一天跑第二次（例如手動重跑）不會留下重複紀錄。

    result 是 analyze.AnalysisResult（鴨子定型，只要求有這些屬性，不強制
    import analyze.py 造成循環依賴）。
    """
    zdte = result.zero_dte_summary
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO daily_snapshots
            (symbol, date, spot, max_pain, call_wall, put_wall, gamma_flip,
             gamma_flip_distance_pct, total_net_gex, zero_dte_net_gex,
             zero_dte_share_pct, alert)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.symbol, date_str, result.spot, result.max_pain,
                result.call_wall, result.put_wall, result.gamma_flip,
                result.gamma_flip_distance_pct, zdte["total_net_gex"],
                zdte["zero_dte_net_gex"], zdte["zero_dte_share_pct"], result.alert,
            ),
        )


def get_recent_snapshots(symbol: str, limit: int = 30, db_path: Path | str = DEFAULT_DB_PATH) -> list[dict]:
    """回傳最近 N 天的快照（新到舊排序），給之後想做趨勢圖/回測的功能用。"""
    with _connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM daily_snapshots WHERE symbol = ? ORDER BY date DESC LIMIT ?",
            (symbol, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def save_strategy_recommendation(
    symbol: str,
    recommended_date: str,
    strategy_name: str,
    strategy_type: Literal["credit", "debit"],
    legs: list[dict],
    net_premium: float,
    max_loss: float,
    expiry_date: str,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> int:
    """存一筆策略建議，回傳新增那筆的 id（strategy_resolver.py 結算時要用
    這個 id 呼叫 mark_strategy_resolved）。legs 存成 JSON 字串——履約價組合
    每個策略形狀都不同（2腳/4腳），不值得為此另開一張正規化的腳位資料表。

    (symbol, recommended_date, strategy_name) 有 UNIQUE 限制、用 INSERT OR
    IGNORE——同一天同一個策略名稱重複寫入（例如手動重跑 run.sh 測試）不會
    產生重複紀錄，避免記分板的勝率/損益被重複計入。這種情況下回傳值不保證
    對應到真正新增的那筆（可能是 0 或既有那筆的 id），呼叫端目前都沒有依賴
    這個回傳值做後續動作，之後真的需要精確 id 的話請改用查詢取得。
    """
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO strategy_recommendations
            (symbol, recommended_date, strategy_name, strategy_type, legs_json,
             net_premium, max_loss, expiry_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol, recommended_date, strategy_name, strategy_type,
                json.dumps(legs), net_premium, max_loss, expiry_date,
            ),
        )
        return cursor.lastrowid


def get_pending_strategy_recommendations(
    as_of_date: str, db_path: Path | str = DEFAULT_DB_PATH,
) -> list[dict]:
    """回傳到期日已到（expiry_date <= as_of_date）但還沒結算的策略建議，
    給 strategy_resolver.py 的每日結算工作用。legs_json 這裡不解析，呼叫端
    自己視需要 json.loads()。
    """
    with _connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM strategy_recommendations "
            "WHERE resolved = 0 AND expiry_date <= ? "
            "ORDER BY expiry_date ASC",
            (as_of_date,),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_strategy_resolved(
    recommendation_id: int,
    settlement_spot: float,
    outcome: Literal["WIN", "LOSS"],
    realized_pnl: float,
    max_loss_hit: bool = False,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> None:
    """把一筆策略建議標記為已結算，回填結算價/勝負/損益/是否觸及最大虧損。

    max_loss_hit（strategy_tracker.score_outcome() 算好的結果）先前只是
    算完就丟掉，沒有存進資料庫——這是實測抓到的缺漏：記分板因此沒辦法
    區分「普通小賠」跟「被完全壓穿最大虧損」，這個資訊對評估策略引擎的
    風險控管品質很重要，理應保留。
    """
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE strategy_recommendations
            SET resolved = 1, settlement_spot = ?, outcome = ?, realized_pnl = ?,
                max_loss_hit = ?, resolved_at = datetime('now')
            WHERE id = ?
            """,
            (settlement_spot, outcome, realized_pnl, int(max_loss_hit), recommendation_id),
        )


def get_strategy_track_record(
    symbol: str | None = None, limit: int = 200, db_path: Path | str = DEFAULT_DB_PATH,
) -> list[dict]:
    """回傳已結算的策略建議（新到舊），給 strategy_tracker.summarize_track_record
    彙總勝率/損益用。symbol=None 時回傳所有標的。"""
    with _connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if symbol is None:
            rows = conn.execute(
                "SELECT * FROM strategy_recommendations WHERE resolved = 1 "
                "ORDER BY resolved_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM strategy_recommendations WHERE resolved = 1 AND symbol = ? "
                "ORDER BY resolved_at DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
    return [dict(row) for row in rows]


def save_oi_snapshot(
    symbol: str, date_str: str, legs: list[dict], db_path: Path | str = DEFAULT_DB_PATH,
) -> None:
    """存一天逐履約價的 OI 快照。legs 是 [{"strike":.., "call_oi":..,
    "put_oi":..}, ...]；同一 symbol+date+strike 重複執行直接覆蓋（INSERT OR
    REPLACE），跟 save_snapshot 一樣的「同一天重跑不留重複紀錄」慣例。
    """
    with _connect(db_path) as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO oi_snapshots (symbol, date, strike, call_oi, put_oi)
            VALUES (?, ?, ?, ?, ?)
            """,
            [(symbol, date_str, leg["strike"], leg["call_oi"], leg["put_oi"]) for leg in legs],
        )


def get_oi_snapshot(
    symbol: str, date_str: str, db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[float, dict[str, float]]:
    """回傳某標的某天的逐履約價OI快照，格式
    {strike: {"call_oi":.., "put_oi":..}}，給 smart_money.detect_unusual_activity
    的 previous_oi_by_strike 參數用。查無資料回傳空 dict（呼叫端視為
    「沒有前一天資料可比較」，不是錯誤）。
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT strike, call_oi, put_oi FROM oi_snapshots WHERE symbol = ? AND date = ?",
            (symbol, date_str),
        ).fetchall()
    return {row[0]: {"call_oi": row[1], "put_oi": row[2]} for row in rows}


def get_most_recent_oi_snapshot_date(
    symbol: str, before_date: str, db_path: Path | str = DEFAULT_DB_PATH,
) -> str | None:
    """找『某天之前』最近一次有存 OI 快照的日期——不能直接假設『昨天』
    一定有資料（可能中間漏跑排程，或前一天是週末/假日），要找『實際
    上一次真的存過』的那天，才能正確比較 OI 變化。查無任何更早的快照
    回傳 None。
    """
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT DISTINCT date FROM oi_snapshots WHERE symbol = ? AND date < ? "
            "ORDER BY date DESC LIMIT 1",
            (symbol, before_date),
        ).fetchone()
    return row[0] if row else None
