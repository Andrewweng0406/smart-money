"""產生自包含的暗黑風格 GEX 分析 Dashboard。"""

from __future__ import annotations

import html
import json
import math
from pathlib import Path
from typing import Any


def _escape(value: Any) -> str:
    """將資料欄位安全地放入 HTML 文字節點。"""
    return html.escape(str(value), quote=True)


def _format_number(value: float | int | None, decimals: int = 2) -> str:
    if value is None:
        return "N/A"
    return f"{value:,.{decimals}f}"


def _format_ratio(value: float | None) -> str:
    if value is None:
        return "N/A"
    if math.isinf(value):
        return "∞"
    return f"{value:.2f}"


def _format_signed_percent(value: float | None, *, scale: float = 1.0) -> str:
    if value is None:
        return "N/A"
    return f"{value * scale:+.1f}%"


def generate_dashboard(data: dict, output_path: Path) -> None:
    """把分析資料寫成單一、自包含（Plotly CDN 除外）的 HTML 檔案。"""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    gex_rows = data["gex_by_strike"] or []
    unusual_rows = data["unusual_activity"] or []
    macro_warnings = data["macro_warnings"] or []
    put_call_ratio = data["put_call_ratio"] or {}
    mm_pressure = data["mm_pressure"]
    pinning = data["pinning"]

    # 莊家做盤視角：總 GEX 正負決定對沖偏向抑制波動或順勢放大波動。
    total_gex = sum((row["net_gex"] or 0) for row in gex_rows)
    gamma_is_positive = total_gex >= 0
    gamma_status = "Positive Gamma" if gamma_is_positive else "Negative Gamma"
    gamma_class = "positive" if gamma_is_positive else "negative"

    strikes = [row["strike"] for row in gex_rows]
    net_gex_values = [row["net_gex"] for row in gex_rows]
    bar_colors = [
        "#2a78d6" if value is None or value >= 0 else "#e34948"
        for value in net_gex_values
    ]

    # json.dumps 只承載數字陣列，避免以 Python repr 產生無效 JavaScript。
    chart_x = json.dumps(strikes)
    chart_y = json.dumps(net_gex_values)
    chart_colors = json.dumps(bar_colors)
    spot_value = data["spot"]
    spot_shape = "[]"
    if spot_value is not None:
        spot_shape = json.dumps(
            [
                {
                    "type": "line",
                    "x0": spot_value,
                    "x1": spot_value,
                    "y0": 0,
                    "y1": 1,
                    "yref": "paper",
                    "line": {"color": "#f2c94c", "width": 2, "dash": "dash"},
                }
            ]
        )

    empty_chart_notice = (
        '<div class="empty-notice">無資料</div>' if not gex_rows else ""
    )

    alert_block = ""
    if data["alert"] is not None:
        alert_block = f'<div class="page-alert">{_escape(data["alert"])}</div>'

    # Regime -> (顯示文字, CSS class)。PINNING 用正面色（磁吸抑制波動，
    # 跟 .positive 同一套語意）；BREAKOUT 用負面色（防線失守，跟現有的
    # page-alert/negative 警示色一致）；NEUTRAL 用中性色，不誇大訊號強度。
    _PINNING_REGIME_DISPLAY = {
        "PINNING": ("🧲 Pinning · 磁吸區間", "positive"),
        "BREAKOUT": ("🚀 Breakout · 突破區間", "negative"),
        "NEUTRAL": ("🔄 Neutral · 中性觀望", "muted"),
    }

    if pinning is None:
        pinning_kpi_value = '<span class="muted">N/A</span>'
        pinning_block = ""
    else:
        regime_text, regime_class = _PINNING_REGIME_DISPLAY.get(
            pinning["regime"], (pinning["regime"], "muted")
        )
        pinning_kpi_value = f'<span class="{regime_class}">{_escape(regime_text)}</span>'
        pin_match_note = (
            "與 Max Pain 重合" if pinning["pin_strike_matches_max_pain"] else "與 Max Pain 不同"
        )
        pinning_block = f"""
        <section class="panel pinning-panel">
          <h2>Pinning 釘價效應判斷</h2>
          <div class="pinning-regime {regime_class}">{_escape(regime_text)}</div>
          <div class="pinning-grid">
            <div class="ratio"><span>Pin Strike</span>${_escape(_format_number(pinning["pin_strike"], 0))}（{_escape(pin_match_note)}）</div>
            <div class="ratio"><span>距離 Pin Strike</span>{_escape(_format_number(pinning["distance_pct"], 1))}%</div>
            <div class="ratio"><span>未平倉量集中度</span>{_escape(_format_number(pinning["oi_concentration_pct"], 1))}%</div>
            <div class="ratio"><span>Pinning 分數</span>{_escape(_format_number(pinning["score"], 0))} / 100（{_escape(pinning["label"])}）</div>
          </div>
        </section>
        """

    warning_block = ""
    if macro_warnings:
        warning_items = "".join(f"<li>{_escape(item)}</li>" for item in macro_warnings)
        warning_block = f"""
        <section class="panel macro-panel">
          <h2>總經事件警示</h2>
          <ul>{warning_items}</ul>
        </section>
        """

    commentary_block = ""
    if data["ai_commentary"] is not None:
        strategy = ""
        if data["strategy_name"] is not None:
            strategy = (
                '<div class="strategy">策略：'
                f'{_escape(data["strategy_name"])}</div>'
            )
        commentary_block = f"""
        <section class="panel commentary-panel">
          <h2>AI 研報</h2>
          {strategy}
          <div class="commentary">{_escape(data["ai_commentary"])}</div>
        </section>
        """

    activity_items = []
    for item in unusual_rows[:5]:
        side = _escape(item["side"])
        side_class = "call" if item["side"] == "call" else "put"
        activity_items.append(
            "<tr>"
            f'<td>{_escape(_format_number(item["strike"]))}</td>'
            f'<td><span class="side {side_class}">{side}</span></td>'
            f'<td>{_escape(_format_number(item["volume"], 0))}</td>'
            f'<td>{_escape(_format_number(item["oi"], 0))}</td>'
            f'<td>{_escape(_format_ratio(item["ratio"]))}</td>'
            "</tr>"
        )
    activity_body = "".join(activity_items)
    if not activity_body:
        activity_body = '<tr><td colspan="5" class="muted">無異常活動</td></tr>'

    if mm_pressure is None:
        pressure_content = '<div class="insufficient">資料不足</div>'
        pressure_class = ""
    else:
        pressure_class = " danger" if mm_pressure["is_death_loop_alert"] else ""
        pressure_alert = ""
        if mm_pressure["is_death_loop_alert"] and mm_pressure["alert_text"] is not None:
            pressure_alert = (
                '<div class="death-loop-alert">'
                f'{_escape(mm_pressure["alert_text"])}</div>'
            )
        pressure_content = f"""
          <div class="pressure-score">{_escape(_format_number(mm_pressure["score"], 0))}</div>
          <div class="pressure-label">{_escape(mm_pressure["label"])}</div>
          {pressure_alert}
        """

    html_document = f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_escape(data["symbol"])} GEX Dashboard</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    :root {{ color-scheme: dark; --bg: #0d0d0d; --panel: #18191b; --line: #303236; --text: #eceff4; --muted: #969ca6; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--text); font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif; }}
    main {{ width: min(1440px, 94vw); margin: 0 auto; padding: 30px 0 50px; }}
    h1 {{ margin: 0 0 22px; font-size: clamp(1.6rem, 3vw, 2.4rem); }}
    h2 {{ margin: 0 0 18px; font-size: 1.05rem; }}
    .kpi-grid {{ display: grid; grid-template-columns: repeat(6, minmax(145px, 1fr)); gap: 12px; }}
    .kpi, .panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 12px; }}
    .kpi {{ padding: 16px; }}
    .kpi-label {{ color: var(--muted); font-size: .78rem; text-transform: uppercase; letter-spacing: .08em; }}
    .kpi-value {{ margin-top: 9px; font-size: 1.25rem; font-weight: 700; }}
    .positive {{ color: #0ca30c; }} .negative {{ color: #d03b3b; }}
    .page-alert, .death-loop-alert {{ color: #ffdddd; background: #3a1518; border: 1px solid #d03b3b; border-radius: 9px; padding: 13px 15px; }}
    .page-alert {{ margin-top: 14px; }}
    .layout {{ display: grid; grid-template-columns: minmax(0, 2fr) minmax(310px, 1fr); gap: 16px; margin-top: 16px; }}
    .panel {{ padding: 20px; margin-top: 16px; }}
    .layout .panel {{ margin-top: 0; }}
    .chart-wrap {{ position: relative; min-height: 430px; }}
    #gex-chart {{ width: 100%; height: 430px; }}
    .empty-notice {{ position: absolute; inset: 50% auto auto 50%; transform: translate(-50%, -50%); color: var(--muted); z-index: 2; }}
    .pressure-card.danger {{ border-color: #d03b3b; box-shadow: 0 0 0 1px #d03b3b, 0 0 24px rgba(208,59,59,.18); }}
    .pressure-score {{ font-size: 3.2rem; font-weight: 800; line-height: 1; }}
    .pressure-label {{ color: #f2c94c; margin: 8px 0 16px; font-weight: 700; }}
    .insufficient, .muted {{ color: var(--muted); }}
    .ratios {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 20px 0; }}
    .ratio {{ background: #111214; border-radius: 8px; padding: 12px; }}
    .ratio span {{ display: block; color: var(--muted); font-size: .75rem; margin-bottom: 6px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: .85rem; }}
    th, td {{ padding: 9px 5px; border-bottom: 1px solid #292b2f; text-align: right; }}
    th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) {{ text-align: left; }}
    .side {{ text-transform: uppercase; font-weight: 700; }} .side.call {{ color: #2a78d6; }} .side.put {{ color: #e34948; }}
    .macro-panel {{ border-color: #725f22; }}
    .macro-panel li {{ margin: 8px 0; }}
    .strategy {{ color: #79aef2; font-weight: 700; margin-bottom: 14px; }}
    .commentary {{ color: #d5d8de; line-height: 1.75; white-space: pre-wrap; }}
    .pinning-panel {{ margin-top: 14px; }}
    .pinning-regime {{ font-size: 1.4rem; font-weight: 800; margin-bottom: 16px; }}
    .pinning-grid {{ display: grid; grid-template-columns: repeat(4, minmax(140px, 1fr)); gap: 10px; }}
    @media (max-width: 900px) {{ .kpi-grid {{ grid-template-columns: repeat(2, 1fr); }} .layout {{ grid-template-columns: 1fr; }} .pinning-grid {{ grid-template-columns: 1fr 1fr; }} }}
    @media (max-width: 520px) {{ .kpi-grid {{ grid-template-columns: 1fr; }} main {{ width: 92vw; }} }}
  </style>
</head>
<body>
  <main>
    <h1>{_escape(data["symbol"])} GEX Dashboard</h1>
    <section class="kpi-grid">
      <div class="kpi"><div class="kpi-label">現貨價</div><div class="kpi-value">{_escape(_format_number(data["spot"]))}</div></div>
      <div class="kpi"><div class="kpi-label">Net GEX 狀態</div><div class="kpi-value {gamma_class}">{gamma_status}</div></div>
      <div class="kpi"><div class="kpi-label">Max Pain</div><div class="kpi-value">{_escape(_format_number(data["max_pain"]))}</div></div>
      <div class="kpi"><div class="kpi-label">Gamma Flip 距離</div><div class="kpi-value">{_escape(_format_signed_percent(data["gamma_flip_distance_pct"]))}</div></div>
      <div class="kpi"><div class="kpi-label">IV Skew</div><div class="kpi-value">{_escape(_format_signed_percent(data["iv_skew"], scale=100))}</div></div>
      <div class="kpi"><div class="kpi-label">Pinning 狀態</div><div class="kpi-value">{pinning_kpi_value}</div></div>
    </section>
    {alert_block}
    {pinning_block}

    <div class="layout">
      <section class="panel">
        <h2>Net GEX by Strike</h2>
        <div class="chart-wrap"><div id="gex-chart"></div>{empty_chart_notice}</div>
      </section>
      <section class="panel pressure-card{pressure_class}">
        <h2>Smart Money 風險評級</h2>
        {pressure_content}
        <div class="ratios">
          <div class="ratio"><span>Put/Call Volume</span>{_escape(_format_ratio(put_call_ratio.get("volume_ratio")))}</div>
          <div class="ratio"><span>Put/Call OI</span>{_escape(_format_ratio(put_call_ratio.get("oi_ratio")))}</div>
        </div>
        <h2>異常期權活動</h2>
        <table><thead><tr><th>Strike</th><th>Side</th><th>Volume</th><th>OI</th><th>Ratio</th></tr></thead><tbody>{activity_body}</tbody></table>
      </section>
    </div>
    {warning_block}
    {commentary_block}
  </main>
  <script>
    const trace = {{
      type: "bar",
      x: {chart_x},
      y: {chart_y},
      marker: {{ color: {chart_colors} }},
      hovertemplate: "Strike: %{{x}}<br>Net GEX: %{{y:,.2f}}<extra></extra>"
    }};
    const layout = {{
      paper_bgcolor: "#18191b", plot_bgcolor: "#18191b",
      font: {{ color: "#cfd3da" }}, margin: {{ l: 70, r: 24, t: 20, b: 60 }},
      xaxis: {{ title: "Strike", gridcolor: "#292b2f" }},
      yaxis: {{ title: "Net GEX", gridcolor: "#292b2f", zerolinecolor: "#69707a" }},
      shapes: {spot_shape}, showlegend: false
    }};
    Plotly.newPlot("gex-chart", [trace], layout, {{ responsive: true, displaylogo: false }});
  </script>
</body>
</html>
"""

    output_path.write_text(html_document, encoding="utf-8")
