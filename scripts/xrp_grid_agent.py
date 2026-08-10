"""Agent #5 — XRP Live Trailing Grid (pending orders, no stops).

Strategy:
  - Living grid: 5 pending buy limits below price, 5 sell limits above price
  - Grid follows price: when price drifts away, unfilled levels re-center
    around the current price (trailing grid), filled levels keep their TP
  - No stop losses: every level exits ONLY at TP (ATR-based, always in profit)
  - Auto-replenish: when a side fully closes in profit -> new grid rebuilt
    around the current price
  - Balance protection: >50% bankroll in positions -> no new entries,
    existing TPs stay untouched
  - Volatility-based spacing: wider ATR -> wider grid steps
  - 50x leverage, $1000 bankroll, honest per-level trade history
"""

import json, os, signal, sys, time
from datetime import datetime, timezone
from pathlib import Path

import requests

BINANCE_BASE = "https://api.binance.com/api/v3"
UA = "XRPBot/6.0"
SYMBOL = "XRPUSDT"

JOURNAL_DIR = Path(__file__).parent.parent / "trading_journal_xrp"
JOURNAL_DIR.mkdir(exist_ok=True)
GRIDS_FILE = JOURNAL_DIR / "open_grids.json"
HISTORY_FILE = JOURNAL_DIR / "xrp_history.json"
STATE_FILE = JOURNAL_DIR / "xrp_state.json"

BANKROLL = 1000.0
LEVERAGE = 50
TAKER_FEE = 0.0004
SLIPPAGE = 0.0002
FUNDING_RATE = 0.0001

GRID_LEVELS = 5
GRID_SPACING_PCT = 0.003   # 0.3% minimum spacing between levels
SPACING_MULT = 0.6         # ATR multiplier for grid spacing
TP_MULT = 2.5              # ATR multiplier for TP distance (always in profit)
MAX_BALANCE_USAGE = 0.50   # 50% — stop new entries if positions exceed this
RE_CENTER_TRIGGER = 2.5    # spacing units of drift before levels re-center

UA_HDR = {"User-Agent": UA}


def fetch_ticker(symbol):
    try:
        r = requests.get(f"{BINANCE_BASE}/ticker/price?symbol={symbol}", headers=UA_HDR, timeout=10)
        return float(r.json()["price"]) if r.status_code == 200 else None
    except Exception:
        return None


def fetch_atr(symbol):
    try:
        r = requests.get(f"{BINANCE_BASE}/klines?symbol={symbol}&interval=30m&limit=30", headers=UA_HDR, timeout=10)
        if r.status_code != 200: return 0.01
        candles = [{"h": float(k[2]), "l": float(k[3]), "c": float(k[4])} for k in r.json()]
        if len(candles) < 15: return 0.01
        trs = [max(candles[i]["h"]-candles[i]["l"],
                    abs(candles[i]["h"]-candles[i-1]["c"]),
                    abs(candles[i]["l"]-candles[i-1]["c"]))
               for i in range(1, len(candles))]
        return sum(trs[-14:]) / 14
    except Exception:
        return 0.01


def fetch_orderbook_imbalance(symbol="XRPUSDT"):
    """Get depth imbalance: >0 = bullish, <0 = bearish."""
    try:
        r = requests.get(f"{BINANCE_BASE}/depth?symbol={symbol}&limit=100", headers=UA_HDR, timeout=10)
        if r.status_code != 200: return 0
        d = r.json()
        bid_vol = sum(float(b[1]) for b in d["bids"][:100])
        ask_vol = sum(float(a[1]) for a in d["asks"][:100])
        total = bid_vol + ask_vol
        return (bid_vol - ask_vol) / total if total > 0 else 0
    except Exception:
        return 0


def get_grid_params():
    """Calculate dynamic grid parameters based on ATR and order book."""
    atr = fetch_atr(SYMBOL)
    imbalance = fetch_orderbook_imbalance(SYMBOL)
    spacing_mult = SPACING_MULT
    tp_mult = TP_MULT
    buy_skew = 0

    if imbalance > 0.15:
        buy_skew = 1
    elif imbalance > 0.08:
        buy_skew = 0.5
    elif imbalance < -0.15:
        buy_skew = -1
    elif imbalance < -0.08:
        buy_skew = -0.5

    return {"spacing_mult": spacing_mult, "tp_mult": tp_mult, "buy_skew": buy_skew,
            "imbalance": imbalance, "atr": atr}


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

