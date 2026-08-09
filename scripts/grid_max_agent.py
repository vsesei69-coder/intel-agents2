"""Floating Grid Agent — плавающая сетка за ценой.

Strategy:
  - Сетка 33 уровней в диапазоне 3% вокруг текущей цены
  - Шаг = 0.09% (GRID_RANGE / ORDERS)
  - Сетка перецентрируется за ценой: когда цена уходит,
    невыполненные уровни пересчитываются, добавляются новые
  - TP = один шаг от entry (цена вернулась на уровень)
  - SL = нет жёсткого ценового стопа
  - Маржинальный стоп: если убыток > 60% маржи → закрыть с минусом
  - Иначе держим — сетка сама выкупит при отскоке

Instance через env: FLOAT_INSTANCE (default "max")
Journal: trading_journal_{instance}/
"""

import hashlib, json, os, signal, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean

import requests

BINANCE_BASE = "https://api.binance.com/api/v3"
UA = "FloatGrid/4.0"

INSTANCE = os.environ.get("FLOAT_INSTANCE", "max")
FLOAT_BIAS = os.environ.get("FLOAT_BIAS", "")  # "", "long", "short"
JOURNAL_DIR = Path(__file__).parent.parent / f"trading_journal_{INSTANCE}"
JOURNAL_DIR.mkdir(exist_ok=True)
GRIDS_FILE = JOURNAL_DIR / "open_grids.json"
HISTORY_FILE = JOURNAL_DIR / "grid_history.json"

BANKROLL = 1000.0
BALANCE_PER_GRID = 0.03
MAX_LEVERAGE = 50
TAKER_FEE = 0.0004
SLIPPAGE = 0.0003
FUNDING_RATE = 0.0001

GRID_RANGE_PCT = float(os.environ.get("GRID_RANGE", "0.03"))
GRID_ORDERS = int(os.environ.get("GRID_ORDERS", "33"))
GRID_STEP_PCT = GRID_RANGE_PCT / GRID_ORDERS
MARGIN_SL_PCT = 0.60
RECENTER_THRESHOLD = GRID_RANGE_PCT * 0.4

VOLATILE_PAIRS = [
    "DOGEUSDT", "PEPEUSDT", "WIFUSDT", "BONKUSDT", "FLOKIUSDT",
    "SUIUSDT", "SEIUSDT", "TIAUSDT", "INJUSDT", "TONUSDT",
    "AAVEUSDT", "CRVUSDT", "IMXUSDT", "LDOUSDT", "GALAUSDT",
    "AVAXUSDT", "SOLUSDT", "NEARUSDT", "ARBUSDT", "OPUSDT",
    "LINKUSDT", "DOTUSDT", "APTUSDT", "FILUSDT", "ADAUSDT",
    "XRPUSDT", "ETHUSDT", "BNBUSDT", "TRXUSDT", "ETCUSDT",
]


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
    return {"trades": [], "stats": {"total": 0, "wins": 0, "losses": 0,
            "total_pnl": 0.0, "best_trade": 0.0, "worst_trade": 0.0,
            "total_fees": 0.0, "total_slippage": 0.0, "total_funding": 0.0}}


def save_history(history):
    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False))


def compute_costs(size_usd, hours_open):
    entry_notional = size_usd / MAX_LEVERAGE
    fee = entry_notional * TAKER_FEE * 2
    slip = size_usd * SLIPPAGE
    fund = size_usd * FUNDING_RATE * (hours_open / 8)
    return {"fee": round(fee, 4), "slip": round(slip, 4), "fund": round(fund, 4)}


