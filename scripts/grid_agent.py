"""Agent #2 — Grid Trading Strategy (scale-in, reverse TP).

Strategy:
  - 1% risk per grid set (5 levels, increasing position size)
  - BB + RSI on 15m for direction bias
  - Pending buy orders below price, sell orders above
  - SL below last grid level
  - TP grid reverses position (buy TP -> open sell, sell TP -> open buy)
  - Grid spacing = ATR

Bankroll: $1000 virtual | Leverage: 50x | Max grids: 3 pairs simultaneously
"""

import hashlib, json, os, signal, sys, time
from datetime import datetime, timezone
from math import sqrt
from pathlib import Path
from statistics import mean, stdev

import requests

BINANCE_BASE = "https://api.binance.com/api/v3"
UA = "GridBot/2.0"

JOURNAL_DIR = Path(__file__).parent.parent / "trading_journal_grid"
JOURNAL_DIR.mkdir(exist_ok=True)
POSITIONS_FILE = JOURNAL_DIR / "open_grids.json"
HISTORY_FILE = JOURNAL_DIR / "grid_history.json"

BANKROLL = 1000.0
RISK_PER_GRID = 0.01  # 1%
MAX_LEVERAGE = 35
MAX_GRIDS = 3
GRID_LEVELS = 5
LEVEL_WEIGHTS = [0.10, 0.15, 0.20, 0.25, 0.30]  # increasing size
GRID_SPACING_ATR = 1.0  # ATR multiplier for grid spacing
SL_ATR = 2.0  # ATR below last level
TP_ATR = 1.5  # ATR above entry for TP

# ── real-world costs ─────────────────────────────────────────────────────
TAKER_FEE = 0.0004       # 0.04% per side (Binance taker)
SLIPPAGE = 0.001         # 0.1% slippage on fill
FUNDING_RATE = 0.0001    # 0.01% per 8h funding (simplified, prorated)

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
                                 "startTime": start_ms, "endTime": end_ms,
                                 "limit": limit},
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
    if POSITIONS_FILE.exists():
        return json.loads(POSITIONS_FILE.read_text())
    return []


def save_grids(grids):
    POSITIONS_FILE.write_text(json.dumps(grids, indent=2, ensure_ascii=False))


def load_history():
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return {"trades": [], "stats": {"total": 0, "wins": 0, "losses": 0,
            "total_pnl": 0.0, "best_trade": 0.0, "worst_trade": 0.0,
            "total_fees": 0.0, "total_slippage": 0.0, "total_funding": 0.0}}


def save_history(history):
    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False))