def load_state():
    if STATE_FILE.exists(): return json.loads(STATE_FILE.read_text())
    return {"avg_entry_long": None, "avg_entry_short": None, "last_replenish": ""}

def save_state(s): STATE_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False))


def compute_costs(size_usd, hours):
    notion = size_usd / LEVERAGE
    return {"fee": notion * TAKER_FEE * 2, "slip": size_usd * SLIPPAGE,
            "fund": size_usd * FUNDING_RATE * (hours / 8)}


def total_exposure(grids):
    """Sum of all filled but unclosed position sizes."""
    total = 0.0
    for g in grids:
        for l in g.get("levels", []):
            if l.get("filled") and not l.get("tp_hit"):
                total += l.get("size_usd", 0)
    return total


def build_level(side, base_price, level_idx, atr, spacing, params):
    size_base = [40, 48, 58, 70, 84][level_idx if level_idx < 5 else 4]
    size = round(size_base * (1.15 ** level_idx) * LEVERAGE, 2)

    if side == "buy":
        mult = 0.7 if params["buy_skew"] > 0 else 1.0
        entry = round(base_price - spacing * (level_idx + 1) * mult, 4)
        tp = round(entry + spacing * params["tp_mult"], 4)
    else:
        mult = 1.3 if params["buy_skew"] > 0 else 1.0
        entry = round(base_price + spacing * (level_idx + 1) * mult, 4)
        tp = round(entry - spacing * params["tp_mult"], 4)

    return {"side": side, "entry": entry, "tp": tp, "size_usd": size,
            "filled": False, "fill_price": None, "fill_time": None,
            "tp_hit": False, "exit_price": None,
            "pnl_usd": None, "gross_pnl": None,
            "fees_paid": None, "slippage_cost": None, "funding_paid": None,
            "level": level_idx + 1}


def build_grid(type_, price, atr, params):
    spacing = max(atr * params["spacing_mult"], price * GRID_SPACING_PCT)
    return {"id": f"xrp_{type_}", "symbol": SYMBOL, "type": type_, "status": "open",
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "last_checked_at": datetime.now(timezone.utc).isoformat(),
            "base_price": price, "spacing": spacing,
            "levels": [build_level(type_, price, i, atr, spacing, params)
                       for i in range(GRID_LEVELS)]}


def migrate_legacy_grids(grids, history):
    """Legacy (v5) grids have no 'spacing' key and closed TRAIL levels with
    realized losses that were never written to history. Fix them: record every
    closed level, then rebuild fresh grids around the current price."""
    now_utc = datetime.now(timezone.utc)
    migrated = False
    for grid in grids:
        if "spacing" in grid:
            continue
        migrated = True
        for lvl in grid.get("levels", []):
            if lvl.get("tp_hit") and not lvl.get("_saved") and lvl.get("pnl_usd") is not None:
                lvl["_saved"] = True
                record_trade(history, lvl, grid, now_utc)
    if migrated:
        print(f"  [MIGRATE] legacy v5 grids closed {history['stats']['total']} trades into history", file=sys.stderr)
    return migrated


def init_grids():
    existing = load_grids()
    if existing:
        history = load_history()
        if migrate_legacy_grids(existing, history):
            save_history(history)
        price = fetch_ticker(SYMBOL)
        atr = fetch_atr(SYMBOL)
        params = get_grid_params()
        if any("spacing" not in g for g in existing):
            existing = [build_grid("buy", price, atr, params),
                        build_grid("sell", price, atr, params)]
            save_grids(existing)
        return existing
    price = fetch_ticker(SYMBOL) or 1.05
    atr = fetch_atr(SYMBOL)
    params = get_grid_params()
    grids = [build_grid("buy", price, atr, params),
             build_grid("sell", price, atr, params)]
    save_grids(grids)
    return grids


def recenter_grid(grid, price, atr, params):
    """Move unfilled levels of a grid around the current price (trailing)."""
    spacing = max(atr * params["spacing_mult"], price * GRID_SPACING_PCT)
    moved = 0
    for i, lvl in enumerate(grid["levels"]):
        if lvl.get("filled") and not lvl.get("tp_hit"):
            continue  # in position — keep entry and TP untouched
        if lvl.get("tp_hit"):
            continue
        fresh = build_level(grid["type"], price, i, atr, spacing, params)
        lvl.update(fresh)
        moved += 1
    grid["base_price"] = price
    grid["spacing"] = spacing
    grid["last_checked_at"] = datetime.now(timezone.utc).isoformat()
    return moved


