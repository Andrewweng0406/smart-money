# 訊號分級（Signal Tiering）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把盤中訊號分成 urgent／watch／silent 三級並各走不同通道，同時修掉「牆位突破是狀態不是事件」與「冷卻機制被履約價 churn 穿透」兩個造成噪音的根因。

**Architecture:** 新增純計算模組 `signal_tiering.py` 負責分級政策（不做 I/O、不讀狀態），新增 `signal_events` 表落地所有偵測結果（三級共用一張表）。`intraday_watcher` 在偵測之後加一層路由：urgent 立即推播、watch 寫入佇列、silent 只落地。`run_watchlist` 在 10:00 摘要與 16:30 日報時清空 watch 佇列。

**Tech Stack:** Python 3.11、SQLite（`sqlite3` 標準函式庫）、pytest。無新增第三方相依。

**Spec:** `docs/superpowers/specs/2026-09-09-signal-tiering-design.md`

## Global Constraints

- **繁體中文註解**：關鍵邏輯（尤其「為什麼這樣做」）一律用繁體中文寫在程式碼裡。
- **純計算與 I/O 分離**：`signal_tiering.py` 不得 import `db_manager`、`data_fetcher`、`telegram_notifier`，不得讀檔案或環境變數。
- **加分項優雅降級**：分級／落地失敗只記 `logger.warning`（用 `except Exception as exc:  # noqa: BLE001`），絕不能讓偵測或既有推播連帶失敗。
- **SQLite migration 慣例**：新欄位必須用 `PRAGMA table_info` 檢查 + `ALTER TABLE` 補上，不能只靠 `CREATE TABLE IF NOT EXISTS`（正式環境的 Volume 上已有舊表，實測會炸 "no such column"）。
- **每日推播上限**：`MAX_URGENT_PUSHES_PER_DAY = 8`，**跨所有標的合計**，不是每檔各 8 則。
- **`trading_date` 一律用美東日期**（`America/New_York`），非 UTC、非本地時區。
- **測試指令**：`python -m pytest -q`（需先 `source .venv/bin/activate`）。全部 364 個既有測試必須保持綠。

---

### Task 1: `signal_tiering.py` 分級政策模組

**Files:**
- Create: `signal_tiering.py`
- Test: `tests/test_signal_tiering.py`

**Interfaces:**
- Consumes: 無（第一個任務，純函式）
- Produces:
  - `Tier = Literal["urgent", "watch", "silent"]`
  - `KIND_CALL_WALL_BREACH`, `KIND_PUT_WALL_BREACH`, `KIND_MM_PRESSURE`, `KIND_UNUSUAL_ACTIVITY`, `KIND_PINNING_HIGH`（皆為 str 常數）
  - `build_regime(spot: float | None, gamma_flip: float | None, total_net_gex: float | None) -> dict`
  - `classify(kind: str, payload: dict, regime: dict) -> tuple[str, str]`（回傳 `(tier, reason)`）
  - `urgent_priority(kind: str) -> int`（數字越小越優先）
  - `PINNING_WATCH_SCORE_THRESHOLD = 70`、`UNUSUAL_ACTIVITY_WATCH_RATIO = 3.0`

- [ ] **Step 1: 寫失敗測試**

建立 `tests/test_signal_tiering.py`：

```python
"""signal_tiering.py 測試——純函式，全部用合成 payload/regime，不碰 I/O。"""

from __future__ import annotations

import pytest

import signal_tiering as st


def _regime(negative_gamma: bool) -> dict:
    return st.build_regime(
        spot=90.0 if negative_gamma else 110.0, gamma_flip=100.0, total_net_gex=1.0,
    )


def test_build_regime_uses_gamma_flip_when_available():
    regime = st.build_regime(spot=90.0, gamma_flip=100.0, total_net_gex=5.0)
    assert regime["negative_gamma"] is True
    assert regime["source"] == "gamma_flip"


def test_build_regime_falls_back_to_net_gex_when_gamma_flip_missing():
    regime = st.build_regime(spot=90.0, gamma_flip=None, total_net_gex=-5.0)
    assert regime["negative_gamma"] is True
    assert regime["source"] == "net_gex_fallback"


def test_put_wall_breach_is_urgent_in_positive_gamma():
    tier, reason = st.classify(st.KIND_PUT_WALL_BREACH, {}, _regime(negative_gamma=False))
    assert tier == "urgent"
    assert reason


def test_put_wall_breach_is_urgent_in_negative_gamma():
    tier, _ = st.classify(st.KIND_PUT_WALL_BREACH, {}, _regime(negative_gamma=True))
    assert tier == "urgent"


def test_call_wall_breach_is_urgent_only_in_negative_gamma():
    assert st.classify(st.KIND_CALL_WALL_BREACH, {}, _regime(True))[0] == "urgent"
    assert st.classify(st.KIND_CALL_WALL_BREACH, {}, _regime(False))[0] == "watch"


def test_mm_pressure_is_urgent_only_in_negative_gamma():
    assert st.classify(st.KIND_MM_PRESSURE, {}, _regime(True))[0] == "urgent"
    assert st.classify(st.KIND_MM_PRESSURE, {}, _regime(False))[0] == "watch"


@pytest.mark.parametrize("ratio", [3.0, 6.0, 100.0, float("inf")])
def test_unusual_activity_is_never_urgent(ratio):
    """盤中無法區分開倉/平倉（OI 隔夜才結算），所以永遠不得升 urgent。"""
    tier, _ = st.classify(
        st.KIND_UNUSUAL_ACTIVITY, {"ratio": ratio, "likely_opening": True}, _regime(True),
    )
    assert tier == "watch"


def test_unusual_activity_below_ratio_is_silent():
    tier, _ = st.classify(st.KIND_UNUSUAL_ACTIVITY, {"ratio": 2.9}, _regime(True))
    assert tier == "silent"


def test_unusual_activity_at_ratio_boundary_is_watch():
    tier, _ = st.classify(st.KIND_UNUSUAL_ACTIVITY, {"ratio": 3.0}, _regime(True))
    assert tier == "watch"


def test_pinning_at_threshold_is_watch():
    assert st.classify(st.KIND_PINNING_HIGH, {"score": 70}, _regime(False))[0] == "watch"


def test_pinning_below_threshold_is_silent():
    assert st.classify(st.KIND_PINNING_HIGH, {"score": 69}, _regime(False))[0] == "silent"


def test_reason_notes_fallback_regime_source():
    """用退化路徑判斷 regime 時，理由必須寫明，否則日後稽核分不出來。"""
    regime = st.build_regime(spot=90.0, gamma_flip=None, total_net_gex=-5.0)
    _, reason = st.classify(st.KIND_CALL_WALL_BREACH, {}, regime)
    assert "net_gex" in reason


def test_urgent_priority_orders_put_wall_first():
    assert st.urgent_priority(st.KIND_PUT_WALL_BREACH) < st.urgent_priority(st.KIND_MM_PRESSURE)
    assert st.urgent_priority(st.KIND_MM_PRESSURE) < st.urgent_priority(st.KIND_CALL_WALL_BREACH)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_signal_tiering.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'signal_tiering'`

- [ ] **Step 3: 寫最小實作**

建立 `signal_tiering.py`：

