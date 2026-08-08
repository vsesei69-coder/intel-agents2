"""Volatility Monitor + Order Book Analyzer — shared module for all agents.

Provides:
  1. Real-time volatility tracking across 20 pairs (ATR, BB width)
  2. Vol spike detection — if ATR jumps 2x vs baseline, trigger alert
  3. Cross-pair correlation — if 3+ pairs spike simultaneously = market-wide event
  4. Order book wall detection at S/R levels — where big orders cluster
  5. Vol-adjusted position sizing — smaller size when vol is high

Usage from any agent:
  from vol_monitor import get_vol_state, is_safe_to_trade, get_orderbook_walls
"""

import json, sys, time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, stdev

import requests

BINANCE_BASE = "https://api.binance.com/api/v3"
UA = "VolMon/1.0"

STATE_FILE = Path(__file__).parent.parent / "trading_journal" / "vol_state.json"

PAIRS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT", "AVAXUSDT",
    "LINKUSDT", "DOTUSDT", "UNIUSDT", "ATOMUSDT", "ARBUSDT",
    "OPUSDT", "NEARUSDT", "APTUSDT", "FILUSDT", "TRXUSDT",
    "ETCUSDT", "LTCUSDT", "BNBUSDT", "XRPUSDT", "MATICUSDT",
]

# Thresholds
VOL_SPIKE_MULTIPLIER = 2.0     # ATR > 2x baseline = spike
BB_WIDTH_DANGER = 4.0          # BB width > 4% = danger zone
CROSS_PAIR_ALERT = 3           # 3+ pairs spiking = market event
VOL_DECAY = 0.95               # exponential decay for baseline


def fetch_klines(symbol, interval="15m", limit=50):
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


def fetch_orderbook(symbol, limit=100):
    try:
        r = requests.get(f"{BINANCE_BASE}/depth",
                         params={"symbol": symbol, "limit": limit},
                         headers={"User-Agent": UA}, timeout=10)
        if r.status_code != 200:
            return None
        d = r.json()
        bids = [(float(b[0]), float(b[1])) for b in d["bids"]]
        asks = [(float(a[0]), float(a[1])) for a in d["asks"]]
        return {"bids": bids, "asks": asks}
    except Exception:
        return None


def compute_atr(candles, period=14):
    if len(candles) < period + 1:
        return 0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return mean(trs[-period:])


def compute_bb_width(closes, period=20, mult=2.0):
    if len(closes) < period:
        return 0
    r = closes[-period:]
    sma = mean(r)
    s = stdev(r) if len(r) > 1 else 0
    return (s * mult * 2) / sma * 100 if sma > 0 else 0


def find_orderbook_walls(orderbook, num_levels=3):
    """Find significant order clusters (walls) in the order book.
    A wall is where cumulative volume at a price level is 3x+ the average."""
    if not orderbook:
        return {"bid_walls": [], "ask_walls": []}

    result = {"bid_walls": [], "ask_walls": []}

    for side, key in [("bids", "bid_walls"), ("asks", "ask_walls")]:
        levels = orderbook[side]
        if not levels:
            continue
        volumes = [v for _, v in levels]
        avg_vol = mean(volumes) if volumes else 0
        threshold = avg_vol * 3.0

        for price, volume in levels:
            if volume >= threshold and volume > 0:
                result[key].append({
                    "price": price,
                    "volume": round(volume, 2),
                    "ratio": round(volume / avg_vol, 1) if avg_vol > 0 else 0,
                })
        result[key] = result[key][:num_levels]

    return result


def find_sr_levels(candles_1h, candles_15m):
    """Find support/resistance from recent highs/lows."""
    levels = {"support": [], "resistance": []}
    all_candles = (candles_1h or []) + (candles_15m or [])

    if len(all_candles) >= 24:
        highs = sorted([c["h"] for c in all_candles[-48:]], reverse=True)
        lows = sorted([c["l"] for c in all_candles[-48:]])
        levels["resistance"] = list(dict.fromkeys(round(h, 6) for h in highs[:5]))
        levels["support"] = list(dict.fromkeys(round(l, 6) for l in lows[:5]))

    return levels


