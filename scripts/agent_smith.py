"""Agent #8 — Smith. Trailing-limit grid that rides price around Bollinger edges.

  - 15m BB(20,2): when price touches the band (>70% zone), place limit orders.
  - Sell-limit ladder above price at upper band; buy-limit below at lower band.
  - Grid trails price every cycle — unfilled levels shift to follow.
  - Each level independent: fill → wait TP (grid-step), no stops, no corridor break.
  - Honest math: gross = size * pct, size already includes leverage.
"""
import hashlib, json, os, signal, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SMITH_INSTANCE = os.environ.get("SMITH_INSTANCE", "smith")
JOURNAL_DIR = Path(__file__).parent.parent / f"trading_journal_{SMITH_INSTANCE}"
JOURNAL_DIR.mkdir(exist_ok=True)
GRIDS_FILE = JOURNAL_DIR / "open_grids.json"
HISTORY_FILE = JOURNAL_DIR / "smith_history.json"

BANKROLL = 1000.0
LEVERAGE = int(os.environ.get("SMITH_LEVERAGE", "20"))
MARGIN_PER_LEVEL = float(os.environ.get("SMITH_MARGIN_PCT", "0.02"))  # 2% of bankroll
GRID_STEP = float(os.environ.get("SMITH_STEP", "0.001"))             # 0.1%
TP_STEP = GRID_STEP
MAX_LEVELS = int(os.environ.get("SMITH_MAX_LEVELS", "20"))
MAX_FILLED_BEFORE_PAUSE = int(os.environ.get("SMITH_MAX_FILLED", "15"))

BB_PERIOD = 20
BB_STD = 2.0
TAKER_FEE = 0.0004
SLIPPAGE = 0.001
FUNDING_RATE = 0.0001

# Pairs to scan
PAIRS = [
    "BTCUSDT", "ETHUSDT", "ADAUSDT", "SOLUSDT", "LINKUSDT",
    "DOTUSDT", "LTCUSDT", "UNIUSDT", "ATOMUSDT", "BCHUSDT",
    "ETCUSDT", "NEARUSDT", "OPUSDT", "XRPUSDT", "TIAUSDT",
    "SUIUSDT", "CRVUSDT", "GALAUSDT", "IMXUSDT", "AAVEUSDT",
    "APTUSDT", "ARBUSDT", "WIFUSDT", "PEPEUSDT", "LDOUSDT",
]

sys.path.insert(0, str(Path(__file__).parent))


def fetch_ticker(symbol):
    try:
        import requests
        r = requests.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}", timeout=10)
        if r.status_code == 200:
            data = r.json()
            return {"symbol": data["symbol"], "price": float(data["price"])}
    except Exception:
        pass
    return None


def fetch_klines(symbol, interval="15m", limit=100):
    try:
        import requests
        r = requests.get(
            f"https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10,
        )
        if r.status_code == 200:
            candles = []
            for k in r.json():
                candles.append({
                    "t": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
                    "o": float(k[1]), "h": float(k[2]),
                    "l": float(k[3]), "c": float(k[4]),
                    "v": float(k[5]),
                })
            return candles
    except Exception:
        pass
    return []


def bollinger(closes, period=20, std=2.0):
    if len(closes) < period:
        return None
    sma = sum(closes[-period:]) / period
    var = sum((c - sma) ** 2 for c in closes[-period:]) / period
    std_val = var ** 0.5
    return {"mid": round(sma, 6), "upper": round(sma + std * std_val, 6),
            "lower": round(sma - std * std_val, 6),
            "pos": round((closes[-1] - (sma - std * std_val)) / (2 * std * std_val) * 100, 1)
            if std_val > 0 else 50}


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains = losses = 0
    for i in range(-period, 0):
        d = closes[i] - closes[i - 1]
        if d > 0: gains += d
        else: losses -= d
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)


def load_grids():
    if GRIDS_FILE.exists():
        try:
            return json.loads(GRIDS_FILE.read_text())
        except Exception:
            pass
    return []


