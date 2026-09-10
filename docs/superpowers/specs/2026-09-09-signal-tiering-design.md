# 訊號分級（Signal Tiering）設計

日期：2026-09-09
狀態：待實作

## 背景

目前盤中監控是「全有全無」：`intraday_watcher.run_check()` 偵測到的每一件事
——牆位突破、Pinning 分數、異常大單——都被 `build_alert_text()` 壓成同一則
訊息推到 Telegram。唯一的音量控制是 60 分鐘冷卻，而那個冷卻有漏洞。

### 問題一：冷卻機制實際上是失效的

`build_alert_signature()` 把所有訊號串成單一簽章：

```python
parts.append("call_wall" / "put_wall")
parts.append("pinning_over_80")
parts.append(f"{item['side']}:{item['strike']}")   # 每筆異常大單
```

異常大單由「成交量/OI 比值」觸發，盤中隨成交量累積，符合條件的履約價會持續
變動。履約價一變、簽章就變、`should_send_alert()` 直接放行。結果是：

- **最吵的訊號擁有最高的推播頻率**（履約價 churn 讓它每次都被當新事件）
- **真正該冷卻的持續性事件被連帶重發**（牆位突破跟異常大單綁在同一個簽章）

這是「Telegram 每個東西都在吵」的機制性成因，不是門檻沒調好。

### 問題二：沒有緊急度概念

一則「某履約價成交量放大 3 倍」跟一則「負 Gamma 下 Put Wall 跌破」用完全相同
的通道、相同的急迫程度送達。使用者無法從推播本身判斷該不該立刻反應。

### 問題三：Pinning 警報從未觸發

`PINNING_ALERT_SCORE_THRESHOLD = 80`，但生產資料 78 筆快照中 `pinning_score`
**最高只有 76**，分布集中在 36~38。這條警報從上線至今一次都沒發出過，是實質
上的死碼。

### 問題四：盤中路徑拿不到開倉/平倉的區分

`check_unusual_activity()` 呼叫 `smart_money.detect_unusual_activity()` 時
**沒有傳 `previous_oi_by_strike`**（`intraday_watcher.py:159-161`），因此盤中
路徑的 `likely_opening` 永遠是 `None`。資料本身存在（`oi_snapshots` 表 +
`db_manager.get_oi_snapshot()` / `get_most_recent_oi_snapshot_date()`），只是
沒有接上。分不出「新開倉」與「平倉/轉倉」，異常大單就無法分級。

## 目標

1. 每個訊號獨立分級為三級之一，並附上可讀的判斷理由
2. 修正冷卻機制，讓去重以「訊號種類」為單位而非全體合併
3. 為「緊急」級加上硬性每日推播上限，超額時依緊急度排序取前 N 則
4. 觀察名單批次交付，不增加推播次數
5. 所有偵測結果（含不推播的）落地成結構化紀錄，作為後續績效儀表板的資料來源

## 非目標

- **不改任何偵測邏輯。** `check_wall_breach` / `check_pinning_alert` /
  `check_unusual_activity` 的判斷條件維持原樣，本次只在偵測「之後」加路由層。
  唯一例外是問題四（補傳 `previous_oi_by_strike`），那是補上既有能力而非改判斷。
- **不做資料驅動的自動分級。** 目前 `signal_auditor` 樣本數不足以支撐（TSLA 僅
  26 筆快照、多數訊號去重後不到 5 段）。本次採規則式分級，符合本專案「透明的
  量化判斷」慣例；未來有資料後再談自動升降級。
- **不動日報。** 日報維持現有完整結構，觀察名單以附加區塊的形式併入。

## 分級定義

| 級別 | 通道 | 時效 |
|---|---|---|
| `urgent` 盤中警報 | 立即推播 Telegram | 即時 |
| `watch` 觀察名單 | 併入 10:00 ET 摘要或 16:30 日報 | 最多延遲約 6.5 小時 |
| `silent` 靜默紀錄 | 只寫入資料庫 | 不主動送達 |

日報（完整結構）不是本次分級的對象，它是既有的獨立輸出。

## 架構

### 方案選擇

採「獨立的純計算分級模組」。理由：本專案已有明確慣例線（`gex_engine`、
`smart_money`、`strategy_tracker` 皆為純計算、不做 I/O、可用合成資料測試），
分級政策集中在單一模組才能一眼看完、獨立測試、單點調整。

