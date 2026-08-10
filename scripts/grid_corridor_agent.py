"""Agent #4 — Dynamic Corridor Grid Strategy (sleeping market detector).

Strategy (from neurotrading-bot/dynamic_corridor_grid_strategy.py):
  - Only small TFs: 5m/15m/30m with leverage 50x/40x/30x
  - Dense grid 22-33 orders in 3-5% corridor
  - Activates ONLY when market is "sleeping" (low volume, tight range)
  - Closes when neuro_brain detects acceleration or price breaks corridor
  - No stops, one refill allowed per TF

Bankroll: $1000 virtual | Max 3 grids (one per TF)

Uses per-cycle klines for fills, real fees/slippage/funding.
"""

import hashlib, json, os, signal, sys, time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, stdev

import requests

BINANCE_BASE = "https://api.binance.com/api/v3"
UA = "CorridorBot/4.0"

CORRIDOR_INSTANCE = os.environ.get("CORRIDOR_INSTANCE", "corridor")
JOURNAL_DIR = Path(__file__).parent.parent / f"trading_journal_{CORRIDOR_INSTANCE}"
JOURNAL_DIR.mkdir(exist_ok=True)
GRIDS_FILE = JOURNAL_DIR / "open_grids.json"
HISTORY_FILE = JOURNAL_DIR / "corridor_history.json"

BANKROLL = 1000.0
CORRIDOR_PCT = 0.04       # 4% corridor width
MAX_GRID_ORDERS = 24      # base, scales to 22-33
MAX_GRIDS = 3             # one per TF
LEVERAGE_MAP = {"5m": 50, "15m": 40, "30m": 30}
TIMEFRAMES = ["5m", "15m", "30m"]

TAKER_FEE = 0.0004
SLIPPAGE = 0.001
FUNDING_RATE = 0.0001

PAIRS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT", "AVAXUSDT",
    "LINKUSDT", "DOTUSDT", "UNIUSDT", "ATOMUSDT", "ARBUSDT",
    "OPUSDT", "NEARUSDT", "XRPUSDT", "LTCUSDT", "TRXUSDT",
    "ETCUSDT", "ALGOUSDT", "XLMUSDT", "VETUSDT", "HBARUSDT",
]


def fetch_klines(symbol, interval="15m", limit=100):
    try:
        r = requests.get(f"{BINANCE_BASE}/klines",
                         params={"symbol": symbol, "interval": interval, "limit": limit},
                         headers={"User-Agent": UA}, timeout=10)
        if r.status_code != 200:
            return []
        return [{"o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                 "c": float(k[4]), "v": float(k[5]),
                 "t": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc)}
                for k in r.json()]
    except Exception:
        return []


def fetch_ticker(symbol):
    try:
        r = requests.get(f"{BINANCE_BASE}/ticker/24hr",
                         params={"symbol": symbol}, headers={"User-Agent": UA}, timeout=10)
        if r.status_code != 200:
            return None
        d = r.json()
        return {"price": float(d["lastPrice"]), "change": float(d["priceChangePercent"])}
    except Exception:
        return None


def fetch_klines_range(symbol, start_time, end_time, interval="1m", limit=1000):
    try:
        start_ms = int(start_time.timestamp() * 1000)
        end_ms = int(end_time.timestamp() * 1000)
        r = requests.get(f"{BINANCE_BASE}/klines",
                         params={"symbol": symbol, "interval": interval,
                                 "startTime": start_ms, "endTime": end_ms, "limit": limit},
                         headers={"User-Agent": UA}, timeout=10)
        if r.status_code != 200:
            return []
        return [{"o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                 "c": float(k[4]),
                 "t": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc)}
                for k in r.json()]
    except Exception:
        return []


