"""Spillover Engine — proactive volatility anticipation for all agents.

Technique: Volatility Spillover / Предвестник волатильности.
  - ETH and LINK vol spikes predict XRP/ADA/SOL vol ~1h later
  - When spillover detected → place pending orders BEFORE the move
  - Used by all grid/corridor agents to anticipate vol expansion

Usage from any agent:
  from spillover import check_spillover, get_anticipation_levels
  signal = check_spillover("XRPUSDT")
  if signal["active"]:
      levels = get_anticipation_levels("XRPUSDT", signal)
      # Place tighter grid, wider TP
"""

import json, sys, time
from datetime import datetime, timezone
from pathlib import Path

import requests

BINANCE = "https://api.binance.com/api/v3"
UA = "Spillover/1.0"

STATE_FILE = Path(__file__).parent.parent / "trading_journal" / "spillover_state.json"

# Known leader-follower pairs (from our research)
SPILLOVER_MAP = {
    "XRPUSDT": {"leaders": ["ETHUSDT", "LINKUSDT"], "lag_minutes": 60, "correlation": 0.87},
    "ADAUSDT": {"leaders": ["ETHUSDT", "BTCUSDT"], "lag_minutes": 30, "correlation": 0.72},
    "SOLUSDT": {"leaders": ["BTCUSDT"], "lag_minutes": 15, "correlation": 0.88},
    "UNIUSDT": {"leaders": ["ETHUSDT"], "lag_minutes": 45, "correlation": 0.74},
    "AVAXUSDT": {"leaders": ["BTCUSDT", "ETHUSDT"], "lag_minutes": 45, "correlation": 0.70},
    "DOTUSDT": {"leaders": ["ETHUSDT"], "lag_minutes": 30, "correlation": 0.76},
    "LINKUSDT": {"leaders": ["BTCUSDT"], "lag_minutes": 30, "correlation": 0.80},
    "NEARUSDT": {"leaders": ["ETHUSDT"], "lag_minutes": 60, "correlation": 0.74},
    "FILUSDT": {"leaders": ["ETHUSDT"], "lag_minutes": 60, "correlation": 0.76},
}


def fetch_klines(symbol, interval="1h", limit=6):
    try:
        r = requests.get(f"{BINANCE}/klines",
                         params={"symbol": symbol, "interval": interval, "limit": limit},
                         headers={"User-Agent": UA}, timeout=10)
        if r.status_code != 200: return []
        return [{"h": float(k[2]), "l": float(k[3]), "c": float(k[4])} for k in r.json()]
    except Exception: return []


def check_spillover(follower_symbol):
    """Check if any leader has a volatility spike that predicts vol on follower.
    
    Returns: {
        "active": True/False,
        "spiking_leaders": [...],
        "avg_spike_ratio": float,
        "recommended_action": "tighten_grid" / "widen_tp" / "none",
        "confidence": 0-1
    }
    """
    mapping = SPILLOVER_MAP.get(follower_symbol)
    if not mapping:
        return {"active": False, "reason": "no_spillover_data"}

    spiking = []
    spike_ratios = []

    for leader in mapping["leaders"]:
        candles = fetch_klines(leader, "1h", 6)
        if len(candles) < 4: continue

        # Compare last 2 candles to previous 2
        recent_range = max(candles[-1]["h"] - candles[-1]["l"],
                          candles[-2]["h"] - candles[-2]["l"])
        prev_range = max(candles[-3]["h"] - candles[-3]["l"],
                        candles[-4]["h"] - candles[-4]["l"]) if len(candles) >= 4 else recent_range

        if prev_range > 0:
            ratio = recent_range / prev_range
            if ratio > 1.4:  # 40% range expansion = spillover signal
                spiking.append(leader)
                spike_ratios.append(ratio)

    if not spiking:
        return {"active": False, "spiking_leaders": [], "reason": "no_spikes"}

    avg_ratio = sum(spike_ratios) / len(spike_ratios)
    confidence = min(0.9, avg_ratio / 3.0 + 0.3)  # stronger spike = higher confidence

    return {
        "active": True,
        "follower": follower_symbol,
        "spiking_leaders": spiking,
        "avg_spike_ratio": round(avg_ratio, 2),
        "lag_minutes": mapping["lag_minutes"],
        "confidence": round(confidence, 2),
        "recommended_action": "place_pending_orders",
    }


