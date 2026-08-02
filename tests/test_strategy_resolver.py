"""strategy_resolver 的結算流程測試——mock 掉 data_fetcher 的報價函式
（不打真的網路），驗證：抓待結算紀錄 -> 用到期日收盤價算損益 -> 寫回
資料庫 -> 標記 resolved 這一整條流程串得起來，以及抓不到到期日收盤價時
會退回用「現在」報價當近似值。
"""

from __future__ import annotations

import json

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


def test_resolve_pending_uses_expiry_date_close_price(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    _save_bull_put_spread(db_path)
    captured = {}

    def fake_get_close_price_on_date(symbol, date_str):
        captured["symbol"], captured["date_str"] = symbol, date_str
        return 310.0

    monkeypatch.setattr(strategy_resolver.data_fetcher, "get_close_price_on_date", fake_get_close_price_on_date)

    resolved = strategy_resolver.resolve_pending("TSLA", as_of_date="2026-08-01", db_path=db_path)

    assert captured == {"symbol": "TSLA", "date_str": "2026-08-01"}  # 用到期日，不是「現在」
    assert len(resolved) == 1
    assert resolved[0]["outcome"] == "WIN"
    assert resolved[0]["realized_pnl"] == 1.5
    assert resolved[0]["approximate"] is False
    assert db_manager.get_pending_strategy_recommendations("2026-08-01", db_path=db_path) == []


def test_resolve_pending_marks_loss_when_settlement_below_both_strikes(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    _save_bull_put_spread(db_path)
    monkeypatch.setattr(strategy_resolver.data_fetcher, "get_close_price_on_date", lambda symbol, date_str: 280.0)

    resolved = strategy_resolver.resolve_pending("TSLA", as_of_date="2026-08-01", db_path=db_path)

    assert resolved[0]["outcome"] == "LOSS"
    assert resolved[0]["realized_pnl"] == -3.5

    track_record = db_manager.get_strategy_track_record("TSLA", db_path=db_path)
    assert track_record[0]["max_loss_hit"] == 1  # 被雙腳穿透，應該存成「觸及最大虧損」


def test_resolve_pending_falls_back_to_current_spot_when_close_price_unavailable(tmp_path, monkeypatch):
    """抓不到到期日當天的歷史收盤價（例如 resolver 漏跑了好幾天、資料源
    缺漏）時，退回用「現在」報價當近似值，並標記 approximate=True。
    """
    db_path = tmp_path / "history.db"
    _save_bull_put_spread(db_path)
    monkeypatch.setattr(strategy_resolver.data_fetcher, "get_close_price_on_date", lambda symbol, date_str: None)
    monkeypatch.setattr(strategy_resolver.data_fetcher, "get_spot_price", lambda symbol: 310.0)

    resolved = strategy_resolver.resolve_pending("TSLA", as_of_date="2026-08-01", db_path=db_path)

    assert len(resolved) == 1
    assert resolved[0]["settlement_spot"] == 310.0
    assert resolved[0]["approximate"] is True


def test_resolve_pending_returns_empty_list_when_nothing_pending(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    monkeypatch.setattr(strategy_resolver.data_fetcher, "get_close_price_on_date", lambda symbol, date_str: 310.0)

    resolved = strategy_resolver.resolve_pending("TSLA", as_of_date="2026-08-01", db_path=db_path)

    assert resolved == []


def test_resolve_pending_ignores_other_symbols(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    _save_bull_put_spread(db_path, symbol="NVDA")
    monkeypatch.setattr(strategy_resolver.data_fetcher, "get_close_price_on_date", lambda symbol, date_str: 310.0)

    resolved = strategy_resolver.resolve_pending("TSLA", as_of_date="2026-08-01", db_path=db_path)

    assert resolved == []


def test_resolve_pending_does_not_write_when_dry_run(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    _save_bull_put_spread(db_path)
    monkeypatch.setattr(strategy_resolver.data_fetcher, "get_close_price_on_date", lambda symbol, date_str: 310.0)

    resolved = strategy_resolver.resolve_pending("TSLA", as_of_date="2026-08-01", db_path=db_path, dry_run=True)

    assert len(resolved) == 1
    # dry_run 不該真的寫入資料庫——這筆應該還是 pending 狀態
    assert len(db_manager.get_pending_strategy_recommendations("2026-08-01", db_path=db_path)) == 1


def test_resolve_pending_handles_price_fetch_failure_gracefully(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    _save_bull_put_spread(db_path)

    def _boom(symbol, date_str):
        raise RuntimeError("network down")

    monkeypatch.setattr(strategy_resolver.data_fetcher, "get_close_price_on_date", _boom)

    resolved = strategy_resolver.resolve_pending("TSLA", as_of_date="2026-08-01", db_path=db_path)

    assert resolved == []
    # 抓價失敗不該把紀錄誤標成已結算
    assert len(db_manager.get_pending_strategy_recommendations("2026-08-01", db_path=db_path)) == 1


def test_build_resolution_summary_text_includes_outcome_and_pnl():
    # realized_pnl 是「每股」單位（1.5），顯示文字要乘上 OPTIONS_MULTIPLIER
    # 變成使用者實際會拿到的美元金額（$150，1口=100股）。
    resolved = [{
        "symbol": "TSLA", "strategy_name": "Bull Put Spread", "expiry_date": "2026-08-01",
        "outcome": "WIN", "realized_pnl": 1.5, "settlement_spot": 310.0, "approximate": False,
    }]
    text = strategy_resolver.build_resolution_summary_text("TSLA", resolved)
    assert "TSLA" in text
    assert "Bull Put Spread" in text
    assert "$150" in text
    assert "WIN" in text


def test_build_resolution_summary_text_notes_approximate_price():
    resolved = [{
        "symbol": "TSLA", "strategy_name": "Bull Put Spread", "expiry_date": "2026-08-01",
        "outcome": "WIN", "realized_pnl": 1.5, "settlement_spot": 310.0, "approximate": True,
    }]
    text = strategy_resolver.build_resolution_summary_text("TSLA", resolved)
    assert "近似值" in text


# ---------- resolve_watchlist / build_multi_symbol_summary_text ----------

def test_resolve_watchlist_iterates_all_symbols(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    _save_bull_put_spread(db_path, symbol="TSLA")
    _save_bull_put_spread(db_path, symbol="NVDA")
    watchlist_path = tmp_path / "watchlist.json"
    watchlist_path.write_text(json.dumps({"symbols": ["TSLA", "NVDA"]}), encoding="utf-8")

    monkeypatch.setattr(strategy_resolver.data_fetcher, "get_close_price_on_date", lambda symbol, date_str: 310.0)

    resolved = strategy_resolver.resolve_watchlist(watchlist_path, as_of_date="2026-08-01", db_path=db_path)

    assert {item["symbol"] for item in resolved} == {"TSLA", "NVDA"}


def test_resolve_watchlist_continues_when_one_symbol_fails(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    _save_bull_put_spread(db_path, symbol="TSLA")
    _save_bull_put_spread(db_path, symbol="NVDA")
    watchlist_path = tmp_path / "watchlist.json"
    watchlist_path.write_text(json.dumps({"symbols": ["TSLA", "NVDA"]}), encoding="utf-8")

    def flaky_get_close_price(symbol, date_str):
        if symbol == "TSLA":
            raise RuntimeError("network down")
        return 310.0

    monkeypatch.setattr(strategy_resolver.data_fetcher, "get_close_price_on_date", flaky_get_close_price)

    resolved = strategy_resolver.resolve_watchlist(watchlist_path, as_of_date="2026-08-01", db_path=db_path)

    assert len(resolved) == 1
    assert resolved[0]["symbol"] == "NVDA"


def test_build_multi_symbol_summary_text_includes_all_symbols():
    resolved = [
        {"symbol": "TSLA", "strategy_name": "Bull Put Spread", "expiry_date": "2026-08-01",
         "outcome": "WIN", "realized_pnl": 1.5, "settlement_spot": 310.0, "approximate": False},
        {"symbol": "NVDA", "strategy_name": "Iron Condor", "expiry_date": "2026-08-01",
         "outcome": "LOSS", "realized_pnl": -2.0, "settlement_spot": 120.0, "approximate": True},
    ]
    text = strategy_resolver.build_multi_symbol_summary_text(resolved)
    assert "TSLA" in text
    assert "NVDA" in text
    assert "Iron Condor" in text
