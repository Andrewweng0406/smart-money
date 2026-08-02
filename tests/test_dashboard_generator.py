from pathlib import Path

from dashboard_generator import generate_dashboard


def make_data() -> dict:
    return {
        "symbol": "TSLA",
        "spot": 250.0,
        "gex_by_strike": [
            {"strike": 240.0, "net_gex": -1200.0, "call_oi": 100.0, "put_oi": 300.0},
            {"strike": 260.0, "net_gex": 2000.0, "call_oi": 400.0, "put_oi": 100.0},
        ],
        "max_pain": 245.0,
        "call_wall": 270.0,
        "put_wall": 230.0,
        "gamma_flip": 248.0,
        "gamma_flip_distance_pct": 0.8,
        "iv_skew": 0.045,
        "put_call_ratio": {"volume_ratio": 0.55, "oi_ratio": 0.9},
        "unusual_activity": [
            {"strike": 260.0, "side": "call", "volume": 5000.0, "oi": 2000.0, "ratio": 2.5}
        ],
        "mm_pressure": {
            "score": 82,
            "label": "極高",
            "is_death_loop_alert": True,
            "alert_text": "死亡 Loop 警示",
        },
        "ai_commentary": "市場處於負 Gamma 區域。",
        "strategy_name": "保守價差策略",
        "macro_warnings": ["FOMC 即將公布"],
        "alert": "現貨進入大負 GEX 區域",
    }


def test_generate_dashboard_writes_normal_html(tmp_path: Path):
    output = tmp_path / "dashboard.html"

    generate_dashboard(make_data(), output)
    content = output.read_text(encoding="utf-8")

    assert "TSLA GEX Dashboard" in content
    assert "Positive Gamma" in content
    assert "+4.5%" in content
    assert "plotly-2.35.2.min.js" in content
    assert "#2a78d6" in content
    assert "#e34948" in content


def test_generate_dashboard_creates_missing_parent_directories(tmp_path: Path):
    output = tmp_path / "nested" / "reports" / "dashboard.html"

    generate_dashboard(make_data(), output)

    assert output.is_file()


def test_generate_dashboard_handles_empty_gex_and_none_pressure(tmp_path: Path):
    data = make_data()
    data["gex_by_strike"] = []
    data["mm_pressure"] = None
    output = tmp_path / "empty.html"

    generate_dashboard(data, output)
    content = output.read_text(encoding="utf-8")

    assert "無資料" in content
    assert "資料不足" in content
    assert "x: []" in content
    assert "y: []" in content


def test_generate_dashboard_escapes_all_user_facing_special_strings(tmp_path: Path):
    data = make_data()
    data["symbol"] = '<script>alert("symbol")</script>'
    data["ai_commentary"] = '<script>alert("commentary")</script> & analysis'
    data["strategy_name"] = '<script>alert("strategy")</script>'
    data["alert"] = '<script>alert("alert")</script>'
    data["macro_warnings"] = ['<script>alert("macro")</script>']
    data["mm_pressure"]["alert_text"] = '<script>alert("pressure")</script>'
    output = tmp_path / "escaped.html"

    generate_dashboard(data, output)
    content = output.read_text(encoding="utf-8")

    assert "<script>alert(" not in content
    assert "&lt;script&gt;alert(&quot;commentary&quot;)&lt;/script&gt;" in content
    assert "&lt;script&gt;alert(&quot;strategy&quot;)&lt;/script&gt;" in content
    assert "&lt;script&gt;alert(&quot;alert&quot;)&lt;/script&gt;" in content


def test_generate_dashboard_renders_infinite_activity_ratio_as_symbol(tmp_path: Path):
    data = make_data()
    data["unusual_activity"][0]["ratio"] = float("inf")
    output = tmp_path / "infinite.html"

    generate_dashboard(data, output)
    content = output.read_text(encoding="utf-8")

    assert "∞" in content
    assert ">inf<" not in content


def test_generate_dashboard_handles_none_numeric_values(tmp_path: Path):
    data = make_data()
    data["spot"] = None
    data["max_pain"] = None
    data["gamma_flip"] = None
    data["gamma_flip_distance_pct"] = None
    data["iv_skew"] = None
    data["put_call_ratio"] = {"volume_ratio": None, "oi_ratio": None}
    output = tmp_path / "none-values.html"

    generate_dashboard(data, output)

    assert output.read_text(encoding="utf-8").count("N/A") >= 5
