"""Autonomous Trading Agent — continuous monitoring, volatility routing, strategy adaptation.

Runs in background loop:
  - Checks open positions every 90s
  - Scans for new signals every 3 min
  - Switches to higher-volatility pairs
  - Circuit breaker after 3 consecutive losses
  - Logs all events to journal
"""

import json, signal, sys, time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

# Import the virtual trader functions
sys.path.insert(0, str(Path(__file__).parent))
from virtual_trader import (
    analyze_pair, open_position, check_positions, can_open_more,
    load_positions, load_history, save_history,
    fetch_ticker, fetch_klines, JOURNAL_DIR,
)

HISTORY_FILE = JOURNAL_DIR / "trade_history.json"
AGENT_LOG = JOURNAL_DIR / "agent_log.jsonl"
STATE_FILE = JOURNAL_DIR / "agent_state.json"

CHECK_INTERVAL = 60       # seconds between position checks
SCAN_INTERVAL = 180       # seconds between signal scans
VOLATILITY_THRESHOLD = 1.5  # % — switch if below
MAX_CONSECUTIVE_LOSSES = 3
CIRCUIT_BREAKER_COOLDOWN = 600  # 10 min

running = True


def log_event(event_type, data):
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "type": event_type, "data": data}
    with open(AGENT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"consecutive_losses": 0, "circuit_breaker_until": None,
            "last_scan": None, "scanned_pairs": [],
            "volatility_map": {}, "pair_performance": {}}


