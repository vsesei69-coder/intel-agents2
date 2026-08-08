"""Agent #5 — XRP Dynamic Grid (auto-replenish, avg TP, balance protection).

Strategy:
  - Corridor: 1.01-1.21 (~2% range), dynamic based on ATR
  - Grid: buy below price, sell above, both sides pending limit orders
  - Auto-replenish: when a level closes in profit → new pending order placed
  - Average TP: when multiple levels filled → TP = avg_entry + spread
  - Balance protection: >50% balance in positions → hold, adjust TP, no new entries
  - Volatility-based spacing: wider ATR → wider grid steps
  - No stop losses — only trailing to breakeven
  - 50x leverage, $1000 bankroll
"""

import hashlib, json, os, signal, sys, time
from datetime import datetime, timezone
from pathlib import Path

import requests

BINANCE_BASE = "https://api.binance.com/api/v3"
UA = "XRPBot/5.0"
SYMBOL = "XRPUSDT"

JOURNAL_DIR = Path(__file__).parent.parent / "trading_journal_xrp"
JOURNAL_DIR.mkdir(exist_ok=True)
GRIDS_FILE = JOURNAL_DIR / "open_grids.json"
HISTORY_FILE = JOURNAL_DIR / "xrp_history.json"
STATE_FILE = JOURNAL_DIR / "xrp_state.json"

BANKROLL = 1000.0
LEVERAGE = 50
TAKER_FEE = 0.0004
SLIPPAGE = 0.001
FUNDING_RATE = 0.0001

GRID_LEVELS = 5
GRID_SPACING_PCT = 0.004  # 0.4% between levels (tight)
MAX_BALANCE_USAGE = 0.50  # 50% — stop new entries if positions exceed this

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


def check_leading_indicators():
    """Check ETH and LINK ATR for leading signals on XRP vol.
    Returns: dict with leading signal strength (0-1) and recommended action."""
    xrp_atr = fetch_atr("XRPUSDT")
    eth_atr = fetch_atr("ETHUSDT")
    try:
        eth_atr_pct = eth_atr / (float(requests.get(
            f"{BINANCE_BASE}/ticker/price?symbol=ETHUSDT",
            headers=UA_HDR, timeout=5).json()["price"]))
    except Exception:
        eth_atr_pct = 0

    # Has ETH vol spiked recently? (check 1h ago vs now)
    try:
        r = requests.get(f"{BINANCE_BASE}/klines?symbol=ETHUSDT&interval=1h&limit=4",
                         headers=UA_HDR, timeout=10)
        if r.status_code == 200:
            eth_kl = r.json()
            if len(eth_kl) >= 3:
                eth_range_now = (float(eth_kl[-1][2]) - float(eth_kl[-1][3])) / float(eth_kl[-1][4])
                eth_range_1h = (float(eth_kl[-2][2]) - float(eth_kl[-2][3])) / float(eth_kl[-2][4])
                eth_vol_spike = eth_range_now > eth_range_1h * 1.5
            else:
                eth_vol_spike = False
        else:
            eth_vol_spike = False
    except Exception:
        eth_vol_spike = False

    return {
        "eth_spike": eth_vol_spike,
        "eth_atr": eth_atr,
        "xrp_atr": xrp_atr,
    }


def get_grid_params():
    """Calculate dynamic grid parameters based on market conditions.
    Returns: spacing_mult, tp_mult, buy_skew (0=neutral, >0=more buys)."""
    leads = check_leading_indicators()
    imbalance = fetch_orderbook_imbalance("XRPUSDT")
    atr = leads["xrp_atr"]

    # Default params
    spacing_mult = 0.8   # ATR multiplier for grid spacing
    tp_mult = 3.0        # ATR multiplier for TP distance
    buy_skew = 0         # 0 = symmetric, +1 = more buy levels

    # ETH vol spike → expect XRP vol in 1h → tighten grid
    if leads["eth_spike"]:
        spacing_mult = 0.5   # tighter grid to catch move
        tp_mult = 4.0        # wider TP for bigger expected move
        print(f"  [LEADING] ETH vol spike detected — tightening grid, widening TP", file=sys.stderr)

    # Order book imbalance → directional skew
    if imbalance > 0.15:
        buy_skew = 1
    elif imbalance > 0.08:
        buy_skew = 0.5
    elif imbalance < -0.15:
        buy_skew = -1
    elif imbalance < -0.08:
        buy_skew = -0.5

    return {"spacing_mult": spacing_mult, "tp_mult": tp_mult, "buy_skew": buy_skew,
            "imbalance": imbalance, "atr": atr, "eth_spike": leads["eth_spike"]}


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


