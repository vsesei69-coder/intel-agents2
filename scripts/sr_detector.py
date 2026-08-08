"""S/R Level Detector — finds support/resistance from multi-TF data.

No indicators. Pure price action:
  1. Pivot highs/lows on 15m, 1h, 4h, 1d
  2. Volume profile — where most volume traded (value areas)
  3. Order book walls — where big orders cluster
  4. Round numbers — psychological levels

Returns ranked levels with confidence scores.
"""

import json, sys, time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, stdev

import requests

BINANCE = "https://api.binance.com/api/v3"
UA = "SRDetector/1.0"


def fetch_klines(symbol, interval, limit=200):
    try:
        r = requests.get(f"{BINANCE}/klines",
                         params={"symbol": symbol, "interval": interval, "limit": limit},
                         headers={"User-Agent": UA}, timeout=10)
        if r.status_code != 200: return []
        return [{"h": float(k[2]), "l": float(k[3]), "c": float(k[4]),
                 "v": float(k[5]), "qv": float(k[7]),
                 "t": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc)}
                for k in r.json()]
    except Exception: return []


def fetch_orderbook(symbol, limit=200):
    try:
        r = requests.get(f"{BINANCE}/depth?symbol={symbol}&limit={limit}", timeout=10,
                         headers={"User-Agent": UA})
        if r.status_code != 200: return None
        d = r.json()
        return {"bids": [(float(b[0]), float(b[1])) for b in d["bids"]],
                "asks": [(float(a[0]), float(a[1])) for a in d["asks"]]}
    except Exception: return None


def find_pivot_levels(candles, lookback=5):
    """Find local pivot highs and lows."""
    highs = []
    lows = []
    for i in range(lookback, len(candles) - lookback):
        window_h = [c["h"] for c in candles[i - lookback:i + lookback + 1]]
        window_l = [c["l"] for c in candles[i - lookback:i + lookback + 1]]
        if candles[i]["h"] == max(window_h):
            highs.append(candles[i]["h"])
        if candles[i]["l"] == min(window_l):
            lows.append(candles[i]["l"])
    return highs, lows


def find_volume_nodes(candles, num_zones=5):
    """Find price levels with highest traded volume (volume profile)."""
    if not candles: return []
    all_prices = []
    for c in candles:
        price_range = c["h"] - c["l"]
        if price_range > 0 and c["v"] > 0:
            ticks = max(1, int(c["v"] / mean([k["v"] for k in candles[-50:]]) * 10)) if candles else 5
            for _ in range(min(ticks, 20)):
                all_prices.append(c["l"] + price_range * (_ % ticks) / ticks)

    if len(all_prices) < 10: return []

    price_min, price_max = min(all_prices), max(all_prices)
    if price_min == price_max: return []

    bins = min(50, len(all_prices) // 10)
    bin_width = (price_max - price_min) / bins
    histogram = Counter()
    for p in all_prices:
        bucket = round((p - price_min) / bin_width) * bin_width + price_min
        histogram[round(bucket, 4)] += 1

    return [price for price, _ in histogram.most_common(num_zones)]


def find_round_numbers(price, count=5):
    """Find nearby psychological levels (round numbers)."""
    levels = []
    magnitude = 10 ** (len(str(int(price))) - 2)
    base = int(price / magnitude) * magnitude
    for i in range(-count, count + 1):
        levels.append(base + i * magnitude)
    return levels


def cluster_levels(levels, tolerance_pct=0.5):
    """Group nearby levels into clusters. Returns (price, strength) pairs."""
    if not levels: return []
    sorted_levels = sorted(set(round(l, 6) for l in levels))
    clusters = []
    current = [sorted_levels[0]]

    for l in sorted_levels[1:]:
        if abs(l - current[-1]) / current[-1] * 100 < tolerance_pct:
            current.append(l)
        else:
            clusters.append((mean(current), len(current)))
            current = [l]
    clusters.append((mean(current), len(current)))

    return sorted(clusters, key=lambda x: x[1], reverse=True)


def detect_levels(symbol):
    """Full S/R detection for a symbol. Returns dict with support and resistance."""
    price = None
    try:
        r = requests.get(f"{BINANCE}/ticker/price?symbol={symbol}", timeout=5,
                         headers={"User-Agent": UA})
        if r.status_code == 200: price = float(r.json()["price"])
    except Exception: pass
    if not price: return None

    all_highs = []
    all_lows = []

    # Multi-TF pivot detection
    for tf, limit, lookback in [("15m", 100, 3), ("1h", 100, 4), ("4h", 50, 3), ("1d", 30, 2)]:
        candles = fetch_klines(symbol, tf, limit)
        if candles:
            h, l = find_pivot_levels(candles, lookback)
            all_highs.extend(h)
            all_lows.extend(l)

    # Volume nodes
    candles_1h = fetch_klines(symbol, "1h", 100)
    if candles_1h:
        vol_nodes = find_volume_nodes(candles_1h)
        all_highs.extend([n for n in vol_nodes if n > price])
        all_lows.extend([n for n in vol_nodes if n < price])

    # Round numbers
    rounds = find_round_numbers(price)
    all_highs.extend([r for r in rounds if r > price])
    all_lows.extend([r for r in rounds if r < price])

    # Order book walls
    ob = fetch_orderbook(symbol, 200)
    if ob:
        bid_sum = sum(v for _, v in ob["bids"])
        ask_sum = sum(v for _, v in ob["asks"])
        avg_bid = bid_sum / len(ob["bids"]) if ob["bids"] else 0
        avg_ask = ask_sum / len(ob["asks"]) if ob["asks"] else 0
        for px, vol in ob["bids"]:
            if vol > avg_bid * 2:
                all_lows.append(px)
        for px, vol in ob["asks"]:
            if vol > avg_ask * 2:
                all_highs.append(px)

    # Cluster
    resistance = cluster_levels([h for h in all_highs if h > price])
    support = cluster_levels([l for l in all_lows if l < price])

    return {
        "symbol": symbol, "price": round(price, 4),
        "support": [{"level": round(p, 4), "strength": s,
                     "distance_pct": round((price - p) / price * 100, 2)}
                    for p, s in support[:5]],
        "resistance": [{"level": round(p, 4), "strength": s,
                        "distance_pct": round((p - price) / price * 100, 2)}
                       for p, s in resistance[:5]],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--watch", type=int, default=0)
    args = p.parse_args()

    levels = detect_levels(args.symbol)
    if levels:
        print(f"\n  S/R LEVELS — {levels['symbol']} @ ${levels['price']:.2f}")
        print(f"\n  SUPPORT:")
        for s in levels["support"]:
            bar = "|" * s["strength"]
            print(f"    ${s['level']:.4f}  ({s['distance_pct']:.1f}% below)  {bar}")
        print(f"\n  RESISTANCE:")
        for r in levels["resistance"]:
            bar = "|" * r["strength"]
            print(f"    ${r['level']:.4f}  ({r['distance_pct']:.1f}% above)  {bar}")

    if args.watch:
        print(f"\n  Watching every {args.watch}s...")
        try:
            while True:
                time.sleep(args.watch)
                lv = detect_levels(args.symbol)
                if lv:
                    print(f"  [{datetime.now().strftime('%H:%M:%S')}] "
                          f"S:{len(lv['support'])} R:{len(lv['resistance'])}")
        except KeyboardInterrupt:
            pass
