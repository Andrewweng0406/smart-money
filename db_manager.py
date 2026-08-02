"""SQLite 歷史快照資料庫——每天分析完後把當日的籌碼面關鍵指標存一筆，之後
才有辦法回頭比對『這些訊號準不準』，不然每天算出來的數字都是各自獨立、
沒有歷史可以對照。

只做最小可用的寫入 + 查詢，不做報表/回測邏輯——那是之後有需要再加的功能，
現在先把資料存下來即可。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).parent / "history.db"

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


@contextmanager
def _connect(db_path: Path | str = DEFAULT_DB_PATH):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(_SCHEMA)
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