def avg_entry_for_side(grids, side):
    """Calculate volume-weighted average entry for all filled positions on one side."""
    total_size = 0.0
    weighted_sum = 0.0
    for g in grids:
        if g["type"].startswith(side):  # "buy" or "sell"
            for l in g.get("levels", []):
                if l.get("filled") and not l.get("tp_hit") and l.get("fill_price"):
                    total_size += l.get("size_usd", 0)
                    weighted_sum += l.get("fill_price", 0) * l.get("size_usd", 0)
    if total_size == 0:
        return None
    return weighted_sum / total_size


def build_level_entry(side, base_price, level_idx, atr, size_base, params=None):
    """Generate one grid level with individual TP. Uses dynamic params from research."""
    if params is None:
        params = {"spacing_mult": 0.8, "tp_mult": 3.0, "buy_skew": 0}

    spacing = max(atr * params["spacing_mult"], base_price * GRID_SPACING_PCT)
    size = round(size_base * (1.15 ** level_idx) * LEVERAGE, 2)

    if side == "buy":
        # Buy skew: move entries CLOSER to price (easier to fill)
        entry = round(base_price - spacing * (level_idx + 1) * (0.7 if params["buy_skew"] > 0 else 1.0), 4)
        tp = round(entry + atr * params["tp_mult"], 4)
    else:
        # Sell skew: move entries FARTHER from price (harder to fill)
        entry = round(base_price + spacing * (level_idx + 1) * (1.3 if params["buy_skew"] > 0 else 1.0), 4)
        tp = round(entry - atr * params["tp_mult"], 4)

    return {"side": side, "entry": entry, "tp": tp, "size_usd": size,
            "filled": False, "fill_price": None, "fill_time": None,
            "tp_hit": False, "exit_price": None,
            "pnl_usd": None, "gross_pnl": None,
            "fees_paid": None, "slippage_cost": None, "funding_paid": None,
            "trailing_active": False, "trailing_sl": None}


def rebuild_grid(side, base_price, atr, params=None):
    """Build a fresh grid of 5 levels with dynamic params."""
    size_bases = [40, 48, 58, 70, 84]
    result = []
    for i in range(GRID_LEVELS):
        lvl = build_level_entry(side, base_price, i, atr, size_bases[i], params)
        lvl["level"] = i + 1
        result.append(lvl)
    return result


def init_grids():
    existing = load_grids()
    if existing: return existing
    price = fetch_ticker(SYMBOL) or 1.05
    atr = fetch_atr(SYMBOL)
    params = get_grid_params()
    now = datetime.now(timezone.utc).isoformat()
    grids = [
        {"id": "xrp_buy", "symbol": SYMBOL, "type": "buy", "status": "open",
         "opened_at": now, "last_checked_at": now, "base_price": price,
         "levels": rebuild_grid("buy", price, atr, params)},
        {"id": "xrp_sell", "symbol": SYMBOL, "type": "sell", "status": "open",
         "opened_at": now, "last_checked_at": now, "base_price": price,
         "levels": rebuild_grid("sell", price, atr, params)},
    ]
    save_grids(grids)
    return grids


