# 訊號分級（Signal Tiering）設計

日期：2026-09-09
狀態：待實作（v2——依量化方法論覆核修訂）

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

### 問題二：牆位突破是「狀態」不是「事件」

```python
if call_wall and spot > call_wall:      # intraday_watcher.py:104
```

這是**價位條件**，不是**穿越事件**。價格整天待在 Call Wall 上方，每一次檢查
都會回傳「突破」。單靠冷卻限流無法治本：價格不動地待在牆上一整天，60 分鐘
冷卻仍會每小時推一次，六個半小時就是 6 則——一檔標的吃掉大半日預算。

**這是最大的噪音來源，也是分級能否成立的前提**：分級假設訊號是離散事件，
而現況下它是連續狀態。不先修這個，分級只是幫噪音貼標籤。

### 問題三：沒有緊急度概念

一則「某履約價成交量放大 3 倍」跟一則「Put Wall 跌破」用完全相同的通道、
相同的急迫程度送達。使用者無法從推播本身判斷該不該立刻反應。

### 問題四：Pinning 警報從未觸發

`PINNING_ALERT_SCORE_THRESHOLD = 80`，但生產資料 78 筆快照中 `pinning_score`
**最高只有 76**，分布集中在 36~38。這條警報從上線至今一次都沒發出過。

### 問題五：盤中無法區分開倉／平倉（結構性限制，非疏漏）

`smart_money.detect_unusual_activity()` 的 `likely_opening` 定義是：

```python
likely_opening = oi > prev_oi        # smart_money.py:131
```

OI 由 OCC 隔夜結算後公布，**盤中不更新**。交易時段內 yfinance 提供的 `oi`
是前一交易日收盤的 OI，`prev_oi` 則是再前一日的。因此盤中若接上
`previous_oi_by_strike`，比較的是 **T-1 對 T-2**，與「今天這筆巨量是開倉還是
平倉」無關，整整落後一日。

**接上去比不接更糟**：`None` 誠實表達「無法判斷」，而一個陳舊的布林值會被
誤當成有效資訊。這與 CLAUDE.md 既有紀錄一致——盤中成交方向判斷需要 OPRA 級
即時資料，屬架構升級而非小修。

（附註：`ratio = volume / oi` 以前一日 OI 為分母是業界標準做法，該計算無誤，
本問題僅影響 `likely_opening`。）

## 目標

1. 每個訊號獨立分級為三級之一，並附上可讀的判斷理由
2. 把牆位突破從「狀態偵測」改為「穿越事件偵測」
3. 修正冷卻機制，讓去重以「訊號種類」為單位而非全體合併
4. 為「緊急」級加上硬性每日推播上限，超額時依緊急度排序取前 N 則
5. 觀察名單批次交付，不增加推播次數
6. 所有偵測結果（含不推播的）落地成結構化紀錄，作為後續績效儀表板的資料來源

## 非目標

- **不改任何偵測門檻。** `UNUSUAL_ACTIVITY_MIN_RATIO` / `MIN_VOLUME`、
  Pinning 分數的計算方式維持原樣。唯一的偵測層改動是問題二的 crossing 判斷，
  那是修正一個型別錯誤（狀態 vs 事件），不是調整靈敏度。
- **不做資料驅動的自動分級。** `signal_auditor` 樣本不足以支撐（TSLA 僅 26 筆
  快照、多數訊號去重後不到 5 段）。本次採規則式分級，符合本專案「透明的量化
  判斷」慣例。
- **不動日報結構。** 觀察名單以附加區塊併入。

## 分級定義

| 級別 | 通道 | 時效 |
|---|---|---|
| `urgent` 盤中警報 | 立即推播 Telegram | 即時 |
| `watch` 觀察名單 | 併入 10:00 ET 摘要或 16:30 日報 | 最多延遲約 6.5 小時 |
| `silent` 靜默紀錄 | 只寫入資料庫 | 不主動送達 |

## 架構

### 方案選擇

採「獨立的純計算分級模組」。本專案已有明確慣例線（`gex_engine`、
`smart_money`、`strategy_tracker` 皆為純計算、不做 I/O、可用合成資料測試），
分級政策集中在單一模組才能一眼看完、獨立測試、單點調整。

已否決：讓各 `check_*` 自行回傳 tier（政策散落、必須起 I/O 才測得到）；
對組好的警報全文做分類（以文字當結構化資料，與問題一的 bug 同源）。

### 新增：`signal_tiering.py`（純計算）

```python
Tier = Literal["urgent", "watch", "silent"]

def classify(kind: str, payload: dict, regime: dict) -> tuple[Tier, str]:
    """回傳 (級別, 理由)。理由給人看，也存進 DB 供日後稽核。"""
```

不做任何 I/O、**不讀任何狀態**。`regime` 由呼叫端組好傳入。

### regime 的定義：用即時代理，不用隔夜 GEX

**不採用 `total_net_gex < 0`。** 該值來自前一交易日收盤快照，而 dealer gamma
盤中就會翻轉——那正是 gamma flip 這個概念存在的理由。用昨收的 GEX 符號判斷
今天的 regime，會在「regime 正在改變」的日子系統性判錯，而那恰好是唯一重要
的日子；且錯誤方向不利：市場剛翻入負 gamma、波動放大時，閘門仍判定「正
gamma，不緊急」。

