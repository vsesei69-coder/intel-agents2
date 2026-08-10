"""Agent #7 — Pure Level Grid (no indicators, S/R-based).

Strategy:
  - Detect S/R levels from multi-TF pivots + volume profile + orderbook
  - Place pending buy orders at support, sell orders at resistance
  - Pre-calculate max risk before placing any order
  - Balance protection: limit total exposure to 50% of bankroll
  - No stop losses — only trailing to breakeven
  - Neutral grid: both sides active simultaneously
  - When level closes in profit → place new order at next S/R level

Bankroll: $1000 | Leverage: adaptive (10-35x based on vol) | Max exposure: 50%
"""

import hashlib, json, os, signal, sys, time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

BINANCE_BASE = "https://api.binance.com/api/v3"
UA = "LevelGrid/7.0"

SYMBOL = "ETHUSDT"
JOURNAL_DIR = Path(__file__).parent.parent / "trading_journal_levels"
JOURNAL_DIR.mkdir(exist_ok=True)
GRIDS_FILE = JOURNAL_DIR / "level_grids.json"
HISTORY_FILE = JOURNAL_DIR / "level_history.json"

BANKROLL = 1000.0
MAX_EXPOSURE_PCT = 0.50
MIN_LEVELS = 3
MAX_LEVELS = 5
TAKER_FEE = 0.0004
SLIPPAGE = 0.001
FUNDING_RATE = 0.0001


def fetch_ticker(symbol):
    try:
        r = requests.get(f"{BINANCE_BASE}/ticker/price?symbol={symbol}",
                         headers={"User-Agent": UA}, timeout=5)
        return float(r.json()["price"]) if r.status_code == 200 else None
    except: return None


def get_sr_levels(symbol):
    """Get S/R levels from sr_detector module."""
    try:
        from sr_detector import detect_levels
        return detect_levels(symbol)
    except Exception:
        return None


def compute_costs(size_usd, hours, leverage):
    n = size_usd / leverage
    return {"fee": n * TAKER_FEE * 2, "slip": size_usd * SLIPPAGE,
            "fund": size_usd * FUNDING_RATE * (hours / 8)}


def load_grids():
    if GRIDS_FILE.exists(): return json.loads(GRIDS_FILE.read_text())
    return []


def save_grids(g): GRIDS_FILE.write_text(json.dumps(g, indent=2, ensure_ascii=False))


def load_history():
    if HISTORY_FILE.exists(): return json.loads(HISTORY_FILE.read_text())
    return {"trades": [], "stats": {"total": 0, "wins": 0, "losses": 0,
            "total_pnl": 0.0, "best_trade": 0.0, "worst_trade": 0.0,
            "total_fees": 0.0, "total_slippage": 0.0, "total_funding": 0.0}}


def save_history(h): HISTORY_FILE.write_text(json.dumps(h, indent=2, ensure_ascii=False))


def total_exposure(grids):
    total = 0.0
    for g in grids:
        for l in g.get("levels", []):
            if l.get("filled") and not l.get("tp_hit"):
                total += l.get("size_usd", 0)
    return total


