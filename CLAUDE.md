# stock.agent

美股期權 GEX（Gamma Exposure）機構級量化戰情室——個人使用的 Mac Mini 排程
工具，每天收盤後自動分析籌碼面（GEX、Max Pain、Wall、Smart Money 指標、
期權策略建議），推播到 Telegram，並累積歷史資料庫供回測。策略建議會
追蹤到期後的實際勝率/損益（策略追蹤記分板），也可以透過常駐的互動式
Telegram 機器人隨時主動查詢（指令或自然語言皆可）。

## 架構總覽

```
資料層：      data_fetcher.py（yfinance）
純計算層：    gex_engine.py（Black-Scholes/GEX）
             options_strategy_engine.py（賣方價差/Iron Condor/買方突圍）
             smart_money.py（IV Skew/PCR/異常大單/莊家壓力分數）
             backtester.py（歷史勝率統計）
             macro_calendar.py（財報/總經事件倒數）
             strategy_tracker.py（策略到期損益計算/勝率彙總）
儲存層：      db_manager.py（SQLite，history.db：每日快照 + 策略追蹤記分板）
輸出層：      analyze.py（Markdown報告）
             dashboard_generator.py（HTML儀表板）
             telegram_notifier.py（Telegram推播）
             line_notifier.py + line_formatter.py（LINE白話文極端警報）
             ai_analyst.py（Claude AI 綜合評語）
編排層：      analyze.py（單一標的）、run_watchlist.py（多標的）、
             intraday_watcher.py（盤中即時監控）、
             strategy_resolver.py（策略建議到期結算）
互動層：      telegram_bot_listener.py（常駐機器人，指令+自然語言查詢）
入口：        run.sh（launchd/cron 排程用）
```

**資料流**：`data_fetcher` 抓 yfinance 原始資料 → 各純計算模組吃原始資料算
出指標（不做I/O，方便單元測試）→ `analyze.py` 的 `fetch_and_aggregate()`
整合成一個 `AnalysisResult` → 輸出層各自把 `AnalysisResult` 轉成
Markdown/HTML/Telegram 訊息。

## 關鍵設計慣例

- **純計算 vs I/O 分離**：`gex_engine.py`、`options_strategy_engine.py`、
  `smart_money.py`、`backtester.py`、`strategy_tracker.py` 都不做網路 I/O，
  只吃參數、回傳結果，方便用合成資料測試，也方便未來換資料源不用動這些檔案。
- **加分項優雅降級**：AI 評語、歷史資料庫寫入、策略建議、Smart Money 指標、
  總經日曆、HTML 儀表板、LINE 極端警報都是「加分項」——計算/寫入失敗只記警告
  （`logger.warning`），絕不能讓核心的 GEX/Max Pain/Wall 計算或 Markdown
  報告連帶失敗。找程式碼裡的 `except Exception as exc:  # noqa: BLE001`
  就能看到這個慣例套用在哪些地方。
- **透明的量化判斷**：策略選擇（`select_strategy`）、回測勝率定義
  （`analyze_gamma_flip_winrate`）、莊家壓力分數（`compute_market_maker_pressure_score`）
  都用清楚的規則式邏輯，並在文件字串（docstring）裡寫明「為什麼這樣定義」
  ——這些不是回測驗證過的最佳解，是「把判斷邏輯攤開透明」的規則型工具。
- **繁體中文註解**：關鍵邏輯（尤其是「為什麼這樣做」而不是「這行在做什麼」）
  一律用繁體中文寫在程式碼裡。
- **測試優先驗證**：每個新模組都先用合成資料寫 pytest，再用真實 yfinance
  資料跑一次確認結果合理，才算完成——這個專案已經靠這個流程抓到過好幾個
  真的 bug（例如 Plotly 把兩個 `$` 符號當數學公式吃掉座標軸文字、Gamma
  翻轉點被遠端雜訊履約價誤導）。

## 常用指令

```bash
source .venv/bin/activate

# 跑全部測試
python -m pytest -q

# 單一標的完整分析（GEX+策略+Smart Money+儀表板+Telegram）
python analyze.py --symbol TSLA --notify

# 多標的 watchlist（讀 watchlist.json）
python run_watchlist.py --notify

# 盤中即時異常監控（單次檢查，不是常駐迴圈）
python intraday_watcher.py --symbol TSLA --force   # --force 忽略交易時間限制，測試用
python analyze.py --symbol TSLA --watch --notify   # 等效寫法

# 歷史籌碼模型回測統計
python backtester.py --symbol TSLA --notify

# 結算到期的策略建議，更新策略追蹤記分板（建議跟每日排程一起排）
python strategy_resolver.py --symbol TSLA --notify

# 互動式 Telegram 機器人（常駐服務，指令 + 自然語言查詢）
python telegram_bot_listener.py

# 檢查 .env 金鑰格式（不會印出完整金鑰）
python check_env.py

# 排程入口（launchd/cron 呼叫這支）
./run.sh              # watchlist 模式
./run.sh TSLA         # 單一標的模式
./run.sh --watch      # 盤中監控模式（給每15分鐘觸發的排程用）
```

## 檔案清單（依模組分類）