def record_trade(history, lvl, grid, now_utc):
    s = history["stats"]
    s["total"] += 1
    pnl = lvl.get("pnl_usd") or 0.0
    if pnl > 0:
        s["wins"] += 1
        s["best_trade"] = max(s["best_trade"], pnl)
    else:
        s["losses"] += 1
        s["worst_trade"] = min(s["worst_trade"], pnl)
    s["total_pnl"] += pnl
    s["total_fees"] = s.get("total_fees", 0) + (lvl.get("fees_paid") or 0)
    s["total_slippage"] = s.get("total_slippage", 0) + (lvl.get("slippage_cost") or 0)
    s["total_funding"] = s.get("total_funding", 0) + (lvl.get("funding_paid") or 0)
    history["trades"].append({
        "ts": now_utc.isoformat(), "side": lvl["side"], "grid_id": grid["id"],
        "entry": lvl.get("fill_price"), "exit": lvl.get("exit_price"),
        "hit_type": lvl.get("hit_type", "TP"), "pnl_usd": pnl,
        "size_usd": lvl.get("size_usd"),
    })
    history["trades"] = history["trades"][-200:]
    lvl["_saved"] = True


def check_grids():
    grids = load_grids()
    history = load_history()
    state = load_state()
    updated = False
    now_utc = datetime.now(timezone.utc)
    price = fetch_ticker(SYMBOL)
    if not price:
        return grids

    atr = fetch_atr(SYMBOL)
    params = get_grid_params()

    for grid in grids:
        if grid["status"] != "open":
            continue

        # Trailing: re-center pending levels if price drifted far from base
        base = grid.get("base_price") or price
        spacing = grid.get("spacing") or max(atr * params["spacing_mult"], price * GRID_SPACING_PCT)
        drift = abs(price - base) / spacing if spacing else 0
        if drift > RE_CENTER_TRIGGER:
            moved = recenter_grid(grid, price, atr, params)
            if moved:
                updated = True
                state["last_replenish"] = now_utc.isoformat()
                print(f"  [TRAIL] price drifted {drift:.1f}x spacing -> levels re-centered ({moved} moved)", file=sys.stderr)

        side = grid["type"]

        for lvl in grid["levels"]:
            if lvl.get("tp_hit"):
                continue
            entry = lvl["entry"]

            # Fill check (pending limit order)
            if not lvl.get("filled"):
                if (side == "buy" and price <= entry) or (side == "sell" and price >= entry):
                    lvl["filled"] = True
                    lvl["fill_price"] = price
                    lvl["fill_time"] = now_utc.isoformat()
                    updated = True

            # TP check — the ONLY exit, always in profit
            if lvl.get("filled"):
                tp = lvl["tp"]
                fill = lvl["fill_price"]
                if (side == "buy" and price >= tp) or (side == "sell" and price <= tp):
                    exit_slip = tp * (1 - SLIPPAGE) if side == "buy" else tp * (1 + SLIPPAGE)
                    lvl["exit_price"] = round(exit_slip, 6)
                    lvl["tp_hit"] = True

                    pct = (exit_slip - fill) / fill if side == "buy" else (fill - exit_slip) / fill
                    gross = lvl["size_usd"] * pct
                    opened_dt = datetime.fromisoformat(grid["opened_at"].replace("Z", "+00:00"))
                    hours = max((now_utc - opened_dt).total_seconds() / 3600, 0)
                    costs = compute_costs(lvl["size_usd"], hours)
                    net = gross - costs["fee"] - costs["slip"] - costs["fund"]
                    lvl["pnl_usd"] = round(net, 2)
                    lvl["gross_pnl"] = round(gross, 2)
                    lvl["fees_paid"] = round(costs["fee"], 4)
                    lvl["slippage_cost"] = round(costs["slip"], 4)
                    lvl["funding_paid"] = round(costs["fund"], 4)
                    lvl["hit_type"] = "TP"
                    lvl["_saved"] = True
                    record_trade(history, lvl, grid, now_utc)
                    updated = True

    # Auto-replenish: rebuild a side once all its levels have closed
    for grid in grids:
        if grid["status"] == "open" and all(lvl.get("tp_hit") for lvl in grid["levels"]):
            total_pnl = sum((l.get("pnl_usd") or 0) for l in grid["levels"])
            if price:
                params = get_grid_params()
                atr = fetch_atr(SYMBOL)
                fresh = build_grid(grid["type"], price, atr, params)
                grid.update(fresh)
                updated = True

    # Balance protection: heavy exposure -> no new levels, TPs stay
    exposure_pct = total_exposure(grids) / BANKROLL * 100
    if exposure_pct > MAX_BALANCE_USAGE * 100:
        print(f"  [BALANCE] Exposure {exposure_pct:.0f}% > {MAX_BALANCE_USAGE*100:.0f}% — holding, no new entries", file=sys.stderr)

    if updated:
        save_grids(grids)
        save_history(history)
        save_state(state)

    return grids


