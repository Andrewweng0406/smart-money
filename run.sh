#!/bin/bash
# 排程用的完整流程：檢查環境變數 -> 跑單元測試 -> 執行分析並推播到 Telegram。
# 任何一步失敗就中止，不會在測試沒過的情況下還硬跑正式分析。
#
# 不帶參數執行：讀 watchlist.json，跑完清單裡所有標的，推播一份綜合摘要。
# 帶一個標的代號執行（例如 ./run.sh TSLA）：只分析單一標的（原本的行為）。
# 帶 --watch 執行：跑一次盤中即時異常監控就結束（給每15分鐘觸發一次的
# launchd 排程用，見 scripts/com.andrewweng.stockgex-intraday.plist）；
# 這個模式刻意跳過單元測試步驟——盤中每15分鐘都要跑一次，跑一次完整測試
# 套件的開銷（約1~2秒）長期累積起來沒必要，日常排程（上面兩種模式）才需要
# 每次都先驗證過程式邏輯沒壞掉。
set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:-}"

if [ ! -d .venv ]; then
    echo "[run.sh] 找不到 .venv，請先執行：python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt" >&2
    exit 1
fi
source .venv/bin/activate

if [ ! -f .env ]; then
    echo "[run.sh] 找不到 .env，請先複製 .env.example 並填入金鑰（見 SETUP.md）" >&2
    exit 1
fi

# 這幾個變數沒設定不是致命錯誤（程式本身會優雅降級：沒有 Telegram 就不推播、
# 沒有 Claude API key 就不產生 AI 評語），但排程情境下先提醒一聲比較好抓問題。
set -a; source .env; set +a
for var in TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID ANTHROPIC_API_KEY LINE_CHANNEL_ACCESS_TOKEN; do
    if [ -z "${!var:-}" ]; then
        echo "[run.sh] 警告：$var 未設定，對應功能會被跳過" >&2
    fi
done

if [ "$MODE" = "--watch" ]; then
    echo "[run.sh] 執行盤中即時異常監控..."
    python intraday_watcher.py --watchlist watchlist.json --notify
    exit 0
fi

echo "[run.sh] 執行單元測試..."
if ! python -m pytest -q; then
    echo "[run.sh] 單元測試失敗，中止排程" >&2
    exit 1
fi

SYMBOL="$MODE"
if [ -z "$SYMBOL" ]; then
    echo "[run.sh] 未指定標的，讀取 watchlist.json 執行多標的分析..."
    python run_watchlist.py --notify
else
    echo "[run.sh] 開始分析 $SYMBOL..."
    python analyze.py --symbol "$SYMBOL" --notify
fi