| 檔案 | 職責 |
|---|---|
| `gex_engine.py` | Black-Scholes Gamma/Delta、Net GEX、Gamma翻轉點 |
| `data_fetcher.py` | yfinance 存取層（現貨價、期權鏈、bid/ask、到期日查詢） |
| `options_strategy_engine.py` | 賣方價差/Iron Condor/買方突圍，依GEX狀態自動選策略 |
| `smart_money.py` | IV Skew、Put/Call Ratio、異常大單偵測、莊家壓力分數 |
| `db_manager.py` | SQLite 讀寫（history.db：每日快照 + 策略追蹤記分板） |
| `backtester.py` | Max Pain偏離度、Gamma Flip支撐/阻力勝率統計 |
| `macro_calendar.py` | 財報/FOMC/CPI倒數天數警示 |
| `strategy_tracker.py` | 策略到期損益計算（score_outcome）+ 勝率彙總（summarize_track_record） |
| `strategy_resolver.py` | 結算到期的策略建議、寫回記分板（排程用） |
| `dashboard_generator.py` | 暗黑風格 HTML 儀表板產生 |
| `telegram_notifier.py` | Telegram 推播（報告+圖表、純文字、失敗通知） |
| `line_formatter.py` | 判斷「重大極端事件」+ 轉譯成3句白話警示文字（純函式） |
| `line_notifier.py` | 發送 LINE Messaging API 廣播（broadcast，發給官方帳號所有好友） |
| `ai_analyst.py` | Claude API 盤後綜合評語 |
| `analyze.py` | 單一標的完整流程編排（也是其他編排腳本會 import 的共用函式庫） |
| `run_watchlist.py` | 多標的 watchlist 編排 |
| `intraday_watcher.py` | 盤中即時異常監控（單次檢查，排程交給 launchd StartInterval；同一事件有冷卻機制避免重複推播） |
| `telegram_bot_listener.py` | 常駐互動機器人（/report /watchlist /backtest /scorecard /status + 自然語言） |
| `run.sh` | 排程統一入口，launchd/cron 都呼叫這支 |
| `cloud_scheduler.py` | Railway 雲端部署的進入點，取代 launchd 排程判斷（見下方） |
| `watchlist.json` | 使用者維護的標的清單 |
| `macro_events.json` | 使用者維護的 FOMC/CPI 等總經事件日期（**佔位範例，需自行更新**） |

部署與排程設定見 `SETUP.md`。**2026-08 起改跑在 Railway 雲端**（`stock-agent`
專案，單一 service 常駐容器同時跑機器人 + 內部排程迴圈，`history.db` 存在
掛載的 Volume），本機 Mac Mini 的三支 launchd 服務已停用（plist 檔案還在，
需要的話可以重新 `launchctl load`）。細節見 `SETUP.md` 第8節。

## 已知風險

- **`line_notifier.py` 用的是 broadcast，會發給官方帳號的所有好友**，不是
  指定對象的 push。個人使用情境下預期只有使用者自己是好友；如果官方帳號
  之後加了其他好友，broadcast 會發給所有人。要改成指定對象需要額外設定
  webhook 才查得到對方的 User ID，目前沒有做。
- **（已修復，2026-08）專案原本放在 `~/Desktop` 下，launchd/crontab 啟動
  的背景程式會被 macOS 的 TCC（隱私保護）擋下存取權限**（實測抓到的真實
  問題，不是理論推測）——症狀是 `PermissionError: Operation not permitted`
  或 `shell-init: error retrieving current directory`。手動在終端機執行
  一切正常是因為終端機App早就被系統授權過，但 launchd/cron 啟動的是全新
  的、沒授權過的行程。修復方式是把整個專案搬到 `~/stock.agent`（一般
  家目錄底下，不受 TCC 保護），`.venv` 重建（不是直接搬移，venv 腳本會
  寫死絕對路徑）、三支 `scripts/*.plist` 跟 `analyze.py` 的
  `DEFAULT_DASHBOARD_PATH`（原本寫死 `~/Desktop/...`，已改成
  `Path(__file__).parent`，不受專案位置影響）都同步更新過。詳見
  `SETUP.md` 第4節。如果之後又把專案搬回 Desktop/Documents/Downloads
  底下的資料夾，會再踩到同一個問題。
- **策略到期結算價是「到期日當天收盤價」的近似值，不是選擇權到期當天
  交易所公告的官方結算價**（AM/PM settlement）——兩者可能有小落差，
  `/scorecard` 的輸出裡有註明這點，見 `strategy_resolver.py`。
- **一次獨立審查（2026-08）抓出並修復了多個真bug**（策略損益單位不一致、
  Iron Condor最大虧損公式、Gamma Flip改用假設現貨價格網格重算、GEX單位
  少乘0.01、negative_gamma判斷誤用Put Wall突破、IV Skew混入清洗後的0值、
  launchd排程時區/交易日判斷、策略到期結算價、盤中警示去重）——這些都已
  修好並補上回歸測試。同一次審查也建議加入更細緻的訊號品質判斷：
  Delta加權Call成交量（`smart_money.compute_delta_adjusted_call_volume`）
  跟 OI 日增減追蹤（`db_manager` 的 `oi_snapshots` 表 + `detect_unusual_activity`
  的 `likely_opening` 欄位）都已補上；「死亡Loop」警示文字也依使用者確認
  改成啟發式用語（原本是使用者明確指定的斷言式文字）。**bid/ask 成交
  方向判斷沒有做**——yfinance 沒有逐筆成交方向資料，需要換成即時OPRA級
  付費資料源才做得到，屬於架構升級而非小修，先不做。
