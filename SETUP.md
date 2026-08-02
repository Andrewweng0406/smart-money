# 部署到 Mac Mini：定時執行 + Telegram 推播

## 1. 安裝與初次測試

```bash
cd /Users/andrewweng/Desktop/stock.agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # 含 requirements.txt + pytest

cp .env.example .env
# 編輯 .env，填入 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID（見下方第3節）
# ANTHROPIC_API_KEY 是選填——設定了才會產生 AI 綜合評語，沒設定會自動跳過

./run.sh TSLA
```

`run.sh` 會依序：檢查 `.env` 是否存在、跑一次 `pytest`（確保 Yahoo Finance
資料格式或程式邏輯沒有壞掉）、通過才執行正式分析並推播。確認 `reports/`
目錄下有產生 `daily_report_TSLA_YYYYMMDD.md`、`gex_chart_TSLA_YYYYMMDD.png/html`，
且 Telegram 有收到訊息，再繼續設定排程。

單獨跑測試（不執行正式分析）：
```bash
python -m pytest -v
```

### 多標的 Watchlist 模式

編輯專案根目錄的 `watchlist.json`，填入想追蹤的標的：

```json
{
    "symbols": ["TSLA", "NVDA", "AAPL"]
}
```

不帶標的參數執行 `run.sh` 就會跑 watchlist 模式——依序分析清單裡每一檔
標的（各自的完整報告、圖表、歷史資料庫紀錄照樣都會產生在 `reports/`），
最後彙整成**一份**多標的綜合摘要推播到 Telegram（不是每檔標的各推一次，
避免每天被轟炸好幾則長訊息）：

```bash
./run.sh          # watchlist 模式（讀 watchlist.json）
./run.sh TSLA     # 單一標的模式（原本的行為，不受 watchlist.json 影響）
```

Watchlist 模式裡某一檔標的分析失敗（例如代號打錯、下市）不會中止整個流程，
會在綜合摘要裡列成一行錯誤訊息，其餘標的照常完成；只有「整份清單全部都
失敗」才視為排程整體失敗。

### HTML 儀表板

每次分析完會自動在 `~/Desktop/stock.agent/dashboard/index.html` 產生一份
暗黑風格的互動式儀表板（KPI卡片、GEX圖表、Smart Money風險評級、異常大單、
AI研報），直接用瀏覽器打開即可。Watchlist 模式下每檔標的另外存一份
`dashboard/{symbol}.html`，`index.html` 固定鏡射清單裡第一檔標的。加
`--no-dashboard` 跳過（`analyze.py` 跟 `run_watchlist.py` 都支援）。

### 總經與財報預警日曆

編輯專案根目錄的 `macro_events.json` 維護 FOMC/CPI 等總經事件日期
（**裡面預設是佔位範例日期，需要自行到
[Fed官網](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm)
查最新公布的排程更新**）：

```json
{
    "events": [
        {"name": "FOMC利率決議", "date": "2026-09-16"},
        {"name": "CPI數據公布", "date": "2026-09-11"}
    ]
}
```

財報日期會自動用 yfinance 查詢，不用手動維護。任何事件（財報或總經）倒數
3天以內，報告頂端跟 HTML 儀表板都會自動出現高波動預警。

### 歷史籌碼模型回測

累積幾週的 `history.db` 資料後，可以查詢 Max Pain 偏離度跟 Gamma Flip
支撐/阻力勝率統計：

```bash
python backtester.py --symbol TSLA --notify
```

### 盤中即時異常監控

輕量級單次檢查（不是常駐程式），偵測「現貨突破 Call Wall / 跌破 Put Wall」
跟「近到期日期權出現巨量異常大單（Volume/OI≥3 且 Volume≥3000）」，觸發時
立刻推播（不等每日排程）：

```bash
python intraday_watcher.py --symbol TSLA --notify        # 單一標的
python intraday_watcher.py --watchlist watchlist.json --notify  # 整份清單
python intraday_watcher.py --symbol TSLA --force         # 忽略交易時間限制，手動測試用
./run.sh --watch                                          # 排程用的統一入口
```

