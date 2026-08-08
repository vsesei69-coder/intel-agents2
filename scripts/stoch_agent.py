"""Agent #6 — Stochastic Mean-Reversion.

Strategy:
  - Entry: %K crosses %D in oversold/overbought zones
  - Long: %K < 20, crosses %D from below
  - Short: %K > 80, crosses %D from above
  - Vol filter: skip if ATR too low (<0.3%) or too high (>3%)
  - Trailing stop to breakeven, hard SL at 5%
  - Best in ranging/flat markets, bad in strong trends

Params:
  Symbol: ETHUSDT (BTC/SOL/XRP/BNB)
  TF: 15m (1h)
  Leverage: 15x (1-35)
  Position: 95% balance (aggressive)
  Balance: $1000
"""

import hashlib, json, os, signal, sys, time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, stdev

import requests

BINANCE_BASE = "https://api.binance.com/api/v3"
UA = "StochAgent/6.0"

SYMBOL = "ETHUSDT"
INTERVAL = "15m"

JOURNAL_DIR = Path(__file__).parent.parent / "trading_journal_stoch"
JOURNAL_DIR.mkdir(exist_ok=True)
POSITIONS_FILE = JOURNAL_DIR / "open_positions.json"
HISTORY_FILE = JOURNAL_DIR / "stoch_history.json"

BANKROLL = 1000.0
LEVERAGE = 15
POSITION_PCT = 0.95        # 95% of balance
MAX_POSITIONS = 1          # single position
LONG_ENABLED = True
SHORT_ENABLED = True

TAKER_FEE = 0.0004
SLIPPAGE = 0.001
FUNDING_RATE = 0.0001

# Stochastic params
STOCH_K = 14
STOCH_D = 3
OVERSOLD = 20
OVERBOUGHT = 80

# Vol filter
MIN_ATR_PCT = 0.3
MAX_ATR_PCT = 3.0

# Risk
HARD_SL_PCT = 0.05          # 5%
TRAIL_ACTIVATION = 0.01     # 1% profit to activate trailing
TRAIL_OFFSET = 0.003        # 0.3% trailing distance


def fetch_klines(symbol, interval, limit=100):
    try:
        r = requests.get(f"{BINANCE_BASE}/klines",
                         params={"symbol": symbol, "interval": interval, "limit": limit},
                         headers={"User-Agent": UA}, timeout=10)
        if r.status_code != 200: return []
        return [{"o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                 "c": float(k[4]), "v": float(k[5]),
                 "t": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc)}
                for k in r.json()]
    except Exception: return []


def fetch_ticker(symbol):
    try:
        r = requests.get(f"{BINANCE_BASE}/ticker/price?symbol={symbol}",
                         headers={"User-Agent": UA}, timeout=10)
        return float(r.json()["price"]) if r.status_code == 200 else None
    except Exception: return None


def compute_stochastic(highs, lows, closes, k_period=14, d_period=3):
    """Stochastic oscillator: returns %K and %D."""
    if len(closes) < k_period: return None, None

    # Calculate %K for the last candle
    h_high = max(highs[-k_period:])
    l_low = min(lows[-k_period:])
    if h_high == l_low:
        return 50, 50
    k_vals = []
    for i in range(k_period - 1, len(closes)):
        hh = max(highs[i - k_period + 1:i + 1])
        ll = min(lows[i - k_period + 1:i + 1])
        k_vals.append((closes[i] - ll) / (hh - ll) * 100 if hh != ll else 50)

    if len(k_vals) < d_period + 1:
        return k_vals[-1], k_vals[-1]

    k_now = k_vals[-1]
    k_prev = k_vals[-2]
    d_now = mean(k_vals[-d_period:])
    d_prev = mean(k_vals[-d_period - 1:-1])

    return k_now, d_now, k_prev, d_prev


def compute_atr(candles, period=14):
    if len(candles) < period + 1: return 0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return mean(trs[-period:])


def compute_costs(size_usd, hours):
    notion = size_usd / LEVERAGE
    return {"fee": notion * TAKER_FEE * 2, "slip": size_usd * SLIPPAGE,
            "fund": size_usd * FUNDING_RATE * (hours / 8)}