def bollinger(closes, period=20, mult=2.0):
    if len(closes) < period:
        return None
    r = closes[-period:]
    sma = mean(r)
    s = stdev(r) if len(r) > 1 else 0
    return {"sma": sma, "upper": sma + mult * s, "lower": sma - mult * s,
            "bw": (s * 2) / sma * 100 if sma > 0 else 0,
            "pos": (closes[-1] - (sma - mult * s)) / (s * 2 * mult) * 100 if s > 0 else 50}


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    g, l = [], []
    for i in range(len(closes) - period, len(closes)):
        d = closes[i] - closes[i - 1]
        (g if d > 0 else l).append(abs(d))
        (l if d > 0 else g).append(0)
    ag, al = mean(g) if g else 0, mean(l) if l else 0
    return 100 - (100 / (1 + ag / al)) if al > 0 else 100


def load_grids():
    if GRIDS_FILE.exists():
        return json.loads(GRIDS_FILE.read_text())
    return []


def save_grids(grids):
    GRIDS_FILE.write_text(json.dumps(grids, indent=2, ensure_ascii=False))


def load_history():
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return {"trades": [], "stats": {"total": 0, "wins": 0, "losses": 0,
            "total_pnl": 0.0, "best_trade": 0.0, "worst_trade": 0.0,
            "total_fees": 0.0, "total_slippage": 0.0, "total_funding": 0.0}}


def save_history(history):
    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False))


def compute_costs(size_usd, hours_open, leverage):
    entry_notional = size_usd / leverage
    fee = entry_notional * TAKER_FEE * 2
    slip = size_usd * SLIPPAGE
    fund = size_usd * FUNDING_RATE * (hours_open / 8)
    return {"fee": round(fee, 4), "slip": round(slip, 4), "fund": round(fund, 4)}


def is_sleeping_market(candles_15m):
    """Detect low-volatility, low-volume 'sleeping' market."""
    if len(candles_15m) < 30:
        return False

    recent = candles_15m[-30:]
    volumes = [c["v"] for c in recent]
    highs = [c["h"] for c in recent]
    lows = [c["l"] for c in recent]

    avg_vol = mean(volumes)
    recent_vol = mean(volumes[-5:])
    volume_declining = recent_vol <= avg_vol * 1.05

    range_pct = (max(highs) - min(lows)) / ((max(highs) + min(lows)) / 2)
    range_tight = range_pct <= CORRIDOR_PCT * 1.5

    return volume_declining and range_tight


def build_corridor_grid(base_price, support, resistance, total_orders):
    """Build neutral symmetric grid: buys below price, sells above."""
    levels = []
    half = total_orders // 2

    buy_step = (base_price - support) / max(half, 1)
    for i in range(1, half + 1):
        entry = base_price - buy_step * i
        if entry <= 0:
            continue
        tp = base_price
        levels.append({
            "side": "buy", "entry": round(entry, 6), "tp": round(tp, 6),
            "filled": False, "fill_price": None, "fill_time": None,
            "tp_hit": False, "exit_price": None, "tp_time": None,
            "pnl_usd": None, "gross_pnl": None,
            "fees_paid": None, "slippage_cost": None, "funding_paid": None,
        })

    sell_step = (resistance - base_price) / max(total_orders - half, 1)
    for i in range(1, total_orders - half + 1):
        entry = base_price + sell_step * i
        tp = base_price
        levels.append({
            "side": "sell", "entry": round(entry, 6), "tp": round(tp, 6),
            "filled": False, "fill_price": None, "fill_time": None,
            "tp_hit": False, "exit_price": None, "tp_time": None,
            "pnl_usd": None, "gross_pnl": None,
            "fees_paid": None, "slippage_cost": None, "funding_paid": None,
        })

    levels.sort(key=lambda x: x["entry"])
    return levels