只有在美股盤前/盤中時間（週一~週五 04:00~16:00 美東）才會真的執行檢查，
其餘時間直接跳過（不會浪費 API 呼叫）。這支腳本**不含美股假日行事曆**——
感恩節、聖誕節等交易所公休日當天執行只會抓到前一交易日的收盤資料，不會
誤判成異常，頂多浪費一次 API 呼叫，風險可控。

排程設定見下方「選項 C：盤中監控排程」。

## 2. 設定 .env 安全存放 Bot Token / Chat ID

- `.env` 已被 `.gitignore` 排除，不會被 `git add` / `git commit` 追蹤到，
  **前提是這個資料夾之後真的初始化成 git repo 也要保持這條規則**。
- 千萬不要把 Token 直接寫進 `analyze.py` 或 `telegram_notifier.py` 裡 ——
  一旦程式碼被分享、上傳到 GitHub 或截圖，Token 就等於外洩，任何人都能控制
  你的機器人。
- 檔案權限建議收緊，避免同機其他使用者帳號讀到：
  ```bash
  chmod 600 .env
  ```
- 如果 Token 不慎外洩，到 Telegram 跟 `@BotFather` 對話，用 `/revoke`
  重新產生一組新 Token，並更新 `.env`。

## 3. 取得 Telegram Bot Token 與 Chat ID

1. 在 Telegram 搜尋 `@BotFather`，傳送 `/newbot`，依指示取名 -> 取得
   `TELEGRAM_BOT_TOKEN`（格式類似 `123456789:ABC-defGhIJKlmNoPQRstuVwxyZ`）。
2. 用你自己的 Telegram 帳號傳一則訊息給剛建立的機器人（隨便打字即可，機器人
   要先收到過一次訊息，才找得到你的 Chat ID）。
3. 瀏覽器打開（把 `<TOKEN>` 換成你的真實 Token）：
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   在回傳的 JSON 裡找 `"chat":{"id": 123456789, ...}`，那組數字就是
   `TELEGRAM_CHAT_ID`。
4. 把兩個值填進 `.env`。

