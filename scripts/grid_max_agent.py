"""Agent #3 — Max Grid Strategy (dual BB + RSI scaled grid).

Strategy (from neurotrading-bot/max_grid_strategy.py):
  - Two independent grids: Bollinger (2.5% balance) + RSI (2.5% balance)
  - Scaled orders: first largest, each subsequent ×0.85
  - 5-12 orders per grid, count based on BB width (volatility)
  - 0.3% grid step, close on opposite signal
  - No hard stops — the grid itself IS the risk management

Bankroll: $1000 virtual | Leverage: 50x | Max 2 grids (one BB + one RSI)

Uses per-cycle klines for fills, real fees/slippage/funding.
"""

import hashlib, json, os, signal, sys, time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, stdev

import requests

BINANCE_BASE = "https://api.binance.com/api/v3"
UA = "MaxGridBot/3.0"

JOURNAL_DIR = Path(__file__).parent.parent / "trading_journal_max"
JOURNAL_DIR.mkdir(exist_ok=True)
GRIDS_FILE = JOURNAL_DIR / "open_grids.json"
HISTORY_FILE = JOURNAL_DIR / "grid_history.json"

BANKROLL = 1000.0
BALANCE_PER_GRID = 0.025  # 2.5% per indicator
MAX_LEVERAGE = 50
SCALE_FACTOR = 0.85       # each subsequent order is ×0.85 of previous
GRID_STEP_PCT = 0.003     # 0.3% between levels
MIN_ORDERS = 5
MAX_ORDERS = 12

TAKER_FEE = 0.0004
SLIPPAGE = 0.001
FUNDING_RATE = 0.0001

PAIRS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT", "AVAXUSDT",
    "LINKUSDT", "DOTUSDT", "MATICUSDT", "UNIUSDT", "ATOMUSDT",
    "ARBUSDT", "OPUSDT", "NEARUSDT", "APTUSDT", "FILUSDT",
    "TRXUSDT", "ETCUSDT", "LTCUSDT", "BNBUSDT", "XRPUSDT",
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
                         params={"symbol": symbol},
                         headers={"User-Agent": UA}, timeout=10)
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


def atr(candles, period=14):
    if len(candles) < period + 1:
        return 0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return mean(trs[-period:])


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


