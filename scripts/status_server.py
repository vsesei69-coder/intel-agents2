"""Status server — мгновенный ответ о состоянии агентов по HTTP.

Читает журналы/состояния из volume и отдаёт компактный JSON/HTML.
GET /        -> HTML статус
GET /status  -> JSON
GET /raw/<journal> -> сырой файл журнала
"""
import json
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path("/opt/intel")
SCRIPTS = BASE / "scripts"
JOURNALS = {
    "trend": BASE / "trading_journal",
    "grid": BASE / "trading_journal_grid",
    "max_grid": BASE / "trading_journal_max",
    "corridor": BASE / "trading_journal_corridor",
    "xrp": BASE / "trading_journal_xrp",
    "stoch": BASE / "trading_journal_stoch",
    "level_grid": BASE / "trading_journal_levels",
}

HISTORY_FILE = {
    "trend": "trade_history.json",
    "grid": "grid_history.json",
    "max_grid": "grid_history.json",
    "corridor": "corridor_history.json",
    "xrp": "xrp_history.json",
    "stoch": "stoch_history.json",
    "level_grid": "level_grids.json",
}
OPEN_FILE = {
    "trend": "open_positions.json",
    "grid": "open_grids.json",
    "max_grid": "open_grids.json",
    "corridor": "open_grids.json",
    "xrp": "open_grids.json",
    "stoch": "open_positions.json",
    "level_grid": "open_grids.json",
}


def jread(path):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return None


def last_line_count(path):
    try:
        if path.exists():
            return sum(1 for _ in path.open("r", encoding="utf-8"))
    except Exception:
        pass
    return 0


def collect_status():
    now = datetime.now(timezone.utc).isoformat()
    agents = {}
    for name, jdir in JOURNALS.items():
        hist = jread(jdir / HISTORY_FILE[name])
        openf = jread(jdir / OPEN_FILE[name])
        if isinstance(hist, dict):
            stats = hist.get("stats", {})
            total = stats.get("total", 0)
            wins = stats.get("wins", 0)
            losses = stats.get("losses", 0)
            pnl = stats.get("total_pnl", 0)
            wr = round(wins / total * 100, 1) if total else 0.0
            open_n = 0
            if isinstance(openf, list):
                open_n = sum(1 for p in openf if isinstance(p, dict) and p.get("status") == "open")
            agents[name] = {
                "pnl": pnl, "trades": total, "wins": wins, "losses": losses,
                "wr": wr, "open": open_n,
            }
        elif isinstance(hist, list):
            open_n = len(openf) if isinstance(openf, list) else 0
            agents[name] = {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0, "wr": 0.0, "open": open_n}
        else:
            open_n = len(openf) if isinstance(openf, list) else 0
            agents[name] = {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0, "wr": 0.0, "open": open_n}
        agents[name]["journal_dir"] = str(jdir.name)
        agents[name]["mtime"] = _fmt(jdir / HISTORY_FILE[name])
    sup = jread(BASE / "trading_journal" / "orchestrator_state.json") or {}
    regime = jread(BASE / "trading_journal" / "market_regime.json") or {}
    return {
        "ts": now,
        "supervisor": {"cycles": sup.get("cycles", 0), "cooldowns": sup.get("cooldowns", {})},
        "regime": regime.get("regime", "UNKNOWN"),
        "agents": agents,
    }


def _fmt(p):
    try:
        if p.exists():
            return datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()
    except Exception:
        pass
    return None


def render_html(status):
    rows = []
    for name, a in status["agents"].items():
        rows.append(
            "<tr>"
            f"<td>{name}</td>"
            f"<td>${a['pnl']:+.2f}</td>"
            f"<td>{a['trades']}</td>"
            f"<td>{a['wins']}W/{a['losses']}L</td>"
            f"<td>{a['wr']}%</td>"
            f"<td>{a['open']}</td>"
            f"<td>{a['mtime'] or '-'}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>intel-agents status</title>
<style>
body{{font-family:monospace;background:#111;color:#eee;padding:20px}}
h1{{color:#4caf50}} table{{border-collapse:collapse}} td,th{{border:1px solid #333;padding:6px 12px}}
th{{background:#222;color:#4caf50}}
</style></head>
<body>
<h1>intel-agents — NEITIS status</h1>
<p>updated: {status['ts']} | regime: <b>{status['regime']}</b> | cycles: {status['supervisor']['cycles']}</p>
<table>
<tr><th>agent</th><th>PnL</th><th>trades</th><th>W/L</th><th>WR</th><th>open</th><th>last journal</th></tr>
{''.join(rows)}
</table>
<p>GET /status for JSON</p>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            if self.path == "/status" or self.path == "/status/":
                self._send(200, json.dumps(collect_status(), indent=2).encode())
            elif self.path == "/" or self.path == "/index.html":
                s = collect_status()
                self._send(200, render_html(s).encode(), "text/html; charset=utf-8")
            elif self.path.startswith("/raw/"):
                name = self.path[len("/raw/"):]
                jdir = JOURNALS.get(name)
                if jdir is None:
                    self._send(404, b'{"error":"unknown agent"}')
                    return
                p = jdir / HISTORY_FILE[name]
                if not p.exists():
                    self._send(404, b'{"error":"no history"}')
                    return
                body = p.read_bytes()
                self._send(200, body, "application/json")
            elif self.path == "/health" or self.path == "/health/":
                self._send(200, json.dumps({"status": "ok"}).encode())
            elif self.path == "/operator" or self.path == "/operator/":
                oplog = BASE / "logs" / "operator.jsonl"
                lines = []
                if oplog.exists():
                    try:
                        lines = [json.loads(l) for l in oplog.read_text().splitlines() if l.strip()][-50:]
                    except Exception:
                        pass
                self._send(200, json.dumps({"operator": lines, "count": len(lines)}, indent=2).encode())
            else:
                self._send(404, b'{"error":"not found"}')
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}).encode())

    def log_message(self, *args):
        pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[status] listening on :{port}", file=sys.stderr, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