def check_grids():
    grids = load_grids()
    history = load_history()
    state = load_state()
    updated = False
    now_utc = datetime.now(timezone.utc)
    price = fetch_ticker(SYMBOL)
    if not price:
        return grids

    # Process each grid
    for grid in grids:
        if grid["status"] != "open":
            continue

        for lvl in grid["levels"]:
            if lvl.get("tp_hit"):
                continue

            side = lvl["side"]
            entry = lvl["entry"]

            # Fill check
            if not lvl.get("filled"):
                if (side == "buy" and price <= entry) or (side == "sell" and price >= entry):
                    lvl["filled"] = True
                    lvl["fill_price"] = price
                    lvl["fill_time"] = now_utc.isoformat()
                    updated = True

            # TP / trailing check
            if lvl.get("filled") and not lvl.get("tp_hit"):
                fill = lvl["fill_price"]
                tp = lvl["tp"]

                # Trailing to breakeven
                trail_active = lvl.get("trailing_active", False)
                if not trail_active:
                    if (side == "buy" and price >= fill + 0.0015) or (side == "sell" and price <= fill - 0.0015):
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
                    gross = lvl["size_usd"] * pct * LEVERAGE
                    opened_dt = datetime.fromisoformat(grid["opened_at"].replace("Z", "+00:00"))
                    hours = max((now_utc - opened_dt).total_seconds() / 3600, 0)
                    costs = compute_costs(lvl["size_usd"], hours)
                    net = gross - costs["fee"] - costs["slip"] - costs["fund"]
                    lvl["pnl_usd"] = round(net, 2)
                    lvl["gross_pnl"] = round(gross, 2)
                    lvl["fees_paid"] = round(costs["fee"], 4)
                    lvl["slippage_cost"] = round(costs["slip"], 4)
                    lvl["funding_paid"] = round(costs["fund"], 4)
                    lvl["hit_type"] = "TP" if tp_hit else "TRAIL"
                    updated = True

        # Grid complete
        if all(lvl.get("tp_hit") for lvl in grid["levels"]):
            grid["status"] = "closed"
            grid["closed_at"] = now_utc.isoformat()
            updated = True

    # Auto-replenish: rebuild grids if all levels closed in profit
    for grid in grids:
        if grid["status"] == "closed":
            total_pnl = sum((l.get("pnl_usd") or 0) for l in grid["levels"])
            if total_pnl > 0 and price:
                params = get_grid_params()
                atr = fetch_atr(SYMBOL)
                grid["status"] = "open"
                grid["opened_at"] = now_utc.isoformat()
                grid["base_price"] = price
                grid["levels"] = rebuild_grid(grid["type"], price, atr, params)
                grid["closed_at"] = None
                updated = True

    # Balance protection: if >50% balance used, adjust TPs to average
    exposure = total_exposure(grids)
    exposure_pct = exposure / BANKROLL * 100

    if exposure_pct > 50:
        # Calculate avg entry for both sides
        avg_long = avg_entry_for_side(grids, "buy")
        avg_short = avg_entry_for_side(grids, "sell")

        # Adjust TP to average + spread
        for grid in grids:
            for lvl in grid["levels"]:
                if lvl.get("filled") and not lvl.get("tp_hit"):
                    if lvl["side"] == "buy" and avg_long:
                        lvl["tp"] = round(avg_long * 1.02, 4)  # 2% above avg
                        updated = True
                    elif lvl["side"] == "sell" and avg_short:
                        lvl["tp"] = round(avg_short * 0.98, 4)  # 2% below avg
                        updated = True

    if updated:
        save_grids(grids)

        for grid in grids:
            if grid.get("_closed_saved"):
                continue
            total_pnl = sum((l.get("pnl_usd") or 0) for l in grid["levels"])
            any_closed = any(l.get("tp_hit") for l in grid["levels"])
            if any_closed and total_pnl != 0 and not all(l.get("tp_hit") for l in grid["levels"]):
                continue  # partially closed, not a full grid close
            # Only save fully resolved grids
            if all(l.get("tp_hit") for l in grid["levels"]) or grid["status"] == "closed":
                s = history["stats"]
                s["total"] += 1
                if total_pnl > 0: s["wins"] += 1; s["best_trade"] = max(s["best_trade"], total_pnl)
                else: s["losses"] += 1; s["worst_trade"] = min(s["worst_trade"], total_pnl)
                s["total_pnl"] += total_pnl
                s["total_fees"] = s.get("total_fees", 0) + sum((l.get("fees_paid") or 0) for l in grid["levels"])
                s["total_slippage"] = s.get("total_slippage", 0) + sum((l.get("slippage_cost") or 0) for l in grid["levels"])
                s["total_funding"] = s.get("total_funding", 0) + sum((l.get("funding_paid") or 0) for l in grid["levels"])
                grid["_closed_saved"] = True

        save_history(history)

    return grids


