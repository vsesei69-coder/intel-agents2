"""Intraday Technical Scanner — volatility, volume, Bollinger Bands, S/R levels.

Data: Binance public API (free, no key) for intraday OHLCV.
Output: pairs sorted by opportunity, entry/exit levels, BB signals.

Usage:
    python intraday_scanner.py                   # Scan all, top 10 by volume
    python intraday_scanner.py --pairs BTC,ETH   # Specific pairs
    python intraday_scanner.py --tf 15m --top 10 # Timeframe override
"""

import json, sys, time
from datetime import datetime, timezone, timedelta
from math import sqrt
from statistics import mean, stdev

import requests

BINANCE_BASE = "https://api.binance.com/api/v3"
UA = "IntradayScanner/1.0"

# USDT pairs with highest volume (pre-filtered, updated periodically)
TOP_PAIRS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "BNBUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
    "MATICUSDT", "UNIUSDT", "LTCUSDT", "ATOMUSDT",
    "ARBUSDT", "OPUSDT", "NEARUSDT", "APTUSDT", "FILUSDT",
    "TRXUSDT", "ETCUSDT", "HBARUSDT", "ALGOUSDT", "VETUSDT",
]


def fetch_klines(symbol, interval="15m", limit=100):
    """Fetch OHLCV candles from Binance public API."""
    try:
        r = requests.get(
            f"{BINANCE_BASE}/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            headers={"User-Agent": UA},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        candles = []
        for k in r.json():
            candles.append({
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
                "time": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
            })
        return candles
    except Exception as e:
        print(f"[WARN] {symbol}: {e}", file=sys.stderr)
        return []


def fetch_24h_ticker(symbol):
    """Fetch 24h price change and volume."""
    try:
        r = requests.get(
            f"{BINANCE_BASE}/ticker/24hr",
            params={"symbol": symbol},
            headers={"User-Agent": UA},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        d = r.json()
        return {
            "symbol": symbol,
            "price": float(d["lastPrice"]),
            "change_pct": float(d["priceChangePercent"]),
            "high_24h": float(d["highPrice"]),
            "low_24h": float(d["lowPrice"]),
            "volume_usdt": float(d["quoteVolume"]),
            "trades": int(d["count"]),
        }
    except Exception:
        return None


def bollinger_bands(closes, period=20, std_mult=2.0):
    """Calculate Bollinger Bands."""
    if len(closes) < period:
        return None
    recent = closes[-period:]
    sma = mean(recent)
    st = stdev(recent) if len(recent) > 1 else 0
    upper = sma + std_mult * st
    lower = sma - std_mult * st
    bandwidth = (upper - lower) / sma * 100 if sma > 0 else 0
    # Position within bands: 0 = lower, 100 = upper
    if upper != lower:
        position = (closes[-1] - lower) / (upper - lower) * 100
    else:
        position = 50
    return {
        "sma": sma,
        "upper": upper,
        "lower": lower,
        "bandwidth": bandwidth,  # wider = more volatile
        "position": position,  # % within band
    }


def find_levels(candles, lookback=20):
    """Find local support and resistance from recent swing points."""
    if len(candles) < lookback:
        return None

    recent = candles[-lookback:]
    highs = [c["high"] for c in recent]
    lows = [c["low"] for c in recent]

    # Simple pivot detection
    resistance = sorted(highs, reverse=True)[:3]
    support = sorted(lows)[:3]

    return {
        "resistance_1": resistance[0] if len(resistance) > 0 else None,
        "resistance_2": resistance[1] if len(resistance) > 1 else None,
        "support_1": support[0] if len(support) > 0 else None,
        "support_2": support[1] if len(support) > 1 else None,
    }


def rsi(closes, period=14):
    """Calculate RSI."""
    if len(closes) < period + 1:
        return 50
    gains = []
    losses = []
    for i in range(len(closes) - period, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
    avg_gain = mean(gains) if gains else 0
    avg_loss = mean(losses) if losses else 0
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def volume_surge(candles, lookback=10):
    """Check if recent volume is above average."""
    if len(candles) < lookback + 3:
        return 1.0
    recent_vol = mean(c["volume"] for c in candles[-3:])
    avg_vol = mean(c["volume"] for c in candles[-lookback - 3:-3])
    if avg_vol == 0:
        return 1.0
    return recent_vol / avg_vol


def scan_pair(symbol, tf="15m"):
    """Full scan of one pair — all indicators."""
    candles = fetch_klines(symbol, tf, limit=100)
    if len(candles) < 30:
        return None

    ticker = fetch_24h_ticker(symbol)
    if not ticker:
        return None

    closes = [c["close"] for c in candles]

    bb = bollinger_bands(closes)
    levels = find_levels(candles)
    rsi_val = rsi(closes)
    vol_ratio = volume_surge(candles)

    # Multi-timeframe: also check 1H for trend context
    h1 = fetch_klines(symbol, "1h", limit=50)
    trend = "neutral"
    if len(h1) >= 20:
        h1_closes = [c["close"] for c in h1]
        sma20 = mean(h1_closes[-20:])
        sma50 = mean(h1_closes[-50:]) if len(h1_closes) >= 50 else sma20
        trend = "up" if sma20 > sma50 else "down"

    # Signal generation
    signal = "WAIT"
    entry = None
    target = None
    stop = None

    if bb:
        pos = bb["position"]
        if pos < 15 and rsi_val < 35:
            signal = "BUY"
            entry = closes[-1]
            target = bb["sma"]
            stop = bb["lower"] * 0.995
        elif pos > 85 and rsi_val > 65:
            signal = "SELL"
            entry = closes[-1]
            target = bb["sma"]
            stop = bb["upper"] * 1.005
        elif pos < 25:
            signal = "WATCH_BUY"
        elif pos > 75:
            signal = "WATCH_SELL"

    # Volatility score (higher = more opportunity)
    volatility = abs(ticker["change_pct"])

    # Volume score
    volume_score = min(ticker["volume_usdt"] / 10_000_000, 10.0)  # 10M+ = max score

    # Opportunity score: combines volatility, volume, band position
    if bb:
        opp_score = volatility * 0.4 + min(bb["bandwidth"] / 2, 5) * 0.3 + abs(50 - bb["position"]) / 50 * 0.3
    else:
        opp_score = volatility * 0.5

    return {
        "symbol": symbol,
        "price": ticker["price"],
        "change_24h": ticker["change_pct"],
        "volume_24h_usdt": ticker["volume_usdt"],
        "high_24h": ticker["high_24h"],
        "low_24h": ticker["low_24h"],
        "opportunity_score": round(opp_score, 2),
        "volatility_24h": round(volatility, 2),
        "volume_score": round(volume_score, 1),
        "hourly_trend": trend,
        "rsi": round(rsi_val, 1),
        "volume_surge": round(vol_ratio, 2),
        "bollinger": {
            "sma": round(bb["sma"], 4) if bb else None,
            "upper": round(bb["upper"], 4) if bb else None,
            "lower": round(bb["lower"], 4) if bb else None,
            "bandwidth_pct": round(bb["bandwidth"], 2) if bb else None,
            "position_pct": round(bb["position"], 1) if bb else None,
        } if bb else None,
        "levels": levels,
        "signal": signal,
        "entry": round(entry, 4) if entry else None,
        "target": round(target, 4) if target else None,
        "stop": round(stop, 4) if stop else None,
    }


def scan_all(pairs=None, tf="15m", top_n=10):
    """Scan multiple pairs, sort by opportunity."""
    pairs = pairs or TOP_PAIRS[:20]
    results = []

    print(f"[*] Scanning {len(pairs)} pairs on {tf} timeframe...", file=sys.stderr)
    for i, pair in enumerate(pairs):
        print(f"   [{i+1}/{len(pairs)}] {pair}...", file=sys.stderr)
        r = scan_pair(pair, tf)
        if r:
            results.append(r)
        time.sleep(0.5)  # rate limit

    results.sort(key=lambda x: (x["opportunity_score"], x["volume_24h_usdt"]), reverse=True)
    return results[:top_n]


def print_report(results):
    """Pretty-print intraday scan report."""
    if not results:
        print("\n  No data. Check network or try later.")
        return

    now = datetime.now().strftime("%H:%M:%S")
    print(f"\n{'='*85}")
    print(f"  INTRADAY SCANNER — {now} UTC | Top {len(results)} pairs by opportunity")
    print(f"{'='*85}")

    for i, r in enumerate(results, 1):
        sig = r["signal"]
        sig_icon = {"BUY": ">>", "SELL": "<<", "WATCH_BUY": ">", "WATCH_SELL": "<"}.get(sig, "  ")
        ch = r["change_24h"]
        ch_sign = "+" if ch > 0 else ""
        trend_icon = "UP" if r["hourly_trend"] == "up" else "dn"

        print(f"\n  [{i:2d}] {r['symbol']:<10s} ${r['price']:<10.4f} {ch_sign}{ch:.2f}% | "
              f"Vol: ${r['volume_24h_usdt']/1e6:.0f}M | RSI: {r['rsi']} | Score: {r['opportunity_score']:.2f}")

        if r["bollinger"]:
            bb = r["bollinger"]
            pos_bar = "|" * int(bb["position_pct"] / 5) + "." * (20 - int(bb["position_pct"] / 5))
            print(f"       BB:  [{pos_bar}] {bb['position_pct']:.0f}%")
            print(f"       L: ${bb['lower']:.4f} | SMA: ${bb['sma']:.4f} | U: ${bb['upper']:.4f} | "
                  f"BW: {bb['bandwidth_pct']}%")

        if r["levels"]:
            lv = r["levels"]
            print(f"       S/R: S={lv['support_1']:.4f} / {lv.get('support_2','-'):} | "
                  f"R={lv['resistance_1']:.4f} / {lv.get('resistance_2','-'):}")

        print(f"       Signal: {sig_icon} {sig} | Trend: {trend_icon} | "
              f"Vol surge: {r['volume_surge']:.1f}x")

        if r["entry"]:
            risk = abs(r["entry"] - r["stop"]) if r["stop"] else 0
            reward = abs(r["target"] - r["entry"]) if r["target"] else 0
            rr = f"{reward/risk:.1f}" if risk > 0 else "?"
            print(f"       Entry: ${r['entry']:.4f} | Target: ${r['target']:.4f} | "
                  f"Stop: ${r['stop']:.4f} | R:R = {rr}")

    # Summary stats
    buys = [r for r in results if r["signal"] in ("BUY", "WATCH_BUY")]
    sells = [r for r in results if r["signal"] in ("SELL", "WATCH_SELL")]
    top_vol = max(r["volume_24h_usdt"] for r in results)
    top_vlt = max(r["volatility_24h"] for r in results)

    print(f"\n{'='*85}")
    print(f"  BUY signals: {len(buys)} | SELL signals: {len(sells)}")
    print(f"  Top volume: ${top_vol/1e9:.2f}B | Top volatility: {top_vlt}%")
    print(f"  5m TF recommended for entry. 15m/1H for trend confirmation.")
    print(f"{'='*85}\n")


def output_json(results):
    print(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "count": len(results),
        "pairs": results,
    }, indent=2, ensure_ascii=False, default=str))


def main():
    import argparse
    p = argparse.ArgumentParser(description="Intraday Technical Scanner")
    p.add_argument("--pairs", type=str, help="Comma-separated pairs (e.g. BTC,ETH,SOL)")
    p.add_argument("--tf", choices=["5m", "15m", "30m", "1h"], default="15m")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--output", choices=["table", "json"], default="table")
    args = p.parse_args()

    pairs = [p.strip().upper() + "USDT" if not p.strip().upper().endswith("USDT")
             else p.strip().upper() for p in args.pairs.split(",")] if args.pairs else None

    results = scan_all(pairs, args.tf, args.top)

    if args.output == "json":
        output_json(results)
    else:
        print_report(results)


if __name__ == "__main__":
    main()
