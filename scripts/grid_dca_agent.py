"""DCA Grid Agent - one pair per bot, scaling-in grid, NO stop losses.

Strategy (Logos spec, 2026-08-10):
  - Single trending volatile pair (chosen by daily/weekly RSI screen).
  - DCA grid: N buy levels BELOW current price. Spacing grows deeper
    (step * STEP_MULT^k) and margin per level grows (margin * MARGIN_MULT^k),
    so we add bigger size as price falls = classic scale-in averaging.
  - Each filled level exits at its own TP: entry * (1 + TP_PCT) - sells the
    bounce back up. NO stop losses: a filled level just waits for the bounce.
  - No time-exit: levels are never force-closed (unlike the scalp grids).
  - Re-center upward when price runs far above the grid so we keep chasing a
    rising trend; never re-center down while levels are filling.
  - $1000 bankroll per bot, leverage 15-25x (env), real fees/slippage/funding.

Journals: trading_journal_{INSTANCE} (INSTANCE = env, default "dca").
"""

import hashlib, json, os, signal, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BINANCE_BASE = "https://fapi.binance.com"
UA = "DcaGrid/1.0"

INSTANCE = os.environ.get("DCA_INSTANCE", "dca")
SYMBOL = os.environ.get("DCA_SYMBOL", "NILUSDT")
JOURNAL_DIR = Path(__file__).parent.parent / f"trading_journal_{INSTANCE}"
JOURNAL_DIR.mkdir(exist_ok=True)
GRIDS_FILE = JOURNAL_DIR / "open_grids.json"
HISTORY_FILE = JOURNAL_DIR / "dca_history.json"

# Strategy params (env-tunable)
BANKROLL = float(os.environ.get("DCA_BALANCE", "1000.0"))
LEVERAGE = int(os.environ.get("DCA_LEVERAGE", "15"))
LEVELS = int(os.environ.get("DCA_LEVELS", "8"))
FIRST_STEP_PCT = float(os.environ.get("DCA_STEP_PCT", "0.015"))   # first gap below price
STEP_MULT = float(os.environ.get("DCA_STEP_MULT", "1.35"))        # spacing grows deeper
MARGIN_MULT = float(os.environ.get("DCA_MARGIN_MULT", "1.5"))     # size grows on scale-in
MARGIN_UTIL = float(os.environ.get("DCA_MARGIN_UTIL", "0.70"))    # fraction of bankroll in grid
TP_PCT = float(os.environ.get("DCA_TP_PCT", "0.02"))              # exit on +2% bounce
RECENTER_PCT = float(os.environ.get("DCA_RECENTER_PCT", "0.06"))  # chase trend upward

TAKER_FEE = 0.0004
SLIPPAGE = 0.0003
FUNDING_RATE = 0.0001

CYCLE_SEC = 24


def log(msg):
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} [{SYMBOL}] {msg}", flush=True)


def fetch_ticker(symbol):
    try:
        r = requests.get(f"{BINANCE_BASE}/fapi/v1/ticker/24hr",
                         params={"symbol": symbol}, headers={"User-Agent": UA}, timeout=10)
        if r.status_code != 200:
            return None
        d = r.json()
        return {"price": float(d["lastPrice"]), "change": float(d["priceChangePercent"])}
    except Exception:
        return None


def fetch_klines_range(symbol, start_time, end_time, interval="1m", limit=500):
    try:
        start_ms = int(start_time.timestamp() * 1000)
        end_ms = int(end_time.timestamp() * 1000)
        r = requests.get(f"{BINANCE_BASE}/fapi/v1/klines",
                         params={"symbol": symbol, "interval": interval,
                                 "startTime": start_ms, "endTime": end_ms, "limit": limit},
                         headers={"User-Agent": UA}, timeout=10)
        if r.status_code != 200:
            return []
        return [{"o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                 "c": float(k[4]), "t": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc)}
                for k in r.json()]
    except Exception:
        return []


def compute_costs(size_usd, hours):
    fee = (size_usd / LEVERAGE) * TAKER_FEE * 2
    slip = size_usd * SLIPPAGE
    fund = size_usd * FUNDING_RATE * (hours / 8)
    return {"fee": round(fee, 4), "slip": round(slip, 4), "fund": round(fund, 4)}


def load_grids():
    if GRIDS_FILE.exists():
        try:
            return json.loads(GRIDS_FILE.read_text())
        except Exception:
            return []
    return []


def save_grids(grids):
    GRIDS_FILE.write_text(json.dumps(grids, indent=2, ensure_ascii=False))


def load_history():
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except Exception:
            pass
    return {"stats": {"total": 0, "wins": 0, "losses": 0, "total_pnl": 0.0,
                      "best_trade": 0.0, "worst_trade": 0.0},
            "trades": []}


def save_history(history):
    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False))