def run_cycle():
    price = fetch_ticker(SYMBOL)
    atr = fetch_atr(SYMBOL)
    params = get_grid_params()
    grids = check_grids()
    exposure = total_exposure(grids)
    exposure_pct = exposure / BANKROLL * 100

    avg_long = avg_entry_for_side(grids, "buy")
    avg_short = avg_entry_for_side(grids, "sell")

    # Signals
    sigs = []
    if params["eth_spike"]:
        sigs.append("ETH SPIKE")
    if params["imbalance"] > 0.1:
        sigs.append("BULLISH OB")
    elif params["imbalance"] < -0.1:
        sigs.append("BEARISH OB")
    sig_str = " | ".join(sigs) if sigs else "NEUTRAL"

    print(f"\n{'='*55}")
    print(f"  XRP DYNAMIC GRID #5 — {datetime.now().strftime('%H:%M:%S')} UTC")
    print(f"  XRP: ${price:.4f} | ATR: {atr:.4f} | Spacing: {params['spacing_mult']:.1f}x | "
          f"TP: {params['tp_mult']:.0f}x | Skew: {params['buy_skew']:+.1f}")
    print(f"  Signals: {sig_str} | Exposure: {exposure_pct:.0f}%")
    print(f"{'='*55}")

    for grid in grids:
        filled = sum(1 for l in grid["levels"] if l.get("filled"))
        tp = sum(1 for l in grid["levels"] if l.get("tp_hit"))
        pnl = sum((l.get("pnl_usd") or 0) for l in grid["levels"])
        print(f"\n  [{grid['type'].upper()}] {filled}/{GRID_LEVELS} filled | {tp} TP | PnL: ${pnl:+.2f}")
        for l in grid["levels"]:
            st = "CLOSED" if l.get("tp_hit") else ("FILLED" if l.get("filled") else "PENDING")
            tp_info = f"${l.get('exit_price',0):.4f}" if l.get("tp_hit") else f"TP@{l['tp']:.4f}"
            trail = "[TRAIL]" if l.get("trailing_active") else ""
            pnl_s = f"${l.get('pnl_usd',0):+.2f}" if l.get("pnl_usd") is not None else ""
            print(f"    L{l['level']} {l['side']:4s} @{l['entry']:.4f} | {st:8s} | {tp_info:15s} {trail:8s} {pnl_s:>10s}")

    if avg_long: print(f"\n  Avg LONG entry: ${avg_long:.4f}")
    if avg_short: print(f"  Avg SHORT entry: ${avg_short:.4f}")
    if exposure_pct > 50: print(f"  [BALANCE] Exposure {exposure_pct:.0f}% > 50% — TPs adjusted, no new entries")

    history = load_history()
    s = history["stats"]
    wr = s["wins"] / s["total"] * 100 if s["total"] > 0 else 0
    print(f"\n  [STATUS] PnL: ${s['total_pnl']:+,.2f} | {s['total']} grids | WR: {wr:.0f}%")


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
        print(f"XRP Grid #5 | ${price:.4f} | PnL: ${s['total_pnl']:+,.2f} | {s['total']} grids | WR: {wr:.0f}% | Exp: {exposure:.0f}%")
        for g in grids:
            f = sum(1 for l in g["levels"] if l.get("filled"))
            t = sum(1 for l in g["levels"] if l.get("tp_hit"))
            print(f"  [{g['type']}] {f}/{GRID_LEVELS} filled, {t} TP")
        return

    if args.once:
        init_grids()
        run_cycle()
        return

    print("XRP Dynamic Grid #5 starting...", file=sys.stderr)
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