def run_cycle():
    price = fetch_ticker(SYMBOL)
    atr = fetch_atr(SYMBOL)
    params = get_grid_params()
    grids = check_grids()
    exposure = total_exposure(grids)
    exposure_pct = exposure / BANKROLL * 100

    sigs = []
    if params["imbalance"] > 0.1:
        sigs.append("BULLISH OB")
    elif params["imbalance"] < -0.1:
        sigs.append("BEARISH OB")
    sig_str = " | ".join(sigs) if sigs else "NEUTRAL"

    print(f"\n{'='*55}")
    print(f"  XRP LIVE TRAILING GRID #5 — {datetime.now().strftime('%H:%M:%S')} UTC")
    print(f"  XRP: ${price:.4f} | ATR: {atr:.4f} | Signals: {sig_str} | Exp: {exposure_pct:.0f}%")
    print(f"{'='*55}")

    for grid in grids:
        filled = sum(1 for l in grid["levels"] if l.get("filled"))
        tp = sum(1 for l in grid["levels"] if l.get("tp_hit"))
        pnl = sum((l.get("pnl_usd") or 0) for l in grid["levels"])
        print(f"\n  [{grid['type'].upper()}] {filled}/{GRID_LEVELS} filled | {tp} TP | PnL: ${pnl:+.2f}")
        for l in grid["levels"]:
            st = "CLOSED" if l.get("tp_hit") else ("FILLED" if l.get("filled") else "PENDING")
            tp_info = f"${l.get('exit_price',0):.4f}" if l.get("tp_hit") else f"TP@{l['tp']:.4f}"
            pnl_s = f"${l.get('pnl_usd',0):+.2f}" if l.get("pnl_usd") is not None else ""
            print(f"    L{l['level']} {l['side']:4s} @{l['entry']:.4f} | {st:8s} | {tp_info:15s} {pnl_s:>10s}")

    history = load_history()
    s = history["stats"]
    wr = s["wins"] / s["total"] * 100 if s["total"] > 0 else 0
    print(f"\n  [STATUS] PnL: ${s['total_pnl']:+,.2f} | {s['total']} trades | WR: {wr:.0f}%")


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--status", action="store_true")
    args = p.parse_args()

    if args.status:
        grids = load_grids()
        history = load_history()
        s = history["stats"]
        wr = s["wins"] / s["total"] * 100 if s["total"] > 0 else 0
        price = fetch_ticker(SYMBOL)
        exposure = total_exposure(grids) / BANKROLL * 100
        print(f"XRP | ${price:.4f} | PnL: ${s['total_pnl']:+,.2f} | {s['total']} trades | WR: {wr:.0f}% | Exp: {exposure:.0f}%")
        for g in grids:
            f = sum(1 for l in g["levels"] if l.get("filled"))
            t = sum(1 for l in g["levels"] if l.get("tp_hit"))
            print(f"  [{g['type']}] {f}/{GRID_LEVELS} filled, {t} TP")
        return

    if args.once:
        init_grids()
        run_cycle()
        return

    print("XRP Live Trailing Grid #5 starting...", file=sys.stderr)
    init_grids()
    running = True
    def handler(sig, frame):
        nonlocal running; running = False
    signal.signal(signal.SIGINT, handler)
    try:
        while running:
            run_cycle()
            time.sleep(30)
    except KeyboardInterrupt:
        pass
    print("\nAgent #5 stopped.", file=sys.stderr)


if __name__ == "__main__":
    main()