def scan_volatility():
    """Scan all pairs for volatility. Returns state dict with alerts."""
    state = load_cached_state()
    now = datetime.now(timezone.utc)

    # Only scan every 60s
    last_scan = state.get("last_scan", "")
    if last_scan:
        last_dt = datetime.fromisoformat(last_scan.replace("Z", "+00:00"))
        if (now - last_dt).total_seconds() < 60:
            return state

    pair_vols = {}
    spikes = []
    baseline = state.get("baseline_atr", {})

    for sym in PAIRS:
        try:
            candles = fetch_klines(sym, "15m", 50)
            if len(candles) < 20:
                continue

            closes = [c["c"] for c in candles]
            atr_val = compute_atr(candles, 14)
            atr_pct = atr_val / closes[-1] * 100 if closes[-1] > 0 else 0
            bb_w = compute_bb_width(closes)

            # Update baseline with exponential decay
            prev = baseline.get(sym, atr_pct)
            baseline[sym] = prev * VOL_DECAY + atr_pct * (1 - VOL_DECAY)

            # Spike detection
            is_spike = atr_pct > baseline[sym] * VOL_SPIKE_MULTIPLIER
            is_danger = bb_w > BB_WIDTH_DANGER

            pair_vols[sym] = {
                "atr_pct": round(atr_pct, 4),
                "atr_baseline": round(baseline[sym], 4),
                "bb_width": round(bb_w, 2),
                "spike": is_spike,
                "danger": is_danger,
                "price": round(closes[-1], 6),
            }
            if is_spike:
                spikes.append(sym)

            time.sleep(0.2)
        except Exception:
            continue

    # Cross-pair alert
    market_event = len(spikes) >= CROSS_PAIR_ALERT
    spike_pairs = spikes

    state = {
        "last_scan": now.isoformat(),
        "baseline_atr": baseline,
        "pairs": pair_vols,
        "spike_count": len(spikes),
        "spike_pairs": spike_pairs,
        "market_event": market_event,
        "market_event_level": "CRITICAL" if market_event else "NORMAL",
        "safe_to_trade": not market_event,
    }

    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    return state


def load_cached_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"baseline_atr": {}, "pairs": {}}


def get_vol_state():
    """Get current volatility state (cached or fresh)."""
    return scan_volatility()


def is_safe_to_trade(symbol=None):
    """Check if it's safe to open new positions."""
    state = get_vol_state()
    if state.get("market_event"):
        return False

    if symbol and symbol in state.get("pairs", {}):
        pair = state["pairs"][symbol]
        if pair.get("danger") or pair.get("spike"):
            return False

    return state.get("safe_to_trade", True)


def get_top_volatile_pairs(count=12):
    """Return list of symbols sorted by ATR% (most volatile first)."""
    state = get_vol_state()
    pairs = state.get("pairs", {})
    ranked = sorted(pairs.items(), key=lambda x: x[1].get("atr_pct", 0), reverse=True)
    return [sym for sym, _ in ranked[:count]]


def get_safe_volatile_pairs(count=12):
    """Return most volatile pairs that are safe to trade (no spike/danger)."""
    state = get_vol_state()
    pairs = state.get("pairs", {})
    safe = [(sym, data) for sym, data in pairs.items()
            if not data.get("spike") and not data.get("danger")]
    safe.sort(key=lambda x: x[1].get("atr_pct", 0), reverse=True)
    return [sym for sym, _ in safe[:count]]


def get_orderbook_imbalance(symbol):
    """Get order book depth imbalance: +1 = all bids, -1 = all asks.
    Positive = bullish pressure, negative = bearish."""
    try:
        ob = fetch_orderbook(symbol, 100)
        if not ob or not ob["bids"] or not ob["asks"]:
            return 0.0
        bid_v = sum(v for _, v in ob["bids"][:100])
        ask_v = sum(v for _, v in ob["asks"][:100])
        total = bid_v + ask_v
        return round((bid_v - ask_v) / total, 3) if total > 0 else 0.0
    except Exception:
        return 0.0