def build_level_grid(sr_data):
    """Build grid from S/R levels. Returns grid dict."""
    if not sr_data: return None

    price = sr_data["price"]
    support = sr_data.get("support", [])
    resistance = sr_data.get("resistance", [])

    # Adaptive leverage: lower on higher vol pairs
    leverage = 20
    vol_proxy = sr_data.get("atr_pct", 1.0) if "atr_pct" in sr_data else 1.0
    if vol_proxy > 2.0: leverage = 10
    elif vol_proxy > 1.0: leverage = 20
    else: leverage = 35

    # Size per level: distribute available balance across levels
    total_levels = min(len(support), MAX_LEVELS) + min(len(resistance), MAX_LEVELS)
    if total_levels == 0: return None
    size_per_level = (BANKROLL * MAX_EXPOSURE_PCT) / total_levels * leverage

    levels = []

    # Buy levels at support
    for i, s in enumerate(support[:MAX_LEVELS]):
        entry = s["level"]
        tp = price  # TP = current price (return to mean)
        size = round(size_per_level * (0.8 + 0.08 * i), 2)  # smaller buys further down
        levels.append({
            "level": i + 1, "side": "buy", "entry": entry, "tp": tp,
            "size_usd": size, "filled": False, "fill_price": None,
            "tp_hit": False, "exit_price": None,
            "pnl_usd": None, "gross_pnl": None,
            "fees_paid": None, "slippage_cost": None, "funding_paid": None,
            "trailing_active": False, "trailing_sl": None,
        })

    # Sell levels at resistance
    for i, r in enumerate(resistance[:MAX_LEVELS]):
        entry = r["level"]
        tp = price
        size = round(size_per_level * (0.8 + 0.08 * i), 2)
        levels.append({
            "level": len(support[:MAX_LEVELS]) + i + 1, "side": "sell", "entry": entry, "tp": tp,
            "size_usd": size, "filled": False, "fill_price": None,
            "tp_hit": False, "exit_price": None,
            "pnl_usd": None, "gross_pnl": None,
            "fees_paid": None, "slippage_cost": None, "funding_paid": None,
            "trailing_active": False, "trailing_sl": None,
        })

    # Pre-calc max risk
    max_risk = sum(l["size_usd"] for l in levels) / leverage

    return {
        "id": hashlib.md5(f"{sr_data['symbol']}{datetime.now().isoformat()}".encode()).hexdigest()[:12],
        "symbol": sr_data["symbol"], "type": "level_grid", "status": "open",
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "last_checked_at": datetime.now(timezone.utc).isoformat(),
        "price": price, "leverage": leverage,
        "levels": levels,
        "max_risk_usd": round(max_risk, 2),
        "max_exposure_pct": round(max_risk / BANKROLL * 100, 0),
    }


def check_grids():
    grids = load_grids()
    history = load_history()
    updated = False
    now_utc = datetime.now(timezone.utc)
    price = fetch_ticker(SYMBOL)
    if not price: return grids

    for grid in grids:
        if grid["status"] != "open": continue

        for lvl in grid["levels"]:
            if lvl.get("tp_hit"): continue

            side = lvl["side"]
            entry = lvl["entry"]

            if not lvl.get("filled"):
                if (side == "buy" and price <= entry) or (side == "sell" and price >= entry):
                    lvl["filled"] = True
                    lvl["fill_price"] = price
                    lvl["fill_time"] = now_utc.isoformat()
                    updated = True

            if lvl.get("filled") and not lvl.get("tp_hit"):
                fill = lvl["fill_price"]
                tp = lvl["tp"]

                # Trailing to breakeven
                if not lvl.get("trailing_active"):
                    if (side == "buy" and price >= fill * 1.005) or (side == "sell" and price <= fill * 0.995):
                        lvl["trailing_active"] = True
                        lvl["trailing_sl"] = round(fill + 0.0001, 6) if side == "buy" else round(fill - 0.0001, 6)
                        updated = True

                trail_sl = lvl.get("trailing_sl")
                tp_hit = (side == "buy" and price >= tp) or (side == "sell" and price <= tp)
                sl_hit = trail_sl and ((side == "buy" and price <= trail_sl) or (side == "sell" and price >= trail_sl))

                if tp_hit or sl_hit:
                    exit_price = tp if tp_hit else trail_sl
                    exit_slip = exit_price * (1 - SLIPPAGE) if side == "buy" else exit_price * (1 + SLIPPAGE)
                    lvl["exit_price"] = round(exit_slip, 6)
                    lvl["tp_hit"] = True

                    pct = (exit_slip - fill) / fill if side == "buy" else (fill - exit_slip) / fill
                    gross = lvl["size_usd"] * pct
                    hours = 1
                    costs = compute_costs(lvl["size_usd"], hours, grid["leverage"])
                    net = gross - costs["fee"] - costs["slip"] - costs["fund"]
                    lvl["pnl_usd"] = round(net, 2)
                    lvl["gross_pnl"] = round(gross, 2)
                    lvl["fees_paid"] = round(costs["fee"], 4)
                    lvl["slippage_cost"] = round(costs["slip"], 4)
                    lvl["funding_paid"] = round(costs["fund"], 4)
                    updated = True

        # Grid complete
        if all(lvl.get("tp_hit") for lvl in grid["levels"]):
            grid["status"] = "closed"
            grid["closed_at"] = now_utc.isoformat()
            updated = True

    if updated:
        save_grids(grids)
        for grid in grids:
            if grid["status"] == "closed":
                tp = sum((l.get("pnl_usd") or 0) for l in grid["levels"])
                tf = sum((l.get("fees_paid") or 0) for l in grid["levels"])
                ts = sum((l.get("slippage_cost") or 0) for l in grid["levels"])
                tfd = sum((l.get("funding_paid") or 0) for l in grid["levels"])
                s = history["stats"]
                s["total"] += 1
                if tp > 0: s["wins"] += 1; s["best_trade"] = max(s["best_trade"], tp)
                else: s["losses"] += 1; s["worst_trade"] = min(s["worst_trade"], tp)
                s["total_pnl"] += tp
                s["total_fees"] = s.get("total_fees", 0) + tf
                s["total_slippage"] = s.get("total_slippage", 0) + ts
                s["total_funding"] = s.get("total_funding", 0) + tfd
        active = [g for g in grids if g["status"] == "open"]
        save_grids(active)
        save_history(history)

    return grids


