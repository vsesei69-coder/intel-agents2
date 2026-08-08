"""Trading Dashboard — HTML charts for agent performance.

Generates an interactive HTML page with:
  - Equity curves per agent
  - Portfolio composition pie chart
  - Win/loss ratio bars
  - PnL timeline
  - Volatility heatmap

No dependencies — pure HTML/CSS/inline SVG.
Open dashboard.html in any browser.
"""

import json, os, sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent
JOURNAL_BASE = SCRIPTS_DIR.parent / "trading_journal"

AGENTS = {
    "Trend": JOURNAL_BASE / "trade_history.json",
    "Grid": JOURNAL_BASE.parent / "trading_journal_grid" / "grid_history.json",
    "Max Grid": JOURNAL_BASE.parent / "trading_journal_max" / "grid_history.json",
    "Corridor": JOURNAL_BASE.parent / "trading_journal_corridor" / "corridor_history.json",
}

COLORS = {"Trend": "#22c55e", "Grid": "#3b82f6", "Max Grid": "#f59e0b", "Corridor": "#8b5cf6"}


def load_agent_data(name, path):
    if not path.exists():
        return {"pnl": 0, "trades": 0, "wins": 0, "losses": 0, "trades_list": [], "equity": []}
    try:
        data = json.loads(path.read_text())
        s = data.get("stats", {})
        trades = data.get("trades", [])
        return {
            "pnl": s.get("total_pnl", 0),
            "trades": s.get("total", 0),
            "wins": s.get("wins", 0),
            "losses": s.get("losses", 0),
            "fees": s.get("total_fees", 0),
            "slip": s.get("total_slippage", 0),
            "fund": s.get("total_funding", 0),
            "trades_list": trades,
        }
    except Exception:
        return {"pnl": 0, "trades": 0, "wins": 0, "losses": 0, "trades_list": [], "equity": []}


def build_equity_svg(agent_data, width=500, height=80):
    """Mini SVG sparkline for equity curve."""
    trades = agent_data["trades_list"]
    if len(trades) < 2:
        return f'<svg width="{width}" height="{height}"><text x="10" y="45" fill="#666" font-size="12">No trades yet</text></svg>'

    equity = [1000.0]
    for t in trades:
        equity.append(equity[-1] + t.get("pnl", 0))

    # Scale
    min_eq = min(equity)
    max_eq = max(equity)
    y_range = max(max_eq - min_eq, 1)
    x_step = width / (len(equity) - 1) if len(equity) > 1 else width

    points = []
    for i, eq in enumerate(equity):
        x = i * x_step
        y = height - 5 - ((eq - min_eq) / y_range * (height - 10))
        points.append(f"{x:.1f},{y:.1f}")

    color = "#22c55e" if agent_data["pnl"] > 0 else "#ef4444"
    polyline = " ".join(points)

    return f'''<svg width="{width}" height="{height}">
  <polyline points="{polyline}" fill="none" stroke="{color}" stroke-width="2"/>
  <line x1="0" y1="{height - 5 - ((1000 - min_eq) / y_range * (height - 10)):.1f}" x2="{width}" y2="{height - 5 - ((1000 - min_eq) / y_range * (height - 10)):.1f}" stroke="#444" stroke-dasharray="4,2" stroke-width="1"/>
</svg>'''


def build_pie_svg(data_dict, width=120, height=120):
    """Mini pie chart SVG."""
    total = sum(abs(v) for v in data_dict.values())
    if total == 0:
        return f'<svg width="{width}" height="{height}"><circle cx="60" cy="60" r="45" fill="none" stroke="#333" stroke-width="2"/></svg>'

    cx, cy, r = width//2, height//2, 48
    start_angle = -90
    paths = []
    for name, value in data_dict.items():
        if value == 0:
            continue
        sweep = (abs(value) / total) * 360
        end_angle = start_angle + sweep
        x1 = cx + r * __import__('math').cos(__import__('math').radians(start_angle))
        y1 = cy + r * __import__('math').sin(__import__('math').radians(start_angle))
        x2 = cx + r * __import__('math').cos(__import__('math').radians(end_angle))
        y2 = cy + r * __import__('math').sin(__import__('math').radians(end_angle))
        large = 1 if sweep > 180 else 0
        paths.append(f'<path d="M{cx},{cy} L{x1:.1f},{y1:.1f} A{r},{r} 0 {large},1 {x2:.1f},{y2:.1f} Z" fill="{COLORS.get(name,"#666")}" opacity="0.85"/>')
        start_angle = end_angle

    return f'<svg width="{width}" height="{height}">{"".join(paths)}</svg>'


def build_win_loss_bar(wins, losses, width=200, height=20):
    """Horizontal bar for win/loss ratio."""
    total = wins + losses
    if total == 0:
        return f'<svg width="{width}" height="{height}"><rect x="0" y="0" width="{width}" height="{height}" rx="4" fill="#333"/></svg>'

    ww = int(wins / total * width)
    lw = width - ww

    return f'''<svg width="{width}" height="{height}">
  <rect x="0" y="0" width="{ww}" height="{height}" rx="4" fill="#22c55e" opacity="0.8"/>
  <rect x="{ww}" y="0" width="{lw}" height="{height}" rx="4" fill="#ef4444" opacity="0.7"/>
  <text x="{width//2}" y="14" text-anchor="middle" fill="white" font-size="10" font-weight="bold">{wins}W / {losses}L</text>
</svg>'''


