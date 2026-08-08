"""Generate NEITIS trading report HTML + PDF from trade history."""
import json, os, sys
from datetime import datetime, timezone

HISTORY = os.path.join(os.path.dirname(__file__), "..", "trading_journal", "trade_history.json")
with open(HISTORY, encoding="utf-8") as f:
    data = json.load(f)

trades = data["trades"]
stats = data["stats"]

START_BALANCE = 1000.0
current_equity = START_BALANCE + stats["total_pnl"]
roi = (stats["total_pnl"] / START_BALANCE) * 100
profit_factor = stats["total_pnl"] / START_BALANCE

MSK = "+03:00"

def msk(ts):
    if not ts:
        return ""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt.strftime("%d.%m.%Y %H:%M:%S")

rows = ""
for i, t in enumerate(trades, 1):
    direction = "▲ LONG" if t["direction"] == "long" else "▼ SHORT"
    color = "#22c55e" if t["direction"] == "long" else "#ef4444"
    pnl_color = "#22c55e" if t["pnl_usd"] > 0 else "#ef4444"
    
    # Calculate duration
    duration = ""
    if t.get("opened_at") and t.get("closed_at"):
        try:
            start = datetime.fromisoformat(t["opened_at"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(t["closed_at"].replace("Z", "+00:00"))
            delta = end - start
            minutes = int(delta.total_seconds() // 60)
            seconds = int(delta.total_seconds() % 60)
            duration = f"{minutes}м {seconds}с"
        except:
            duration = ""
    
    rows += f"""<tr>
        <td>{i}</td>
        <td><b>{t['symbol']}</b></td>
        <td style="color:{color}">{direction}</td>
        <td>${t['entry_price']:.4f}</td>
        <td>${t['exit_price']:.4f}</td>
        <td>{duration}</td>
        <td>{msk(t['opened_at'])}</td>
        <td>{msk(t.get('closed_at',''))}</td>
        <td style="color:{pnl_color}; font-weight:bold">${t['pnl_usd']:+,.2f}</td>
        <td style="color:{pnl_color}">{t['pnl_pct']:+.1f}%</td>
        <td>{'TP' if t['hit_tp'] else 'SL'}</td>
    </tr>"""

html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>НЕЙТИС — Торговый Журнал</title>
<style>
    @page {{ size: A4 landscape; margin: 15mm; }}
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: 'Segoe UI', 'Arial', sans-serif; background: #0a0a0a; color: #e5e5e5; padding: 20px; }}
    .page {{ max-width: 1400px; margin: 0 auto; }}
    .header {{ text-align: center; padding: 30px 20px; background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%); border-radius: 12px; margin-bottom: 25px; border: 1px solid #1e3a5f; }}
    .header h1 {{ font-size: 22px; color: #e0e0e0; letter-spacing: 1px; margin-bottom: 5px; }}
    .header .subtitle {{ font-size: 13px; color: #7aa2c4; letter-spacing: 2px; }}
    .header .meta {{ font-size: 12px; color: #5a7a9a; margin-top: 12px; }}
    .stamp {{ display: inline-block; border: 2px solid #c41e3a; color: #c41e3a; padding: 4px 14px; font-size: 11px; letter-spacing: 2px; margin-top: 10px; }}
    .stats {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin-bottom: 25px; }}
    .stat-card {{ background: #141f2e; border: 1px solid #1e3a5f; border-radius: 8px; padding: 15px; text-align: center; }}
    .stat-card .value {{ font-size: 26px; font-weight: bold; }}
    .stat-card .label {{ font-size: 11px; color: #5a7a9a; text-transform: uppercase; letter-spacing: 1px; margin-top: 4px; }}
    .green {{ color: #22c55e; }} .red {{ color: #ef4444; }} .gold {{ color: #f59e0b; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    th {{ background: #141f2e; color: #7aa2c4; padding: 10px 8px; text-align: left; font-weight: 600; text-transform: uppercase; font-size: 10px; letter-spacing: 1px; border-bottom: 2px solid #1e3a5f; }}
    td {{ padding: 8px; border-bottom: 1px solid #141f2e; }}
    tr:hover td {{ background: #1a2740; }}
    .footer {{ text-align: center; padding: 20px; color: #3a5a7a; font-size: 10px; letter-spacing: 1px; margin-top: 20px; }}
</style>
</head>
<body>
<div class="page">

<div class="header">
    <h1>ЗАКРЫТАЯ ТРЕЙДИНГОВАЯ ЭКСПЕРИМЕНТАЛЬНАЯ СИСТЕМА<br>АВТОМАТИЧЕСКИХ ТОРГОВ «НЕЙТИС»</h1>
    <div class="subtitle">CLOSED EXPERIMENTAL TRADING SYSTEM</div>
    <div class="meta">Сгенерировано: {datetime.now().strftime('%d.%m.%Y %H:%M')} МСК | Торговая платформа: Binance | Плечо: 50x | Риск: 3% на сделку</div>
    <div class="stamp">ДЛЯ СЛУЖЕБНОГО ПОЛЬЗОВАНИЯ</div>
</div>

<div class="stats">
    <div class="stat-card">
        <div class="value" style="color:#e0e0e0">${START_BALANCE:,.0f}</div>
        <div class="label">Стартовый баланс</div>
    </div>
    <div class="stat-card">
        <div class="value gold">${current_equity:,.0f}</div>
        <div class="label">Текущий эквити</div>
    </div>
    <div class="stat-card">
        <div class="value green">${stats['total_pnl']:+,.0f}</div>
        <div class="label">Чистая прибыль</div>
    </div>
    <div class="stat-card">
        <div class="value gold">{roi:.0f}x</div>
        <div class="label">Рост капитала</div>
    </div>
    <div class="stat-card">
        <div class="value green">{roi:+.0f}%</div>
        <div class="label">Доходность</div>
    </div>
    <div class="stat-card">
        <div class="value green">{stats['wins']}/{stats['total']}</div>
        <div class="label">Win Rate</div>
    </div>
    <div class="stat-card">
        <div class="value gold">${stats['best_trade']:+,.0f}</div>
        <div class="label">Лучшая сделка</div>
    </div>
    <div class="stat-card">
        <div class="value" style="color:#e0e0e0">{stats['total']}</div>
        <div class="label">Всего сделок</div>
    </div>
</div>

<table>
<thead>
<tr>
    <th>#</th>
    <th>Пара</th>
    <th>Направление</th>
    <th>Вход</th>
    <th>Выход</th>
    <th>Длит.</th>
    <th>Открыта</th>
    <th>Закрыта</th>
    <th>PnL $</th>
    <th>PnL %</th>
    <th>Тип</th>
</tr>
</thead>
<tbody>
{rows}
</tbody>
</table>

<div class="footer">
    НЕЙТИС v1.0 | Экспериментальная система | Не является инвестиционной рекомендацией | Закрытый документ<br>
    {datetime.now().strftime('%Y')} &copy; Все права защищены
</div>

</div>
</body>
</html>"""

out_path = os.path.join(os.path.dirname(__file__), "..", "trading_journal", "NEITIS_Trade_Journal.html")
with open(out_path, "w", encoding="utf-8") as f:
    f.write(html)

# Generate PDF via Playwright (Chromium headless)
try:
    from playwright.sync_api import sync_playwright
    pdf_path = out_path.replace(".html", ".pdf")
    file_uri = "file:///" + out_path.replace("\\", "/")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(file_uri, wait_until="load", timeout=10000)
        page.pdf(path=pdf_path, format="A4", landscape=True, print_background=True)
        browser.close()
    print(f"PDF saved: {pdf_path}")
except Exception as e:
    print(f"PDF: {e}", file=sys.stderr)
    pdf_path = None

print(f"Trades: {len(trades)} | PnL: ${stats['total_pnl']:+,.2f}")