def run_cycle():
    price = fetch_ticker(SYMBOL)
    grids = check_grids()
    exposure = total_exposure(grids) / BANKROLL * 100

    print(f"\n{'='*55}")
    print(f"  LEVEL GRID AGENT #7 — {datetime.now().strftime('%H:%M:%S')} UTC")
    print(f"  {SYMBOL} ${price:.2f} | Neutral grid | Exposure: {exposure:.0f}%")
    print(f"{'='*55}")

    for grid in grids:
        f = sum(1 for l in grid["levels"] if l.get("filled"))
        t = sum(1 for l in grid["levels"] if l.get("tp_hit"))
        pnl = sum((l.get("pnl_usd") or 0) for l in grid["levels"])
        print(f"\n  [GRID] {f}/{len(grid['levels'])} filled | {t} TP | PnL: ${pnl:+.2f} | "
              f"Max risk: ${grid['max_risk_usd']:.2f} ({grid['max_exposure_pct']:.0f}%)")
        for l in grid["levels"]:
            st = "CLOSED" if l.get("tp_hit") else ("FILLED" if l.get("filled") else "PENDING")
            pnl_s = f"${l.get('pnl_usd',0):+.2f}" if l.get("pnl_usd") is not None else ""
            print(f"    L{l['level']} {l['side']:4s} @{l['entry']:.4f} | {st:8s} {pnl_s:>10s}")

    # Open new grid if no active grid and exposure allows
    if not grids and exposure < MAX_EXPOSURE_PCT * 100:
        sr = get_sr_levels(SYMBOL)
        if sr:
            grid = build_level_grid(sr)
            if grid:
                grids_list = [grid]
                save_grids(grids_list)
                print(f"\n  [NEW GRID] {len(grid['levels'])} levels | "
                      f"Max risk: ${grid['max_risk_usd']:.2f} ({grid['max_exposure_pct']:.0f}%)")
                print(f"  Support: {[l['entry'] for l in grid['levels'] if l['side'] == 'buy']}")
                print(f"  Resistance: {[l['entry'] for l in grid['levels'] if l['side'] == 'sell']}")

    history = load_history()
    s = history["stats"]
    wr = s["wins"] / s["total"] * 100 if s["total"] > 0 else 0
    print(f"\n  [STATUS] PnL: ${s['total_pnl']:+,.2f} | {s['total']} grids | WR: {wr:.0f}%")


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--status", action="store_true")
    global SYMBOL
    args = p.parse_args()

    if args.status:
        grids = load_grids()
        history = load_history()
        s = history["stats"]
        wr = s["wins"] / s["total"] * 100 if s["total"] > 0 else 0
        price = fetch_ticker(SYMBOL)
        exp = total_exposure(grids) / BANKROLL * 100
        print(f"Level Grid #7 | {SYMBOL} ${price:.2f} | PnL: ${s['total_pnl']:+,.2f} | "
              f"{s['total']} grids | WR: {wr:.0f}% | Exp: {exp:.0f}%")
        for g in grids:
            f = sum(1 for l in g["levels"] if l.get("filled"))
            t = sum(1 for l in g["levels"] if l.get("tp_hit"))
            print(f"  [{g['type']}] {f}/{len(g['levels'])} filled, {t} TP, "
                  f"risk ${g['max_risk_usd']:.2f} ({g['max_exposure_pct']:.0f}%)")
        return

    if args.once:
        run_cycle()
        return

    print(f"Level Grid Agent #7 starting on {SYMBOL}...", file=sys.stderr)
    running = True
    def handler(sig, frame): nonlocal running; running = False
    signal.signal(signal.SIGINT, handler)
    try:
        while running:
            run_cycle()
            time.sleep(30)
    except KeyboardInterrupt: pass
    print("\nAgent #7 stopped.", file=sys.stderr)


if __name__ == "__main__":
    main()
