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

import json
import logging
import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import db_manager
import market_calendar
import run_watchlist
import telegram_notifier

US_EASTERN = ZoneInfo("America/New_York")

# 開盤 09:30 ET，緩衝30分鐘讓開盤初期的價格/成交量雜訊沉澱一些，觸發一次
# 輕量的盤中 GEX+Pinning 摘要（run_watchlist.py --intraday-summary）。
INTRADAY_SUMMARY_HOUR = 10
INTRADAY_SUMMARY_MINUTE = 0

# 每15分鐘觸發一次盤中檢查，等同本機 com.andrewweng.stockgex-intraday.plist
# 的 StartInterval=900；雲端這邊也先用正式期權交易時段擋掉盤前/盤後，
# intraday_watcher.py 內部仍有第二層 gate，避免排程或手動呼叫漏防。
INTRADAY_INTERVAL_MINUTES = 15
LOOP_SLEEP_SECONDS = 30
WATCHLIST_PATH = Path("watchlist.json")
SCHEDULER_STATE_PATH = (
    Path(os.environ["SCHEDULER_STATE_PATH"])
    if os.environ.get("SCHEDULER_STATE_PATH")
    else db_manager.DEFAULT_DB_PATH.with_name("scheduler_state.json")
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cloud_scheduler")


def should_trigger_daily(now_et: datetime, last_run_date: date | None) -> bool:
    """判斷是否到達當日實際收盤後 30 分鐘且尚未執行。"""
    run_at = market_calendar.daily_analysis_time(now_et.date())
    return (
        run_at is not None
        and now_et >= run_at
        and last_run_date != now_et.date()
    )


def should_trigger_intraday_summary(now_et: datetime, last_run_date: date | None) -> bool:
    """判斷現在是否該觸發開盤盤中摘要——跟 should_trigger_daily 同一個
    形狀（平日、時間點吻合、當天還沒跑過），只是時間點跟對應的任務不同。
    """
    return (
        market_calendar.is_market_trading_day(now_et.date())
        and now_et.hour == INTRADAY_SUMMARY_HOUR
        and now_et.minute == INTRADAY_SUMMARY_MINUTE
        and last_run_date != now_et.date()
    )


def should_trigger_intraday(now_et: datetime, last_run_bucket: tuple | None) -> bool:
    """判斷現在是否該觸發盤中檢查——每15分鐘一次，用 (日期, 小時, 第幾個15分
    區間) 當作 bucket 判斷這個區間內是否已經跑過，避免迴圈輪詢間隔（30秒）
    造成同一個15分鐘區間內重複觸發。

    這裡刻意只允許 09:30~16:00 ET：這套 intraday watcher 發的是股票期權
    訊號，盤前現貨雖然會動，但一般股票期權還沒進入正式交易時段。
    """
    if not market_calendar.is_market_hours(now_et):
        return False

    bucket = (now_et.date(), now_et.hour, now_et.minute // INTRADAY_INTERVAL_MINUTES)
    return now_et.minute % INTRADAY_INTERVAL_MINUTES == 0 and last_run_bucket != bucket


def _run_job(args: list[str]) -> bool:
    """執行一次性排程任務（每日分析／盤中監控）。失敗只記錄錯誤，不能讓
    排程迴圈或常駐機器人一起掛掉——呼應專案「加分項優雅降級」的慣例，
    這裡的「加分項」是整個排程機制本身。"""
    logger.info("執行排程任務：%s", " ".join(args))
    try:
        subprocess.run([sys.executable, *args], check=True)
        return True
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.error("排程任務失敗（%s）：%s", " ".join(args), exc)
        return False


def find_missing_daily_snapshots(
    symbols: list[str], trading_date: date,
    db_path: Path | str = db_manager.DEFAULT_DB_PATH,
) -> list[str]:
    """找出當日尚未成功寫入快照的標的。"""
    expected_date = trading_date.isoformat()
    missing = []
    for symbol in symbols:
        rows = db_manager.get_recent_snapshots(symbol, limit=1, db_path=db_path)
        if not rows or rows[0]["date"] != expected_date:
            missing.append(symbol)
    return missing


def _load_scheduler_state(state_path: Path) -> dict:
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_scheduler_state(state: dict, state_path: Path) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state), encoding="utf-8")


def run_daily_analysis(
    trading_date: date,
    db_path: Path | str = db_manager.DEFAULT_DB_PATH,
    state_path: Path = SCHEDULER_STATE_PATH,
    watchlist_path: Path = WATCHLIST_PATH,
) -> bool:
    """補跑每日分析，並以資料庫快照作為真正成功的驗收依據。"""
    symbols = run_watchlist.load_watchlist(watchlist_path)
    missing_before = find_missing_daily_snapshots(symbols, trading_date, db_path=db_path)
    if not missing_before:
        logger.info("%s 每日快照已齊全，不重複執行", trading_date.isoformat())
        return True

    job_succeeded = _run_job(["run_watchlist.py", "--notify"])
    missing_after = find_missing_daily_snapshots(symbols, trading_date, db_path=db_path)
    if job_succeeded and not missing_after:
        return True

    state = _load_scheduler_state(state_path)
    alert_key = trading_date.isoformat()
    if state.get("last_daily_failure_alert") != alert_key:
        missing_text = "、".join(missing_after) if missing_after else "無（程序本身回傳失敗）"
        telegram_notifier.send_text_report(
            "🚨 每日分析完整性告警\n"
            f"交易日：{alert_key}\n"
            f"缺少快照：{missing_text}\n"
            "系統已嘗試補跑，請檢查 Railway 與資料源日誌。"
        )
        state["last_daily_failure_alert"] = alert_key
        _save_scheduler_state(state, state_path)
    return False


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
            run_daily_analysis(now_et.date())

        if should_trigger_intraday_summary(now_et, last_intraday_summary_run_date):
            last_intraday_summary_run_date = now_et.date()
            _run_job(["run_watchlist.py", "--intraday-summary", "--notify"])

        if should_trigger_intraday(now_et, last_intraday_bucket):
            last_intraday_bucket = (now_et.date(), now_et.hour, now_et.minute // INTRADAY_INTERVAL_MINUTES)
            _run_job(["intraday_watcher.py", "--watchlist", "watchlist.json", "--notify"])

        time.sleep(LOOP_SLEEP_SECONDS)


if __name__ == "__main__":
    main()
