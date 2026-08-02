"""strategy_resolver 的結算流程測試——mock 掉 data_fetcher.get_spot_price
（不打真的網路），驗證：抓待結算紀錄 -> 用結算價算損益 -> 寫回資料庫
-> 標記 resolved 這一整條流程串得起來。
"""

from __future__ import annotations

import db_manager
import strategy_resolver


def _save_bull_put_spread(db_path, symbol="TSLA", expiry_date="2026-08-01"):
    # 5塊寬的價差、每股收1.5元權利金，最大虧損=寬度-權利金=3.5（每股/每口單位，
    # 跟 strategy_tracker 的慣例一致，不乘上100股/口數）。
    legs = [
        {"action": "SELL", "option_type": "PUT", "strike_price": 300.0},
        {"action": "BUY", "option_type": "PUT", "strike_price": 295.0},
    ]
    db_manager.save_strategy_recommendation(
        symbol=symbol, recommended_date="2026-07-01", strategy_name="Bull Put Spread",
        strategy_type="credit", legs=legs, net_premium=1.5, max_loss=3.5,
        expiry_date=expiry_date, db_path=db_path,
    )


def test_resolve_pending_marks_win_when_settlement_above_short_strike(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    _save_bull_put_spread(db_path)
    monkeypatch.setattr(strategy_resolver.data_fetcher, "get_spot_price", lambda symbol: 310.0)

    resolved = strategy_resolver.resolve_pending("TSLA", as_of_date="2026-08-01", db_path=db_path)

    assert len(resolved) == 1
    assert resolved[0]["outcome"] == "WIN"
    assert resolved[0]["realized_pnl"] == 1.5
    assert db_manager.get_pending_strategy_recommendations("2026-08-01", db_path=db_path) == []


def test_resolve_pending_marks_loss_when_settlement_below_both_strikes(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    _save_bull_put_spread(db_path)
    monkeypatch.setattr(strategy_resolver.data_fetcher, "get_spot_price", lambda symbol: 280.0)

    resolved = strategy_resolver.resolve_pending("TSLA", as_of_date="2026-08-01", db_path=db_path)

    assert resolved[0]["outcome"] == "LOSS"
    assert resolved[0]["realized_pnl"] == -3.5


def test_resolve_pending_returns_empty_list_when_nothing_pending(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    monkeypatch.setattr(strategy_resolver.data_fetcher, "get_spot_price", lambda symbol: 310.0)

    resolved = strategy_resolver.resolve_pending("TSLA", as_of_date="2026-08-01", db_path=db_path)

    assert resolved == []


def test_resolve_pending_ignores_other_symbols(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    _save_bull_put_spread(db_path, symbol="NVDA")
    monkeypatch.setattr(strategy_resolver.data_fetcher, "get_spot_price", lambda symbol: 310.0)

    resolved = strategy_resolver.resolve_pending("TSLA", as_of_date="2026-08-01", db_path=db_path)

    assert resolved == []


def test_resolve_pending_does_not_write_when_dry_run(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    _save_bull_put_spread(db_path)
    monkeypatch.setattr(strategy_resolver.data_fetcher, "get_spot_price", lambda symbol: 310.0)

    resolved = strategy_resolver.resolve_pending("TSLA", as_of_date="2026-08-01", db_path=db_path, dry_run=True)

    assert len(resolved) == 1
    # dry_run 不該真的寫入資料庫——這筆應該還是 pending 狀態
    assert len(db_manager.get_pending_strategy_recommendations("2026-08-01", db_path=db_path)) == 1


def test_resolve_pending_handles_spot_price_fetch_failure_gracefully(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    _save_bull_put_spread(db_path)

    def _boom(symbol):
        raise RuntimeError("network down")

    monkeypatch.setattr(strategy_resolver.data_fetcher, "get_spot_price", _boom)

    resolved = strategy_resolver.resolve_pending("TSLA", as_of_date="2026-08-01", db_path=db_path)

    assert resolved == []
    # 抓價失敗不該把紀錄誤標成已結算
    assert len(db_manager.get_pending_strategy_recommendations("2026-08-01", db_path=db_path)) == 1


def test_build_resolution_summary_text_includes_outcome_and_pnl():
    resolved = [{
        "symbol": "TSLA", "strategy_name": "Bull Put Spread", "expiry_date": "2026-08-01",
        "outcome": "WIN", "realized_pnl": 150.0, "settlement_spot": 310.0,
    }]
    text = strategy_resolver.build_resolution_summary_text("TSLA", resolved)
    assert "TSLA" in text
    assert "Bull Put Spread" in text
    assert "WIN" in text
    assert "150" in text