def get_ob_skew(direction, symbol="BTCUSDT"):
    """Get grid skew based on order book: +1 = favor buys, -1 = favor sells."""
    imbalance = get_orderbook_imbalance(symbol)
    if direction == "long" and imbalance < -0.05:
        return -0.5  # bearish OB, reduce long entries
    elif direction == "long" and imbalance > 0.05:
        return 0.5   # bullish OB, increase long entries
    elif direction == "short" and imbalance > 0.05:
        return -0.5  # bullish OB, reduce short entries
    elif direction == "short" and imbalance < -0.05:
        return 0.5   # bearish OB, increase short entries
    return 0.0
    """Reduce position size when volatility is high."""
    state = get_vol_state()
    pair = state.get("pairs", {}).get(symbol, {})
    atr_pct = pair.get("atr_pct", 1.0)
    baseline = pair.get("atr_baseline", atr_pct)

    if baseline > 0:
        ratio = atr_pct / baseline
        if ratio > 1.5:
            return base_size_usd * 0.5  # half size in high vol
        elif ratio > 1.2:
            return base_size_usd * 0.75
    return base_size_usd


def get_orderbook_walls(symbol):
    """Get order book walls for a symbol."""
    ob = fetch_orderbook(symbol, 100)
    return find_orderbook_walls(ob)


def get_sr_with_walls(symbol):
    """Get S/R levels with order book wall data."""
    c_1h = fetch_klines(symbol, "1h", 50)
    c_15m = fetch_klines(symbol, "15m", 50)
    sr = find_sr_levels(c_1h, c_15m)
    ob = fetch_orderbook(symbol, 200)
    walls = find_orderbook_walls(ob, 5) if ob else {"bid_walls": [], "ask_walls": []}

    # Match walls to S/R levels
    for side, wall_key in [("support", "bid_walls"), ("resistance", "ask_walls")]:
        for level in sr.get(side, []):
            nearby_walls = [w for w in walls.get(wall_key, [])
                            if abs(w["price"] - level) / level < 0.005]
            if nearby_walls:
                level_data = {"price": level, "walls": nearby_walls,
                              "total_volume": sum(w["volume"] for w in nearby_walls)}
                if side not in sr:
                    sr[side] = []
                # Replace simple price with enriched level
                for i, existing in enumerate(sr[side]):
                    if abs(existing - level) / level < 0.005:
                        sr[side][i] = level_data
                        break
                else:
                    sr[side] = [level_data if abs(l - level) / level < 0.005
                                else l for l in sr[side]]

    return {"sr_levels": sr, "walls": walls}


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--watch", type=int, default=0)
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--walls", action="store_true")
    p.add_argument("--sr", action="store_true")
    args = p.parse_args()

    if args.walls:
        walls = get_orderbook_walls(args.symbol)
        print(f"\n  ORDER BOOK WALLS — {args.symbol}")
        print(f"  Bid walls: {walls['bid_walls']}")
        print(f"  Ask walls: {walls['ask_walls']}")

    if args.sr:
        data = get_sr_with_walls(args.symbol)
        print(f"\n  S/R LEVELS + WALLS — {args.symbol}")
        print(f"  Support: {data['sr_levels'].get('support', [])}")
        print(f"  Resistance: {data['sr_levels'].get('resistance', [])}")

    state = get_vol_state()
    print(f"\n  VOLATILITY STATE — {state['last_scan'][:19]}")
    print(f"  Market event: {state['market_event']} | Safe: {state['safe_to_trade']}")
    print(f"  Spike pairs ({state['spike_count']}): {state['spike_pairs'][:5]}")

    # Top 5 most volatile
    ranked = sorted(state.get("pairs", {}).items(),
                    key=lambda x: x[1].get("atr_pct", 0), reverse=True)
    print(f"\n  Top 5 by ATR%:")
    for sym, data in ranked[:5]:
        spike = " [SPIKE!]" if data["spike"] else ""
        danger = " [DANGER]" if data["danger"] else ""
        print(f"    {sym:<10s} ATR: {data['atr_pct']:.2f}% "
              f"(base: {data['atr_baseline']:.2f}%) "
              f"BB: {data['bb_width']:.1f}%{spike}{danger}")

    if args.watch:
        print(f"\n  Watching every {args.watch}s. Ctrl+C to stop.")
        try:
            while True:
                time.sleep(args.watch)
                state = get_vol_state()
                spikes = state.get("spike_pairs", [])
                status = "EVENT!" if state["market_event"] else ("WARN" if spikes else "OK")
                print(f"  [{datetime.now().strftime('%H:%M:%S')}] "
                      f"{status} | Spikes: {len(spikes)} | Safe: {state['safe_to_trade']}")
        except KeyboardInterrupt:
            pass