def load_positions():
    if POSITIONS_FILE.exists(): return json.loads(POSITIONS_FILE.read_text())
    return []


def save_positions(p): POSITIONS_FILE.write_text(json.dumps(p, indent=2, ensure_ascii=False))


def load_history():
    if HISTORY_FILE.exists(): return json.loads(HISTORY_FILE.read_text())
    return {"trades": [], "stats": {"total": 0, "wins": 0, "losses": 0,
            "total_pnl": 0.0, "best_trade": 0.0, "worst_trade": 0.0,
            "total_fees": 0.0, "total_slippage": 0.0, "total_funding": 0.0}}


def save_history(h): HISTORY_FILE.write_text(json.dumps(h, indent=2, ensure_ascii=False))


def analyze():
    """Generate stochastic signal. Returns signal dict or None."""
    candles = fetch_klines(SYMBOL, INTERVAL, 50)
    if len(candles) < 25:
        return None

    price = candles[-1]["c"]
    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    closes = [c["c"] for c in candles]

    # Volatility filter
    atr = compute_atr(candles, 14)
    atr_pct = atr / price * 100 if price > 0 else 0
    if atr_pct < MIN_ATR_PCT or atr_pct > MAX_ATR_PCT:
        return None  # vol too low or too high — skip

    # Stochastic
    stoch = compute_stochastic(highs, lows, closes, STOCH_K, STOCH_D)
    if stoch is None:
        return None

    k_now, d_now, k_prev, d_prev = stoch

    direction = None
    confidence = 0

    # LONG: %K crosses %D from below in oversold zone
    if LONG_ENABLED and k_prev < d_prev and k_now > d_now and k_now < OVERSOLD:
        direction = "long"
        confidence = min(0.9, (OVERSOLD - k_now) / OVERSOLD * 0.5 + 0.5)

    # SHORT: %K crosses %D from above in overbought zone
    elif SHORT_ENABLED and k_prev > d_prev and k_now < d_now and k_now > OVERBOUGHT:
        direction = "short"
        confidence = min(0.9, (k_now - OVERBOUGHT) / (100 - OVERBOUGHT) * 0.5 + 0.5)

    if not direction:
        return None

    # Position sizing
    size_usd = BANKROLL * POSITION_PCT * LEVERAGE

    # Hard SL at 5%
    sl = price * (1 - HARD_SL_PCT) if direction == "long" else price * (1 + HARD_SL_PCT)
    tp = price * 1.03 if direction == "long" else price * 0.97  # 3% TP

    return {
        "symbol": SYMBOL, "direction": direction, "price": price,
        "confidence": round(confidence, 2), "size_usd": round(size_usd, 2),
        "entry": price, "take_profit": round(tp, 4), "stop_loss": round(sl, 4),
        "leverage": LEVERAGE, "atr_pct": round(atr_pct, 2),
        "stoch_k": round(k_now, 1), "stoch_d": round(d_now, 1),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def open_position(signal):
    pos = {
        "id": hashlib.md5(f"{signal['symbol']}{signal['timestamp']}".encode()).hexdigest()[:12],
        "symbol": signal["symbol"], "direction": signal["direction"],
        "entry_price": signal["entry"], "take_profit": signal["take_profit"],
        "stop_loss": signal["stop_loss"], "size_usd": signal["size_usd"],
        "leverage": signal["leverage"], "confidence": signal["confidence"],
        "opened_at": signal["timestamp"], "status": "open",
        "closed_at": None, "exit_price": None, "pnl_usd": None, "pnl_pct": None,
        "hit_tp": False, "hit_sl": False, "trailing_active": False, "trailing_sl": None,
        "fees_paid": 0, "slippage_cost": 0, "funding_paid": 0,
    }
    save_positions([pos])
    return pos


def check_position():
    """Monitor open position: TP, SL, trailing stop."""
    positions = load_positions()
    history = load_history()
    updated = False

    for pos in positions:
        if pos["status"] != "open":
            continue

        ticker = fetch_ticker(SYMBOL)
        if not ticker:
            continue

        price = ticker
        direction = pos["direction"]
        entry = pos["entry_price"]
        tp = pos["take_profit"]
        sl = pos["stop_loss"]
        size = pos["size_usd"]

        # === TRAILING STOP ===
        if not pos["trailing_active"]:
            if direction == "long" and price >= entry * (1 + TRAIL_ACTIVATION):
                pos["trailing_active"] = True
                pos["trailing_sl"] = round(entry + TRAIL_OFFSET, 4)
                updated = True
            elif direction == "short" and price <= entry * (1 - TRAIL_ACTIVATION):
                pos["trailing_active"] = True
                pos["trailing_sl"] = round(entry - TRAIL_OFFSET, 4)
                updated = True

        if pos["trailing_active"]:
            trail_sl = pos["trailing_sl"]
            if direction == "long":
                new_sl = round(price * (1 - TRAIL_OFFSET), 4)
                if new_sl > trail_sl:
                    pos["trailing_sl"] = new_sl
                    updated = True
            else:
                new_sl = round(price * (1 + TRAIL_OFFSET), 4)
                if new_sl < trail_sl:
                    pos["trailing_sl"] = new_sl
                    updated = True

        effective_sl = pos.get("trailing_sl", sl)

        # Check exit conditions
        tp_hit = (direction == "long" and price >= tp) or (direction == "short" and price <= tp)
        sl_hit = (direction == "long" and price <= effective_sl) or (direction == "short" and price >= effective_sl)

        if tp_hit or sl_hit:
            exit_price = tp if tp_hit else effective_sl
            exit_slip = exit_price * (1 - SLIPPAGE) if direction == "long" else exit_price * (1 + SLIPPAGE)

            pos["exit_price"] = round(exit_slip, 4)
            pos["hit_tp"] = tp_hit
            pos["hit_sl"] = sl_hit
            pos["status"] = "closed"
            pos["closed_at"] = datetime.now(timezone.utc).isoformat()

            pct = (exit_slip - entry) / entry if direction == "long" else (entry - exit_slip) / entry
            gross = size * pct * LEVERAGE
            opened_dt = datetime.fromisoformat(pos["opened_at"].replace("Z", "+00:00"))
            hours = max((datetime.now(timezone.utc) - opened_dt).total_seconds() / 3600, 0)
            costs = compute_costs(size, hours)
            net = gross - costs["fee"] - costs["slip"] - costs["fund"]

            pos["pnl_usd"] = round(net, 2)
            pos["pnl_pct"] = round(pct * LEVERAGE * 100, 2)
            pos["fees_paid"] = round(costs["fee"], 4)
            pos["slippage_cost"] = round(costs["slip"], 4)
            pos["funding_paid"] = round(costs["fund"], 4)

            updated = True

            # Update history
            history["trades"].append(pos)
            s = history["stats"]
            s["total"] += 1
            if net > 0: s["wins"] += 1; s["best_trade"] = max(s["best_trade"], net)
            else: s["losses"] += 1; s["worst_trade"] = min(s["worst_trade"], net)
            s["total_pnl"] += net
            s["total_fees"] = s.get("total_fees", 0) + costs["fee"]
            s["total_slippage"] = s.get("total_slippage", 0) + costs["slip"]
            s["total_funding"] = s.get("total_funding", 0) + costs["fund"]

    if updated:
        active = [p for p in positions if p["status"] == "open"]
        save_positions(active)
        save_history(history)

    return positions


def run_cycle():
    price = fetch_ticker(SYMBOL)
    positions = check_position()

    print(f"\n{'='*55}")
    print(f"  STOCHASTIC AGENT #6 — {datetime.now().strftime('%H:%M:%S')} UTC")
    print(f"  {SYMBOL} ${price:.2f} | {INTERVAL} | {LEVERAGE}x | Long:{LONG_ENABLED} Short:{SHORT_ENABLED}")
    print(f"{'='*55}")

    active = load_positions()
    if active:
        for p in active:
            trail_info = f"TS: ${p['trailing_sl']:.2f}" if p.get("trailing_sl") else "no trail"
            print(f"  [{p['direction'].upper()}] Entry: ${p['entry_price']:.2f} | "
                  f"Now: ${price:.2f} | TP: ${p['take_profit']:.2f} | "
                  f"SL: ${p['stop_loss']:.2f} | {trail_info}")
    else:
        # Generate new signal
        signal = analyze()
        if signal:
            if signal["direction"] == "long":
                pos = open_position(signal)
                print(f"\n  [OPEN LONG] Entry: ${signal['entry']:.2f} | "
                      f"TP: ${signal['take_profit']:.2f} | SL: ${signal['stop_loss']:.2f}")
                print(f"  Size: ${signal['size_usd']:,.0f} | Conf: {signal['confidence']:.0%} | "
                      f"Stoch K:{signal['stoch_k']:.0f} D:{signal['stoch_d']:.0f} | ATR:{signal['atr_pct']:.1f}%")
            else:
                pos = open_position(signal)
                print(f"\n  [OPEN SHORT] Entry: ${signal['entry']:.2f} | "
                      f"TP: ${signal['take_profit']:.2f} | SL: ${signal['stop_loss']:.2f}")
                print(f"  Size: ${signal['size_usd']:,.0f} | Conf: {signal['confidence']:.0%} | "
                      f"Stoch K:{signal['stoch_k']:.0f} D:{signal['stoch_d']:.0f} | ATR:{signal['atr_pct']:.1f}%")
        else:
            # Show why no signal
            candles = fetch_klines(SYMBOL, INTERVAL, 30)
            if candles:
                atr = compute_atr(candles, 14)
                atr_pct = atr / candles[-1]["c"] * 100
                if atr_pct < MIN_ATR_PCT or atr_pct > MAX_ATR_PCT:
                    print(f"  No signal: ATR {atr_pct:.1f}% outside range ({MIN_ATR_PCT}-{MAX_ATR_PCT}%)")
                else:
                    stoch = compute_stochastic(
                        [c["h"] for c in candles], [c["l"] for c in candles],
                        [c["c"] for c in candles], STOCH_K, STOCH_D)
                    if stoch:
                        k, d = stoch[0], stoch[1]
                        print(f"  No signal: Stoch K:{k:.0f} D:{d:.0f} (oversold<{OVERSOLD}, overbought>{OVERBOUGHT})")
                    else:
                        print(f"  No signal: waiting for data")
            else:
                print(f"  No signal: API unavailable")

    history = load_history()
    s = history["stats"]
    wr = s["wins"] / s["total"] * 100 if s["total"] > 0 else 0
    print(f"\n  [STATUS] PnL: ${s['total_pnl']:+,.2f} | {s['total']} trades | WR: {wr:.0f}% | "
          f"Balance: ${BANKROLL + s['total_pnl']:,.2f}")


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--status", action="store_true")
    p.add_argument("--long-only", action="store_true")
    p.add_argument("--short-only", action="store_true")
    p.add_argument("--symbol", default=None)
    p.add_argument("--tf", default=None)
    p.add_argument("--lev", type=int, default=None)
    args = p.parse_args()

    global SYMBOL, INTERVAL, LEVERAGE, LONG_ENABLED, SHORT_ENABLED
    if args.symbol: SYMBOL = args.symbol
    if args.tf: INTERVAL = args.tf
    if args.lev: LEVERAGE = args.lev
    if args.long_only: SHORT_ENABLED = False
    if args.short_only: LONG_ENABLED = False

    if args.status:
        pos = load_positions()
        history = load_history()
        s = history["stats"]
        wr = s["wins"] / s["total"] * 100 if s["total"] > 0 else 0
        price = fetch_ticker(SYMBOL)
        print(f"Stoch Agent #6 | {SYMBOL} {INTERVAL} {LEVERAGE}x | "
              f"PnL: ${s['total_pnl']:+,.2f} | {s['total']} trades | WR: {wr:.0f}%")
        if pos:
            for p in pos:
                print(f"  [{p['direction']}] Entry: ${p['entry_price']:.2f}")
        return

    if args.once:
        run_cycle()
        return

    print(f"Stochastic Agent #6 starting on {SYMBOL} {INTERVAL} {LEVERAGE}x...", file=sys.stderr)
    running = True
    def handler(sig, frame): nonlocal running; running = False
    signal.signal(signal.SIGINT, handler)
    try:
        while running:
            run_cycle()
            time.sleep(30)
    except KeyboardInterrupt: pass
    print("\nAgent #6 stopped.", file=sys.stderr)


if __name__ == "__main__":
    main()