def save_grids(grids):
    GRIDS_FILE.write_text(json.dumps(grids, indent=2, ensure_ascii=False))


def load_history():
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except Exception:
            pass
    return {"trades": [], "stats": {"total": 0, "wins": 0, "losses": 0,
            "total_pnl": 0.0, "best_trade": 0.0, "worst_trade": 0.0,
            "total_fees": 0.0, "total_slippage": 0.0, "total_funding": 0.0}}


def save_history(history):
    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False))


def compute_costs(size_usd, hours):
    return {"fee": round(size_usd * TAKER_FEE * 2, 4),
            "slip": round(size_usd * SLIPPAGE, 4),
            "fund": round(size_usd * FUNDING_RATE * (hours / 8), 4)}


def scan_entry(symbol):
    """Return entry signal if price at BB edge, or None."""
    klines = fetch_klines(symbol, "15m", 50)
    if len(klines) < 30:
        return None
    ticker = fetch_ticker(symbol)
    if not ticker or ticker["price"] < 0.01:
        return None

    price = ticker["price"]
    closes = [c["c"] for c in klines]
    bb = bollinger(closes, BB_PERIOD, BB_STD)
    if not bb:
        return None

    r = rsi(closes, 14)
    pos = bb["pos"]
    atr_candles = [max(c["h"] - c["l"], 0.0001) for c in klines[-14:]]
    atr = sum(atr_candles) / len(atr_candles)
    atr_pct = atr / price

    # Volume check — avoid dead pairs
    vols = [c["v"] for c in klines[-10:]]
    avg_vol = sum(vols) / len(vols) if vols else 0
    if avg_vol < 100:
        return None

    direction = None
    if pos >= 70 and r >= 50:
        direction = "sell"
    elif pos <= 30 and r <= 50:
        direction = "buy"
    else:
        return None

    return {"symbol": symbol, "direction": direction, "price": price,
            "bb_upper": bb["upper"], "bb_lower": bb["lower"], "bb_pos": pos,
            "rsi": r, "atr_pct": round(atr_pct * 100, 2)}