```python
#!/usr/bin/env python3
"""訊號分級政策——決定每個偵測到的訊號該走哪個通道。

這是純計算層：不做任何 I/O、不讀任何狀態。分級只看「這個訊號本身是什麼」，
不知道剛剛有沒有發過同樣的東西——持續中的狀態由 crossing 偵測跟冷卻層處理，
不是分級層的職責。在這裡讀狀態會摧毀可測性。
"""

from __future__ import annotations

from typing import Literal

Tier = Literal["urgent", "watch", "silent"]

KIND_CALL_WALL_BREACH = "call_wall_breach"
KIND_PUT_WALL_BREACH = "put_wall_breach"
KIND_MM_PRESSURE = "mm_pressure"
KIND_UNUSUAL_ACTIVITY = "unusual_activity"
KIND_PINNING_HIGH = "pinning_high"

# Pinning 門檻從 80 降到 70：80 分在 78 筆生產資料中從未觸發（實測最高 76），
# 是實質死碼。70 分回溯會觸發約 2.6%，稀有度合理。只放 watch——Pinning 描述
# 的是「價格可能被釘住」，本質上不急迫。
PINNING_WATCH_SCORE_THRESHOLD = 70

# 沿用 intraday_watcher 既有的偵測門檻，本模組不調整靈敏度。
UNUSUAL_ACTIVITY_WATCH_RATIO = 3.0

# 每日推播預算超額時的取捨順序，數字越小越優先保留。
_URGENT_PRIORITY = {
    KIND_PUT_WALL_BREACH: 1,
    KIND_MM_PRESSURE: 2,
    KIND_CALL_WALL_BREACH: 3,
}
_LOWEST_PRIORITY = 99


def build_regime(
    spot: float | None, gamma_flip: float | None, total_net_gex: float | None,
) -> dict:
    """組出分級要用的市場狀態。

    刻意「不用」total_net_gex 的正負號當主判斷：那個值來自前一交易日收盤
    快照，而 dealer gamma 盤中就會翻轉——那正是 gamma flip 這個概念存在的
    理由。用昨收的符號判斷今天的 regime，會在「regime 正在改變」的日子
    系統性判錯，而那恰好是唯一重要的日子。

    改用 spot 相對 gamma_flip 的位置當即時代理：gamma_flip 已存在於快照、
    spot 本來就會抓，零額外 API 成本。gamma_flip 缺值時才退回 net_gex，
    並在 source 標記，讓事後稽核分得出哪些判斷走了退化路徑。
    """
    if gamma_flip is not None and spot is not None:
        return {
            "negative_gamma": spot < gamma_flip,
            "source": "gamma_flip",
            "gamma_flip": gamma_flip,
            "total_net_gex": total_net_gex,
        }
    return {
        "negative_gamma": (total_net_gex or 0) < 0,
        "source": "net_gex_fallback",
        "gamma_flip": gamma_flip,
        "total_net_gex": total_net_gex,
    }


def _regime_suffix(regime: dict) -> str:
    return "（regime 來源：net_gex 退化路徑）" if regime.get("source") == "net_gex_fallback" else ""


def classify(kind: str, payload: dict, regime: dict) -> tuple[str, str]:
    """回傳 (級別, 理由)。理由給人看，也存進 DB 供日後稽核。"""
    negative_gamma = bool(regime.get("negative_gamma"))
    suffix = _regime_suffix(regime)

    if kind == KIND_PUT_WALL_BREACH:
        # 不分 regime 一律緊急。這是風險偏好設定，不是實證發現：正 Gamma 下
        # Call Wall 突破被壓回只是錯過漲幅，Put Wall 跌破未被壓回卻是實際虧損。
        return "urgent", f"Put Wall 向下穿越，採保護優先一律緊急{suffix}"

    if kind in (KIND_CALL_WALL_BREACH, KIND_MM_PRESSURE):
        label = "Call Wall 向上穿越" if kind == KIND_CALL_WALL_BREACH else "做市商賣壓警報"
        if negative_gamma:
            return "urgent", f"{label}且處於負 Gamma，波動易放大{suffix}"
        return "watch", f"{label}但處於正 Gamma，通常被壓回{suffix}"

    if kind == KIND_UNUSUAL_ACTIVITY:
        # 永遠不會是 urgent：OI 由 OCC 隔夜結算公布，盤中拿不到今日 OI，
        # 因此無法區分開倉與平倉。平倉的巨量不代表有人在建方向性部位，
        # 推成緊急警報是純噪音。這個天花板要換 OPRA 級資料源才打得開。
        ratio = payload.get("ratio", 0.0)
        if ratio >= UNUSUAL_ACTIVITY_WATCH_RATIO:
            return "watch", f"異常大單比值 {ratio:.1f}x，盤中無法判定開倉/平倉故不升級"
        return "silent", f"異常大單比值 {ratio:.1f}x 未達觀察門檻"

    if kind == KIND_PINNING_HIGH:
        score = payload.get("score", 0)
        if score >= PINNING_WATCH_SCORE_THRESHOLD:
            return "watch", f"Pinning 分數 {score} 達觀察門檻"
        return "silent", f"Pinning 分數 {score} 未達觀察門檻"

    return "silent", f"未知訊號種類 {kind}，保守落地不推播"


def urgent_priority(kind: str) -> int:
    """每日推播預算超額時的取捨順序，數字越小越優先保留。"""
    return _URGENT_PRIORITY.get(kind, _LOWEST_PRIORITY)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_signal_tiering.py -q`
Expected: PASS（14 passed）

- [ ] **Step 5: 跑全套確認沒破壞既有測試**

Run: `python -m pytest -q`
Expected: 378 passed（364 既有 + 14 新增）

- [ ] **Step 6: Commit**

```bash
git add signal_tiering.py tests/test_signal_tiering.py
git commit -m "Add signal_tiering.py: pure tier classification policy"
```

---

### Task 2: `signal_events` 資料表與存取函式

**Files:**
- Modify: `db_manager.py`（在 `oi_snapshots` 相關函式之後追加）
- Test: `tests/test_db_manager.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 tier 字串值（`"urgent"` / `"watch"` / `"silent"`）
- Produces:
  - `save_signal_event(symbol, detected_at, trading_date, kind, classified_tier, delivered_tier, reason, signature, payload, db_path=DEFAULT_DB_PATH) -> int`（回傳 rowid）
  - `get_undelivered_watch_events(symbol, db_path=DEFAULT_DB_PATH) -> list[dict]`
  - `mark_events_delivered(event_ids, delivered_at, channel, db_path=DEFAULT_DB_PATH) -> None`
  - `count_urgent_delivered(trading_date, db_path=DEFAULT_DB_PATH) -> int`（**跨所有標的合計**）

- [ ] **Step 1: 寫失敗測試**

追加到 `tests/test_db_manager.py`：

```python
# ---------- signal_events ----------

def test_save_and_read_undelivered_watch_event(tmp_path):
    db_path = tmp_path / "history.db"
    db_manager.save_signal_event(
        "TSLA", "2026-09-09T14:00:00+00:00", "2026-09-09", "call_wall_breach",
        classified_tier="watch", delivered_tier="watch",
        reason="正 Gamma", signature="call_wall", payload={"wall": 110.0},
        db_path=db_path,
    )

    rows = db_manager.get_undelivered_watch_events("TSLA", db_path=db_path)

    assert len(rows) == 1
    assert rows[0]["kind"] == "call_wall_breach"
    assert rows[0]["payload"]["wall"] == 110.0
    assert rows[0]["reason"] == "正 Gamma"