已否決的替代方案：
- **讓各 `check_*` 函式自行回傳 tier**：分級邏輯散落各處，看不到完整政策，
  且必須連帶起 I/O 才測得到。
- **對組好的警報全文做分類**：以文字當結構化資料——與問題一的 signature bug
  是同一個錯誤，必然重蹈覆轍。

### 新增：`signal_tiering.py`（純計算）

```python
Tier = Literal["urgent", "watch", "silent"]

def classify(kind: str, payload: dict, regime: dict) -> tuple[Tier, str]:
    """回傳 (級別, 理由)。理由是給人看的短句，也會存進 DB 供日後稽核。"""
```

- `kind`：`"call_wall_breach"` / `"put_wall_breach"` / `"pinning_high"` /
  `"unusual_activity"` / `"mm_pressure"`
- `payload`：該訊號的細節（履約價、比值、分數、`likely_opening` 等）
- `regime`：由最近一筆 `daily_snapshots` 推導的市場狀態，至少含
  `total_net_gex`、`zero_dte_share_pct`

不做任何 I/O。`regime` 由呼叫端組好傳入。

### 新增：`signal_events` 資料表

一張表服務三級——靜默是「永不被 drain 的列」，觀察是「等待 drain 的列」，
緊急是「已推播並標記的列」。同時作為未來績效儀表板的資料來源，不另設第二套。

| 欄位 | 說明 |
|---|---|
| `id` | 主鍵 |
| `symbol` | 標的 |
| `detected_at` | 偵測時間（UTC ISO） |
| `trading_date` | 所屬交易日，取偵測當下的**美東日期**（非 UTC，非本地時區），供每日預算計數與日後統計分組 |
| `kind` | 訊號種類 |
| `tier` | `urgent` / `watch` / `silent` |
| `reason` | 分級理由（人可讀） |
| `signature` | 該訊號種類的去重簽章 |
| `payload_json` | 訊號細節，JSON 字串 |
| `delivered_at` | 送達時間；`NULL` 表示尚未送達 |
| `delivery_channel` | `telegram_urgent` / `intraday_summary` / `daily_report` |

索引：`(symbol, trading_date)`、`(tier, delivered_at)`（drain 查詢用）。

### 改動：`intraday_watcher.py`

- `run_check()` 維持原樣（仍然只負責偵測）
- 新增 `classify_and_route()`：把 `run_check()` 的結果拆成獨立訊號，逐一
  `classify()`，全部寫入 `signal_events`，回傳其中的 `urgent` 清單
- `build_alert_text()` 改為只組 `urgent` 訊號
- **冷卻改為每訊號種類獨立**：`should_send_alert()` 的狀態鍵從 `symbol` 改為
  `(symbol, kind)`。異常大單的履約價 churn 不再能穿透牆位突破的冷卻。
- `check_unusual_activity()` 補傳 `previous_oi_by_strike`（用
  `db_manager.get_most_recent_oi_snapshot_date()` +
  `get_oi_snapshot()`），讓 `likely_opening` 在盤中路徑真的有值

### 改動：`run_watchlist.py`

`--intraday-summary` 與日報兩條路徑都在輸出末端追加「觀察名單」區塊：drain
該標的所有 `tier='watch' AND delivered_at IS NULL` 的列，附進訊息，成功送出
後標記 `delivered_at` 與 `delivery_channel`。

**交付規則就是「每個摘要時間點清空待送佇列」，不需要額外的時間判斷邏輯。**
「盤中的進 16:30、隔夜的進 10:00」是這個機制的自然結果而非另一條規則——
10:00 跑的時候，佇列裡只會有前一天 16:30 之後累積的項目；16:30 跑的時候，
只會有當天 10:00 之後累積的項目。`delivered_at` 是唯一的狀態，天然保證每筆
只送一次。

### 每日推播預算

```python
MAX_URGENT_PUSHES_PER_DAY = 8      # 使用者定案：寬鬆 5~10 則/天
```

單一可調常數。超過上限時不是先到先贏，而是依緊急度排序取前 N；被擠掉的訊號
降級為 `watch`（不是丟棄），理由欄位註明「超出當日推播預算」。

緊急度排序（數字越小越優先）：