改用 **`spot` 相對 `gamma_flip` 的位置**作為即時代理：

```python
regime = {
    "negative_gamma": spot < gamma_flip,      # 即時，零額外成本
    "gamma_flip": gamma_flip,
    "total_net_gex": total_net_gex,           # 保留供稽核與日後分析，不作閘門
}
```

`gamma_flip` 已存在於快照，`spot` 本來就會抓，因此無額外 API 成本。
`gamma_flip` 為 `NULL` 時 `negative_gamma` 退回以 `total_net_gex < 0` 判斷，
並在 `reason` 註明使用了退化路徑。

### 新增：`signal_events` 資料表

一張表服務三級——靜默是「永不被 drain 的列」，觀察是「等待 drain 的列」，
緊急是「已推播並標記的列」。同時作為未來績效儀表板的資料來源。

| 欄位 | 說明 |
|---|---|
| `id` | 主鍵 |
| `symbol` | 標的 |
| `detected_at` | 偵測時間（UTC ISO） |
| `trading_date` | 所屬交易日，取偵測當下的**美東日期**（非 UTC、非本地時區） |
| `kind` | 訊號種類 |
| `classified_tier` | **分級層的判定**，純訊號品質，不受當日其他事件影響 |
| `delivered_tier` | **實際走的通道**，可能因每日預算而低於 `classified_tier` |
| `reason` | 分級理由（人可讀） |
| `signature` | 該訊號種類的去重簽章 |
| `payload_json` | 訊號細節，JSON 字串 |
| `delivered_at` | 送達時間；`NULL` 表示尚未送達 |
| `delivery_channel` | `telegram_urgent` / `intraday_summary` / `daily_report` |

索引：`(symbol, trading_date)`、`(delivered_tier, delivered_at)`。

#### 為什麼要拆兩個 tier 欄位

若只存單一 `tier`，且預算超額時直接把它改成 `watch`，那 `tier` 就變成「當天
還發生了什麼事」的函數，而非訊號品質的函數——同一個訊號，安靜的一天是
`urgent`，熱鬧的一天是 `watch`。

後果是：績效儀表板（roadmap #5）若用 `tier='urgent'` 取樣，該樣本是被**當日
到達順序**條件化過的，不是被訊號品質條件化的。這是會在三個月後才浮現的資料
污染。

**規則：所有績效統計一律用 `classified_tier`；`delivered_tier` 只用於稽核
推播行為本身。**

### 改動：`intraday_watcher.py`

**牆位突破改為 crossing 偵測**（問題二）。`check_wall_breach()` 需要上一次
檢查的 spot，存於現有的 alert state 檔：

```
突破事件成立 ⟺ spot_prev <= wall < spot_now   （向上穿越 Call Wall）
              ⟺ spot_prev >= wall > spot_now   （向下穿越 Put Wall）
```

沒有前次 spot 時（當日首次檢查、狀態檔遺失）**不視為突破事件**——寧可漏報
一次，也不要把「本來就在牆上」誤報成新突破。此行為需明確測試。

其餘改動：

- `run_check()` 維持原樣（仍只負責偵測）
- 新增 `classify_and_route()`：拆成獨立訊號、逐一 `classify()`、全部寫入
  `signal_events`、回傳 `urgent` 清單
- `build_alert_text()` 改為只組 `urgent` 訊號
- **冷卻改為每訊號種類獨立**：狀態鍵從 `symbol` 改為 `(symbol, kind)`
- **不接** `previous_oi_by_strike`（問題五：接上去會產生誤導性的陳舊旗標）

### 改動：`run_watchlist.py`

`--intraday-summary` 與日報兩條路徑都在輸出末端追加「觀察名單」區塊：drain
該標的所有 `delivered_tier='watch' AND delivered_at IS NULL` 的列，附進訊息，
成功送出後標記 `delivered_at` 與 `delivery_channel`。

**交付規則就是「每個摘要時間點清空待送佇列」，不需要額外的時間判斷邏輯。**
「盤中的進 16:30、隔夜的進 10:00」是這個機制的自然結果——10:00 執行時佇列裡
只會有前一日 16:30 之後累積的項目，反之亦然。`delivered_at` 是唯一狀態，天然
保證每筆只送一次。

### 每日推播預算

```python
MAX_URGENT_PUSHES_PER_DAY = 8      # 使用者定案：寬鬆 5~10 則/天
```

單一可調常數。超過上限時依緊急度排序取前 N；被擠掉者 `delivered_tier` 降為
`watch`（`classified_tier` **維持 `urgent` 不變**），`reason` 註明「超出當日
推播預算」。

緊急度排序（數字越小越優先）：

1. `put_wall_breach`（防守優先，見下）
2. `mm_pressure` 於負 Gamma
3. `call_wall_breach` 於負 Gamma

## 分級規則