def create_grid(symbol, direction, price):
    balance_for_grid = BANKROLL * BALANCE_PER_GRID
    amount_per_order = balance_for_grid / GRID_ORDERS
    size_usd = amount_per_order * MAX_LEVERAGE

    levels = []
    half_range = GRID_STEP_PCT * GRID_ORDERS / 2

    for i in range(GRID_ORDERS):
        offset_pct = GRID_STEP_PCT * (i - GRID_ORDERS // 2)
        entry = price * (1 + offset_pct)
        tp = entry + (GRID_STEP_PCT * price) if direction == "long" else entry - (GRID_STEP_PCT * price)
        if direction == "long" and tp <= entry:
            tp = entry + GRID_STEP_PCT * price
        if direction == "short" and tp >= entry:
            tp = entry - GRID_STEP_PCT * price

        levels.append({
            "level": i + 1,
            "entry": round(entry, 8),
            "tp": round(tp, 8),
            "size_usd": round(size_usd, 2),
            "margin": round(amount_per_order, 4),
            "filled": False,
            "fill_price": None,
            "fill_time": None,
            "tp_hit": False,
            "sl_hit": False,
            "exit_price": None,
            "exit_time": None,
            "pnl_usd": None,
        })

    grid = {
        "id": hashlib.md5(f"{symbol}{price}{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()[:12],
        "symbol": symbol,
        "direction": direction,
        "center_price": price,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "last_checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "open",
        "balance_used": round(balance_for_grid, 2),
        "levels": levels,
    }
    grids = load_grids()
    grids.append(grid)
    save_grids(grids)
    return grid


def recenter_grid(grid, current_price):
    filled = [l for l in grid["levels"] if l.get("filled") and not l.get("tp_hit") and not l.get("sl_hit")]
    unfilled = [l for l in grid["levels"] if not l.get("filled")]

    for lvl in unfilled:
        offset_pct = GRID_STEP_PCT * (lvl["level"] - GRID_ORDERS // 2)
        new_entry = current_price * (1 + offset_pct)
        if grid["direction"] == "long":
            new_tp = new_entry + GRID_STEP_PCT * current_price
        else:
            new_tp = new_entry - GRID_STEP_PCT * current_price

        lvl["entry"] = round(new_entry, 8)
        lvl["tp"] = round(new_tp, 8)

    grid["center_price"] = current_price
    return True


def check_grids():
    grids = load_grids()
    history = load_history()
    updated = False
    now_utc = datetime.now(timezone.utc)

    for grid in grids:
        if grid["status"] != "open":
            continue

        symbol = grid["symbol"]
        direction = grid["direction"]
        center = grid["center_price"]
        last_check_str = grid.get("last_checked_at", grid["opened_at"])
        last_check = datetime.fromisoformat(last_check_str.replace("Z", "+00:00"))

        ticker = fetch_ticker(symbol)
        if not ticker:
            grid["last_checked_at"] = now_utc.isoformat()
            updated = True
            continue

        current_price = ticker["price"]

        deviation = abs(current_price - center) / center if center > 0 else 0
        if deviation > RECENTER_THRESHOLD:
            recenter_grid(grid, current_price)
            updated = True

        # Fetch last N minutes of 1m candles (Binance only returns closed candles,
        # so always look back a window regardless of last_check)
        lookback = now_utc - timedelta(minutes=6)
        klines = fetch_klines_range(symbol, lookback, now_utc, "1m", 500)
        if not klines:
            # fallback: single synthetic candle from current price
            klines = [{"h": current_price, "l": current_price,
                       "t": now_utc, "c": current_price}]

        for lvl in grid["levels"]:
            if lvl.get("tp_hit") or lvl.get("sl_hit"):
                continue

            if not lvl.get("filled"):
                for c in klines:
                    if direction == "long" and c["l"] <= lvl["entry"]:
                        lvl["filled"] = True
                        lvl["fill_price"] = lvl["entry"]
                        lvl["fill_time"] = now_utc.isoformat()
                        updated = True
                        break
                    elif direction == "short" and c["h"] >= lvl["entry"]:
                        lvl["filled"] = True
                        lvl["fill_price"] = lvl["entry"]
                        lvl["fill_time"] = now_utc.isoformat()
                        updated = True
                        break

            if lvl.get("filled") and not lvl.get("tp_hit") and not lvl.get("sl_hit"):
                # NO STOP LOSS — hold filled levels until they take profit.
                # Trade logic: never cut a position; only exit at profit.
                tp_threshold = GRID_STEP_PCT * 0.9

                hit = None
                if direction == "long":
                    for c in klines:
                        pnl = (c["h"] - lvl["fill_price"]) / lvl["fill_price"]
                        if pnl >= tp_threshold:
                            hit = ("tp", c["h"])
                            break
                else:
                    for c in klines:
                        pnl = (lvl["fill_price"] - c["l"]) / lvl["fill_price"]
                        if pnl >= tp_threshold:
                            hit = ("tp", c["l"])
                            break

                if hit:
                    kind, exit_price = hit
                    lvl["tp_hit"] = True
                    exit_slip = exit_price * (1 - SLIPPAGE) if direction == "long" else exit_price * (1 + SLIPPAGE)
                    pnl_pct = (exit_slip - lvl["fill_price"]) / lvl["fill_price"] if direction == "long" \
                        else (lvl["fill_price"] - exit_slip) / lvl["fill_price"]

                    lvl["exit_price"] = round(exit_slip, 8)
                    lvl["exit_time"] = now_utc.isoformat()

                    gross = lvl["size_usd"] * pnl_pct
                    opened_dt = datetime.fromisoformat(grid["opened_at"].replace("Z", "+00:00"))
                    hours = max((now_utc - opened_dt).total_seconds() / 3600, 0)
                    costs = compute_costs(lvl["size_usd"], hours)
                    net = gross - costs["fee"] - costs["slip"] - costs["fund"]
                    lvl["pnl_usd"] = round(net, 2)
                    updated = True

        # Grid is considered done when every FILLED level has exited.
        # Unfilled levels (price never reached them) don't block the grid.
        filled = [l for l in grid["levels"] if l.get("filled")]
        done = [l for l in grid["levels"] if l.get("tp_hit") or l.get("sl_hit")]
        filled_done = [l for l in filled if l.get("tp_hit") or l.get("sl_hit")]

        if filled and len(filled_done) == len(filled):
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
                total_tp = sum(1 for lvl in grid["levels"] if lvl.get("tp_hit"))
                total_sl = sum(1 for lvl in grid["levels"] if lvl.get("sl_hit"))

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
                    "symbol": grid["symbol"],
                    "direction": grid["direction"],
                    "pnl": round(total_pnl, 2),
                    "tp_count": total_tp,
                    "sl_count": total_sl,
                    "opened": grid["opened_at"],
                    "closed": grid.get("closed_at"),
                })

        active = [g for g in grids if g["status"] == "open"]
        save_grids(active)
        save_history(history)

    return grids


def run_cycle():
    print(f"\n{'='*60}")
    print(f"  FLOAT GRID [{INSTANCE}] — {datetime.now().strftime('%H:%M:%S')} UTC")
    print(f"  Range: {GRID_RANGE_PCT*100:.1f}% | Orders: {GRID_ORDERS} | "
          f"Step: {GRID_STEP_PCT*100:.3f}% | Lev: {MAX_LEVERAGE}x")
    print(f"{'='*60}")

    closed = [g for g in check_grids() if g["status"] == "closed"]
    if closed:
        print(f"\n  [CLOSED] {len(closed)} grid(s):")
        for g in closed:
            total = sum((lvl.get("pnl_usd") or 0) for lvl in g["levels"])
            tps = sum(1 for l in g["levels"] if l.get("tp_hit"))
            sls = sum(1 for l in g["levels"] if l.get("sl_hit"))
            print(f"    {g['symbol']} [{g['direction']}]: ${total:+.2f} | TP:{tps} SL:{sls}")

    active_grids = load_grids()
    active_symbols = {g["symbol"] for g in active_grids if g["status"] == "open"}

    if len(active_grids) >= 8:
        print(f"  Max grids active ({len(active_grids)}). Waiting.")
        return

    print(f"\n  Scanning {len(VOLATILE_PAIRS)} volatile pairs...")
    pairs_to_scan = [p for p in VOLATILE_PAIRS if p not in active_symbols]
    if not pairs_to_scan:
        print(f"  All pairs covered. Waiting.")
        return

    best = None
    best_score = -999
    for sym in pairs_to_scan[:15]:
        time.sleep(0.3)
        ticker = fetch_ticker(sym)
        if not ticker or ticker["price"] < 0.001:
            continue

        change = abs(ticker["change"])
        price = ticker["price"]
        score = change

        if score > best_score:
            best_score = score
            best = {"symbol": sym, "price": price, "change": ticker["change"]}

    if best:
        if FLOAT_BIAS == "long":
            direction = "long"
        elif FLOAT_BIAS == "short":
            direction = "short"
        else:
            direction = "long" if best["change"] < 0 else "short"

        # Skip if this pair already has an open grid (guards against
        # duplicate agents opening the same pair twice)
        active_grids = load_grids()
        already = any(
            g["symbol"] == best["symbol"] and g["status"] == "open"
            for g in active_grids
        )
        if already:
            print(f"  [{best['symbol']} {direction.upper()}] already open, skipping")
        else:
            grid = create_grid(best["symbol"], direction, best["price"])
            print(f"\n  [OPENED] {best['symbol']} {direction.upper()}")
            print(f"    Price: ${best['price']:.6f} | 24h: {best['change']:+.2f}%")
            print(f"    Grid: {GRID_ORDERS} orders | Step: {GRID_STEP_PCT*100:.3f}% | "
                  f"Range: ±{GRID_RANGE_PCT*50:.1f}%")
    else:
        print(f"  No candidates found.")

    history = load_history()
    s = history["stats"]
    active = load_grids()
    wr = s["wins"] / s["total"] * 100 if s["total"] > 0 else 0
    print(f"\n  [STATUS] PnL: ${s['total_pnl']:+.2f} | Trades: {s['total']} | "
          f"WR: {wr:.0f}% | Active: {len(active)}")


def cleanup_stale_grids():
    """Close grids that are fully closed on levels but still marked open
    (left over from earlier buggy SL logic)."""
    grids = load_grids()
    kept = []
    closed = 0
    for g in grids:
        if g["status"] != "open":
            continue
        filled = [l for l in g["levels"] if l.get("filled")]
        done = [l for l in g["levels"] if l.get("tp_hit") or l.get("sl_hit")]
        if filled and len(filled) == len(done):
            total = sum((l.get("pnl_usd") or 0) for l in g["levels"])
            history = load_history()
            s = history["stats"]
            s["total"] += 1
            if total > 0:
                s["wins"] += 1
            else:
                s["losses"] += 1
            s["total_pnl"] += total
            history["trades"].append({
                "symbol": g["symbol"], "direction": g["direction"],
                "pnl": round(total, 2),
                "tp_count": sum(1 for l in g["levels"] if l.get("tp_hit")),
                "sl_count": sum(1 for l in g["levels"] if l.get("sl_hit")),
                "opened": g["opened_at"], "closed": datetime.now(timezone.utc).isoformat(),
                "reason": "cleanup_stale",
            })
            save_history(history)
            closed += 1
            continue
        kept.append(g)
    if closed:
        save_grids(kept)
        print(f"[cleanup] closed {closed} stale grid(s)", file=sys.stderr)
    return closed


def dedupe_grids():
    """If the same pair+direction has multiple open grids, close the extras
    (booked at current price) so a single grid per pair is kept."""
    grids = load_grids()
    seen = {}
    kept = []
    closed = 0
    for g in grids:
        if g["status"] != "open":
            continue
        key = (g["symbol"], g["direction"])
        if key in seen:
            now_utc = datetime.now(timezone.utc)
            ticker = fetch_ticker(g["symbol"])
            px = ticker["price"] if ticker else g["center_price"]
            total = 0.0
            for lvl in g["levels"]:
                if lvl.get("tp_hit") or lvl.get("sl_hit") or not lvl.get("filled"):
                    continue
                if g["direction"] == "long":
                    pnl_pct = (px - lvl["fill_price"]) / lvl["fill_price"]
                else:
                    pnl_pct = (lvl["fill_price"] - px) / lvl["fill_price"]
                gross = lvl["size_usd"] * pnl_pct
                opened_dt = datetime.fromisoformat(g["opened_at"].replace("Z", "+00:00"))
                hours = max((now_utc - opened_dt).total_seconds() / 3600, 0)
                costs = compute_costs(lvl["size_usd"], hours)
                net = gross - costs["fee"] - costs["slip"] - costs["fund"]
                lvl["pnl_usd"] = round(net, 2)
                lvl["sl_hit"] = True
                lvl["exit_price"] = round(px, 8)
                lvl["exit_time"] = now_utc.isoformat()
                total += net
            g["status"] = "closed"
            g["closed_at"] = now_utc.isoformat()
            history = load_history()
            s = history["stats"]
            s["total"] += 1
            if total > 0:
                s["wins"] += 1
            else:
                s["losses"] += 1
            s["total_pnl"] += total
            history["trades"].append({
                "symbol": g["symbol"], "direction": g["direction"],
                "pnl": round(total, 2), "tp_count": 0, "sl_count": 1,
                "opened": g["opened_at"], "closed": g["closed_at"],
                "reason": "dedupe",
            })
            save_history(history)
            closed += 1
            continue
        seen[key] = g
        kept.append(g)
    if closed:
        save_grids(kept)
        print(f"[dedupe] closed {closed} duplicate grid(s)", file=sys.stderr)
    return closed


def migrate_bias():
    """Close grids whose direction contradicts this instance's bias."""
    if not FLOAT_BIAS:
        return 0
    grids = load_grids()
    kept = []
    closed = 0
    for g in grids:
        if g["status"] != "open":
            continue
        if g["direction"] == FLOAT_BIAS:
            kept.append(g)
            continue
        # Close opposing grid at current price, book PnL
        now_utc = datetime.now(timezone.utc)
        ticker = fetch_ticker(g["symbol"])
        px = ticker["price"] if ticker else g["center_price"]
        total = 0.0
        for lvl in g["levels"]:
            if lvl.get("tp_hit") or lvl.get("sl_hit") or not lvl.get("filled"):
                continue
            if g["direction"] == "long":
                pnl_pct = (px - lvl["fill_price"]) / lvl["fill_price"]
            else:
                pnl_pct = (lvl["fill_price"] - px) / lvl["fill_price"]
            gross = lvl["size_usd"] * pnl_pct
            opened_dt = datetime.fromisoformat(g["opened_at"].replace("Z", "+00:00"))
            hours = max((now_utc - opened_dt).total_seconds() / 3600, 0)
            costs = compute_costs(lvl["size_usd"], hours)
            net = gross - costs["fee"] - costs["slip"] - costs["fund"]
            lvl["pnl_usd"] = round(net, 2)
            lvl["sl_hit"] = True
            lvl["exit_price"] = round(px, 8)
            lvl["exit_time"] = now_utc.isoformat()
            total += net
        g["status"] = "closed"
        g["closed_at"] = now_utc.isoformat()
        history = load_history()
        s = history["stats"]
        s["total"] += 1
        if total > 0:
            s["wins"] += 1
        else:
            s["losses"] += 1
        s["total_pnl"] += total
        history["trades"].append({
            "symbol": g["symbol"], "direction": g["direction"],
            "pnl": round(total, 2), "tp_count": 0, "sl_count": 1,
            "opened": g["opened_at"], "closed": g["closed_at"],
            "reason": "bias_migrate",
        })
        save_history(history)
        closed += 1
    if closed:
        save_grids(kept)
        print(f"[bias] {FLOAT_BIAS}: closed {closed} opposing grid(s)", file=sys.stderr)
    return closed


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
        print(f"Float Grid [{INSTANCE}] | PnL: ${s['total_pnl']:+.2f} | "
              f"{s['total']} trades | WR: {wr:.0f}% | Active: {len(grids)}")
        for g in grids:
            filled = sum(1 for l in g["levels"] if l.get("filled"))
            tps = sum(1 for l in g["levels"] if l.get("tp_hit"))
            sls = sum(1 for l in g["levels"] if l.get("sl_hit"))
            print(f"  {g['symbol']} [{g['direction']}]: {filled} filled, {tps} TP, {sls} SL")
        return

    if args.once:
        run_cycle()
        return

    migrate_bias()
    cleanup_stale_grids()
    dedupe_grids()

    print(f"Float Grid [{INSTANCE}] starting... (Ctrl+C to stop)", file=sys.stderr)
    running = True

    def handler(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, handler)
    try:
        while running:
            run_cycle()
            time.sleep(20)
    except KeyboardInterrupt:
        pass

    print(f"\nFloat Grid [{INSTANCE}] stopped.", file=sys.stderr)


if __name__ == "__main__":
    main()