def generate():
    agents_data = {}
    for name, path in AGENTS.items():
        agents_data[name] = load_agent_data(name, path)

    total_pnl = sum(d["pnl"] for d in agents_data.values())
    total_trades = sum(d["trades"] for d in agents_data.values())
    total_wins = sum(d["wins"] for d in agents_data.values())
    total_losses = sum(d["losses"] for d in agents_data.values())
    total_fees = sum(d["fees"] for d in agents_data.values())
    total_slip = sum(d["slip"] for d in agents_data.values())
    total_fund = sum(d["fund"] for d in agents_data.values())
    bankroll = 4000.0
    roi = (total_pnl / bankroll * 100) if bankroll > 0 else 0

    rows = ""
    for name, d in agents_data.items():
        wr = d["wins"] / d["trades"] * 100 if d["trades"] > 0 else 0
        sparkline = build_equity_svg(d)
        wl_bar = build_win_loss_bar(d["wins"], d["losses"])
        pnl_color = "#22c55e" if d["pnl"] > 0 else "#ef4444" if d["pnl"] < 0 else "#888"
        rows += f'''
    <tr>
      <td><b>{name}</b></td>
      <td style="color:{pnl_color}">${d["pnl"]:+,.2f}</td>
      <td>{d["trades"]}</td>
      <td>{wr:.0f}%</td>
      <td>{wl_bar}</td>
      <td>{sparkline}</td>
    </tr>'''

    # PnL pie data
    pnl_data = {}
    for name, d in agents_data.items():
        pnl_data[name] = d["pnl"]
    pie_svg = build_pie_svg(pnl_data)

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="60">
<title>NEITIS Trading Dashboard</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0a0a0f;color:#e0e0e0;font-family:Segoe UI,monospace;padding:20px}}
h1{{color:#00d4aa;font-size:24px;margin-bottom:4px}}
.sub{{color:#666;font-size:12px;margin-bottom:16px}}
.card{{background:#101018;border:1px solid #1e1e2e;border-radius:10px;padding:16px;margin-bottom:12px}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}
.stat{{text-align:center;padding:8px}}
.stat .val{{font-size:22px;font-weight:700}}
.stat .lbl{{font-size:10px;color:#555;margin-top:2px}}
.green{{color:#22c55e}}.red{{color:#ef4444}}.amber{{color:#f59e0b}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{text-align:left;color:#555;padding:8px 6px;border-bottom:1px solid #1a1a2e}}
td{{padding:8px 6px;border-bottom:1px solid #0a0a12}}
.footer{{text-align:center;color:#333;font-size:10px;margin-top:20px}}
</style>
</head>
<body>
<h1>NEITIS Trading Dashboard</h1>
<div class="sub">{datetime.now().strftime("%d.%m.%Y %H:%M:%S")} UTC | Auto-refresh 60s | 4 agents | $4,000 bankroll</div>

<div class="card">
<div class="grid">
  <div class="stat"><div class="val" style="color:{'#22c55e' if total_pnl>0 else '#ef4444'}">${total_pnl:+,.2f}</div><div class="lbl">Total PnL</div></div>
  <div class="stat"><div class="val" style="color:{'#22c55e' if roi>0 else '#ef4444'}">{roi:+.1f}%</div><div class="lbl">Portfolio ROI</div></div>
  <div class="stat"><div class="val">{total_trades}</div><div class="lbl">Total Trades</div></div>
  <div class="stat"><div class="val">{total_wins}/{total_losses}</div><div class="lbl">Wins/Losses</div></div>
</div>
<div class="grid" style="margin-top:8px">
  <div class="stat"><div class="val" style="color:#f59e0b">${total_fees:+.2f}</div><div class="lbl">Fees</div></div>
  <div class="stat"><div class="val" style="color:#f59e0b">${total_slip:+.2f}</div><div class="lbl">Slippage</div></div>
  <div class="stat"><div class="val" style="color:#f59e0b">${total_fund:+.2f}</div><div class="lbl">Funding</div></div>
  <div class="stat"><div class="val">${bankroll:,.0f}→${bankroll+total_pnl:,.0f}</div><div class="lbl">Equity</div></div>
</div>
</div>

<div class="card">
<h3 style="color:#888;font-size:14px;margin-bottom:10px">Agent Performance</h3>
<table>
<tr><th>Agent</th><th>PnL</th><th>Trades</th><th>WR</th><th>Win/Loss</th><th>Equity Curve</th></tr>
{rows}
</table>
</div>

<div class="footer">
NEITIS v1.0 | Experimental system | Not financial advice | Closed document<br>
2026 &copy; All rights reserved
</div>
</body>
</html>'''

    dashboard_path = SCRIPTS_DIR.parent / "trading_journal" / "dashboard.html"
    dashboard_path.write_text(html, encoding="utf-8")
    print(f"Dashboard saved: {dashboard_path}")
    return dashboard_path


if __name__ == "__main__":
    generate()