def build_dca_grid(price):
    """Build DCA grid below price: growing spacing, growing margin."""
    total_margin = BANKROLL * MARGIN_UTIL
    m0 = total_margin * (MARGIN_MULT - 1) / (MARGIN_MULT ** LEVELS - 1) if MARGIN_MULT != 1 else total_margin / LEVELS

    levels = []
    for i in range(LEVELS):
        spacing = FIRST_STEP_PCT * (STEP_MULT ** i)
        entry = price * (1 - spacing)
        margin = m0 * (MARGIN_MULT ** i)
        size_usd = margin * LEVERAGE
        levels.append({
            "level": i + 1,
            "spacing_pct": round(spacing * 100, 4),
            "entry": round(entry, 8),
            "tp": round(entry * (1 + TP_PCT), 8),
            "margin": round(margin, 4),
            "size_usd": round(size_usd, 2),
            "filled": False,
            "fill_price": None,
            "fill_time": None,
            "tp_hit": False,
            "exit_price": None,
            "exit_time": None,
            "pnl_usd": None,
        })

    return {
        "id": hashlib.md5(f"{SYMBOL}{price}{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()[:12],
        "symbol": SYMBOL,
        "direction": "long",
        "center_price": price,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "last_checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "open",
        "levels": levels,
    }


def check_grid():
    grids = load_grids()
    history = load_history()
    if not grids:
        return False, "no open grid"

    grid = grids[0]
    if grid.get("status") != "open":
        return False, "grid closed"

    symbol = grid["symbol"]
    now_utc = datetime.now(timezone.utc)
    ticker = fetch_ticker(symbol)
    if not ticker:
        return False, "no ticker"
    px = ticker["price"]

    center = grid.get("center_price") or 0.0

    # Chase rising trend: re-center upward when price runs above the whole grid.
    if center > 0 and px > center * (1 + RECENTER_PCT):
        closed_pnl = _close_filled(grid, px, history, now_utc, "recenter-up")
        if closed_pnl:
            s = history["stats"]
            s["total"] += 1
            if closed_pnl > 0:
                s["wins"] += 1
                s["best_trade"] = max(s.get("best_trade", 0), closed_pnl)
            else:
                s["losses"] += 1
                s["worst_trade"] = min(s.get("worst_trade", 0), closed_pnl)
            s["total_pnl"] += closed_pnl
            history["trades"].append({
                "symbol": symbol, "direction": "long",
                "pnl": round(closed_pnl, 2), "reason": "recenter-up",
                "opened": grid["opened_at"], "closed": now_utc.isoformat(),
            })
            save_history(history)
        grid["center_price"] = px
        grid["last_checked_at"] = now_utc.isoformat()
        # reset: drop unfilled levels, rebuild fresh around new price
        grid["levels"] = build_dca_grid(px)["levels"]
        save_grids([grid])
        return True, f"re-centered up to {px}"

    lookback = now_utc - timedelta(minutes=6)
    try:
        opened_dt = datetime.fromisoformat(grid["opened_at"].replace("Z", "+00:00"))
        if opened_dt > lookback:
            lookback = opened_dt
    except Exception:
        pass
    klines = fetch_klines_range(symbol, lookback, now_utc, "1m", 500)
    if not klines:
        klines = [{"h": px, "l": px, "c": px, "t": now_utc}]

    changed = False
    for lvl in grid["levels"]:
        if lvl.get("tp_hit"):
            continue
        if not lvl.get("filled"):
            for c in klines:
                if c["l"] <= lvl["entry"]:
                    lvl["filled"] = True
                    lvl["fill_price"] = lvl["entry"]
                    lvl["fill_time"] = now_utc.isoformat()
                    changed = True
                    break
        else:
            for c in klines:
                if c["h"] >= lvl["tp"]:
                    exit_slip = lvl["tp"] * (1 - SLIPPAGE)
                    pnl_pct = (exit_slip - lvl["fill_price"]) / lvl["fill_price"]
                    lvl["tp_hit"] = True
                    lvl["exit_price"] = round(exit_slip, 8)
                    lvl["exit_time"] = now_utc.isoformat()
                    try:
                        ft = datetime.fromisoformat(lvl["fill_time"].replace("Z", "+00:00"))
                        hours = max((now_utc - ft).total_seconds() / 3600, 0)
                    except Exception:
                        hours = 0
                    costs = compute_costs(lvl["size_usd"], hours)
                    gross = lvl["size_usd"] * pnl_pct
                    net = gross - costs["fee"] - costs["slip"] - costs["fund"]
                    lvl["pnl_usd"] = round(net, 2)
                    changed = True
                    break

    grid["last_checked_at"] = now_utc.isoformat()

    filled = [l for l in grid["levels"] if l.get("filled")]
    done = [l for l in filled if l.get("tp_hit")]
    if filled and len(done) == len(filled):
        grid["status"] = "closed"
        grid["closed_at"] = now_utc.isoformat()
        total_pnl = sum((l.get("pnl_usd") or 0) for l in grid["levels"])
        s = history["stats"]
        s["total"] += 1
        if total_pnl > 0:
            s["wins"] += 1
            s["best_trade"] = max(s.get("best_trade", 0), total_pnl)
        else:
            s["losses"] += 1
            s["worst_trade"] = min(s.get("worst_trade", 0), total_pnl)
        s["total_pnl"] += total_pnl
        history["trades"].append({
            "symbol": symbol, "direction": "long",
            "pnl": round(total_pnl, 2),
            "levels": len(grid["levels"]),
            "opened": grid["opened_at"], "closed": grid["closed_at"],
        })
        save_history(history)
        save_grids([])
        return True, f"grid closed, pnl ${total_pnl:+.2f}"

    save_grids(grids if changed else [grid] if grids else [])
    return True, "checked"


def _close_filled(grid, px, history, now_utc, reason):
    total_pnl = 0.0
    for lvl in grid["levels"]:
        if lvl.get("tp_hit") or not lvl.get("filled"):
            continue
        exit_slip = px * (1 - SLIPPAGE)
        pnl_pct = (exit_slip - lvl["fill_price"]) / lvl["fill_price"]
        lvl["tp_hit"] = True
        lvl["exit_price"] = round(exit_slip, 8)
        lvl["exit_time"] = now_utc.isoformat()
        lvl["exit_reason"] = reason
        try:
            ft = datetime.fromisoformat(lvl["fill_time"].replace("Z", "+00:00"))
            hours = max((now_utc - ft).total_seconds() / 3600, 0)
        except Exception:
            hours = 0
        costs = compute_costs(lvl["size_usd"], hours)
        gross = lvl["size_usd"] * pnl_pct
        net = gross - costs["fee"] - costs["slip"] - costs["fund"]
        lvl["pnl_usd"] = round(net, 2)
        total_pnl += net
    return total_pnl


def open_new_grid():
    grids = load_grids()
    if grids:
        return None
    ticker = fetch_ticker(SYMBOL)
    if not ticker or ticker["price"] <= 0:
        return None
    grid = build_dca_grid(ticker["price"])
    save_grids([grid])
    return grid


def run_cycle():
    print(f"\n  DCA GRID [{INSTANCE}] {SYMBOL} - {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
    print(f"  Levels: {LEVELS} | Step: {FIRST_STEP_PCT*100:.1f}% x{STEP_MULT} | "
          f"Margin x{MARGIN_MULT} | Lev: {LEVERAGE}x | TP: {TP_PCT*100:.1f}% | No SL")
    print(f"  Bankroll: ${BANKROLL:.0f} | Util: {MARGIN_UTIL*100:.0f}%")

    grid = open_new_grid()
    if grid:
        print(f"  [OPENED] {SYMBOL} long, center ${grid['center_price']:.6f}")

    msg, detail = check_grid()
    if msg:
        print(f"  {detail}")

    grids = load_grids()
    if grids:
        g = grids[0]
        filled = sum(1 for l in g["levels"] if l.get("filled"))
        done = sum(1 for l in g["levels"] if l.get("tp_hit"))
        open_pnl = 0.0
        for l in g["levels"]:
            if l.get("filled") and not l.get("tp_hit"):
                tick = fetch_ticker(g["symbol"])
                px = tick["price"] if tick else g["center_price"]
                open_pnl += l["size_usd"] * (px - l["fill_price"]) / l["fill_price"]
        print(f"  [GRID] filled {filled}/{len(g['levels'])} | exited {done} | "
              f"open_pnl ${open_pnl:+.2f}")

    history = load_history()
    s = history["stats"]
    wr = s["wins"] / s["total"] * 100 if s["total"] > 0 else 0
    print(f"  [STATUS] PnL: ${s['total_pnl']:+.2f} | Trades: {s['total']} | WR: {wr:.0f}%")


def main():
    print(f"DCA Grid Agent [{INSTANCE}] {SYMBOL} starting... (Ctrl+C to stop)")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        try:
            run_cycle()
        except Exception as e:
            print(f"  [ERR] {type(e).__name__}: {e}")
        time.sleep(CYCLE_SEC)


if __name__ == "__main__":
    main()