def analyze_pair(symbol):
    """Signal based on BB + RSI — determines if we should open a grid."""
    c_15 = fetch_klines(symbol, "15m", 100)
    c_5 = fetch_klines(symbol, "5m", 60)

    if len(c_15) < 30:
        return None

    ticker = fetch_ticker(symbol)
    if not ticker or ticker["price"] < 0.01:
        return None

    price = ticker["price"]
    closes_15 = [c["c"] for c in c_15]

    bb = bollinger(closes_15, 20, 2.0)
    r = rsi(closes_15, 14)
    a = atr(c_15, 14)
    if not bb or a == 0:
        return None

    # Determine direction and which indicator triggered
    indicators = []

    # BB signal
    if bb["pos"] < 25:
        indicators.append(("bb", "long", (1 - bb["pos"]/25) * 0.5 + 0.4))
    elif bb["pos"] > 75:
        indicators.append(("bb", "short", (bb["pos"]/100) * 0.5 + 0.4))

    # RSI signal
    if r < 35:
        indicators.append(("rsi", "long", (1 - r/35) * 0.3 + 0.3))
    elif r > 65:
        indicators.append(("rsi", "short", (r/100) * 0.3 + 0.3))

    if not indicators:
        return None

    # Pick best indicator signal
    best = max(indicators, key=lambda x: x[2])
    indicator, direction, confidence = best

    if confidence < 0.5:
        return None

    # Dynamic order count based on BB width
    bw = bb.get("bw", 2.0)
    num_orders = MIN_ORDERS
    if bw > 3.0:
        num_orders = MAX_ORDERS
    elif bw > 1.0:
        ratio = (bw - 1.0) / 2.0
        num_orders = int(MIN_ORDERS + ratio * (MAX_ORDERS - MIN_ORDERS))

    # Build scaled grid levels
    balance_for_grid = BANKROLL * BALANCE_PER_GRID
    grid_step = price * GRID_STEP_PCT
    total_scale = sum(SCALE_FACTOR ** i for i in range(num_orders))
    base_amount = balance_for_grid / total_scale

    levels = []
    for i in range(num_orders):
        amount = base_amount * (SCALE_FACTOR ** (num_orders - 1 - i))  # largest first
        size_usd = amount * MAX_LEVERAGE

        if direction == "long":
            entry = price - grid_step * (i + 1)
            tp = entry + a * 1.5
        else:
            entry = price + grid_step * (i + 1)
            tp = entry - a * 1.5

        levels.append({
            "level": i + 1,
            "entry": round(entry, 6),
            "tp": round(tp, 6),
            "size_usd": round(size_usd, 2),
            "filled": False,
            "fill_price": None,
            "fill_time": None,
            "tp_hit": False,
            "exit_price": None,
            "tp_time": None,
            "pnl_usd": None,
            "gross_pnl": None,
            "fees_paid": None,
            "slippage_cost": None,
            "funding_paid": None,
        })

    sl = (levels[-1]["entry"] - a * 2) if direction == "long" else (levels[-1]["entry"] + a * 2)

    return {
        "symbol": symbol, "direction": direction, "indicator": indicator,
        "price": price, "confidence": round(confidence, 2),
        "bb_pos": round(bb["pos"], 1), "rsi": round(r, 1),
        "bb_bw": round(bw, 2), "num_orders": num_orders,
        "grid_step": round(grid_step, 6), "stop_loss": round(sl, 6),
        "balance_used": round(balance_for_grid, 2),
        "levels": levels,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def create_grid(signal):
    grid = {
        "id": hashlib.md5(f"{signal['symbol']}{signal['indicator']}{signal['timestamp']}".encode()).hexdigest()[:12],
        "symbol": signal["symbol"],
        "direction": signal["direction"],
        "indicator": signal["indicator"],
        "stop_loss": signal["stop_loss"],
        "balance_used": signal["balance_used"],
        "confidence": signal["confidence"],
        "opened_at": signal["timestamp"],
        "status": "open",
        "last_checked_at": signal["timestamp"],
        "levels": signal["levels"],
        "closed_at": None,
        "sl_hit": False,
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

    for grid in grids:
        if grid["status"] != "open":
            continue

        direction = grid["direction"]
        sl = grid["stop_loss"]
        last_check_str = grid.get("last_checked_at", grid["opened_at"])
        last_check = datetime.fromisoformat(last_check_str.replace("Z", "+00:00"))

        klines = fetch_klines_range(grid["symbol"], last_check, now_utc, "1m", 1000)
        if not klines:
            grid["last_checked_at"] = now_utc.isoformat()
            updated = True
            continue

        for lvl in grid["levels"]:
            if lvl.get("filled") and lvl.get("tp_hit"):
                continue

            if not lvl.get("filled"):
                for c in klines:
                    cross = (direction == "long" and c["l"] <= lvl["entry"]) or \
                            (direction == "short" and c["h"] >= lvl["entry"])
                    if cross:
                        lvl["filled"] = True
                        lvl["fill_price"] = round(c["l"] if direction == "long" else c["h"], 8)
                        lvl["fill_time"] = c["t"].isoformat()
                        updated = True
                        break

            if lvl.get("filled") and not lvl.get("tp_hit"):
                for c in klines:
                    cross = (direction == "long" and c["h"] >= lvl["tp"]) or \
                            (direction == "short" and c["l"] <= lvl["tp"])
                    if cross:
                        lvl["tp_hit"] = True
                        raw_exit = c["h"] if direction == "long" else c["l"]
                        exit_slip = raw_exit * (1 - SLIPPAGE) if direction == "long" else raw_exit * (1 + SLIPPAGE)
                        lvl["exit_price"] = round(exit_slip, 8)
                        lvl["tp_time"] = c["t"].isoformat()

                        pct = (exit_slip - lvl["fill_price"]) / lvl["fill_price"] if direction == "long" \
                              else (lvl["fill_price"] - exit_slip) / lvl["fill_price"]
                        gross = lvl["size_usd"] * pct * MAX_LEVERAGE

                        opened_dt = datetime.fromisoformat(grid["opened_at"].replace("Z", "+00:00"))
                        hours = max((c["t"] - opened_dt).total_seconds() / 3600, 0)
                        costs = compute_costs(lvl["size_usd"], hours, MAX_LEVERAGE)
                        net = gross - costs["fee"] - costs["slip"] - costs["fund"]

                        lvl["pnl_usd"] = round(net, 2)
                        lvl["gross_pnl"] = round(gross, 2)
                        lvl["fees_paid"] = costs["fee"]
                        lvl["slippage_cost"] = costs["slip"]
                        lvl["funding_paid"] = costs["fund"]
                        updated = True
                        break

        # SL check
        any_filled = any(lvl.get("filled") for lvl in grid["levels"])
        if any_filled:
            for c in klines:
                sl_hit = (direction == "long" and c["l"] <= sl) or (direction == "short" and c["h"] >= sl)
                if sl_hit:
                    grid["status"] = "closed"
                    grid["closed_at"] = c["t"].isoformat()
                    grid["sl_hit"] = True
                    for lvl in grid["levels"]:
                        if lvl.get("filled") and not lvl.get("tp_hit"):
                            raw_exit = c["l"] if direction == "long" else c["h"]
                            exit_slip = raw_exit * (1 - SLIPPAGE) if direction == "long" else raw_exit * (1 + SLIPPAGE)
                            lvl["exit_price"] = round(exit_slip, 8)
                            if lvl.get("fill_price"):
                                pct = (exit_slip - lvl["fill_price"]) / lvl["fill_price"] if direction == "long" \
                                      else (lvl["fill_price"] - exit_slip) / lvl["fill_price"]
                                gross = lvl["size_usd"] * pct * MAX_LEVERAGE
                                opened_dt = datetime.fromisoformat(grid["opened_at"].replace("Z", "+00:00"))
                                hours = max((c["t"] - opened_dt).total_seconds() / 3600, 0)
                                costs = compute_costs(lvl["size_usd"], hours, MAX_LEVERAGE)
                                net = gross - costs["fee"] - costs["slip"] - costs["fund"]
                                lvl["pnl_usd"] = round(net, 2)
                                lvl["gross_pnl"] = round(gross, 2)
                                lvl["fees_paid"] = costs["fee"]
                                lvl["slippage_cost"] = costs["slip"]
                                lvl["funding_paid"] = costs["fund"]
                    updated = True
                    break

        # Grid complete: all filled levels hit TP
        if not grid.get("sl_hit"):
            filled = [l for l in grid["levels"] if l.get("filled")]
            all_done = all(l.get("tp_hit") for l in filled) if filled else False
            if all_done and len(filled) > 0:
                grid["status"] = "closed"
                grid["closed_at"] = now_utc.isoformat()
                grid["sl_hit"] = False
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


def can_open_indicator(indicator):
    active = [g for g in load_grids() if g["status"] == "open"]
    return not any(g["indicator"] == indicator for g in active)


def run_cycle():
    print(f"\n{'='*60}")
    print(f"  MAX GRID AGENT #3 — {datetime.now().strftime('%H:%M:%S')} UTC")
    print(f"  Dual BB+RSI scaled grids | Bal: ${BANKROLL} | Lev: {MAX_LEVERAGE}x")
    print(f"{'='*60}")

    closed = [g for g in check_grids() if g["status"] == "closed"]
    if closed:
        print(f"\n  [CLOSED] {len(closed)} grid(s):")
        for g in closed:
            total = sum((lvl.get("pnl_usd") or 0) for lvl in g["levels"])
            tp_sl = "SL" if g.get("sl_hit") else "TP"
            print(f"    {g['symbol']} [{g['indicator']}] {g['direction']}: ${total:+.2f} | {tp_sl}")

    if not can_open_indicator("bb") and not can_open_indicator("rsi"):
        print(f"  Both grids active. Waiting for closures.")
        return

    print(f"\n  Scanning for signals...")
    try:
        from vol_monitor import get_top_volatile_pairs
        scan_pairs = get_top_volatile_pairs(12)
    except Exception:
        scan_pairs = PAIRS[:12]
    signals = []
    for sym in scan_pairs:
        time.sleep(0.4)
        s = analyze_pair(sym)
        if s:
            signals.append(s)

    signals.sort(key=lambda x: x["confidence"], reverse=True)

    opened = 0
    for sig in signals:
        if not can_open_indicator(sig["indicator"]):
            continue
        try:
            from regime_detector import check_direction_allowed
            ok, msg = check_direction_allowed(sig["direction"], "max_grid")
            if not ok:
                continue
        except Exception:
            pass
        grid = create_grid(sig)
        entries = [l["entry"] for l in sig["levels"]]
        print(f"\n  [OPENED] {sig['direction'].upper()} {sig['symbol']} [{sig['indicator']}]")
        print(f"    Price: ${sig['price']:.4f} | Conf: {sig['confidence']:.0%} | "
              f"BB: {sig['bb_pos']}% | RSI: {sig['rsi']}")
        print(f"    Orders: {sig['num_orders']} | Step: {sig['grid_step']:.4f} | "
              f"Bal: ${sig['balance_used']:.2f}")
        print(f"    Levels: {[f'${e:.4f}' for e in entries[:5]]}{'...' if len(entries) > 5 else ''}")
        opened += 1

    if not opened:
        print(f"  No valid signals.")

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
        print(f"Max Grid Agent #3 | PnL: ${s['total_pnl']:+.2f} | "
              f"{s['total']} trades | WR: {wr:.0f}% | Open: {len(grids)}")
        for g in grids:
            filled_count = sum(1 for l in g["levels"] if l.get("filled"))
            print(f"  {g['symbol']} [{g['indicator']}] {g['direction']}: "
                  f"{filled_count}/{len(g['levels'])} filled")
        return

    if args.once:
        run_cycle()
        return

    print("Max Grid Agent #3 starting... (Ctrl+C to stop)", file=sys.stderr)
    running = True

    def handler(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, handler)
    try:
        while running:
            run_cycle()
            time.sleep(45)
    except KeyboardInterrupt:
        pass

    print("\nAgent #3 stopped.", file=sys.stderr)


if __name__ == "__main__":
    main()