def test_silent_events_are_never_returned_as_watch(tmp_path):
    """靜默紀錄只落地，永遠不該被 drain 出來推播。"""
    db_path = tmp_path / "history.db"
    db_manager.save_signal_event(
        "TSLA", "2026-09-09T14:00:00+00:00", "2026-09-09", "pinning_high",
        classified_tier="silent", delivered_tier="silent",
        reason="分數不足", signature="pinning", payload={"score": 40},
        db_path=db_path,
    )

    assert db_manager.get_undelivered_watch_events("TSLA", db_path=db_path) == []


def test_marking_delivered_removes_from_queue(tmp_path):
    db_path = tmp_path / "history.db"
    event_id = db_manager.save_signal_event(
        "TSLA", "2026-09-09T14:00:00+00:00", "2026-09-09", "call_wall_breach",
        classified_tier="watch", delivered_tier="watch",
        reason="正 Gamma", signature="call_wall", payload={},
        db_path=db_path,
    )

    db_manager.mark_events_delivered(
        [event_id], "2026-09-09T20:30:00+00:00", "daily_report", db_path=db_path,
    )

    assert db_manager.get_undelivered_watch_events("TSLA", db_path=db_path) == []


def test_count_urgent_delivered_is_global_across_symbols(tmp_path):
    """每日推播預算是跨所有標的合計，不是每檔各算一份。"""
    db_path = tmp_path / "history.db"
    for symbol in ("TSLA", "MU", "SPCX"):
        db_manager.save_signal_event(
            symbol, "2026-09-09T14:00:00+00:00", "2026-09-09", "put_wall_breach",
            classified_tier="urgent", delivered_tier="urgent",
            reason="保護優先", signature="put_wall", payload={},
            db_path=db_path,
        )

    assert db_manager.count_urgent_delivered("2026-09-09", db_path=db_path) == 3
    assert db_manager.count_urgent_delivered("2026-09-10", db_path=db_path) == 0


def test_demoted_event_keeps_classified_tier_urgent(tmp_path):
    """預算擠掉的訊號 delivered_tier 降級，但 classified_tier 必須維持
    urgent——否則日後績效統計會被當日到達順序污染。"""
    db_path = tmp_path / "history.db"
    db_manager.save_signal_event(
        "TSLA", "2026-09-09T14:00:00+00:00", "2026-09-09", "put_wall_breach",
        classified_tier="urgent", delivered_tier="watch",
        reason="超出當日推播預算", signature="put_wall", payload={},
        db_path=db_path,
    )

    rows = db_manager.get_undelivered_watch_events("TSLA", db_path=db_path)

    assert len(rows) == 1
    assert rows[0]["classified_tier"] == "urgent"
    assert db_manager.count_urgent_delivered("2026-09-09", db_path=db_path) == 0
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_db_manager.py -q -k signal_event`
Expected: FAIL，`AttributeError: module 'db_manager' has no attribute 'save_signal_event'`

- [ ] **Step 3: 寫最小實作**

在 `db_manager.py` 的 schema 常數區加入建表 SQL，並在既有的 `_init_db()`（或等效的初始化函式，跟 `daily_snapshots` / `oi_snapshots` 同一處）加上執行：

```python
_SIGNAL_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    trading_date TEXT NOT NULL,
    kind TEXT NOT NULL,
    classified_tier TEXT NOT NULL,
    delivered_tier TEXT NOT NULL,
    reason TEXT,
    signature TEXT,
    payload_json TEXT,
    delivered_at TEXT,
    delivery_channel TEXT
)
"""

_SIGNAL_EVENTS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_signal_events_symbol_date ON signal_events (symbol, trading_date)",
    "CREATE INDEX IF NOT EXISTS idx_signal_events_delivery ON signal_events (delivered_tier, delivered_at)",
]
```

在初始化函式裡：

```python
conn.execute(_SIGNAL_EVENTS_SCHEMA)
for statement in _SIGNAL_EVENTS_INDEXES:
    conn.execute(statement)
```

追加存取函式（放在檔案既有的 `oi_snapshots` 函式之後）：

```python
def save_signal_event(
    symbol: str,
    detected_at: str,
    trading_date: str,
    kind: str,
    classified_tier: str,
    delivered_tier: str,
    reason: str,
    signature: str,
    payload: dict,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> int:
    """存一筆訊號事件，回傳 rowid。

    三個級別共用這張表：silent 就是「永遠不會被 drain 的列」，watch 是
    「等待 drain 的列」，urgent 是「已推播並標記的列」。這張表同時是未來
    績效儀表板的資料來源，所以連不推播的訊號也要落地。
    """
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """INSERT INTO signal_events
               (symbol, detected_at, trading_date, kind, classified_tier,
                delivered_tier, reason, signature, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, detected_at, trading_date, kind, classified_tier,
             delivered_tier, reason, signature, json.dumps(payload)),
        )
        return cursor.lastrowid