def analyze_pair(symbol):
    """Determine direction bias and create grid if signal is strong."""
    c_15 = fetch_klines(symbol, "15m", 100)
    c_5 = fetch_klines(symbol, "5m", 60)

    if len(c_15) < 30:
        return None

    ticker = fetch_ticker(symbol)
    if not ticker or ticker["price"] < 0.01:
        return None

    price = ticker["price"]
    closes = [c["c"] for c in c_15]

    bb = bollinger(closes, 20, 2.0)
    r = rsi(closes, 14)
    a = atr(c_15, 14)
    if not bb or a == 0:
        return None

    # Direction bias
    direction = None
    confidence = 0

    # BUY bias: price near lower BB + oversold
    if bb["pos"] < 30 and r < 40:
        if len(c_5) >= 20:
            c5_closes = [c["c"] for c in c_5]
            bb5 = bollinger(c5_closes, 20, 2.0)
            r5 = rsi(c5_closes, 14)
            if bb5 and bb5["pos"] < 35 and r5 < 45:
                direction = "long"
                confidence = (1 - bb["pos"] / 30) * 0.4 + (1 - r / 40) * 0.3 + 0.3
        else:
            direction = "long"
            confidence = (1 - bb["pos"] / 30) * 0.3 + (1 - r / 40) * 0.3 + 0.2

    # SELL bias
    elif bb["pos"] > 70 and r > 60:
        if len(c_5) >= 20:
            c5_closes = [c["c"] for c in c_5]
            bb5 = bollinger(c5_closes, 20, 2.0)
            r5 = rsi(c5_closes, 14)
            if bb5 and bb5["pos"] > 65 and r5 > 55:
                direction = "short"
                confidence = (bb["pos"] / 100) * 0.4 + (r / 100) * 0.3 + 0.3
        else:
            direction = "short"
            confidence = (bb["pos"] / 100) * 0.3 + (r / 100) * 0.3 + 0.2

    if not direction or confidence < 0.5:
        return None

    # Calculate grid levels
    risk_amount = BANKROLL * RISK_PER_GRID  # $10
    grid_step = a * GRID_SPACING_ATR

    levels = []
    total_weight = sum(LEVEL_WEIGHTS)

    for i in range(GRID_LEVELS):
        weight = LEVEL_WEIGHTS[i] / total_weight
        level_size = risk_amount * weight * MAX_LEVERAGE

        if direction == "long":
            entry = price - grid_step * (i + 1)
            tp = entry + a * TP_ATR
        else:
            entry = price + grid_step * (i + 1)
            tp = entry - a * TP_ATR

        levels.append({
            "level": i + 1,
            "entry": round(entry, 6),
            "tp": round(tp, 6),
            "size_usd": round(level_size, 2),
            "filled": False,
            "tp_hit": False,
        })

    # SL below last level for long, above for short
    if direction == "long":
        sl = levels[-1]["entry"] - a * SL_ATR
    else:
        sl = levels[-1]["entry"] + a * SL_ATR

    return {
        "symbol": symbol,
        "direction": direction,
        "price": price,
        "confidence": round(confidence, 2),
        "atr": round(a, 6),
        "grid_step": round(grid_step, 6),
        "stop_loss": round(sl, 6),
        "risk_usd": round(risk_amount, 2),
        "levels": levels,
        "bb_pos": round(bb["pos"], 1),
        "rsi_15m": round(r, 1),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def create_grid(signal):
    """Open a new grid position."""
    levels_json = []
    for lvl in signal["levels"]:
        levels_json.append({
            "level": lvl["level"],
            "entry": lvl["entry"],
            "tp": lvl["tp"],
            "size_usd": lvl["size_usd"],
            "filled": False,
            "fill_price": None,
            "tp_hit": False,
            "exit_price": None,
            "pnl_usd": None,
        })

    grid = {
        "id": hashlib.md5(f"{signal['symbol']}{signal['timestamp']}".encode()).hexdigest()[:12],
        "symbol": signal["symbol"],
        "direction": signal["direction"],
        "stop_loss": signal["stop_loss"],
        "risk_usd": signal["risk_usd"],
        "confidence": signal["confidence"],
        "opened_at": signal["timestamp"],
        "status": "open",
        "last_checked_at": signal["timestamp"],
        "levels": levels_json,
    }
    grids = load_grids()
    grids.append(grid)
    save_grids(grids)
    return grid


def check_grids():
    """Check all open grids against per-cycle klines (not current price).
    For each grid, fetches 1m candles from last_checked_at to now,
    checking each level for fill, TP, and SL crosses.
    Includes taker fees, slippage, and funding rate."""
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

        # Check each level against each candle
        last_price = klines[-1]["c"] if klines else 0
        for lvl in grid["levels"]:
            if lvl["filled"] and lvl.get("tp_hit"):
                continue  # already resolved

            if not lvl["filled"]:
                # Check if any candle crossed the entry price
                for c in klines:
                    if direction == "long" and c["l"] <= lvl["entry"]:
                        lvl["filled"] = True
                        lvl["fill_price"] = round(c["l"], 8)
                        lvl["fill_time"] = c["t"].isoformat()
                        updated = True
                        break
                    elif direction == "short" and c["h"] >= lvl["entry"]:
                        lvl["filled"] = True
                        lvl["fill_price"] = round(c["h"], 8)
                        lvl["fill_time"] = c["t"].isoformat()
                        updated = True
                        break

            # === TRAILING STOP (per level) ===
            if lvl.get("filled") and not lvl.get("tp_hit") and lvl.get("fill_price") and last_price > 0:
                fill = lvl["fill_price"]
                trail_active = lvl.get("trailing_active", False)
                trail_sl = lvl.get("trailing_sl")

                if direction == "long":
                    # Activate trailing when price > fill + 1.5%
                    if not trail_active and last_price >= fill * 1.015:
                        lvl["trailing_active"] = True
                        trail_active = True
                    if trail_active:
                        new_sl = last_price * 0.992  # trail 0.8% behind
                        if trail_sl is None or new_sl > trail_sl:
                            lvl["trailing_sl"] = round(new_sl, 8)
                            updated = True
                else:  # short
                    if not trail_active and last_price <= fill * 0.985:
                        lvl["trailing_active"] = True
                        trail_active = True
                    if trail_active:
                        new_sl = last_price * 1.008
                        if trail_sl is None or new_sl < trail_sl:
                            lvl["trailing_sl"] = round(new_sl, 8)
                            updated = True

            if lvl["filled"] and not lvl.get("tp_hit"):
                # Check TP
                for c in klines:
                    if direction == "long" and c["h"] >= lvl["tp"]:
                        lvl["tp_hit"] = True
                        real_exit = c["h"]
                        # Apply slippage
                        exit_with_slip = real_exit * (1 - SLIPPAGE)
                        lvl["exit_price"] = round(exit_with_slip, 8)
                        lvl["tp_time"] = c["t"].isoformat()

                        # Gross PnL
                        gross_pnl = (exit_with_slip - lvl["fill_price"]) / lvl["fill_price"] * lvl["size_usd"]
                        # Costs
                        entry_notional = lvl["size_usd"] / MAX_LEVERAGE
                        fee = entry_notional * TAKER_FEE * 2  # open + close
                        slip_cost = lvl["size_usd"] * SLIPPAGE
                        opened_dt = datetime.fromisoformat(grid["opened_at"].replace("Z", "+00:00"))
                        hours_open = max((c["t"] - opened_dt).total_seconds() / 3600, 0)
                        fund_cost = lvl["size_usd"] * FUNDING_RATE * (hours_open / 8)

                        net_pnl = gross_pnl - fee - slip_cost - fund_cost
                        lvl["pnl_usd"] = round(net_pnl, 2)
                        lvl["gross_pnl"] = round(gross_pnl, 2)
                        lvl["fees_paid"] = round(fee, 4)
                        lvl["slippage_cost"] = round(slip_cost, 4)
                        lvl["funding_paid"] = round(fund_cost, 4)
                        updated = True
                        break
                    elif direction == "short" and c["l"] <= lvl["tp"]:
                        lvl["tp_hit"] = True
                        real_exit = c["l"]
                        exit_with_slip = real_exit * (1 + SLIPPAGE)
                        lvl["exit_price"] = round(exit_with_slip, 8)
                        lvl["tp_time"] = c["t"].isoformat()

                        gross_pnl = (lvl["fill_price"] - exit_with_slip) / lvl["fill_price"] * lvl["size_usd"]
                        entry_notional = lvl["size_usd"] / MAX_LEVERAGE
                        fee = entry_notional * TAKER_FEE * 2
                        slip_cost = lvl["size_usd"] * SLIPPAGE
                        opened_dt = datetime.fromisoformat(grid["opened_at"].replace("Z", "+00:00"))
                        hours_open = max((c["t"] - opened_dt).total_seconds() / 3600, 0)
                        fund_cost = lvl["size_usd"] * FUNDING_RATE * (hours_open / 8)

                        net_pnl = gross_pnl - fee - slip_cost - fund_cost
                        lvl["pnl_usd"] = round(net_pnl, 2)
                        lvl["gross_pnl"] = round(gross_pnl, 2)
                        lvl["fees_paid"] = round(fee, 4)
                        lvl["slippage_cost"] = round(slip_cost, 4)
                        lvl["funding_paid"] = round(fund_cost, 4)
                        updated = True
                        break

        # Check SL on candles — global SL AND per-level trailing SL
        any_filled = any(lvl.get("filled") for lvl in grid["levels"])
        if any_filled:
            # Per-level trailing SL check
            for lvl in grid["levels"]:
                if not lvl.get("filled") or lvl.get("tp_hit"):
                    continue
                trail_sl = lvl.get("trailing_sl")
                if not trail_sl:
                    continue
                for c in klines:
                    trail_hit = (direction == "long" and c["l"] <= trail_sl) or \
                               (direction == "short" and c["h"] >= trail_sl)
                    if trail_hit:
                        lvl["tp_hit"] = True  # mark as closed via trailing SL
                        lvl["trail_exit"] = True
                        exit_slip = trail_sl * (1 - SLIPPAGE) if direction == "long" else trail_sl * (1 + SLIPPAGE)
                        lvl["exit_price"] = round(exit_slip, 8)
                        lvl["sl_time"] = c["t"].isoformat()
                        if lvl.get("fill_price"):
                            if direction == "long":
                                gross = (exit_slip - lvl["fill_price"]) / lvl["fill_price"] * lvl["size_usd"]
                            else:
                                gross = (lvl["fill_price"] - exit_slip) / lvl["fill_price"] * lvl["size_usd"]
                            entry_notional = lvl["size_usd"] / MAX_LEVERAGE
                            fee = entry_notional * TAKER_FEE * 2
                            slip_cost = lvl["size_usd"] * SLIPPAGE
                            opened_dt = datetime.fromisoformat(grid["opened_at"].replace("Z", "+00:00"))
                            hours = max((c["t"] - opened_dt).total_seconds() / 3600, 0)
                            fund = lvl["size_usd"] * FUNDING_RATE * (hours / 8)
                            net = gross - fee - slip_cost - fund
                            lvl["pnl_usd"] = round(net, 2)
                            lvl["gross_pnl"] = round(gross, 2)
                            lvl["fees_paid"] = round(fee, 4)
                            lvl["slippage_cost"] = round(slip_cost, 4)
                            lvl["funding_paid"] = round(fund, 4)
                        updated = True
                        break

            for c in klines:
                sl_hit = (direction == "long" and c["l"] <= sl) or (direction == "short" and c["h"] >= sl)
                if sl_hit:
                    grid["status"] = "closed"
                    grid["closed_at"] = c["t"].isoformat()
                    grid["sl_hit"] = True
                    for lvl in grid["levels"]:
                        if lvl["filled"] and not lvl.get("tp_hit"):
                            real_exit = c["l"] if direction == "long" else c["h"]
                            exit_with_slip = real_exit * (1 - SLIPPAGE) if direction == "long" else real_exit * (1 + SLIPPAGE)
                            lvl["exit_price"] = round(exit_with_slip, 8)
                            lvl["sl_time"] = c["t"].isoformat()
                            if lvl["fill_price"]:
                                if direction == "long":
                                    gross_pnl = (exit_with_slip - lvl["fill_price"]) / lvl["fill_price"] * lvl["size_usd"]
                                else:
                                    gross_pnl = (lvl["fill_price"] - exit_with_slip) / lvl["fill_price"] * lvl["size_usd"]
                                entry_notional = lvl["size_usd"] / MAX_LEVERAGE
                                fee = entry_notional * TAKER_FEE * 2
                                slip_cost = lvl["size_usd"] * SLIPPAGE
                                opened_dt = datetime.fromisoformat(grid["opened_at"].replace("Z", "+00:00"))
                                hours_open = max((c["t"] - opened_dt).total_seconds() / 3600, 0)
                                fund_cost = lvl["size_usd"] * FUNDING_RATE * (hours_open / 8)
                                net_pnl = gross_pnl - fee - slip_cost - fund_cost
                                lvl["pnl_usd"] = round(net_pnl, 2)
                                lvl["gross_pnl"] = round(gross_pnl, 2)
                                lvl["fees_paid"] = round(fee, 4)
                                lvl["slippage_cost"] = round(slip_cost, 4)
                                lvl["funding_paid"] = round(fund_cost, 4)
                    updated = True
                    break

        # Check if ALL filled levels have hit TP -> grid complete
        if not grid.get("sl_hit"):
            filled = [l for l in grid["levels"] if l["filled"]]
            all_resolved = all(l.get("tp_hit") for l in filled) if filled else False
            if all_resolved and len(filled) == GRID_LEVELS:
                grid["status"] = "closed"
                grid["closed_at"] = now_utc.isoformat()
                grid["sl_hit"] = False
                updated = True

        # === TRAILING GRID: shift unfilled levels toward price ===
        if grid["status"] == "open" and not grid.get("sl_hit"):
            unfilled = [l for l in grid["levels"] if not l.get("filled")]
            if unfilled and last_price > 0:
                # Check if price moved >1% away from nearest unfilled level
                nearest_dist = min(abs(last_price - l["entry"]) / last_price for l in unfilled)
                last_reposition = grid.get("last_reposition_at", "")
                can_reposition = True
                if last_reposition:
                    last_repos_dt = datetime.fromisoformat(last_reposition.replace("Z", "+00:00"))
                    can_reposition = (now_utc - last_repos_dt).total_seconds() > 300  # 5 min cooldown

                if nearest_dist > 0.01 and can_reposition:
                    # Shift unfilled levels by 50% of the distance toward price
                    shift = (last_price - unfilled[0]["entry"]) * 0.5
                    for l in unfilled:
                        l["entry"] = round(l["entry"] + shift, 6)
                        l["tp"] = round(l["tp"] + shift, 6)
                    # Also shift SL
                    grid["stop_loss"] = round(sl + shift, 6)
                    grid["last_reposition_at"] = now_utc.isoformat()
                    updated = True

        if grid["status"] == "open":
            grid["last_checked_at"] = now_utc.isoformat()
            updated = True

    if updated:
        save_grids(grids)

        for grid in grids:
            if grid["status"] == "closed":
                total_pnl = sum(lvl.get("pnl_usd", 0) or 0 for lvl in grid["levels"])
                total_fees = sum(lvl.get("fees_paid", 0) or 0 for lvl in grid["levels"])
                total_slip = sum(lvl.get("slippage_cost", 0) or 0 for lvl in grid["levels"])
                total_fund = sum(lvl.get("funding_paid", 0) or 0 for lvl in grid["levels"])
                hist = history["stats"]
                hist["total"] += 1
                if total_pnl > 0:
                    hist["wins"] += 1
                    hist["best_trade"] = max(hist["best_trade"], total_pnl)
                else:
                    hist["losses"] += 1
                    hist["worst_trade"] = min(hist["worst_trade"], total_pnl)
                hist["total_pnl"] += total_pnl
                hist["total_fees"] = hist.get("total_fees", 0.0) + total_fees
                hist["total_slippage"] = hist.get("total_slippage", 0.0) + total_slip
                hist["total_funding"] = hist.get("total_funding", 0.0) + total_fund

        active = [g for g in grids if g["status"] == "open"]
        save_grids(active)
        save_history(history)

    return grids


def run_cycle():
    """One full scan + trade cycle."""
    print(f"\n{'='*60}")
    print(f"  GRID AGENT #2 — {datetime.now().strftime('%H:%M:%S')} UTC")
    print(f"  Bankroll: ${BANKROLL} | Risk/grid: 1% | Levels: {GRID_LEVELS} | Leverage: {MAX_LEVERAGE}x")
    print(f"{'='*60}")

    # Check existing grids
    grids = check_grids()
    closed = [g for g in grids if g["status"] == "closed"]
    if closed:
        print(f"\n  [CLOSED] {len(closed)} grid(s):")
        for g in closed:
            total = sum(lvl.get("pnl_usd", 0) or 0 for lvl in g["levels"])
            print(f"    {g['symbol']} {g['direction']}: ${total:+.2f} | {'SL' if g.get('sl_hit') else 'TP'}")

    # Open new grids
    active = load_grids()
    if len(active) >= MAX_GRIDS:
        print(f"  Max grids ({MAX_GRIDS}). Waiting for closures.")
        return

    print(f"\n  Scanning for signals...")
    # Use most volatile pairs for scanning
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
        active = load_grids()
        if len(active) >= MAX_GRIDS:
            break
        try:
            from regime_detector import get_current_regime
            regime = get_current_regime()
            advice = regime.get("agent_advice", {}).get("grid", {})
            if not advice.get("active", True):
                continue
        except Exception:
            pass
        grid = create_grid(sig)
        entries = [l["entry"] for l in sig["levels"]]
        print(f"\n  [OPENED] {sig['direction'].upper()} {sig['symbol']}")
        print(f"    Price: ${sig['price']:.4f} | Conf: {sig['confidence']:.0%} | "
              f"BB: {sig['bb_pos']}% | RSI: {sig['rsi_15m']}")
        print(f"    Grid entries: {[f'${e:.4f}' for e in entries]}")
        print(f"    SL: ${sig['stop_loss']:.4f} | Risk: ${sig['risk_usd']:.2f}")
        opened += 1

    if not opened:
        print(f"  No signals found.")

    # Status
    history = load_history()
    s = history["stats"]
    active = load_grids()
    wr = s["wins"] / s["total"] * 100 if s["total"] > 0 else 0
    print(f"\n  [STATUS] PnL: ${s['total_pnl']:+.2f} | Trades: {s['total']} | "
          f"WR: {wr:.0f}% | Grids open: {len(active)}")


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
        print(f"Grid Agent #2 | PnL: ${s['total_pnl']:+.2f} | {s['total']} trades | "
              f"WR: {wr:.0f}% | Open: {len(grids)}")
        return

    if args.once:
        run_cycle()
        return

    print("Grid Agent #2 starting... (Ctrl+C to stop)", file=sys.stderr)
    running = True

    def handler(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, handler)
    try:
        while running:
            run_cycle()
            time.sleep(30)  # 30s cycle
    except KeyboardInterrupt:
        pass

    print("\nAgent #2 stopped.", file=sys.stderr)


if __name__ == "__main__":
    main()
