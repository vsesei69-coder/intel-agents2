"""Market Regime Detector — determines if market is trending, ranging, or volatile.

Detects 5 regimes:
  - BULL_TREND: SMA20 > SMA50 on 1H, price above SMA20, BB width normal
  - BEAR_TREND: SMA20 < SMA50 on 1H, price below SMA20, BB width normal  
  - RANGING: BB width < 2%, price between bands, RSI 40-60
  - HIGH_VOL: BB width > 4%, large recent moves
  - CRASH: price dropped >5% in last 4h, extreme volume

Used by agents to adapt behavior:
  - TREND agents: go WITH trend, not against it
  - GRID agents: only in RANGING or mild BULL_TREND
  - CORRIDOR agent: only in RANGING (sleeping market)
  - All agents: reduce position size in HIGH_VOL, stop in CRASH
"""

import json, sys, time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, stdev

import requests

BINANCE_BASE = "https://api.binance.com/api/v3"
UA = "RegimeDetector/1.0"

REGIME_FILE = Path(__file__).parent.parent / "trading_journal" / "market_regime.json"


def fetch_klines(symbol, interval="1h", limit=100):
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


def detect_regime(symbol="BTCUSDT"):
    """Detect market regime for a symbol using multi-timeframe analysis."""
    c_1h = fetch_klines(symbol, "1h", 50)
    c_4h = fetch_klines(symbol, "4h", 25)
    c_15m = fetch_klines(symbol, "15m", 96)  # 24h of 15m candles

    if len(c_1h) < 20:
        return {"regime": "UNKNOWN", "confidence": 0, "symbol": symbol}

    closes_1h = [c["c"] for c in c_1h]
    closes_15m = [c["c"] for c in c_15m] if c_15m else closes_1h[-24:]
    volumes_1h = [c["v"] for c in c_1h[-24:]]
    price = closes_1h[-1]

    # Trend indicators
    sma20 = mean(closes_1h[-20:])
    sma50 = mean(closes_1h[-50:]) if len(closes_1h) >= 50 else sma20
    trend_up = sma20 > sma50
    price_above_sma = price > sma20

    # Bollinger Bands for volatility
    r = closes_1h[-20:]
    sma = mean(r)
    s = stdev(r) if len(r) > 1 else 0
    bb_width = (s * 4) / sma * 100 if sma > 0 else 0  # BB width %

    # RSI
    if len(closes_1h) >= 15:
        g, l = [], []
        for i in range(len(closes_1h) - 14, len(closes_1h)):
            d = closes_1h[i] - closes_1h[i - 1]
            (g if d > 0 else l).append(abs(d))
            (l if d > 0 else g).append(0)
        ag, al = mean(g) if g else 0, mean(l) if l else 0
        rsi_val = 100 - (100 / (1 + ag / al)) if al > 0 else 100
    else:
        rsi_val = 50

    # Recent momentum
    change_4h = (price - closes_1h[-4]) / closes_1h[-4] * 100 if len(closes_1h) >= 4 else 0
    change_24h = (price - closes_15m[-1]) / closes_15m[-1] * 100 if closes_15m else 0

    # Volume analysis
    avg_vol = mean(volumes_1h) if volumes_1h else 0
    recent_vol = mean(volumes_1h[-4:]) if len(volumes_1h) >= 4 else avg_vol
    vol_spike = (recent_vol / avg_vol > 1.5) if avg_vol > 0 else False

    # Determine regime
    regime = "RANGING"
    confidence = 0.5
    allowed_directions = ["long", "short"]

    if abs(change_4h) > 5 and vol_spike:
        regime = "CRASH" if change_4h < 0 else "SURGE"
        confidence = 0.85
        allowed_directions = ["long"] if regime == "SURGE" else ["short"]

    elif bb_width > 4.0:
        regime = "HIGH_VOL"
        confidence = 0.7
        # In high vol, trend agents should reduce size, grids should pause

    elif trend_up and price_above_sma and bb_width > 1.5:
        regime = "BULL_TREND"
        confidence = 0.75
        allowed_directions = ["long"]  # Only go long in bull trend

    elif not trend_up and not price_above_sma and bb_width > 1.5:
        regime = "BEAR_TREND"
        confidence = 0.75
        allowed_directions = ["short"]  # Only go short in bear trend

    elif bb_width < 2.0 and 35 <= rsi_val <= 65:
        regime = "RANGING"
        confidence = 0.7
        allowed_directions = ["long", "short"]  # Both OK in range

    else:
        regime = "RANGING"
        confidence = 0.5
        allowed_directions = ["long", "short"]

    # Agent-specific recommendations
    agent_advice = {
        "trend": {
            "allowed": allowed_directions,
            "note": "go WITH trend" if regime in ("BULL_TREND", "BEAR_TREND") else "both OK",
        },
        "grid": {
            "active": regime in ("RANGING", "BULL_TREND") and bb_width < 3.5,
            "note": "grids work best in ranging markets",
        },
        "max_grid": {
            "active": regime not in ("CRASH", "SURGE"),
            "allowed": allowed_directions,
            "note": "pause in extreme regimes",
        },
        "corridor": {
            "active": regime == "RANGING",
            "note": "corridor only in sleeping/ranging markets",
        },
    }

    result = {
        "symbol": symbol,
        "regime": regime,
        "confidence": round(confidence, 2),
        "price": round(price, 2),
        "sma20": round(sma20, 2),
        "sma50": round(sma50, 2),
        "bb_width_pct": round(bb_width, 2),
        "rsi_1h": round(rsi_val, 1),
        "change_4h_pct": round(change_4h, 2),
        "change_24h_pct": round(change_24h, 2),
        "vol_spike": vol_spike,
        "allowed_directions": allowed_directions,
        "agent_advice": agent_advice,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Save to file
    REGIME_FILE.write_text(json.dumps(result, indent=2, ensure_ascii=False))

    return result


def get_current_regime():
    """Get cached regime or detect fresh one."""
    if REGIME_FILE.exists():
        try:
            data = json.loads(REGIME_FILE.read_text())
            ts = datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age < 300:  # Cache for 5 min
                return data
        except Exception:
            pass
    return detect_regime()


def check_direction_allowed(direction, agent_type="trend"):
    """Check if a trading direction is allowed in current regime."""
    regime_data = get_current_regime()
    advice = regime_data.get("agent_advice", {}).get(agent_type, {})

    if not advice.get("active", True):
        return False, f"agent {agent_type} inactive in {regime_data['regime']}"

    allowed = advice.get("allowed", regime_data.get("allowed_directions", ["long", "short"]))
    if direction not in allowed:
        return False, f"{direction} blocked in {regime_data['regime']} — only {allowed}"

    return True, f"{direction} OK in {regime_data['regime']}"


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--watch", type=int, default=0, help="Watch interval in seconds")
    args = p.parse_args()

    regime = detect_regime(args.symbol)

    print(f"\n  MARKET REGIME: {regime['regime']} (conf: {regime['confidence']:.0%})")
    print(f"  BTC: ${regime['price']:,.0f} | BB width: {regime['bb_width_pct']:.1f}% | "
          f"RSI(1h): {regime['rsi_1h']:.0f}")
    print(f"  4h change: {regime['change_4h_pct']:+.1f}% | 24h: {regime['change_24h_pct']:+.1f}%")
    print(f"  Allowed directions: {regime['allowed_directions']}")
    print(f"\n  Agent advice:")
    for agent, advice in regime["agent_advice"].items():
        active = advice.get("active", True)
        note = advice.get("note", "")
        print(f"    {agent:<12}: active={active} | {note}")

    if args.watch:
        print(f"\n  Watching every {args.watch}s. Ctrl+C to stop.")
        try:
            while True:
                time.sleep(args.watch)
                regime = detect_regime(args.symbol)
                print(f"  [{datetime.now().strftime('%H:%M:%S')}] "
                      f"Regime: {regime['regime']} | "
                      f"Direction: {regime['allowed_directions']} | "
                      f"BB: {regime['bb_width_pct']:.1f}% | "
                      f"RSI: {regime['rsi_1h']:.0f}")
        except KeyboardInterrupt:
            pass