def get_undelivered_watch_events(
    symbol: str, db_path: Path | str = DEFAULT_DB_PATH,
) -> list[dict]:
    """取出該標的所有還沒送達的觀察名單項目。

    條件用 delivered_tier 而非 classified_tier：被每日預算擠下來的 urgent
    訊號 delivered_tier 是 watch，應該跟著觀察名單一起送出去（不是丟棄）。
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT * FROM signal_events
               WHERE symbol = ? AND delivered_tier = 'watch' AND delivered_at IS NULL
               ORDER BY detected_at""",
            (symbol,),
        ).fetchall()

    events = []
    for row in rows:
        event = dict(row)
        try:
            event["payload"] = json.loads(event.get("payload_json") or "{}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("訊號事件 payload 解析失敗（id=%s）：%s", event.get("id"), exc)
            event["payload"] = {}
        events.append(event)
    return events


def mark_events_delivered(
    event_ids: list[int],
    delivered_at: str,
    channel: str,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> None:
    """標記已送達。推播失敗時「不要」呼叫這支——沒標記的項目下次會重試。"""
    if not event_ids:
        return
    placeholders = ",".join("?" for _ in event_ids)
    with _connect(db_path) as conn:
        conn.execute(
            f"UPDATE signal_events SET delivered_at = ?, delivery_channel = ? "
            f"WHERE id IN ({placeholders})",
            (delivered_at, channel, *event_ids),
        )


def count_urgent_delivered(
    trading_date: str, db_path: Path | str = DEFAULT_DB_PATH,
) -> int:
    """當日已實際推播的緊急訊號數——跨所有標的合計，不是每檔各算一份。"""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM signal_events "
            "WHERE trading_date = ? AND delivered_tier = 'urgent'",
            (trading_date,),
        ).fetchone()
    return row[0] if row else 0
```

注意：若 `db_manager.py` 尚未 `import json`，須補上。`_connect` 為既有的連線
helper，須沿用檔案中既有的名稱與 row_factory 設定（`sqlite3.Row`）。

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_db_manager.py -q`
Expected: PASS

- [ ] **Step 5: 跑全套**

Run: `python -m pytest -q`
Expected: 383 passed

- [ ] **Step 6: Commit**

```bash
git add db_manager.py tests/test_db_manager.py
git commit -m "Add signal_events table for tiered signal persistence"
```

---

### Task 3: 牆位突破改為 crossing 偵測

**Files:**
- Modify: `intraday_watcher.py:90-110`（`check_wall_breach`）、`run_check`、`build_alert_text`、`build_alert_signature`
- Test: `tests/test_intraday_watcher.py`（修改既有 4 個 `check_wall_breach` 測試 + 新增）

**Interfaces:**
- Consumes: Task 1 的 `KIND_CALL_WALL_BREACH` / `KIND_PUT_WALL_BREACH`
- Produces:
  - `check_wall_breach(symbol, spot, prev_spot=None, db_path=...) -> dict | None`
    回傳 `{"kind": str, "wall_price": float, "spot": float, "text": str}`
  - `current_trading_date(now: datetime | None = None) -> str`（美東日期 `YYYY-MM-DD`）
  - `run_check(...)` 的 `result["wall_breach"]` 型別從 `str | None` 改為 `dict | None`

**⚠️ 這個任務會改變既有行為並使 4 個既有測試失敗**——那是預期的：舊測試編碼的
是「價位條件」語意，新契約是「穿越事件」語意。必須改測試而非改實作。

- [ ] **Step 1: 改寫既有測試 + 寫新測試**

把 `tests/test_intraday_watcher.py` 中 `# ---------- check_wall_breach ----------`
區塊（約 80-127 行）整段換成：

```python
# ---------- check_wall_breach（crossing 偵測）----------

def test_check_wall_breach_returns_none_without_history(tmp_path):
    db_path = tmp_path / "history.db"
    assert intraday_watcher.check_wall_breach(
        "TSLA", spot=100.0, prev_spot=99.0, db_path=db_path,
    ) is None


def test_check_wall_breach_detects_upward_call_wall_crossing(tmp_path):
    db_path = tmp_path / "history.db"
    _save_snapshot(db_path, call_wall=110.0, put_wall=90.0)

    breach = intraday_watcher.check_wall_breach(
        "TSLA", spot=115.0, prev_spot=105.0, db_path=db_path,
    )

    assert breach is not None
    assert breach["kind"] == signal_tiering.KIND_CALL_WALL_BREACH
    assert breach["wall_price"] == 110.0
    assert "Call Wall" in breach["text"]


def test_check_wall_breach_detects_downward_put_wall_crossing(tmp_path):
    db_path = tmp_path / "history.db"
    _save_snapshot(db_path, call_wall=110.0, put_wall=90.0)

    breach = intraday_watcher.check_wall_breach(
        "TSLA", spot=85.0, prev_spot=95.0, db_path=db_path,
    )

    assert breach is not None
    assert breach["kind"] == signal_tiering.KIND_PUT_WALL_BREACH


def test_check_wall_breach_ignores_price_already_above_wall(tmp_path):
    """問題二的迴歸測試：價格整天待在牆上不是持續的『突破事件』。

    沒有這個測試，價格不動地停在 Call Wall 上方會每次檢查都回報突破，
    60 分鐘冷卻仍會每小時推一次，六個半小時吃掉大半日推播預算。
    """
    db_path = tmp_path / "history.db"
    _save_snapshot(db_path, call_wall=110.0, put_wall=90.0)

    assert intraday_watcher.check_wall_breach(
        "TSLA", spot=115.0, prev_spot=114.0, db_path=db_path,
    ) is None


def test_check_wall_breach_without_prev_spot_never_triggers(tmp_path):
    """當日首次檢查或狀態檔遺失時寧可漏報，也不要把既有狀態誤報成新突破。"""
    db_path = tmp_path / "history.db"
    _save_snapshot(db_path, call_wall=110.0, put_wall=90.0)

    assert intraday_watcher.check_wall_breach(
        "TSLA", spot=115.0, prev_spot=None, db_path=db_path,
    ) is None


def test_check_wall_breach_no_crossing_within_range(tmp_path):
    db_path = tmp_path / "history.db"
    _save_snapshot(db_path, call_wall=110.0, put_wall=90.0)

    assert intraday_watcher.check_wall_breach(
        "TSLA", spot=100.0, prev_spot=101.0, db_path=db_path,
    ) is None


def test_current_trading_date_uses_eastern_not_utc():
    """UTC 已跨日但美東還是前一天時，交易日必須是美東日期。"""
    from datetime import datetime, timezone

    utc_after_midnight = datetime(2026, 9, 10, 2, 0, tzinfo=timezone.utc)

    assert intraday_watcher.current_trading_date(utc_after_midnight) == "2026-09-09"
```

在測試檔頂端補 `import signal_tiering`。`_save_snapshot` 是該測試檔既有的
helper——若名稱不同，沿用檔案中原本 4 個測試所使用的那一個。

同時更新 `build_alert_text` 的既有測試（約 262-290 行）：其 `result` 內的
`"wall_breach"` 由字串改為 dict，例如
`{"kind": "call_wall_breach", "wall_price": 110.0, "spot": 115.0, "text": "TSLA 現貨 $115.00 向上穿越 Call Wall $110"}`。

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_intraday_watcher.py -q -k wall_breach`
Expected: FAIL，`TypeError: check_wall_breach() got an unexpected keyword argument 'prev_spot'`

- [ ] **Step 3: 寫最小實作**

在 `intraday_watcher.py` 加入 import 與 helper：

```python
import signal_tiering


def current_trading_date(now: datetime | None = None) -> str:
    """回傳所屬交易日（美東日期）。

    刻意不用 UTC 也不用本機時區：容器跑在 UTC，使用者在台灣，兩者跨日的
    時間點都跟美股交易日不一致。用美東日期才能讓每日推播預算跟「一個交易日」
    對齊。
    """
    now = now.astimezone(US_EASTERN) if now is not None else datetime.now(US_EASTERN)
    return now.strftime("%Y-%m-%d")
```

替換 `check_wall_breach`：

```python
def check_wall_breach(
    symbol: str,
    spot: float,
    prev_spot: float | None = None,
    db_path: Path | str = db_manager.DEFAULT_DB_PATH,
) -> dict | None:
    """偵測現貨「穿越」Call/Put Wall 的事件，不是「位於牆外」的狀態。

    原本的寫法是 `if spot > call_wall` ——那是價位條件。價格整天待在牆上，
    每 15 分鐘的檢查都會回報突破，冷卻機制只能限流不能治本。分級的整個前提
    是訊號為離散事件，所以這裡必須改成比較前後兩次的相對位置。

    prev_spot 為 None（當日首次檢查、狀態檔遺失）時一律不觸發：寧可漏報一次
    真突破，也不要把「本來就在牆外」誤報成新事件。
    """
    if prev_spot is None:
        return None

    rows = db_manager.get_recent_snapshots(symbol, limit=1, db_path=db_path)
    if not rows:
        return None  # 還沒有歷史快照可以當參考牆位，優雅跳過

    latest = rows[0]
    call_wall = latest["call_wall"]
    put_wall = latest["put_wall"]

    if call_wall and prev_spot <= call_wall < spot:
        return {
            "kind": signal_tiering.KIND_CALL_WALL_BREACH,
            "wall_price": call_wall,
            "spot": spot,
            "text": f"{symbol} 現貨 ${spot:.2f} 向上穿越 Call Wall ${call_wall:.0f}（潛在壓力位失守）",
        }
    if put_wall and prev_spot >= put_wall > spot:
        return {
            "kind": signal_tiering.KIND_PUT_WALL_BREACH,
            "wall_price": put_wall,
            "spot": spot,
            "text": f"{symbol} 現貨 ${spot:.2f} 向下穿越 Put Wall ${put_wall:.0f}（潛在支撐位失守）",
        }
    return None
```

`run_check` 改為接受並傳遞 `prev_spot`：

```python
def run_check(
    symbol: str,
    db_path: Path | str = db_manager.DEFAULT_DB_PATH,
    now: datetime | None = None,
    prev_spot: float | None = None,
) -> dict:
```

其內部呼叫改為
`result["wall_breach"] = check_wall_breach(symbol, spot, prev_spot=prev_spot, db_path=db_path)`。

`build_alert_text` 中取用改為 `result["wall_breach"]["text"]`：

```python
    if result["wall_breach"]:
        lines.append(result["wall_breach"]["text"])
```

`build_alert_signature` 中改為用 `kind`（不再用中文字串比對）：

```python
    if result["wall_breach"]:
        parts.append(result["wall_breach"]["kind"])
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_intraday_watcher.py -q`
Expected: PASS

- [ ] **Step 5: 跑全套**

Run: `python -m pytest -q`
Expected: 全綠（總數因測試增減而變動，不得有 FAIL）

- [ ] **Step 6: Commit**

```bash
git add intraday_watcher.py tests/test_intraday_watcher.py
git commit -m "Detect wall crossings as events, not price-level states"
```

---

### Task 4: 冷卻改為每訊號種類獨立

**Files:**
- Modify: `intraday_watcher.py:259-290`（`should_send_alert` / `record_alert_sent`）
- Test: `tests/test_intraday_watcher.py`（追加）

**Interfaces:**
- Consumes: Task 3 的 `check_wall_breach` 回傳的 `kind`
- Produces:
  - `should_send_alert(symbol, kind, signature, now=None, state_path=None, cooldown_minutes=ALERT_COOLDOWN_MINUTES) -> bool`
  - `record_alert_sent(symbol, kind, signature, now=None, state_path=None) -> None`
  - 狀態檔結構從 `{symbol: {...}}` 改為 `{f"{symbol}|{kind}": {...}}`

- [ ] **Step 1: 寫失敗測試**

追加到 `tests/test_intraday_watcher.py`：

```python
# ---------- 冷卻：每訊號種類獨立 ----------

def test_cooldown_blocks_same_kind_within_window(tmp_path):
    state_path = tmp_path / "state.json"
    now = datetime(2026, 9, 9, 14, 0, tzinfo=timezone.utc)
    intraday_watcher.record_alert_sent(
        "TSLA", "call_wall_breach", "sig-a", now=now, state_path=state_path,
    )

    assert intraday_watcher.should_send_alert(
        "TSLA", "call_wall_breach", "sig-a",
        now=now + timedelta(minutes=30), state_path=state_path,
    ) is False


def test_cooldown_expires_after_window(tmp_path):
    state_path = tmp_path / "state.json"
    now = datetime(2026, 9, 9, 14, 0, tzinfo=timezone.utc)
    intraday_watcher.record_alert_sent(
        "TSLA", "call_wall_breach", "sig-a", now=now, state_path=state_path,
    )

    assert intraday_watcher.should_send_alert(
        "TSLA", "call_wall_breach", "sig-a",
        now=now + timedelta(minutes=61), state_path=state_path,
    ) is True


def test_different_kinds_have_independent_cooldowns(tmp_path):
    """問題一的迴歸測試。

    原本所有訊號共用一個簽章：異常大單的履約價盤中會 churn，簽章一變就
    穿透冷卻，導致最吵的訊號擁有最高推播頻率，且牆位突破被綁在同一個簽章
    裡跟著重發。分開之後，異常大單怎麼變都不能影響牆位突破的冷卻。
    """
    state_path = tmp_path / "state.json"
    now = datetime(2026, 9, 9, 14, 0, tzinfo=timezone.utc)
    intraday_watcher.record_alert_sent(
        "TSLA", "call_wall_breach", "wall-sig", now=now, state_path=state_path,
    )

    # 異常大單是全新的種類，不該被牆位突破的冷卻擋住
    assert intraday_watcher.should_send_alert(
        "TSLA", "unusual_activity", "strike-123",
        now=now + timedelta(minutes=1), state_path=state_path,
    ) is True

    # 而牆位突破自己仍然在冷卻中——不受異常大單的簽章變動影響
    assert intraday_watcher.should_send_alert(
        "TSLA", "call_wall_breach", "wall-sig",
        now=now + timedelta(minutes=1), state_path=state_path,
    ) is False


def test_same_kind_different_symbols_are_independent(tmp_path):
    state_path = tmp_path / "state.json"
    now = datetime(2026, 9, 9, 14, 0, tzinfo=timezone.utc)
    intraday_watcher.record_alert_sent(
        "TSLA", "call_wall_breach", "sig-a", now=now, state_path=state_path,
    )

    assert intraday_watcher.should_send_alert(
        "MU", "call_wall_breach", "sig-a",
        now=now + timedelta(minutes=1), state_path=state_path,
    ) is True
```

測試檔頂端須有 `from datetime import datetime, timedelta, timezone`（若已存在則不重複）。

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_intraday_watcher.py -q -k cooldown`
Expected: FAIL，`TypeError: should_send_alert() takes ... positional arguments but ... were given`

- [ ] **Step 3: 寫最小實作**

```python
def _cooldown_key(symbol: str, kind: str) -> str:
    """冷卻狀態的鍵——刻意含 kind。

    原本只用 symbol，所有訊號共用一筆狀態與一個合併簽章。異常大單的履約價
    盤中會持續變動，簽章一變就整組放行，等於冷卻對「最吵的訊號」完全失效，
    而且會連帶把還在冷卻中的牆位突破一起重發。
    """
    return f"{symbol}|{kind}"


def should_send_alert(
    symbol: str, kind: str, signature: str, now: datetime | None = None,
    state_path: Path | None = None, cooldown_minutes: int = ALERT_COOLDOWN_MINUTES,
) -> bool:
    """同一種訊號（symbol+kind）在冷卻時間內不重複推播。

    state_path 預設 None、在函式內才解析成 ALERT_STATE_PATH（而不是直接
    寫在參數預設值上）——Python 的參數預設值在函式「定義」當下就綁定，
    測試裡 monkeypatch 模組層級的 ALERT_STATE_PATH 不會反映到已綁定的預設值，
    會在測試環境意外寫到專案裡真正的狀態檔案（實測踩到的真bug）。
    """
    if state_path is None:
        state_path = ALERT_STATE_PATH
    now = now or datetime.now(timezone.utc)
    last = _load_alert_state(state_path).get(_cooldown_key(symbol, kind))
    if last is None or last.get("signature") != signature:
        return True
    last_sent_at = datetime.fromisoformat(last["sent_at"])
    return (now - last_sent_at) >= timedelta(minutes=cooldown_minutes)


def record_alert_sent(
    symbol: str, kind: str, signature: str, now: datetime | None = None,
    state_path: Path | None = None,
) -> None:
    if state_path is None:
        state_path = ALERT_STATE_PATH
    now = now or datetime.now(timezone.utc)
    state = _load_alert_state(state_path)
    state[_cooldown_key(symbol, kind)] = {"signature": signature, "sent_at": now.isoformat()}
    _save_alert_state(state, state_path)
```

既有呼叫端（`run_watch_cycle`）會在 Task 5 一併改寫；本任務若因簽章改變而
使 `run_watch_cycle` 的既有測試失敗，暫時在 `run_watch_cycle` 內以
`kind="legacy_combined"` 傳入以維持綠燈，Task 5 再移除。

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_intraday_watcher.py -q`
Expected: PASS

- [ ] **Step 5: 跑全套**

Run: `python -m pytest -q`
Expected: 全綠

- [ ] **Step 6: Commit**

```bash
git add intraday_watcher.py tests/test_intraday_watcher.py
git commit -m "Scope alert cooldown per signal kind to fix churn bypass"
```

---

### Task 5: 分級路由、落地與每日推播預算

**Files:**
- Modify: `intraday_watcher.py`（新增 `classify_and_route`、改寫 `run_watch_cycle`）
- Test: `tests/test_intraday_watcher.py`（追加）

**Interfaces:**
- Consumes: Task 1 `signal_tiering.classify/build_regime/urgent_priority`；Task 2 `db_manager.save_signal_event/count_urgent_delivered`；Task 3 `current_trading_date`、`check_wall_breach` 的 dict；Task 4 的 `should_send_alert(symbol, kind, signature, ...)`
- Produces:
  - `MAX_URGENT_PUSHES_PER_DAY = 8`
  - `extract_signals(result: dict) -> list[dict]`（把 `run_check` 結果拆成 `[{"kind","payload","signature","text"}]`）
  - `classify_and_route(symbol, result, regime, trading_date, db_path=..., now=None) -> list[dict]`（回傳實際要推播的 urgent 訊號）

- [ ] **Step 1: 寫失敗測試**

```python
# ---------- 分級路由與預算 ----------

def _result_with_put_wall_breach(symbol="TSLA"):
    return {
        "symbol": symbol, "spot": 85.0,
        "wall_breach": {
            "kind": "put_wall_breach", "wall_price": 90.0, "spot": 85.0,
            "text": f"{symbol} 現貨 $85.00 向下穿越 Put Wall $90",
        },
        "pinning_alert": None, "unusual_activity": [], "error": None,
    }


def test_extract_signals_splits_result_into_independent_signals():
    result = {
        "symbol": "TSLA", "spot": 115.0,
        "wall_breach": {"kind": "call_wall_breach", "wall_price": 110.0,
                        "spot": 115.0, "text": "穿越"},
        "pinning_alert": None,
        "unusual_activity": [{"strike": 120.0, "side": "call", "volume": 5000,
                              "oi": 1000, "ratio": 5.0, "likely_opening": None}],
        "error": None,
    }

    signals = intraday_watcher.extract_signals(result)

    kinds = {s["kind"] for s in signals}
    assert "call_wall_breach" in kinds
    assert "unusual_activity" in kinds
    assert len(signals) == 2


def test_classify_and_route_persists_every_signal_including_silent(tmp_path):
    """靜默訊號也必須落地——那是未來績效儀表板的資料來源。"""
    db_path = tmp_path / "history.db"
    result = {
        "symbol": "TSLA", "spot": 100.0, "wall_breach": None,
        "pinning_alert": {"score": 40}, "unusual_activity": [], "error": None,
    }
    regime = signal_tiering.build_regime(spot=100.0, gamma_flip=95.0, total_net_gex=1.0)

    urgent = intraday_watcher.classify_and_route(
        "TSLA", result, regime, "2026-09-09", db_path=db_path,
    )

    assert urgent == []
    with sqlite3.connect(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM signal_events WHERE classified_tier='silent'"
        ).fetchone()[0]
    assert count == 1


def test_classify_and_route_returns_urgent_for_put_wall_breach(tmp_path):
    db_path = tmp_path / "history.db"
    regime = signal_tiering.build_regime(spot=85.0, gamma_flip=80.0, total_net_gex=1.0)

    urgent = intraday_watcher.classify_and_route(
        "TSLA", _result_with_put_wall_breach(), regime, "2026-09-09", db_path=db_path,
    )

    assert len(urgent) == 1
    assert urgent[0]["kind"] == "put_wall_breach"


def test_daily_budget_demotes_excess_urgent_to_watch(tmp_path, monkeypatch):
    """超出預算的訊號 delivered_tier 降為 watch，但 classified_tier 不變。"""
    db_path = tmp_path / "history.db"
    monkeypatch.setattr(intraday_watcher, "MAX_URGENT_PUSHES_PER_DAY", 1)
    regime = signal_tiering.build_regime(spot=85.0, gamma_flip=80.0, total_net_gex=1.0)

    first = intraday_watcher.classify_and_route(
        "TSLA", _result_with_put_wall_breach("TSLA"), regime, "2026-09-09", db_path=db_path,
    )
    second = intraday_watcher.classify_and_route(
        "MU", _result_with_put_wall_breach("MU"), regime, "2026-09-09", db_path=db_path,
    )

    assert len(first) == 1
    assert second == []          # 預算已用盡，被擠掉

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT classified_tier, delivered_tier, reason FROM signal_events "
            "WHERE symbol='MU'"
        ).fetchone()
    assert row["classified_tier"] == "urgent"     # 訊號品質不變
    assert row["delivered_tier"] == "watch"       # 只是通道降級
    assert "預算" in row["reason"]