| 訊號 | urgent | watch | silent |
|---|---|---|---|
| Put Wall 向下穿越 | **一律** | — | — |
| Call Wall 向上穿越 | 負 Gamma | 正 Gamma | — |
| 做市商賣壓警報 | 負 Gamma | 正 Gamma | — |
| 異常大單 | **不適用**（見問題五） | 比值 ≥ 3.0 | 比值 < 3.0 |
| Pinning 高分 | — | 分數 ≥ 70 | 分數 < 70 |

**分級與去重是兩層。** `classify()` 無狀態，只看「這個訊號本身是什麼」。
持續中的狀態由 crossing 偵測（問題二）與冷卻層處理，不是分級層的職責。
實作時不可在 `classify()` 裡讀狀態，那會摧毀其可測性。

### 為什麼異常大單永遠不是 urgent

盤中無法區分開倉與平倉（問題五）。平倉的巨量不代表有人在建立方向性部位，
把它推成緊急警報是純噪音。在取得 OPRA 級即時資料之前，這個限制無法繞過，
因此異常大單的最高級別就是 `watch`。

### 為什麼 Put Wall 穿越不分 regime

**這是風險偏好設定，不是實證發現。** 上下方向的效用不對稱：正 Gamma 下 Call
Wall 突破被壓回，代價是錯過漲幅；正 Gamma 下 Put Wall 跌破未被壓回，代價是
實際虧損。此格採「保護優先」。

不可將此格解讀為「資料顯示 Put Wall 跌破較危險」——目前沒有任何資料支持該
結論。

### 為什麼 Pinning 門檻從 80 降到 70

80 分門檻在 78 筆生產資料中從未觸發（最高 76）。降至 70 回溯會觸發 2 次
（約 2.6%），稀有度合理。僅放進 `watch`：Pinning 描述「價格可能被釘住」的
狀態，本質不急迫。

## 測試策略

依本專案慣例：先用合成資料寫 pytest，再用真實資料跑一次確認合理。

`signal_tiering.py`（純函式）：

- 每一格分級規則各一個測試，含邊界值（比值恰為 3.0、分數恰為 70）
- Put Wall 穿越在正負 Gamma 下都必須是 `urgent`（不對稱規則的迴歸測試）
- 異常大單在任何 `payload` 下都不得回傳 `urgent`
- `gamma_flip` 為 `NULL` 時退回 `total_net_gex` 判斷，且 `reason` 須註明

crossing 偵測：

- `spot_prev` 在牆下、`spot_now` 在牆上 → 觸發
- **`spot_prev` 與 `spot_now` 都在牆上 → 不觸發**（問題二的迴歸測試）
- 沒有 `spot_prev` 時不觸發

路由與冷卻：

- 同種訊號在冷卻期內不重送
- **不同種訊號互不干擾**——問題一的迴歸測試：異常大單履約價改變時，牆位
  突破的冷卻必須仍然生效
- 超出每日預算時 `delivered_tier` 降級但 `classified_tier` 不變

drain：

- 已 drain 的項目不會在下一次摘要重複出現
- drain 失敗（推播例外）時不得標記 `delivered_at`，下次要能重試

## 已知限制

- **分級規則是規則式判斷，不是回測驗證過的最佳解。** 與既有的
  `select_strategy` / `compute_market_maker_pressure_score` 同性質：把判斷邏輯
  攤開透明，而非宣稱最佳。
- **所有門檻是暫定值，且只在單一 regime 上校準過。** 生產資料橫跨
  2026-08-04 ~ 09-09 共五週，該期間 TSLA 上漲 12.4%——是一段**趨勢盤**。
  Pinning 70 這類常數沒有理由在震盪盤或下跌盤同樣適用。累積跨 regime 的資料
  後必須複查。
- **門檻是絕對常數而非分位數。** 「Pinning ≥ 70」會隨標的池與波動環境漂移；
  「落在該標的歷史前 5%」才會自我校準。改為分位數需要先建立每檔標的的歷史
  分布，工作量明顯較大，列為第二階段。
- **`UNUSUAL_ACTIVITY_MIN_VOLUME = 3000` 未做跨標的正規化。** 絕對口數門檻
  套用在 AAPL 與 SPCX 上並不對等（流動性差異數量級）。此常數位於**偵測層**，
  本次範圍不改；記錄為待處理缺陷。
  （註：`ratio`、`pinning_score` 皆為無量綱／已正規化，不受此問題影響。）
- **regime 代理仍非真實 dealer gamma。** `spot vs gamma_flip` 優於隔夜 GEX
  符號，但 `gamma_flip` 本身仍來自前一交易日的計算。盤中不重算整條期權鏈的
  GEX 是既有的架構取捨（成本考量）。
- **每日推播預算的排序是靜態優先序**，不是動態重要性評分。有績效資料後可改
  為依 `signal_auditor` 的超額表現排序。

## 後續（不在本次範圍）

- 門檻改為分位數 + `min_volume` 跨標的正規化
- 依 `signal_auditor` 的超額表現自動升降級訊號
- 績效儀表板（roadmap #5）直接讀 `signal_events`，統計一律用 `classified_tier`
- `gamma_flip_touch` 的距離配對對照組