def analyze_symbol(symbol, verbose=True):
    """Check if market is sleeping and corridor is suitable."""
    c_15 = fetch_klines(symbol, "15m", 50)
    if len(c_15) < 30:
        if verbose:
            print(f"    {symbol}: <30 candles, skip")
        return None

    ticker = fetch_ticker(symbol)
    if not ticker or ticker["price"] < 0.01:
        if verbose:
            print(f"    {symbol}: no ticker/price, skip")
        return None

    price = ticker["price"]
    closes = [c["c"] for c in c_15]

    bb = bollinger(closes, 20, 2.0)
    r = rsi(closes, 14)
    if not bb:
        if verbose:
            print(f"    {symbol}: no bb data, skip")
        return None

    # Must be sleeping (low vol, tight range) AND mid-range RSI (35-65)
    if not is_sleeping_market(c_15):
        if verbose:
            print(f"    {symbol}: not sleeping (vol/volume), skip")
        return None
    if not (35 <= r <= 65):
        if verbose:
            print(f"    {symbol}: RSI {r:.0f} out of 35-65, skip")
        return None

    recent = c_15[-30:]
    recent_high = max(c["h"] for c in recent)
    recent_low = min(c["l"] for c in recent)
    mid_price = (recent_high + recent_low) / 2
    corridor_width = (recent_high - recent_low) / mid_price

    # Corridor must be a tight channel. Lower bound is LOW: a calm flat pair
    # with a 0.6-3% channel is EXACTLY what this strategy wants (yesterday it
    # profited on narrow channels). Only exclude a waking/trending market
    # (corridor widening beyond ~1.4x) — then we hop to another flat pair.
    if corridor_width > CORRIDOR_PCT * 1.4:
        if verbose:
            print(f"    {symbol}: corridor {corridor_width*100:.2f}% too wide "
                  f"(>{CORRIDOR_PCT*1.4*100:.1f}%) - waking, skip")
        return None

    # Dynamic order count: wider corridor = more orders
    ratio = min(1.0, corridor_width / CORRIDOR_PCT)
    num_orders = max(16, min(33, int(MAX_GRID_ORDERS + ratio * 9)))

    # Pick best leverage TF
    tf = "15m"
    lev = LEVERAGE_MAP[tf]

    levels = build_corridor_grid(mid_price, recent_low, recent_high, num_orders)
    size_per_order = BANKROLL * 0.03 / len(levels)  # 3% of bankroll split across all orders

    for lvl in levels:
        lvl["size_usd"] = round(size_per_order * lev, 2)

    return {
        "symbol": symbol, "timeframe": tf, "leverage": lev,
        "price": round(price, 6), "mid_price": round(mid_price, 6),
        "support": round(recent_low, 6), "resistance": round(recent_high, 6),
        "corridor_pct": round(corridor_width * 100, 2), "num_orders": num_orders,
        "bb_pos": round(bb["pos"], 1), "rsi": round(r, 1),
        "levels": levels,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def create_grid(signal):
    grid = {
        "id": hashlib.md5(f"{signal['symbol']}{signal['timeframe']}{signal['timestamp']}".encode()).hexdigest()[:12],
        "symbol": signal["symbol"],
        "timeframe": signal["timeframe"],
        "leverage": signal["leverage"],
        "mid_price": signal["mid_price"],
        "support": signal["support"],
        "resistance": signal["resistance"],
        "opened_at": signal["timestamp"],
        "status": "open",
        "last_checked_at": signal["timestamp"],
        "levels": signal["levels"],
        "closed_at": None,
        "corridor_broken": False,
    }
    grids = load_grids()
    grids.append(grid)
    save_grids(grids)
    return grid


def check_grids():
    grids = load_grids()
    history = load_history()
    updated = False
    now_utc = datetime.now(timezone.utc)

    # Volatility check: if market waking up, adapt or close
    vol_spike = False
    try:
        from vol_monitor import get_vol_state
        vs = get_vol_state()
        for g in grids:
            if g["status"] == "open":
                pair_data = vs.get("pairs", {}).get(g["symbol"], {})
                atr_ratio = pair_data.get("atr_pct", 1) / max(pair_data.get("atr_baseline", 1), 0.001)
                if atr_ratio > 2.0:
                    # ATR > 2x baseline → emergency close
                    g["status"] = "closed"
                    g["closed_at"] = now_utc.isoformat()
                    g["corridor_broken"] = True
                    g["vol_emergency"] = True
                    updated = True
                    print(f"  [VOL SPIKE] {g['symbol']} ATR {atr_ratio:.1f}x — emergency close!", file=sys.stderr)
                elif atr_ratio > 1.5:
                    vol_spike = True
    except Exception:
        pass

    for grid in grids:
        if grid["status"] != "open":
            continue

        symbol = grid["symbol"]
        last_check_str = grid.get("last_checked_at", grid["opened_at"])
        last_check = datetime.fromisoformat(last_check_str.replace("Z", "+00:00"))

        klines = fetch_klines_range(symbol, last_check, now_utc, "1m", 1000)
        if not klines:
            grid["last_checked_at"] = now_utc.isoformat()
            updated = True
            continue

        support = grid["support"]
        resistance = grid["resistance"]
        threshold = (resistance - support) * 0.3

        # Check corridor break first
        for c in klines:
            price_broke = c["c"] < (support - threshold) or c["c"] > (resistance + threshold)
            if price_broke:
                grid["status"] = "closed"
                grid["closed_at"] = c["t"].isoformat()
                grid["corridor_broken"] = True
                # Close all open levels at current price with slippage
                for lvl in grid["levels"]:
                    if lvl.get("filled") and not lvl.get("tp_hit"):
                        exit_slip = c["c"] * (1 - SLIPPAGE) if lvl["side"] == "buy" else c["c"] * (1 + SLIPPAGE)
                        lvl["exit_price"] = round(exit_slip, 8)
                        if lvl.get("fill_price"):
                            pct = (exit_slip - lvl["fill_price"]) / lvl["fill_price"] if lvl["side"] == "buy" \
                                  else (lvl["fill_price"] - exit_slip) / lvl["fill_price"]
                            gross = lvl["size_usd"] * pct * grid["leverage"]
                            opened_dt = datetime.fromisoformat(grid["opened_at"].replace("Z", "+00:00"))
                            hours = max((c["t"] - opened_dt).total_seconds() / 3600, 0)
                            costs = compute_costs(lvl["size_usd"], hours, grid["leverage"])
                            lvl["pnl_usd"] = round(gross - costs["fee"] - costs["slip"] - costs["fund"], 2)
                            lvl["gross_pnl"] = round(gross, 2)
                            lvl["fees_paid"] = costs["fee"]
                            lvl["slippage_cost"] = costs["slip"]
                            lvl["funding_paid"] = costs["fund"]
                updated = True
                break

        if grid.get("corridor_broken"):
            continue

        # Vol-adaptive: if volatility rising, widen corridor boundaries
        if vol_spike and not grid.get("vol_widened"):
            grid["support"] = round(grid["support"] * 0.97, 6)  # lower support
            grid["resistance"] = round(grid["resistance"] * 1.03, 6)  # higher resistance
            grid["vol_widened"] = True
            updated = True

        # Check fills and TPs
        for lvl in grid["levels"]:
            if lvl.get("filled") and lvl.get("tp_hit"):
                continue

            if not lvl.get("filled"):
                for c in klines:
                    if lvl["side"] == "buy":
                        if c["l"] <= lvl["entry"]:
                            lvl["filled"] = True
                            lvl["fill_price"] = round(c["l"], 8)
                            lvl["fill_time"] = c["t"].isoformat()
                            updated = True
                            break
                    else:
                        if c["h"] >= lvl["entry"]:
                            lvl["filled"] = True
                            lvl["fill_price"] = round(c["h"], 8)
                            lvl["fill_time"] = c["t"].isoformat()
                            updated = True
                            break

            if lvl.get("filled") and not lvl.get("tp_hit"):
                for c in klines:
                    if lvl["side"] == "buy":
                        if c["h"] >= lvl["tp"]:
                            lvl["tp_hit"] = True
                            exit_slip = c["h"] * (1 - SLIPPAGE)
                            lvl["exit_price"] = round(exit_slip, 8)
                            lvl["tp_time"] = c["t"].isoformat()
                            pct = (exit_slip - lvl["fill_price"]) / lvl["fill_price"]
                            gross = lvl["size_usd"] * pct * grid["leverage"]
                            opened_dt = datetime.fromisoformat(grid["opened_at"].replace("Z", "+00:00"))
                            hours = max((c["t"] - opened_dt).total_seconds() / 3600, 0)
                            costs = compute_costs(lvl["size_usd"], hours, grid["leverage"])
                            lvl["pnl_usd"] = round(gross - costs["fee"] - costs["slip"] - costs["fund"], 2)
                            lvl["gross_pnl"] = round(gross, 2)
                            lvl["fees_paid"] = costs["fee"]
                            lvl["slippage_cost"] = costs["slip"]
                            lvl["funding_paid"] = costs["fund"]
                            updated = True
                            break
                    else:
                        if c["l"] <= lvl["tp"]:
                            lvl["tp_hit"] = True
                            exit_slip = c["l"] * (1 + SLIPPAGE)
                            lvl["exit_price"] = round(exit_slip, 8)
                            lvl["tp_time"] = c["t"].isoformat()
                            pct = (lvl["fill_price"] - exit_slip) / lvl["fill_price"]
                            gross = lvl["size_usd"] * pct * grid["leverage"]
                            opened_dt = datetime.fromisoformat(grid["opened_at"].replace("Z", "+00:00"))
                            hours = max((c["t"] - opened_dt).total_seconds() / 3600, 0)
                            costs = compute_costs(lvl["size_usd"], hours, grid["leverage"])
                            lvl["pnl_usd"] = round(gross - costs["fee"] - costs["slip"] - costs["fund"], 2)
                            lvl["gross_pnl"] = round(gross, 2)
                            lvl["fees_paid"] = costs["fee"]
                            lvl["slippage_cost"] = costs["slip"]
                            lvl["funding_paid"] = costs["fund"]
                            updated = True
                            break

        # Grid complete: all filled + all TP hit
        filled = [l for l in grid["levels"] if l.get("filled")]
        all_done = filled and all(l.get("tp_hit") for l in filled)
        if all_done:
            grid["status"] = "closed"
            grid["closed_at"] = now_utc.isoformat()
            updated = True

        if grid["status"] == "open":
            grid["last_checked_at"] = now_utc.isoformat()
            updated = True

    if updated:
        save_grids(grids)

        for grid in grids:
            if grid["status"] == "closed":
                total_pnl = sum((lvl.get("pnl_usd") or 0) for lvl in grid["levels"])
                total_fees = sum((lvl.get("fees_paid") or 0) for lvl in grid["levels"])
                total_slip = sum((lvl.get("slippage_cost") or 0) for lvl in grid["levels"])
                total_fund = sum((lvl.get("funding_paid") or 0) for lvl in grid["levels"])
                s = history["stats"]
                s["total"] += 1
                if total_pnl > 0:
                    s["wins"] += 1
                    s["best_trade"] = max(s["best_trade"], total_pnl)
                else:
                    s["losses"] += 1
                    s["worst_trade"] = min(s["worst_trade"], total_pnl)
                s["total_pnl"] += total_pnl
                s["total_fees"] = s.get("total_fees", 0) + total_fees
                s["total_slippage"] = s.get("total_slippage", 0) + total_slip
                s["total_funding"] = s.get("total_funding", 0) + total_fund

        active = [g for g in grids if g["status"] == "open"]
        save_grids(active)
        save_history(history)

    return grids


def run_cycle():
    print(f"\n{'='*60}")
    print(f"  CORRIDOR GRID AGENT #4 — {datetime.now().strftime('%H:%M:%S')} UTC")
    print(f"  Sleeping market detector | Corridor 3-5% | 22-33 orders")
    print(f"{'='*60}")

    closed = [g for g in check_grids() if g["status"] == "closed"]
    if closed:
        print(f"\n  [CLOSED] {len(closed)} grid(s):")
        for g in closed:
            total = sum((lvl.get("pnl_usd") or 0) for lvl in g["levels"])
            reason = "BROKE" if g.get("corridor_broken") else "TP"
            print(f"    {g['symbol']} [{g['timeframe']}]: ${total:+.2f} | {reason}")

    active = load_grids()
    if len(active) >= MAX_GRIDS:
        print(f"  Max grids ({MAX_GRIDS}). Waiting for closures.")
        return

    print(f"\n  Scanning for sleeping markets...")
    scan_pairs = list(PAIRS)
    try:
        from vol_monitor import get_calm_pairs
        calm = get_calm_pairs(12) or []
        for s in calm:
            if s not in scan_pairs:
                scan_pairs.append(s)
    except Exception:
        pass
    scan_pairs = scan_pairs[:16]
    signals = []
    for sym in scan_pairs:
        time.sleep(0.4)
        s = analyze_symbol(sym)
        if s:
            signals.append(s)

    opened = 0
    for sig in signals:
        active = load_grids()
        if len(active) >= MAX_GRIDS:
            break
        # Don't open two grids on same symbol
        if any(g["symbol"] == sig["symbol"] and g["status"] == "open" for g in active):
            continue
        try:
            from regime_detector import get_current_regime
            rd = get_current_regime()
            advice = rd.get("agent_advice", {}).get("corridor", {})
            if not advice.get("active", True):
                continue
        except Exception:
            pass
        grid = create_grid(sig)
        print(f"\n  [OPENED] {sig['symbol']} [{sig['timeframe']}]")
        print(f"    Corridor: ${sig['support']:.4f} - ${sig['resistance']:.4f} "
              f"({sig['corridor_pct']:.1f}%) | {sig['num_orders']} orders")
        print(f"    BB: {sig['bb_pos']}% | RSI: {sig['rsi']} | "
              f"Lev: {sig['leverage']}x | Sleeping: YES")
        opened += 1

    if not opened:
        print(f"  No sleeping markets found. Market too volatile for corridor strategy.")

    history = load_history()
    s = history["stats"]
    active = load_grids()
    wr = s["wins"] / s["total"] * 100 if s["total"] > 0 else 0
    print(f"\n  [STATUS] PnL: ${s['total_pnl']:+.2f} | Trades: {s['total']} | "
          f"WR: {wr:.0f}% | Active: {len(active)}")
    tf = s.get("total_fees", 0)
    if tf > 0:
        print(f"  [COSTS]  Fees: ${tf:.2f} | Slippage: ${s.get('total_slippage',0):.2f} | "
              f"Funding: ${s.get('total_funding',0):.2f}")


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
        print(f"Corridor Grid Agent #4 | PnL: ${s['total_pnl']:+.2f} | "
              f"{s['total']} trades | WR: {wr:.0f}% | Open: {len(grids)}")
        for g in grids:
            filled = sum(1 for l in g["levels"] if l.get("filled"))
            tp = sum(1 for l in g["levels"] if l.get("tp_hit"))
            print(f"  {g['symbol']} [{g['timeframe']}]: {filled}/{len(g['levels'])} filled, "
                  f"{tp} TP hit")
        return

    if args.once:
        run_cycle()
        return

    print("Corridor Grid Agent #4 starting... (Ctrl+C to stop)", file=sys.stderr)
    running = True

    def handler(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, handler)
    try:
        while running:
            run_cycle()
            time.sleep(60)
    except KeyboardInterrupt:
        pass

    print("\nAgent #4 stopped.", file=sys.stderr)


if __name__ == "__main__":
    main()