def test_budget_is_global_across_symbols(tmp_path, monkeypatch):
    """預算是跨標的合計——TSLA 用掉的額度會影響 MU。"""
    db_path = tmp_path / "history.db"
    monkeypatch.setattr(intraday_watcher, "MAX_URGENT_PUSHES_PER_DAY", 2)
    regime = signal_tiering.build_regime(spot=85.0, gamma_flip=80.0, total_net_gex=1.0)

    for symbol in ("TSLA", "MU"):
        intraday_watcher.classify_and_route(
            symbol, _result_with_put_wall_breach(symbol), regime, "2026-09-09", db_path=db_path,
        )
    third = intraday_watcher.classify_and_route(
        "SPCX", _result_with_put_wall_breach("SPCX"), regime, "2026-09-09", db_path=db_path,
    )

    assert third == []
```

測試檔頂端須有 `import sqlite3` 與 `import signal_tiering`。

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_intraday_watcher.py -q -k "extract_signals or classify_and_route or budget"`
Expected: FAIL，`AttributeError: module 'intraday_watcher' has no attribute 'extract_signals'`

- [ ] **Step 3: 寫最小實作**

在 `intraday_watcher.py` 常數區加入：

```python
# 每日緊急推播上限——跨所有標的合計，不是每檔各 8 則。使用者定案的區間是
# 5~10 則/天，取中值。做成單一常數而非分散的門檻：跑幾週之後依實際感受調
# 這一個數字就好，不用重新校準每個訊號的靈敏度。
MAX_URGENT_PUSHES_PER_DAY = 8
```