def create_grid(signal):
    direction = signal["direction"]
    price = signal["price"]
    sym = signal["symbol"]
    now_utc = datetime.now(timezone.utc)

    margin = BANKROLL * MARGIN_PER_LEVEL
    size_usd = round(margin * LEVERAGE, 2)

    levels = []
    for i in range(1, MAX_LEVELS + 1):
        if direction == "sell":
            entry = round(price * (1 + GRID_STEP * i), 6)
            tp = round(entry * (1 - TP_STEP), 6)
        else:
            entry = round(price * (1 - GRID_STEP * i), 6)
            tp = round(entry * (1 + TP_STEP), 6)
        levels.append({
            "side": direction, "entry": entry, "tp": tp,
            "filled": False, "fill_price": None, "fill_time": None,
            "tp_hit": False, "exit_price": None, "tp_time": None,
            "pnl_usd": None, "gross_pnl": None,
            "fees_paid": None, "slippage_cost": None, "funding_paid": None,
            "size_usd": size_usd,
        })

    grid = {
        "id": hashlib.md5(f"{sym}{direction}{now_utc.isoformat()}".encode()).hexdigest()[:12],
        "symbol": sym, "direction": direction, "leverage": LEVERAGE,
        "bb_pos": signal["bb_pos"], "rsi": signal["rsi"],
        "opened_at": now_utc.isoformat(), "status": "open",
        "last_checked_at": now_utc.isoformat(), "closed_at": None,
        "levels": levels,
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

    for grid in list(grids):
        if grid["status"] != "open":
            continue

        sym = grid["symbol"]
        direction = grid["direction"]
        ticker = fetch_ticker(sym)
        if not ticker:
            continue
        price = ticker["price"]

        last_check_str = grid.get("last_checked_at", grid["opened_at"])
        last_check = datetime.fromisoformat(last_check_str.replace("Z", "+00:00"))

        klines = fetch_klines(sym, "15m", 100)
        if not klines:
            grid["last_checked_at"] = now_utc.isoformat()
            updated = True
            continue

        # Check fills and TPs using fresh klines
        for lvl in grid["levels"]:
            if lvl.get("filled") and lvl.get("tp_hit"):
                continue

            if not lvl.get("filled"):
                for c in klines:
                    if c["t"] < last_check:
                        continue
                    if direction == "sell":
                        if c["h"] >= lvl["entry"]:
                            lvl["filled"] = True
                            lvl["fill_price"] = round(c["h"], 8)
                            lvl["fill_time"] = c["t"].isoformat()
                            updated = True
                            break
                    else:
                        if c["l"] <= lvl["entry"]:
                            lvl["filled"] = True
                            lvl["fill_price"] = round(c["l"], 8)
                            lvl["fill_time"] = c["t"].isoformat()
                            updated = True
                            break

            if lvl.get("filled") and not lvl.get("tp_hit"):
                for c in klines:
                    if c["t"] < last_check:
                        continue
                    if direction == "sell":
                        if c["l"] <= lvl["tp"]:
                            lvl["tp_hit"] = True
                            exit_slip = c["l"] * (1 + SLIPPAGE)
                            lvl["exit_price"] = round(exit_slip, 8)
                            lvl["tp_time"] = c["t"].isoformat()
                            pct = (lvl["fill_price"] - exit_slip) / lvl["fill_price"]
                            gross = lvl["size_usd"] * pct
                            opened_dt = datetime.fromisoformat(grid["opened_at"].replace("Z", "+00:00"))
                            hours = max((c["t"] - opened_dt).total_seconds() / 3600, 0)
                            costs = compute_costs(lvl["size_usd"], hours)
                            lvl["pnl_usd"] = round(gross - costs["fee"] - costs["slip"] - costs["fund"], 2)
                            lvl["gross_pnl"] = round(gross, 2)
                            lvl["fees_paid"] = costs["fee"]
                            lvl["slippage_cost"] = costs["slip"]
                            lvl["funding_paid"] = costs["fund"]
                            updated = True
                            break
                    else:
                        if c["h"] >= lvl["tp"]:
                            lvl["tp_hit"] = True
                            exit_slip = c["h"] * (1 - SLIPPAGE)
                            lvl["exit_price"] = round(exit_slip, 8)
                            lvl["tp_time"] = c["t"].isoformat()
                            pct = (exit_slip - lvl["fill_price"]) / lvl["fill_price"]
                            gross = lvl["size_usd"] * pct
                            opened_dt = datetime.fromisoformat(grid["opened_at"].replace("Z", "+00:00"))
                            hours = max((c["t"] - opened_dt).total_seconds() / 3600, 0)
                            costs = compute_costs(lvl["size_usd"], hours)
                            lvl["pnl_usd"] = round(gross - costs["fee"] - costs["slip"] - costs["fund"], 2)
                            lvl["gross_pnl"] = round(gross, 2)
                            lvl["fees_paid"] = costs["fee"]
                            lvl["slippage_cost"] = costs["slip"]
                            lvl["funding_paid"] = costs["fund"]
                            updated = True
                            break

        # Trailing: re-center unfilled levels toward current price
        unfilled = [l for l in grid["levels"] if not l.get("filled")]
        if unfilled and klines:
            last_price = klines[-1]["c"]
            if direction == "sell" and last_price > unfilled[0]["entry"]:
                shift = (last_price - unfilled[0]["entry"])
                for lvl in unfilled:
                    lvl["entry"] = round(lvl["entry"] + shift, 6)
                    lvl["tp"] = round(lvl["entry"] * (1 - TP_STEP), 6)
                updated = True
            elif direction == "buy" and last_price < unfilled[0]["entry"]:
                shift = (last_price - unfilled[0]["entry"])
                for lvl in unfilled:
                    lvl["entry"] = round(lvl["entry"] + shift, 6)
                    lvl["tp"] = round(lvl["entry"] * (1 + TP_STEP), 6)
                updated = True

        # Pause if too many filled without return
        filled_open = [l for l in grid["levels"] if l.get("filled") and not l.get("tp_hit")]
        if len(filled_open) >= MAX_FILLED_BEFORE_PAUSE and not grid.get("paused"):
            grid["paused"] = True
            grid["paused_at"] = now_utc.isoformat()
            updated = True

        # Close grid if all levels done
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
                total_pnl = sum((l.get("pnl_usd") or 0) for l in grid["levels"])
                total_fees = sum((l.get("fees_paid") or 0) for l in grid["levels"])
                total_slip = sum((l.get("slippage_cost") or 0) for l in grid["levels"])
                total_fund = sum((l.get("funding_paid") or 0) for l in grid["levels"])
                filled_count = sum(1 for l in grid["levels"] if l.get("filled"))
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
                history.setdefault("trades", []).append({
                    "symbol": grid["symbol"],
                    "direction": grid["direction"],
                    "pnl": round(total_pnl, 2),
                    "opened": grid["opened_at"],
                    "closed": grid.get("closed_at"),
                    "leverage": grid.get("leverage"),
                    "levels": len(grid.get("levels", [])),
                    "filled": filled_count,
                    "bb_pos": grid.get("bb_pos"),
                })

        active = [g for g in grids if g["status"] == "open"]
        save_grids(active)
        save_history(history)

    return grids


def run_cycle():
    print(f"\n{'='*60}")
    print(f"  AGENT SMITH [{SMITH_INSTANCE}] — {datetime.now().strftime('%H:%M:%S')} UTC")
    print(f"  Trailing-limit grid | BB(20,2) edges | {LEVERAGE}x | Step {GRID_STEP*100:.1f}%")
    print(f"{'='*60}")

    closed = [g for g in check_grids() if g["status"] == "closed"]
    if closed:
        print(f"\n  [CLOSED] {len(closed)} grid(s):")
        for g in closed:
            total = sum((l.get("pnl_usd") or 0) for l in g["levels"])
            print(f"    {g['symbol']} [{g['direction']}]: ${total:+.2f}")

    active = load_grids()
    for g in active:
        if g.get("paused"):
            print(f"  [PAUSED] {g['symbol']} — too many fills, waiting pullback")

    # Scan for new entries
    print(f"\n  Scanning {len(PAIRS)} pairs for BB edge...")
    entries = []
    for sym in PAIRS:
        time.sleep(0.3)
        existing = [g for g in active if g["symbol"] == sym and g["status"] == "open"]
        if len(existing) >= 1:
            continue
        sig = scan_entry(sym)
        if sig:
            entries.append(sig)
            print(f"    {sym} [{sig['direction']}] BB:{sig['bb_pos']}% RSI:{sig['rsi']}")

    for sig in entries:
        grid = create_grid(sig)
        print(f"\n  [OPENED] {sig['symbol']} [{sig['direction']}] "
              f"${sig['price']:.4f} | BB:{sig['bb_pos']}% | RSI:{sig['rsi']} | "
              f"{MAX_LEVELS} levels @ {LEVERAGE}x")

    history = load_history()
    s = history["stats"]
    active = load_grids()
    wr = s["wins"] / s["total"] * 100 if s["total"] > 0 else 0
    print(f"\n  [STATUS] PnL: ${s['total_pnl']:+.2f} | Trades: {s['total']} | "
          f"WR: {wr:.0f}% | Active: {len(active)}")


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    args = p.parse_args()

    if args.once:
        run_cycle()
        return

    print(f"Agent Smith [{SMITH_INSTANCE}] | {LEVERAGE}x | step {GRID_STEP*100:.1f}% | "
          f"{MAX_LEVELS} levels", file=sys.stderr)
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
    print("\nAgent Smith stopped.", file=sys.stderr)


if __name__ == "__main__":
    main()
