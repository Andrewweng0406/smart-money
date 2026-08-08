"""backtester.py 的測試——用 db_manager.save_snapshot 把合成資料寫進
tmp_path 的獨立 sqlite 檔案，不會碰到專案真正的 history.db，也不需要網路。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest

import backtester
import db_manager


@dataclass
class _FakeResult:
    symbol: str
    spot: float
    max_pain: float
    call_wall: float = 0.0
    put_wall: float = 0.0
    gamma_flip: float | None = None
    gamma_flip_distance_pct: float | None = None
    zero_dte_summary: dict = None
    alert: str | None = None
    pinning: dict | None = None

    def __post_init__(self):
        if self.zero_dte_summary is None:
            self.zero_dte_summary = {"total_net_gex": 0, "zero_dte_net_gex": 0, "ex_zero_dte_net_gex": 0, "zero_dte_share_pct": 0.0}


def _save(db_path, symbol, date_str, spot, max_pain=100.0, gamma_flip=None):
    db_manager.save_snapshot(_FakeResult(symbol=symbol, spot=spot, max_pain=max_pain, gamma_flip=gamma_flip), date_str, db_path=db_path)


# ---------- Max Pain deviation ----------

def test_max_pain_deviation_no_data_returns_zero_sample(tmp_path):
    db_path = tmp_path / "history.db"
    result = backtester.analyze_max_pain_deviation("TSLA", db_path=db_path)
    assert result["sample_size"] == 0
    assert result["mean_deviation_pct"] is None


def test_max_pain_deviation_computes_correct_percentage(tmp_path):
    db_path = tmp_path / "history.db"
    monday = date.fromisocalendar(2026, 32, 1).isoformat()
    friday = date.fromisocalendar(2026, 32, 5).isoformat()

    _save(db_path, "TSLA", monday, spot=300.0, max_pain=310.0)
    _save(db_path, "TSLA", friday, spot=300.0, max_pain=310.0)  # spot 這天代表當週五收盤價

    result = backtester.analyze_max_pain_deviation("TSLA", db_path=db_path)

    assert result["sample_size"] == 1
    # (300 - 310) / 310 * 100
    assert result["mean_deviation_pct"] == pytest.approx(-3.2258, 0.01)


def test_max_pain_deviation_prefers_wednesday_over_monday(tmp_path):
    db_path = tmp_path / "history.db"
    monday = date.fromisocalendar(2026, 32, 1).isoformat()
    wednesday = date.fromisocalendar(2026, 32, 3).isoformat()
    friday = date.fromisocalendar(2026, 32, 5).isoformat()

    _save(db_path, "TSLA", monday, spot=200.0, max_pain=200.0)   # 不該被用到
    _save(db_path, "TSLA", wednesday, spot=250.0, max_pain=250.0)  # 應該用這筆
    _save(db_path, "TSLA", friday, spot=250.0, max_pain=999.0)   # max_pain 欄位這天不重要，只用spot

    result = backtester.analyze_max_pain_deviation("TSLA", db_path=db_path)

    assert result["sample_size"] == 1
    assert result["mean_deviation_pct"] == pytest.approx(0.0, 0.01)  # 250 vs 250，偏離0%


def test_max_pain_deviation_skips_incomplete_weeks(tmp_path):
    db_path = tmp_path / "history.db"
    monday = date.fromisocalendar(2026, 32, 1).isoformat()
    _save(db_path, "TSLA", monday, spot=300.0, max_pain=310.0)  # 這週沒有週五資料

    result = backtester.analyze_max_pain_deviation("TSLA", db_path=db_path)
    assert result["sample_size"] == 0


# ---------- Gamma Flip win-rate ----------

def test_gamma_flip_winrate_no_touch_events(tmp_path):
    db_path = tmp_path / "history.db"
    for i, spot in enumerate([100.0, 105.0, 110.0]):
        _save(db_path, "TSLA", f"2026-08-{i+1:02d}", spot=spot, gamma_flip=200.0)  # 現貨離翻轉點很遠

    result = backtester.analyze_gamma_flip_winrate("TSLA", db_path=db_path, horizons=(1,))
    assert result["horizons"][1]["sample_size"] == 0


def test_gamma_flip_winrate_records_win_when_level_holds(tmp_path):
    db_path = tmp_path / "history.db"
    # 第0天觸及翻轉點（現貨在翻轉點之下，距離在門檻內），接下來3天都守在下方（win）
    _save(db_path, "TSLA", "2026-08-01", spot=99.0, gamma_flip=100.0)
    _save(db_path, "TSLA", "2026-08-02", spot=97.0, gamma_flip=100.0)
    _save(db_path, "TSLA", "2026-08-03", spot=95.0, gamma_flip=100.0)
    _save(db_path, "TSLA", "2026-08-04", spot=94.0, gamma_flip=100.0)

    result = backtester.analyze_gamma_flip_winrate("TSLA", db_path=db_path, horizons=(1, 3))

    assert result["horizons"][1]["sample_size"] == 1
    assert result["horizons"][1]["win_rate_pct"] == 100.0
    assert result["horizons"][3]["sample_size"] == 1
    assert result["horizons"][3]["win_rate_pct"] == 100.0


def test_gamma_flip_winrate_records_loss_when_level_breaks(tmp_path):
    db_path = tmp_path / "history.db"
    # 第0天觸及翻轉點（現貨在翻轉點之上），隔天直接跌破（loss）
    _save(db_path, "TSLA", "2026-08-01", spot=101.0, gamma_flip=100.0)
    _save(db_path, "TSLA", "2026-08-02", spot=90.0, gamma_flip=100.0)

    result = backtester.analyze_gamma_flip_winrate("TSLA", db_path=db_path, horizons=(1,))

    assert result["horizons"][1]["sample_size"] == 1
    assert result["horizons"][1]["win_rate_pct"] == 0.0
    assert result["horizons"][1]["avg_return_pct_above"] is not None
    assert result["horizons"][1]["avg_return_pct_below"] is None


def test_gamma_flip_winrate_skips_horizon_without_enough_future_data(tmp_path):
    db_path = tmp_path / "history.db"
    # 只有觸及當天的資料，沒有任何未來資料可以算 horizon
    _save(db_path, "TSLA", "2026-08-01", spot=99.0, gamma_flip=100.0)

    result = backtester.analyze_gamma_flip_winrate("TSLA", db_path=db_path, horizons=(1, 5))
    assert result["horizons"][1]["sample_size"] == 0
    assert result["horizons"][5]["sample_size"] == 0


def test_gamma_flip_winrate_no_data_returns_empty_horizons(tmp_path):
    db_path = tmp_path / "history.db"
    result = backtester.analyze_gamma_flip_winrate("TSLA", db_path=db_path)
    assert result["horizons"] == {}


# ---------- Report text ----------

def test_generate_backtest_report_handles_empty_database(tmp_path):
    db_path = tmp_path / "history.db"
    report = backtester.generate_backtest_report("TSLA", db_path=db_path)
    assert "樣本數不足" in report
    assert "TSLA" in report


def test_generate_backtest_report_includes_stats_when_data_available(tmp_path):
    db_path = tmp_path / "history.db"
    monday = date.fromisocalendar(2026, 32, 1).isoformat()
    friday = date.fromisocalendar(2026, 32, 5).isoformat()
    _save(db_path, "TSLA", monday, spot=300.0, max_pain=310.0)
    _save(db_path, "TSLA", friday, spot=305.0, max_pain=310.0)

    report = backtester.generate_backtest_report("TSLA", db_path=db_path)
    assert "平均偏離" in report
    assert "免責聲明" in report