新增：

```python
def extract_signals(result: dict) -> list[dict]:
    """把 run_check() 的結果拆成彼此獨立的訊號。

    拆開是分級的前提：原本所有異常被壓成同一則訊息、共用同一個簽章，
    導致無法分別判斷緊急度，也讓最吵的訊號穿透冷卻。
    """
    signals: list[dict] = []
    symbol = result["symbol"]

    breach = result.get("wall_breach")
    if breach:
        signals.append({
            "kind": breach["kind"],
            "payload": {"wall_price": breach["wall_price"], "spot": breach["spot"]},
            "signature": breach["kind"],
            "text": breach["text"],
        })

    pinning = result.get("pinning_alert")
    if pinning:
        score = pinning.get("score") if isinstance(pinning, dict) else None
        signals.append({
            "kind": signal_tiering.KIND_PINNING_HIGH,
            "payload": {"score": score or 0},
            "signature": f"pinning:{score}",
            "text": pinning.get("text") if isinstance(pinning, dict) else str(pinning),
        })

    for item in result.get("unusual_activity") or []:
        ratio = item["ratio"]
        ratio_text = "∞" if ratio == float("inf") else f"{ratio:.1f}x"
        signals.append({
            "kind": signal_tiering.KIND_UNUSUAL_ACTIVITY,
            "payload": {"strike": item["strike"], "side": item["side"],
                        "volume": item["volume"], "ratio": ratio,
                        "likely_opening": item.get("likely_opening")},
            "signature": f"{item['side']}:{item['strike']}",
            "text": (f"{symbol} ${item['strike']:.0f} {item['side'].upper()} 出現巨量："
                     f"成交量 {item['volume']:,.0f} 張（OI的 {ratio_text}）"),
        })

    return signals


def classify_and_route(
    symbol: str,
    result: dict,
    regime: dict,
    trading_date: str,
    db_path: Path | str = db_manager.DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> list[dict]:
    """分級、落地、套用每日預算，回傳實際要推播的 urgent 訊號。

    分級跟落地都是「加分項」：失敗只記警告，不能讓核心的偵測與既有推播
    連帶失敗。
    """
    now = now or datetime.now(timezone.utc)
    detected_at = now.isoformat()

    classified: list[dict] = []
    for signal in extract_signals(result):
        try:
            tier, reason = signal_tiering.classify(signal["kind"], signal["payload"], regime)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 訊號分級失敗（%s）：%s", symbol, signal["kind"], exc)
            continue
        classified.append({**signal, "tier": tier, "reason": reason})

    # 緊急訊號依優先序排列，讓預算用盡時保留最重要的那幾則，而不是先到先贏。
    urgent_candidates = sorted(
        (s for s in classified if s["tier"] == "urgent"),
        key=lambda s: signal_tiering.urgent_priority(s["kind"]),
    )

    try:
        already_sent = db_manager.count_urgent_delivered(trading_date, db_path=db_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("讀取當日推播計數失敗，本輪不套用預算限制：%s", exc)
        already_sent = 0

    remaining = max(MAX_URGENT_PUSHES_PER_DAY - already_sent, 0)
    to_push: list[dict] = []

    for signal in classified:
        delivered_tier = signal["tier"]
        reason = signal["reason"]

        if signal["tier"] == "urgent":
            if signal in urgent_candidates[:remaining]:
                to_push.append(signal)
            else:
                # 只降 delivered_tier，classified_tier 維持 urgent——否則日後
                # 績效統計會被「當天還發生了什麼事」條件化，而非訊號品質。
                delivered_tier = "watch"
                reason = f"{reason}｜超出當日推播預算（上限 {MAX_URGENT_PUSHES_PER_DAY}）"

        try:
            db_manager.save_signal_event(
                symbol, detected_at, trading_date, signal["kind"],
                classified_tier=signal["tier"], delivered_tier=delivered_tier,
                reason=reason, signature=signal["signature"],
                payload={**signal["payload"], "text": signal["text"]},
                db_path=db_path,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 訊號落地失敗（%s）：%s", symbol, signal["kind"], exc)

    return to_push
```

