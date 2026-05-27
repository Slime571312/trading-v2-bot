"""HTML-Report-Generator — Equity-Curve, Drawdown, Trade-Liste mit Bokeh."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from bokeh.embed import file_html
from bokeh.layouts import column
from bokeh.models import ColumnDataSource, DataTable, HoverTool, TableColumn
from bokeh.plotting import figure
from bokeh.resources import CDN

from ._types import BacktestResult

REPORT_DIR = Path(__file__).resolve().parent.parent.parent.parent / "backend" / "reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)


def _summary_html(r: BacktestResult) -> str:
    m = r.metrics
    return f"""
    <div style="font-family: -apple-system, sans-serif; padding: 16px 24px;
                background: #f5f5f7; border-radius: 8px; margin-bottom: 24px;">
      <h2 style="margin: 0 0 12px 0;">{r.instrument} · {r.iter_tf} · {r.start.date()} → {r.end.date()}</h2>
      <table style="border-collapse: collapse; font-size: 13px;">
        <tr>
          <td style="padding: 4px 16px 4px 0;"><b>Trades:</b> {m.total_trades} ({m.longs}L / {m.shorts}S)</td>
          <td style="padding: 4px 16px 4px 0;"><b>Win-Rate:</b> {m.win_rate:.1%}</td>
          <td style="padding: 4px 16px 4px 0;"><b>Total Return:</b> {m.total_return_pct:+.2f}%</td>
          <td style="padding: 4px 16px 4px 0;"><b>Max DD:</b> {m.max_drawdown_pct:.2f}%</td>
        </tr>
        <tr>
          <td style="padding: 4px 16px 4px 0;"><b>Avg Win R:</b> {m.avg_win_r:+.2f}</td>
          <td style="padding: 4px 16px 4px 0;"><b>Avg Loss R:</b> {m.avg_loss_r:+.2f}</td>
          <td style="padding: 4px 16px 4px 0;"><b>Expectancy:</b> {m.expectancy_r:+.2f}R</td>
          <td style="padding: 4px 16px 4px 0;"><b>Profit Factor:</b> {m.profit_factor:.2f}</td>
        </tr>
        <tr>
          <td style="padding: 4px 16px 4px 0;"><b>Sharpe:</b> {m.sharpe:.2f}</td>
          <td style="padding: 4px 16px 4px 0;"><b>Exposure:</b> {m.exposure_pct:.1f}%</td>
          <td style="padding: 4px 16px 4px 0;"><b>Initial:</b> €{r.initial_capital:,.0f}</td>
          <td style="padding: 4px 16px 4px 0;"><b>Final:</b> €{r.final_equity:,.2f}</td>
        </tr>
      </table>
    </div>
    """


def render_html(result: BacktestResult, filename: str | None = None) -> Path:
    """Schreibt einen kompletten HTML-Report; gibt Pfad zurück."""
    # Equity-Curve-Plot
    eq_times = [t for t, _ in result.equity_curve]
    eq_values = [v for _, v in result.equity_curve]
    eq_source = ColumnDataSource(data=dict(time=eq_times, equity=eq_values))

    p_eq = figure(
        title="Equity-Curve",
        x_axis_type="datetime", height=300, sizing_mode="stretch_width",
        background_fill_color="#fafafa",
    )
    p_eq.line("time", "equity", source=eq_source, line_width=2, color="#2563eb")
    p_eq.add_tools(HoverTool(tooltips=[("Time", "@time{%F %H:%M}"), ("Equity", "@equity{0,0.00}")],
                              formatters={"@time": "datetime"}, mode="vline"))

    # Drawdown-Plot
    arr = np.array(eq_values)
    running_max = np.maximum.accumulate(arr)
    dd = (arr - running_max) / running_max * 100.0
    dd_source = ColumnDataSource(data=dict(time=eq_times, dd=dd.tolist()))
    p_dd = figure(
        title="Drawdown (%)",
        x_axis_type="datetime", height=200, sizing_mode="stretch_width",
        background_fill_color="#fafafa",
    )
    p_dd.varea(x="time", y1=0, y2="dd", source=dd_source, fill_color="#dc2626", fill_alpha=0.4)
    p_dd.line("time", "dd", source=dd_source, line_width=1, color="#dc2626")

    # Trade-Tabelle
    trades_data = {
        "open": [t.open_time.strftime("%Y-%m-%d %H:%M") for t in result.trades],
        "close": [t.close_time.strftime("%Y-%m-%d %H:%M") for t in result.trades],
        "side": [t.side for t in result.trades],
        "variant": [t.variant for t in result.trades],
        "entry": [f"{t.entry:.2f}" for t in result.trades],
        "exit": [f"{t.exit:.2f}" for t in result.trades],
        "sl": [f"{t.sl:.2f}" for t in result.trades],
        "tp": [f"{t.tp:.2f}" for t in result.trades],
        "R": [f"{t.r_multiple:+.2f}" for t in result.trades],
        "pnl": [f"{t.pnl_abs:+.2f}" for t in result.trades],
        "reason": [t.exit_reason for t in result.trades],
        "bars": [str(t.bars_held) for t in result.trades],
    }
    table_source = ColumnDataSource(trades_data)
    columns = [
        TableColumn(field=k, title=k) for k in
        ["open", "close", "side", "variant", "entry", "exit", "sl", "tp", "R", "pnl", "reason", "bars"]
    ]
    trade_table = DataTable(source=table_source, columns=columns, sizing_mode="stretch_width",
                             height=320, index_position=None)

    layout = column(p_eq, p_dd, trade_table, sizing_mode="stretch_width")
    summary = _summary_html(result)

    html_doc = file_html(layout, CDN, f"Backtest {result.instrument} {result.iter_tf}")
    # Summary in den <body> einsetzen
    html_doc = html_doc.replace("<body>", f"<body>{summary}", 1)

    if filename is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        filename = f"backtest_{result.instrument}_{result.iter_tf}_{ts}.html"
    out = REPORT_DIR / filename
    out.write_text(html_doc, encoding="utf-8")
    return out