選填：到 [console.anthropic.com](https://console.anthropic.com/) 登入後在
API Keys 頁面建立一組金鑰，填入 `.env` 的 `ANTHROPIC_API_KEY`，報告就會多一段
Claude 生成的「盤後籌碼與新聞綜合評語」。不設定也完全不影響其餘功能。

選填：LINE 白話文極端警報，用的是 LINE Messaging API：

1. 到 [developers.line.biz/console](https://developers.line.biz/console/)
   建立一個 Provider，底下建立一個 **Messaging API** 頻道。
2. 進頻道設定頁的「Messaging API」分頁，找「Channel access token」按
   發行，把權杖填進 `.env` 的 `LINE_CHANNEL_ACCESS_TOKEN`。
3. 用手機掃該頻道頁面上的 QR Code，把這個官方帳號加為好友——這一步不能
   跳過，程式用的是「廣播（broadcast）」，只會發給好友，沒加好友收不到
   任何訊息。
4. 填完用 `python check_env.py` 檢查格式，再跑一次 `--notify` 實測確認
   真的收得到。

設定後只有在「現貨突破 Call/Put Wall、Net GEX轉負、散戶死亡Loop」這幾種
重大極端事件才會收到白話文警示（比 Telegram 每日報告稀有很多）。

⚠️ Broadcast 會發給這個官方帳號的**所有**好友，個人使用情境下預期只有你
自己，但如果之後加了其他好友，他們也會收到——屆時要改成指定對象的
push，需要額外設定 webhook 才查得到對方的 User ID。

## 4. 排程方式擇一：launchd（推薦）或 crontab

Mac Mini 上蘋果官方建議用 **launchd** 而非 crontab——crontab 在 macOS 上
仍可用，但不保證電腦剛好在排定時間處於睡眠喚醒狀態時能可靠觸發，
launchd 對「電腦剛好在跑排程時間點睡著」的行為處理較好，且是 macOS 現行
的標準機制。

### ⚠️ 先看這個：專案放在 `~/Desktop` 下，launchd/crontab 會被 macOS 擋權限

**這是實測抓到的真實問題，不是理論上的提醒。** macOS（Mojave 之後）對
`~/Desktop`、`~/Documents`、`~/Downloads` 這幾個資料夾有額外的隱私保護
（TCC，Transparency Consent and Control）——不是一般的 Unix 檔案權限
（`chmod`/`chown` 都沒用），而是「這個程式有沒有被系統明確授權讀取這個
資料夾」。你在終端機手動執行 `python analyze.py` 之所以正常，是因為
Terminal.app（或你用的終端機/IDE）早就被你自己（或系統）授權過。但
launchd 或 crontab 啟動的背景程式是「全新的、沒被授權過的行程」，一碰到
`~/Desktop/stock.agent` 底下的檔案就會被擋下來，錯誤長這樣：

```
PermissionError: [Errno 1] Operation not permitted: '.../stock.agent/.venv/pyvenv.cfg'
# 或
shell-init: error retrieving current directory: getcwd: cannot access parent directories: Operation not permitted
```

修復方式二選一：

**方式一：把整個專案搬出 `~/Desktop`（推薦，一勞永逸）**
搬到 `~/stock.agent` 或 `~/Projects/stock.agent` 這種一般家目錄底下的
資料夾——只有 Desktop/Documents/Downloads/iCloud Drive 這幾個特殊資料夾
有這層保護，一般資料夾不受影響。搬移後要記得：
1. 更新三支 `scripts/*.plist` 裡所有寫死 `/Users/andrewweng/Desktop/stock.agent`
   的路徑（`ProgramArguments`、`WorkingDirectory`）
2. 重新 `cp` 到 `~/Library/LaunchAgents/` 並 `launchctl unload` 舊的、
   `load` 新的
3. Desktop 上少了一個資料夾圖示是預期的，不影響 git remote／功能

**方式二：手動幫特定執行檔授予「完整磁碟取用權限」**
系統設定 → 隱私權與安全性 → 完整磁碟取用權限 → 加入：
- `crontab`/launchd 排程走 `/bin/bash`（`com.andrewweng.stockgex.plist`、
  `com.andrewweng.stockgex-intraday.plist` 都是）→ 加入 `/bin/bash`
- 互動機器人走 `.venv/bin/python3`（實際上是個 symlink，指向
  `/Library/Developer/CommandLineTools/usr/bin/python3`）→ 把這個真正的
  Python 執行檔路徑加進去
授權後不用重開機，但要 `launchctl unload` 再 `load` 一次該 plist 才會
生效。這個方式的缺點是「完整磁碟取用權限」授權範圍很廣（那個執行檔對
你整台電腦的檔案都有讀寫權限），安全性不如方式一乾淨。

不管選哪個方式，改完都建議先用 `launchctl start <Label>`（或直接跑
`telegram_bot_listener.py`）手動觸發一次、看 log 確認真的不再出現
`Operation not permitted`，再放著讓它自動排程。

### 選項 A：launchd（推薦）

1. 專案內已附範例 `scripts/com.andrewweng.stockgex.plist`（預設跑 watchlist
   模式，也就是讀 `watchlist.json` 分析清單裡所有標的），複製到 launchd
   會自動掃描的使用者目錄：
   ```bash
   cp scripts/com.andrewweng.stockgex.plist ~/Library/LaunchAgents/
   mkdir -p ~/Library/Logs/stockgex
   ```
   如果只想排程單一標的（不用 watchlist），把 plist 裡 `ProgramArguments`
   陣列最後加一行 `<string>TSLA</string>`（或你想要的代號）即可。
2. **plist 裡的時間是照「主機系統時區」設定，不是台灣時間**——先用
   `date` 指令確認你的 Mac Mini 實際系統時區是什麼（`System Settings →
   General → Date & Time` 也看得到）。專案內附的範例假設主機是美國
   **太平洋時間（Pacific）**，設定 `Hour=13, Minute=30`（週一到週五）：
   美東(ET)收盤 16:00 跟太平洋(PT)全年都固定差3小時（兩地同步隨美國
   夏/冬令時間切換，不像台灣跟美國的時差每年會變），所以 13:30 PT
   永遠對應美東收盤後30分鐘，不需要每年手動調整。
   - 如果你的主機系統時區實際上是**台灣時間**，才需要換算：美國夏令
     時間（約3月中~11月初）美東收盤=台灣時間隔天04:00；冬令時間
     （約11月初~3月中）=台灣時間隔天05:00，且每年這兩個切換日要手動
     改一次 plist 裡的 `Hour`。
   - 不管主機是哪個時區，`analyze.py`/`run_watchlist.py` 內部都會用
     `data_fetcher.is_market_trading_day()` 檢查當天美股是否真的有開盤
     交易（排掉平日休市的假期），不是交易日就不會寫進歷史資料庫——這是
     實測抓到的真bug的第二層防護，但排程時間本身還是建議設對，不要
     依賴這層防護去掩蓋排錯時間的問題。
3. 載入排程：
   ```bash
   launchctl load ~/Library/LaunchAgents/com.andrewweng.stockgex.plist
   ```
4. 驗證是否註冊成功：
   ```bash
   launchctl list | grep stockgex
   ```
5. 想立刻手動觸發一次（不用等到排程時間）：
   ```bash
   launchctl start com.andrewweng.stockgex
   ```
6. 查看執行紀錄／除錯：
   ```bash
   tail -f ~/Library/Logs/stockgex/stockgex.log
   tail -f ~/Library/Logs/stockgex/stockgex.err.log
   ```
7. 停用排程（例如要改設定時先關掉）：
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.andrewweng.stockgex.plist
   ```

### 選項 B：crontab

```bash
crontab -e
```

加入一行（夏令時間台灣 04:30，對應美東收盤後30分鐘；冬令時間要改成
`30 5`）：

```
30 4 * * 2-6 /bin/bash /Users/andrewweng/Desktop/stock.agent/run.sh >> /Users/andrewweng/Library/Logs/stockgex/stockgex.log 2>&1
```

不帶參數執行 `run.sh` 就是 watchlist 模式（讀 `watchlist.json`）；只想排程
單一標的的話在後面加代號，例如 `run.sh TSLA`。

`2-6` 是週二到週六（對應美股週一到週五收盤後、換算成台灣日期通常是隔天），
如果換算後的星期幾对不上，用 `date` 指令實際测一次，依你排定的時間微调。

crontab 使用注意：
- 直接呼叫 `run.sh`（而不是自己組 `python analyze.py ...` 這行）——`run.sh`
  內部會自己 `cd` 到專案目錄、`activate` 虛擬環境、跑完測試才執行正式分析，
  不用在 crontab 這一行重複组這些步骤，也不會漏掉測試這一關。
- 確保 `run.sh` 有執行權限：`chmod +x run.sh`（專案內已經設定過，git clone
  到別台機器時可能需要重新設定一次）。

## 5. 兩種方式怎麼選

- **launchd**：macOS 原生機制，處理「電腦剛好在該時間點睡眠」的情況較可靠，
  且有現成的 log 導向設定，除錯較方便。一般建議選這個。
- **crontab**：設定語法多數人更熟悉，臨時要改一次性排程比較快，但功能上
  是 launchd 的子集。

兩者只需要擇一，不要同時啟用同一支腳本，否則會重複推播兩次。

## 6. 盤中監控排程（選用，額外的背景排程）

⚠️ **這是一個持續全天候觸發的背景排程**（每15分鐘一次，全年無休），跟前面
「每天一次」的每日排程性質不同——請自行評估是否需要，不要無腦跟著每日
排程一起裝上去。

```bash
cp scripts/com.andrewweng.stockgex-intraday.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.andrewweng.stockgex-intraday.plist
launchctl list | grep stockgex-intraday   # 驗證是否註冊成功
```

這支排程用 `StartInterval=900`（每900秒=15分鐘觸發一次），不是像每日排程
那樣用固定時鐘時間——它會全天候每15分鐘觸發一次 `run.sh --watch`，但腳本
內部的 `is_market_hours()` 會在非美股交易時間直接跳過，不會真的打 API。
停用方式跟每日排程一樣：

```bash
launchctl unload ~/Library/LaunchAgents/com.andrewweng.stockgex-intraday.plist
```

log 在 `~/Library/Logs/stockgex/stockgex-intraday.log`（跟 `.err.log`）。

## 7. 互動式 Telegram 機器人（隨時主動查詢，不用等排程）

前面幾節都是「排程自動推播」——這節是另一種用法：讓你隨時在 Telegram
打字（指令或口語都可以）主動問，不用等每天固定時間的報告。這是一個
**常駐服務**（要一直開著監聽），跟前面「排程觸發、跑完就結束」的腳本
性質不同。

### 支援的指令

- `/report <代號>` - 立即分析單一標的（不指定預設 TSLA）
- `/watchlist` - 立即分析整份 `watchlist.json`
- `/backtest <代號>` - 歷史籌碼模型回測統計
- `/scorecard <代號>` - 策略追蹤記分板（見下方說明）
- `/status` - 排程健康檢查（各標的最後一次成功分析是什麼時候）
- `/help` - 顯示說明

也可以直接打口語，例如「幫我看一下特斯拉」「TSLA的策略推薦準不準」
「排程還活著嗎」——機器人會用 Claude 判斷你想做哪一種操作，不需要背
指令格式。看不出意圖時會請你改用上面的精確指令，不會亂猜。

### 部署（launchd，KeepAlive 常駐）

```bash
cp scripts/com.andrewweng.stockgex-bot.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.andrewweng.stockgex-bot.plist
launchctl list | grep stockgex-bot   # 驗證是否註冊成功（PID應該是個數字，不是"-"）
tail -f ~/Library/Logs/stockgex/bot.log      # 確認有印出「開始長輪詢」
tail -f ~/Library/Logs/stockgex/bot.err.log  # 有任何錯誤看這裡
```

跟前面兩支排程用 `StartCalendarInterval`/`StartInterval` 不同，這支用
`KeepAlive=true`——process 只要結束（不管正常退出、被殺掉、還是崩潰）
launchd 就會自動重啟。這是跟 `telegram_bot_listener.py` 內部自己的
supervisor while迴圈互補的**第二層防護**：內部迴圈處理「同一個process裡
發生例外，重建Application再試」，launchd 處理「整個process本身掛掉了」。

⚠️ 這支也一樣會踩到上面「## 4a」提到的 `~/Desktop` 權限問題（症狀是
`bot.err.log` 出現 `PermissionError` 或 `Fatal Python error:
init_import_site`），請先照那節的兩個方式之一處理好，再 load 這支 plist。

停用：
```bash
launchctl unload ~/Library/LaunchAgents/com.andrewweng.stockgex-bot.plist
```

### 策略追蹤記分板：讓「策略建議」變成「可驗證的績效」

`options_strategy_engine.py` 每天都會依 GEX 狀態推薦一個策略（Bull Put
Spread / Bear Call Spread / Iron Condor / Long Strangle），但推薦完就
結束了——`CLAUDE.md` 裡說得很清楚，這是**規則式判斷，不是回測驗證過的
最佳解**。策略追蹤記分板就是把這個「感覺合理」變成「可以驗證的績效」：

1. 每次排程（`run.sh` / `run_watchlist.py`）跑出策略建議，就存一筆進
   `history.db` 的 `strategy_recommendations` 表（`analyze.py` 的
   `save_strategy_recommendation_if_trackable`）。
2. 到期日之後，執行 `strategy_resolver.py` 結算：抓當下現貨價當結算價，
   用 `strategy_tracker.py` 算出這組策略最後是賺是賠，寫回資料庫。
3. 用 `/scorecard <代號>` 隨時查累積勝率/損益。

`strategy_resolver.py` 需要排程執行（建議跟每日排程同一個 `run.sh` 掛在
一起，或另外開一個每日 launchd job）：

```bash
python strategy_resolver.py --symbol TSLA --notify
```

`--notify` 會在有結算到東西時發一則 Telegram 摘要；沒有到期的建議時
安靜跳過，不會每天洗版。用結算日當天的**收盤價**當結算價的近似值，不是
選擇權到期當天官方公告的精確結算價——兩者可能有小落差，`/scorecard` 的
輸出裡有註明這點。