改寫 `run_watch_cycle`：

```python
def run_watch_cycle(symbols: list[str], notify: bool = False, force: bool = False) -> None:
    if not force and not is_market_hours():
        logger.info("目前不是美股股票期權正式交易時間，略過本次檢查")
        return

    trading_date = current_trading_date()
    state_path = ALERT_STATE_PATH
    prev_spots = _load_alert_state(state_path).get("_prev_spots", {})

    for symbol in symbols:
        result = run_check(symbol, prev_spot=prev_spots.get(symbol))
        if result["error"]:
            continue

        # 記下這次的 spot 供下一輪做 crossing 比較
        prev_spots[symbol] = result["spot"]

        rows = db_manager.get_recent_snapshots(symbol, limit=1)
        latest = rows[0] if rows else {}
        regime = signal_tiering.build_regime(
            spot=result["spot"],
            gamma_flip=latest.get("gamma_flip"),
            total_net_gex=latest.get("total_net_gex"),
        )

        urgent = classify_and_route(symbol, result, regime, trading_date)
        if not urgent:
            logger.info("%s 本輪無緊急訊號（現貨 $%.2f）", symbol, result["spot"])
            continue

        for signal in urgent:
            alert_text = f"{INTRADAY_ALERT_PREFIX}\n\n{signal['text']}"
            logger.warning(alert_text)
            print(alert_text)
            if notify and should_send_alert(symbol, signal["kind"], signal["signature"]):
                import telegram_notifier
                telegram_notifier.send_text_report(alert_text)
                record_alert_sent(symbol, signal["kind"], signal["signature"])
            elif notify:
                logger.info("%s %s 仍在冷卻時間內，略過重複推播", symbol, signal["kind"])

    state = _load_alert_state(state_path)
    state["_prev_spots"] = prev_spots
    _save_alert_state(state, state_path)
```

移除 Task 4 暫時加入的 `kind="legacy_combined"` 呼叫。

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_intraday_watcher.py -q`
Expected: PASS

- [ ] **Step 5: 跑全套**

Run: `python -m pytest -q`
Expected: 全綠

- [ ] **Step 6: Commit**

```bash
git add intraday_watcher.py tests/test_intraday_watcher.py
git commit -m "Route signals by tier with a global daily push budget"
```

---

### Task 6: 觀察名單併入 10:00 摘要與 16:30 日報

**Files:**
- Modify: `run_watchlist.py`（`run_intraday_summary` 與 daily 主流程）
- Test: `tests/test_run_watchlist.py`（追加）

**Interfaces:**
- Consumes: Task 2 `db_manager.get_undelivered_watch_events/mark_events_delivered`
- Produces: `build_watch_section(symbols, db_path=...) -> tuple[str, list[int]]`（回傳 `(文字區塊, 要標記已送的 event id 清單)`；沒有待送項目時文字為空字串）

- [ ] **Step 1: 寫失敗測試**

```python
# ---------- 觀察名單 ----------

def test_build_watch_section_is_empty_without_pending_events(tmp_path):
    db_path = tmp_path / "history.db"
    db_manager.get_undelivered_watch_events("TSLA", db_path=db_path)  # 建表

    text, ids = run_watchlist.build_watch_section(["TSLA"], db_path=db_path)

    assert text == ""
    assert ids == []


def test_build_watch_section_lists_pending_events(tmp_path):
    db_path = tmp_path / "history.db"
    event_id = db_manager.save_signal_event(
        "TSLA", "2026-09-09T14:00:00+00:00", "2026-09-09", "call_wall_breach",
        classified_tier="watch", delivered_tier="watch",
        reason="正 Gamma 通常被壓回",
        signature="call_wall_breach",
        payload={"text": "TSLA 現貨 $115.00 向上穿越 Call Wall $110"},
        db_path=db_path,
    )

    text, ids = run_watchlist.build_watch_section(["TSLA"], db_path=db_path)

    assert "觀察名單" in text
    assert "Call Wall" in text
    assert ids == [event_id]


