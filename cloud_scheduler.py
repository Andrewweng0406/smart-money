"""Railway 雲端部署用的排程進入點——本機 Mac Mini 是用 launchd 的三支
plist（`scripts/com.andrewweng.stockgex*.plist`）分別排每日收盤分析、每15
分鐘盤中監控、常駐機器人這三件事；Railway 沒有 launchd，而且同一個 repo
如果拆成三個獨立 service，Volume 沒辦法跨 service 共用（每個 service 只能
掛自己的 Volume），機器人查 `/scorecard` 會讀不到排程寫入 history.db 的資料
——所以改成單一 service、單一 process 常駐執行，內部用一個迴圈模擬 launchd
的排程時機，三件事共用同一個容器檔案系統（也就是同一個掛載的 Volume）。

時區判斷特地用 zoneinfo 換算「美東時間現在幾點」，而不是把 launchd plist
裡「PT 13:30 = 收盤後30分鐘」的邏輯直接轉譯成寫死的 UTC 時間——PT/ET
全年固定差3小時沒錯，但 UTC 跟兩者的時差都會隨美國夏令/冬令時間切換，
寫死 UTC 會在每年兩次日光節約切換時多跑或漏跑一次，用 zoneinfo 讓系統自己
處理 DST 才是真正不受季節影響的寫法。
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

US_EASTERN = ZoneInfo("America/New_York")

# 收盤 16:00 ET，緩衝30分鐘讓 yfinance 資料更新穩定，等同本機
# com.andrewweng.stockgex.plist 裡的 13:30 PT（PT/ET 固定差3小時）。
DAILY_RUN_HOUR = 16
DAILY_RUN_MINUTE = 30

# 開盤 09:30 ET，緩衝30分鐘讓開盤初期的價格/成交量雜訊沉澱一些，觸發一次
# 輕量的盤中 GEX+Pinning 摘要（run_watchlist.py --intraday-summary）。
INTRADAY_SUMMARY_HOUR = 10
INTRADAY_SUMMARY_MINUTE = 0

# 每15分鐘觸發一次盤中檢查，等同本機 com.andrewweng.stockgex-intraday.plist
# 的 StartInterval=900；是否真的要做檢查交給 intraday_watcher.py 內部的
# is_market_hours() 判斷（盤外時間會直接跳過，不會打任何 API），這裡只負責
# 「時間到了就觸發」。
INTRADAY_INTERVAL_MINUTES = 15

LOOP_SLEEP_SECONDS = 30

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cloud_scheduler")


def should_trigger_daily(now_et: datetime, last_run_date: date | None) -> bool:
    """判斷現在是否該觸發每日收盤分析——平日、時間點吻合、當天還沒跑過。"""
    return (
        now_et.weekday() < 5
        and now_et.hour == DAILY_RUN_HOUR
        and now_et.minute == DAILY_RUN_MINUTE
        and last_run_date != now_et.date()
    )


def should_trigger_intraday_summary(now_et: datetime, last_run_date: date | None) -> bool:
    """判斷現在是否該觸發開盤盤中摘要——跟 should_trigger_daily 同一個
    形狀（平日、時間點吻合、當天還沒跑過），只是時間點跟對應的任務不同。
    """
    return (
        now_et.weekday() < 5
        and now_et.hour == INTRADAY_SUMMARY_HOUR
        and now_et.minute == INTRADAY_SUMMARY_MINUTE
        and last_run_date != now_et.date()
    )


def should_trigger_intraday(now_et: datetime, last_run_bucket: tuple | None) -> bool:
    """判斷現在是否該觸發盤中檢查——每15分鐘一次，用 (日期, 小時, 第幾個15分
    區間) 當作 bucket 判斷這個區間內是否已經跑過，避免迴圈輪詢間隔（30秒）
    造成同一個15分鐘區間內重複觸發。"""
    bucket = (now_et.date(), now_et.hour, now_et.minute // INTRADAY_INTERVAL_MINUTES)
    return now_et.minute % INTRADAY_INTERVAL_MINUTES == 0 and last_run_bucket != bucket


def _run_job(args: list[str]) -> None:
    """執行一次性排程任務（每日分析／盤中監控）。失敗只記錄錯誤，不能讓
    排程迴圈或常駐機器人一起掛掉——呼應專案「加分項優雅降級」的慣例，
    這裡的「加分項」是整個排程機制本身。"""
    logger.info("執行排程任務：%s", " ".join(args))
    try:
        subprocess.run([sys.executable, *args], check=True)
    except subprocess.CalledProcessError as exc:
        logger.error("排程任務失敗（%s）：%s", " ".join(args), exc)


def _start_bot() -> subprocess.Popen:
    logger.info("啟動常駐互動機器人 telegram_bot_listener.py")
    return subprocess.Popen([sys.executable, "telegram_bot_listener.py"])


def main() -> None:
    logger.info("執行部署前單元測試...")
    test_result = subprocess.run([sys.executable, "-m", "pytest", "-q"])
    if test_result.returncode != 0:
        logger.error("單元測試失敗，中止啟動")
        sys.exit(1)

    bot_process = _start_bot()
    last_daily_run_date: date | None = None
    last_intraday_summary_run_date: date | None = None
    last_intraday_bucket: tuple | None = None

    while True:
        if bot_process.poll() is not None:
            logger.warning("機器人程序意外結束（exit code %s），重新啟動", bot_process.returncode)
            bot_process = _start_bot()

        now_et = datetime.now(US_EASTERN)

        if should_trigger_daily(now_et, last_daily_run_date):
            last_daily_run_date = now_et.date()
            _run_job(["run_watchlist.py", "--notify"])

        if should_trigger_intraday_summary(now_et, last_intraday_summary_run_date):
            last_intraday_summary_run_date = now_et.date()
            _run_job(["run_watchlist.py", "--intraday-summary", "--notify"])

        if should_trigger_intraday(now_et, last_intraday_bucket):
            last_intraday_bucket = (now_et.date(), now_et.hour, now_et.minute // INTRADAY_INTERVAL_MINUTES)
            _run_job(["intraday_watcher.py", "--watchlist", "watchlist.json", "--notify"])

        time.sleep(LOOP_SLEEP_SECONDS)


if __name__ == "__main__":
    main()