def save_state(state):
    state["updated"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def get_pair_volatility(symbol):
    """Calculate recent volatility from 15m candles."""
    candles = fetch_klines(symbol, "15m", 30)
    if len(candles) < 10:
        return 0
    closes = [c["c"] for c in candles]
    high = max(closes[-10:])
    low = min(closes[-10:])
    if low == 0:
        return 0
    return (high - low) / low * 100


def pick_high_volatility_pairs(state, base_pairs, count=12):
    """Select most volatile pairs for scanning."""
    vol_map = {}
    for sym in base_pairs:
        vol = get_pair_volatility(sym)
        vol_map[sym] = round(vol, 2)
        time.sleep(0.3)

    state["volatility_map"] = vol_map
    sorted_pairs = sorted(vol_map.items(), key=lambda x: x[1], reverse=True)
    selected = [p[0] for p in sorted_pairs[:count]]
    state["scanned_pairs"] = selected

    print(f"  Volatility scan: {dict(sorted_pairs[:5])}", file=sys.stderr)
    print(f"  Selected: {', '.join(s[:4] for s in selected[:8])}...", file=sys.stderr)
    return selected


def print_status():
    """Display current trading status."""
    positions = load_positions()
    history = load_history()
    s = history["stats"]
    state = load_state()

    total_pnl = s["total_pnl"]
    wr = s["wins"] / s["total"] * 100 if s["total"] > 0 else 0
    cb = state.get("circuit_breaker_until")
    cb_str = f" | BREAKER: until {cb[:19]}" if cb else ""

    print(f"\n{'='*65}")
    print(f"  AGENT MONITOR — {datetime.now().strftime('%H:%M:%S')} UTC{cb_str}")
    print(f"  PnL: ${total_pnl:+.2f} | Trades: {s['total']} ({s['wins']}W/{s['losses']}L) | "
          f"WR: {wr:.0f}%")
    tf = s.get("total_fees", 0)
    if tf > 0:
        print(f"  Costs: fees ${tf:.2f} | slippage ${s.get('total_slippage', 0):.2f} | "
              f"funding ${s.get('total_funding', 0):.2f}")
    print(f"  Open positions: {len(positions)}/{5}")
    print(f"{'='*65}")

    for p in positions:
        ticker = fetch_ticker(p["symbol"])
        if ticker:
            price = ticker["price"]
            d = p["direction"]
            if d == "long":
                pnl_pct = (price - p["entry_price"]) / p["entry_price"] * p["leverage"] * 100
            else:
                pnl_pct = (p["entry_price"] - price) / p["entry_price"] * p["leverage"] * 100
            dist_tp = abs(p["take_profit"] - price) / price * 100
            dist_sl = abs(price - p["stop_loss"]) / price * 100
            ts = "[TS]" if p.get("trailing_activated") else "     "
            trail_sl = f" TS: ${p['trailing_stop']:.6f}" if p.get("trailing_stop") else ""
            print(f"    {p['symbol']:<10s} {d:5s} {ts} | Entry: ${p['entry_price']:.6f} | "
                  f"Now: ${price:.6f} | PnL: {pnl_pct:+.1f}% | "
                  f"TP: {dist_tp:.1f}% | SL: {dist_sl:.1f}%{trail_sl}")

    # Top volatile pairs
    vm = state.get("volatility_map", {})
    if vm:
        top_vol = sorted(vm.items(), key=lambda x: x[1], reverse=True)[:5]
        print(f"\n  Top volatility: {' | '.join(f'{k[:6]} {v}%' for k, v in top_vol)}")


def monitoring_loop():
    global running
    state = load_state()
    base_pairs = [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT",
        "BNBUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "MATICUSDT", "UNIUSDT",
        "LTCUSDT", "ATOMUSDT", "ARBUSDT", "OPUSDT", "NEARUSDT", "APTUSDT",
        "FILUSDT", "TRXUSDT", "SHIBUSDT", "ETCUSDT", "HBARUSDT", "VETUSDT",
    ]

    log_event("agent_started", {"bankroll": 1000, "risk": "3%", "leverage": 50})

    cycle = 0
    last_scan = 0
    scanned_pairs = base_pairs[:15]

    while running:
        cycle += 1
        now = time.time()

        # Check circuit breaker
        cb = state.get("circuit_breaker_until")
        if cb:
            cb_time = datetime.fromisoformat(cb).timestamp()
            if now < cb_time:
                remaining = int(cb_time - now)
                print(f"\n[CIRCUIT BREAKER] {state['consecutive_losses']} consecutive losses. "
                      f"Cooldown: {remaining}s remaining...", file=sys.stderr)
                time.sleep(min(30, remaining))
                continue
            else:
                state["circuit_breaker_until"] = None
                state["consecutive_losses"] = 0
                print(f"\n[CIRCUIT BREAKER] Reset. Resuming trading.", file=sys.stderr)

        # Check & close positions
        closed = check_positions()
        if closed:
            for c in closed:
                emoji = "WIN" if c["pnl_usd"] > 0 else "LOSS"
                log_event("position_closed", {"symbol": c["symbol"], "direction": c["direction"],
                           "pnl": c["pnl_usd"], "pnl_pct": c["pnl_pct"], "hit_tp": c["hit_tp"]})
                print(f"\n  [{emoji}] {c['symbol']} closed: ${c['pnl_usd']:+.2f} ({c['pnl_pct']:+.1f}%) | "
                      f"{'TP' if c['hit_tp'] else 'SL'} "
                      f"[costs: ${c.get('fees_paid',0) + c.get('slippage_cost',0) + c.get('funding_paid',0):.2f}]", file=sys.stderr)

                # Circuit breaker check
                if c["pnl_usd"] < 0:
                    state["consecutive_losses"] += 1
                    if state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
                        cb_time = datetime.now(timezone.utc)
                        state["circuit_breaker_until"] = (
                            cb_time.replace(second=cb_time.second + CIRCUIT_BREAKER_COOLDOWN).isoformat()
                        )
                        log_event("circuit_breaker", {"losses": state["consecutive_losses"]})
                        print(f"\n[CIRCUIT BREAKER] ACTIVATED after {state['consecutive_losses']} losses.",
                              file=sys.stderr)
                else:
                    state["consecutive_losses"] = 0

            save_state(state)

        # Periodic scan for new signals
        if now - last_scan > SCAN_INTERVAL:
            last_scan = now

            # Pick most volatile pairs
            scanned_pairs = pick_high_volatility_pairs(state, base_pairs, 15)
            save_state(state)

            # Scan for signals
            signals = []
            for sym in scanned_pairs:
                if not can_open_more():
                    break
                time.sleep(0.5)
                sig = analyze_pair(sym)
                if sig and sig["confidence"] >= 0.5:
                    signals.append(sig)

            signals.sort(key=lambda x: (x["confidence"] + x.get("volume_score", 0)), reverse=True)

            for sig in signals:
                if not can_open_more():
                    break
                # Regime filter: only open in allowed direction
                try:
                    from regime_detector import get_current_regime
                    regime = get_current_regime()
                    if sig["direction"] not in regime["allowed_directions"]:
                        continue
                except Exception:
                    pass
                # OB skew: adjust position size based on order book pressure
                try:
                    from vol_monitor import get_ob_skew
                    skew = get_ob_skew(sig["direction"], sig["symbol"])
                    if skew > 0:
                        sig["position_size_usd"] *= (1 + abs(skew))
                        sig["confidence"] = min(1.0, sig["confidence"] + 0.05)
                    elif skew < 0:
                        sig["position_size_usd"] *= (1 - abs(skew) * 0.5)
                        sig["confidence"] = max(0.5, sig["confidence"] - 0.05)
                except Exception:
                    pass
                pos = open_position(sig)
                log_event("position_opened", {"symbol": sig["symbol"], "direction": sig["direction"],
                           "entry": sig["entry"], "tp": sig["take_profit"], "sl": sig["stop_loss"],
                           "confidence": sig["confidence"]})
                print(f"\n  [OPEN] {sig['direction'].upper()} {sig['symbol']} "
                      f"@ ${sig['entry']:.6f} | Conf: {sig['confidence']:.0%} | "
                      f"RSI: {sig['rsi_15m']}", file=sys.stderr)

            if not signals:
                print(f"\n  No signals found in current volatility regime.", file=sys.stderr)

        # Display status every cycle
        if cycle % 2 == 0:
            print_status()

        time.sleep(CHECK_INTERVAL)


def main():
    parser = __import__('argparse').ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--status", action="store_true", help="Show current status only")
    args = parser.parse_args()

    if args.status:
        print_status()
        return

    if args.once:
        check_positions()
        print_status()
        return

    print("Autonomous Trading Agent starting... (Ctrl+C to stop)", file=sys.stderr)
    print(f"Check interval: {CHECK_INTERVAL}s | Scan interval: {SCAN_INTERVAL}s", file=sys.stderr)
    print(f"Max positions: 5 | Circuit breaker: {MAX_CONSECUTIVE_LOSSES} losses", file=sys.stderr)

    def handler(sig, frame):
        global running
        print("\n[AGENT] Shutting down...", file=sys.stderr)
        running = False

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    try:
        monitoring_loop()
    except KeyboardInterrupt:
        print("\n[AGENT] Stopped by user.", file=sys.stderr)

    print_status()
    log_event("agent_stopped", {"reason": "manual"})


if __name__ == "__main__":
    main()