def test_watch_events_are_not_repeated_after_delivery(tmp_path):
    """drain 之後標記已送，下一個摘要時間點不得重複出現。"""
    db_path = tmp_path / "history.db"
    db_manager.save_signal_event(
        "TSLA", "2026-09-09T14:00:00+00:00", "2026-09-09", "call_wall_breach",
        classified_tier="watch", delivered_tier="watch", reason="正 Gamma",
        signature="call_wall_breach", payload={"text": "穿越 Call Wall"},
        db_path=db_path,
    )

    _, ids = run_watchlist.build_watch_section(["TSLA"], db_path=db_path)
    db_manager.mark_events_delivered(
        ids, "2026-09-09T20:30:00+00:00", "daily_report", db_path=db_path,
    )

    text, ids_again = run_watchlist.build_watch_section(["TSLA"], db_path=db_path)

    assert text == ""
    assert ids_again == []
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_run_watchlist.py -q -k watch_section`
Expected: FAIL，`AttributeError: module 'run_watchlist' has no attribute 'build_watch_section'`

- [ ] **Step 3: 寫最小實作**

在 `run_watchlist.py` 加入：

```python
def build_watch_section(
    symbols: list[str], db_path: Path | str = db_manager.DEFAULT_DB_PATH,
) -> tuple[str, list[int]]:
    """組出「觀察名單」文字區塊，並回傳要標記已送的 event id。

    交付規則就是「每個摘要時間點清空待送佇列」，不需要額外的時間判斷邏輯：
    10:00 執行時佇列裡只會有前一日 16:30 之後累積的項目，反之亦然。
    delivered_at 是唯一狀態，天然保證每筆只送一次。
    """
    lines: list[str] = []
    event_ids: list[int] = []

    for symbol in symbols:
        try:
            events = db_manager.get_undelivered_watch_events(symbol, db_path=db_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 讀取觀察名單失敗：%s", symbol, exc)
            continue
        for event in events:
            text = (event.get("payload") or {}).get("text") or event["kind"]
            lines.append(f"• {text}\n  （{event['reason']}）")
            event_ids.append(event["id"])

    if not lines:
        return "", []

    section = "👀 觀察名單（值得注意，不需立刻動作）\n\n" + "\n".join(lines)
    return section, event_ids
```

在 `run_intraday_summary` 中，於 `text = "\n".join(lines).strip()` 之後、
`if notify:` 之前插入：

```python
    watch_text, watch_ids = build_watch_section(symbols)
    if watch_text:
        text = f"{text}\n\n{watch_text}"
```

並把 `notify` 區塊改為推播成功後才標記：

```python
    if notify:
        import telegram_notifier
        telegram_notifier.send_text_report(text)
        # 推播成功才標記已送——失敗時不標記，下次摘要會重試。
        if watch_ids:
            db_manager.mark_events_delivered(
                watch_ids, datetime.now(timezone.utc).isoformat(), "intraday_summary",
            )
```

在 daily 主流程中（`summary_text = build_watchlist_summary(summaries)` 之後）
做同樣的事，`channel` 用 `"daily_report"`：

```python
    watch_text, watch_ids = build_watch_section(symbols)
    if watch_text:
        summary_text = f"{summary_text}\n\n{watch_text}"
```

並在既有的 `if args.notify:` 區塊內，`send_text_report(summary_text)` 之後加上
標記（同樣是成功才標記）。

須確認 `run_watchlist.py` 已 import `db_manager`、`Path`、
`from datetime import timezone`。

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_run_watchlist.py -q`
Expected: PASS

- [ ] **Step 5: 跑全套**

Run: `python -m pytest -q`
Expected: 全綠

- [ ] **Step 6: 用真實生產資料驗證一次**

依本專案「測試優先驗證」慣例，合成資料綠燈之後要用真實資料跑一次確認合理。
把線上 `history.db` 拉下來（唯讀，不回寫）：

```bash
railway ssh "python -c \"
import base64,sys
sys.stdout.write(base64.b64encode(open('/app/data/history.db','rb').read()).decode())
\"" > /tmp/db.b64
```

解碼後用該檔跑 `classify_and_route` 的乾跑，確認：
- 分級結果沒有例外
- urgent 數量落在合理範圍（不應該一天就爆掉 8 則）
- `signal_events` 有正確寫入三種 tier

- [ ] **Step 7: Commit**

```bash
git add run_watchlist.py tests/test_run_watchlist.py
git commit -m "Deliver watch-tier signals in intraday summary and daily report"
```

---

### Task 7: 更新文件

**Files:**
- Modify: `CLAUDE.md`（檔案清單表格 + 架構總覽）
- Modify: `SETUP.md`（第8節，說明分級行為與可調常數）

- [ ] **Step 1: 更新 `CLAUDE.md` 檔案清單**

在表格中 `smart_money.py` 之後加入一列：

```markdown
| `signal_tiering.py` | 訊號分級政策（urgent/watch/silent）——純計算，不做 I/O、不讀狀態 |
```

在架構總覽的「純計算層」區塊加入 `signal_tiering.py`，在儲存層說明加入
`signal_events` 表。

- [ ] **Step 2: 更新 `SETUP.md` 第8節**

加入一小節說明：

```markdown
### 盤中訊號分級

盤中訊號分成三級：
- **緊急**：立即推播 Telegram。每日上限由 `intraday_watcher.MAX_URGENT_PUSHES_PER_DAY`
  控制（預設 8，跨所有標的合計）。要調鬆緊改這一個常數即可。
- **觀察**：不即時推播，累積後併入 10:00 ET 摘要或 16:30 日報。
- **靜默**：只寫入 `signal_events` 表，不推播。

分級規則寫在 `signal_tiering.py`，是規則式判斷、不是回測驗證過的最佳解，
且門檻只在單一趨勢盤 regime 上校準過——詳見
`docs/superpowers/specs/2026-09-09-signal-tiering-design.md` 的「已知限制」。
```

- [ ] **Step 3: 跑全套確認文件改動沒影響測試**

Run: `python -m pytest -q`
Expected: 全綠

- [ ] **Step 4: Commit 並部署**

```bash
git add CLAUDE.md SETUP.md
git commit -m "Document signal tiering behavior and tunable push budget"
git push origin main    # Railway 自動部署
```

部署後依 `SETUP.md` 驗證：確認 deployment 狀態為 SUCCESS、容器內測試全綠、
bot 正常長輪詢。

---

## Self-Review

**Spec 覆蓋檢查**

| Spec 章節 | 對應任務 |
|---|---|
| 問題一（冷卻被穿透） | Task 4 |
| 問題二（狀態 vs 事件） | Task 3 |
| 問題三（沒有緊急度） | Task 1 + Task 5 |
| 問題四（Pinning 門檻） | Task 1（`PINNING_WATCH_SCORE_THRESHOLD = 70`） |
| 問題五（likely_opening） | Task 1（異常大單永不 urgent）+ Task 5（不接 previous_oi） |
| regime 用即時代理 | Task 1 `build_regime` |
| `classified_tier` / `delivered_tier` 拆兩欄 | Task 2 + Task 5 |
| 每日推播預算 | Task 5 |
| 觀察名單交付 | Task 6 |
| 測試策略各項 | Task 1、3、4、5、6 的測試步驟 |

無未覆蓋項目。

**型別一致性**

- `check_wall_breach` 回傳 dict，鍵 `kind`/`wall_price`/`spot`/`text` —— Task 3
  定義，Task 5 `extract_signals` 消費，一致。
- `classify` 回傳 `(tier, reason)` —— Task 1 定義，Task 5 消費，一致。
- `should_send_alert(symbol, kind, signature, ...)` —— Task 4 定義，Task 5 呼叫，一致。
- `save_signal_event` 參數順序與關鍵字 —— Task 2 定義，Task 5 呼叫，一致。
- `build_watch_section` 回傳 `(str, list[int])` —— Task 6 定義並自用，一致。
