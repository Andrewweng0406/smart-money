"""The UI must use the premium-funded study, not the cash-funded exploratory run."""

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "tsla_strategy/build_data.py"
SPEC = importlib.util.spec_from_file_location("tsla_strategy_build", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_dashboard_uses_realized_premium_funding():
    data = MODULE.build()
    assert data["funding_policy"] == "realized"
    assert set(data["cases"]) == {
        "2019_dip_10", "2019_month_first", "2023_dip_10", "2023_month_first"
    }
    row = data["cases"]["2023_dip_10"]
    assert row["variants"]["pmcc_premium_put"]["account_return_pct"] == -1.73
    assert row["counts"]["risk_signals_without_funded_put"] == 55
    assert row["funding"]["short_credits"] > row["funding"]["short_buybacks"]
    assert data["price"][-1] == {"date": "2026-10-14", "close": 367.16}
    assert {m["id"] for m in data["macro"]["metrics"]} == {
        "dgs10", "curve", "vix", "unrate", "cpi"
    }
    assert all(event["source"].startswith("https://") for event in data["events"])
    assert any(event["name"] == "TSLA Q3 財報" and event["date"] == "2026-10-21"
               for event in data["events"])


def test_group_overview_has_no_browser_local_portfolio_controls():
    html = (ROOT / "tsla_strategy/index.html").read_text(encoding="utf-8")
    script = (ROOT / "tsla_strategy/app.js").read_text(encoding="utf-8")
    assert 'id="decision-grid"' in html
    assert 'id="provenance-grid"' in html
    assert 'id="matrix-body"' in html
    assert 'id="matrix-verdict"' in html
    assert 'id="add-position"' not in html
    assert 'id="edit-goals"' not in html
    assert "localStorage" not in script
    assert "renderStrategyMatrix" in script


def test_public_data_ledger_keeps_source_dates_and_update_log():
    data = MODULE.build()
    html = (ROOT / "tsla_strategy/index.html").read_text(encoding="utf-8")
    assert data["price_source"]["history_downloaded_at"] == "2026-10-13T05:47:39.847634+00:00"
    assert data["price_source"]["provider"] == "Nasdaq historical quotes"
    assert data["price_source"]["url"].startswith("https://")
    assert data["site_updates"][0]["date"] == "2026-10-16"
    assert len(data["site_updates"]) >= 2
    assert 'id="source-ledger-grid"' in html
    assert 'id="update-history-list"' in html
    assert "公開唯讀研究版" in html


def test_capital_sensitivity_keeps_partial_windows_separate():
    data = MODULE.build()
    study = data["capital_sensitivity"]
    assert study["capital"] == 100000
    assert study["reserve_pct"] == .15
    assert len(study["cases"]) == 30
    assert study["cases"]["2019_six_slots_month_first"]["reserve_breach_slot_sessions"] == 72
    assert study["cases"]["2022_two_slots_month_first"]["window"] == {
        "start": "2022-01-03", "end": "2022-05-10"
    }
    assert study["cases"]["2022_two_slots_month_first"]["account_return_pct"] == -29.608
    assert set(data["cases"]) == {
        "2019_dip_10", "2019_month_first", "2023_dip_10", "2023_month_first"
    }


def test_complete_sample_matrix_readout_matches_case_results():
    cases = MODULE.build()["cases"].values()
    assert sum(row["variants"]["pmcc"]["account_return_pct"] >
               row["variants"]["long_call"]["account_return_pct"] for row in cases) == 4
    assert sum(row["variants"]["pmcc_premium_put"]["account_return_pct"] >
               row["variants"]["pmcc"]["account_return_pct"] for row in cases) == 0
    assert sum(row["variants"]["pmcc_premium_put"]["max_daily_nav_drawdown_pct"] <
               row["variants"]["pmcc"]["max_daily_nav_drawdown_pct"] for row in cases) == 0
    assert sum(row["counts"].get("pmcc_premium_put_put_entries", 0) for row in cases) == 30


def test_financial_scenarios_use_h1_anchor_once():
    financial = MODULE.financial_research()
    reported = financial["reported"]
    base = financial["forecast"]["scenarios"]["base"]
    assert reported["liquidity"] == 43.524
    assert reported["fcf"] == .352
    assert base["implied_h2_2026"] == {"ocf": 7.366, "capex": 17.718, "fcf": -10.352}
    assert base["years"]["2026"]["simplified_liquidity"] == 33.172
    assert base["years"]["2028"]["simplified_liquidity"] == 15.172
    assert financial["forecast"]["scenarios"]["bear"]["years"]["2028"]["simplified_liquidity"] == -20.828
    assert all(value is None for value in financial["next_report"]["actuals"].values())
    assert reported["source"].startswith("https://www.sec.gov/")