1. `put_wall_breach`（防守優先，見下）
2. `mm_pressure` 於負 Gamma
3. `call_wall_breach` 於負 Gamma
4. `unusual_activity` 極端比值且 `likely_opening`

## 分級規則

`regime` 定義：`total_net_gex < 0` 為負 Gamma，否則為正 Gamma。

| 訊號 | urgent | watch | silent |
|---|---|---|---|
| Put Wall 跌破 | **一律** | — | — |
| Call Wall 突破 | 負 Gamma | 正 Gamma | — |
| 做市商賣壓警報 | 負 Gamma | 正 Gamma | — |
| 異常大單 | 比值 ≥ 6.0 且 `likely_opening` 為真 | 比值 ≥ 3.0 | 比值 < 3.0，或 `likely_opening` 為假／`None` |
| Pinning 高分 | — | 分數 ≥ 70 | 分數 < 70 |

**分級與去重是兩層，不要混為一談。** `classify()` 是純函式、無狀態，它只看
「這個訊號本身是什麼」，不知道剛剛有沒有發過同樣的東西。持續中的同一段行情
會**每次都被分成同一級**——把它壓下來是冷卻層的職責（見下節），不是分級層的。
實作時不可以在 `classify()` 裡讀狀態，那會讓它失去可測性。

### 為什麼 Put Wall 跌破不分 regime

上下方向的風險不對稱。正 Gamma 下 Call Wall 突破被壓回，代價是錯過一段漲幅；
正 Gamma 下 Put Wall 跌破若未被壓回，代價是實際虧損。此格採「保護優先」而非
「對稱處理」。

### 為什麼 Pinning 門檻從 80 降到 70

生產資料 78 筆中 `pinning_score` 最高 76，80 分門檻從未觸發。降至 70 後回溯
會觸發 2 次（約 2.6%），是合理的稀有度。同時只放進 `watch` 而非 `urgent`：
Pinning 描述的是「價格可能被釘住」的狀態，本質上不急迫。

### 為什麼異常大單的 urgent 需要 `likely_opening`

單看成交量/OI 比值無法分辨新開倉與平倉/轉倉。平倉的巨量不代表有人在建立方向性
部位，把它推成緊急警報是純噪音。要求 `likely_opening` 為真才升級，是把問題四
接上線之後才可能的判斷。

## 測試策略

依本專案慣例：先用合成資料寫 pytest，再用真實資料跑一次確認合理。

`signal_tiering.py` 為純函式，全部用合成 `payload` + `regime` 測：

- 每一格分級規則各一個測試（含邊界值：比值恰為 3.0 / 6.0、分數恰為 70）
- Put Wall 跌破在正負 Gamma 下都必須是 `urgent`（不對稱規則的迴歸測試）
- `likely_opening` 為 `None`（資料缺失）時異常大單不得升級為 `urgent`

`intraday_watcher` 的路由與冷卻：

- **同種訊號在冷卻期內不重送**
- **不同種訊號互不干擾**——這是問題一的迴歸測試：異常大單履約價改變時，
  牆位突破的冷卻必須仍然生效
- 超出每日預算時，低優先訊號降級為 `watch` 而非消失

`run_watchlist` 的 drain：

- 已 drain 的項目不會在下一次摘要重複出現
- drain 失敗（推播例外）時不得標記 `delivered_at`，下次要能重試

## 已知限制

- **分級規則是規則式判斷，不是回測驗證過的最佳解。** 與本專案既有的
  `select_strategy` / `compute_market_maker_pressure_score` 同性質：把判斷邏輯
  攤開透明，而非宣稱最佳。
- **`regime` 取自最近一筆日快照**，盤中不重算 GEX（重算整條期權鏈成本過高，
  這是既有的架構取捨）。因此盤中 regime 判斷最舊可能是前一交易日收盤的狀態。
- **每日推播預算的排序是靜態優先序**，不是動態的重要性評分。有績效資料之後
  可以改成依 `signal_auditor` 的超額表現排序。
- **Pinning 門檻 70 是依 78 筆資料訂的**，樣本仍然很小，累積更多資料後應複查。

## 後續（不在本次範圍）

- 依 `signal_auditor` 的超額表現自動升降級訊號
- 績效儀表板（roadmap #5）直接讀 `signal_events` 表
- `gamma_flip_touch` 的距離配對對照組