def get_anticipation_levels(follower_symbol, spillover_signal):
    """When spillover is active, calculate where to place pending orders.
    
    Returns tighter grid levels anticipating the vol expansion.
    """
    if not spillover_signal.get("active"):
        return None

    try:
        r = requests.get(f"{BINANCE}/ticker/price?symbol={follower_symbol}",
                         headers={"User-Agent": UA}, timeout=5)
        if r.status_code != 200: return None
        price = float(r.json()["price"])
    except Exception: return None

    # Get ATR for spacing
    candles = fetch_klines(follower_symbol, "1h", 24)
    if not candles: return None

    atr = 0
    if len(candles) >= 15:
        trs = []
        for i in range(1, len(candles)):
            tr = max(candles[i]["h"] - candles[i]["l"],
                    abs(candles[i]["h"] - candles[i-1]["c"]),
                    abs(candles[i]["l"] - candles[i-1]["c"]))
            trs.append(tr)
        atr = sum(trs[-14:]) / 14
    atr = max(atr, price * 0.002)

    # Anticipation: tighter spacing (catch the move early), wider TP (bigger expected move)
    ratio = spillover_signal["avg_spike_ratio"]
    spacing = atr * 0.5  # tighter than normal
    tp_distance = atr * (2 + ratio)  # wider TP proportional to spike

    buy_levels = [round(price - spacing * (i + 1), 4) for i in range(5)]
    sell_levels = [round(price + spacing * (i + 1), 4) for i in range(5)]

    return {
        "price": price,
        "atr": round(atr, 4),
        "spacing": round(spacing, 4),
        "tp_distance": round(tp_distance, 4),
        "buy_levels": buy_levels,
        "sell_levels": sell_levels,
        "buy_tp": [round(l + tp_distance, 4) for l in buy_levels],
        "sell_tp": [round(l - tp_distance, 4) for l in sell_levels],
    }


def scan_all():
    """Scan all known followers for spillover signals."""
    results = {}
    for follower in SPILLOVER_MAP:
        signal = check_spillover(follower)
        if signal["active"]:
            anticipation = get_anticipation_levels(follower, signal)
            results[follower] = {"signal": signal, "anticipation": anticipation}
    return results


def get_active_alerts():
    """Get list of pairs with active spillover signals for agent consumption."""
    alerts = scan_all()
    active = []
    for symbol, data in alerts.items():
        if data["signal"]["active"]:
            active.append({
                "symbol": symbol,
                "leaders": data["signal"]["spiking_leaders"],
                "confidence": data["signal"]["confidence"],
                "lag_minutes": data["signal"]["lag_minutes"],
                "anticipation": data["anticipation"],
            })
    return sorted(active, key=lambda x: x["confidence"], reverse=True)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--watch", type=int, default=0)
    p.add_argument("--symbol", default="XRPUSDT")
    p.add_argument("--all", action="store_true")
    args = p.parse_args()

    if args.all:
        alerts = get_active_alerts()
        print(f"\n  SPILLOVER ALERTS ({len(alerts)} active):")
        for a in alerts:
            print(f"\n  {a['symbol']}: confidence {a['confidence']:.0%}")
            print(f"    Leaders spiking: {a['leaders']}")
            print(f"    Lag: {a['lag_minutes']}min")
            if a.get("anticipation"):
                ant = a["anticipation"]
                print(f"    Price: ${ant['price']:.4f}")
                print(f"    Buy levels: {ant['buy_levels'][:3]}")
                print(f"    Sell levels: {ant['sell_levels'][:3]}")
        if not alerts:
            print("  No active spillover signals")
    else:
        signal = check_spillover(args.symbol)
        print(f"\n  SPILLOVER — {args.symbol}")
        print(f"  Active: {signal['active']}")
        if signal["active"]:
            print(f"  Leaders: {signal['spiking_leaders']}")
            print(f"  Spike ratio: {signal['avg_spike_ratio']}x")
            print(f"  Lag: {signal['lag_minutes']}min")
            print(f"  Confidence: {signal['confidence']:.0%}")
            anticipation = get_anticipation_levels(args.symbol, signal)
            if anticipation:
                print(f"\n  ANTICIPATION LEVELS:")
                print(f"  Buy:  {anticipation['buy_levels'][:3]}")
                print(f"  Sell: {anticipation['sell_levels'][:3]}")
                print(f"  TP distance: {anticipation['tp_distance']}")

    if args.watch:
        print(f"\n  Watching every {args.watch}s...")
        try:
            while True:
                time.sleep(args.watch)
                alerts = get_active_alerts()
                active_syms = [a["symbol"] for a in alerts]
                print(f"  [{datetime.now().strftime('%H:%M:%S')}] "
                      f"Spillover active: {active_syms if active_syms else 'none'}")
        except KeyboardInterrupt: pass
