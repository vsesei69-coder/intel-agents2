"""XRP Volatility Research — pure technical correlations.

Analyzes:
  1. Cross-pair correlations: which assets lead/lag XRP volatility
  2. Order book depth vs ATR: does thin book predict vol spikes
  3. Volume profile: accumulation before breakout
  4. BTC/ETH dominance effect on XRP vol
  5. Stablecoin flow patterns (USDT dominance)

Data: Binance public API, 30 days of 1h candles.
"""

import json, sys, time
from datetime import datetime, timezone, timedelta
from statistics import mean, stdev, correlation
from pathlib import Path

import requests

BINANCE = "https://api.binance.com/api/v3"
UA = "XRPResearch/1.0"
OUT = Path(__file__).parent.parent / "backtest_results" / "xrp_vol_research.json"

PAIRS = [
    "XRPUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT",
    "LINKUSDT", "DOTUSDT", "UNIUSDT", "AVAXUSDT", "MATICUSDT",
    "BNBUSDT", "LTCUSDT", "ATOMUSDT", "NEARUSDT", "FILUSDT",
]

STABLECOIN_PROXIES = ["USDCUSDT", "DAIUSDT"]


def fetch_klines(symbol, interval="1h", days=30):
    all_data = []
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    while start_ms < end_ms:
        try:
            r = requests.get(f"{BINANCE}/klines",
                             params={"symbol": symbol, "interval": interval,
                                     "startTime": start_ms, "endTime": end_ms, "limit": 1000},
                             headers={"User-Agent": UA}, timeout=15)
            if r.status_code != 200: break
            batch = r.json()
            if not batch: break
            for k in batch:
                all_data.append({
                    "t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
                    "l": float(k[3]), "c": float(k[4]), "v": float(k[5]),
                    "qv": float(k[7]),
                })
            start_ms = int(batch[-1][0]) + 1
            time.sleep(0.2)
        except Exception:
            break
    return all_data


def fetch_orderbook_snapshot(symbol):
    """Single order book snapshot."""
    try:
        r = requests.get(f"{BINANCE}/depth?symbol={symbol}&limit=500", timeout=10,
                         headers={"User-Agent": UA})
        if r.status_code != 200: return None
        d = r.json()
        bids = [(float(b[0]), float(b[1])) for b in d["bids"]]
        asks = [(float(a[0]), float(a[1])) for a in d["asks"]]
        total_bid = sum(v for _, v in bids[:100])
        total_ask = sum(v for _, v in asks[:100])
        spread_pct = (asks[0][0] - bids[0][0]) / bids[0][0] * 100 if bids and asks else 0
        return {
            "total_bid_100": total_bid, "total_ask_100": total_ask,
            "bid_ask_ratio": total_bid / total_ask if total_ask > 0 else 0,
            "spread_pct": spread_pct,
            "depth_imbalance": (total_bid - total_ask) / (total_bid + total_ask) if (total_bid + total_ask) > 0 else 0,
        }
    except Exception:
        return None


def compute_atr_series(candles, period=14):
    atrs = []
    for i in range(period, len(candles)):
        tr_sum = 0
        for j in range(i - period + 1, i + 1):
            c = candles[j]
            pc = candles[j - 1]
            tr = max(c["h"] - c["l"], abs(c["h"] - pc["c"]), abs(c["l"] - pc["c"]))
            tr_sum += tr
        atrs.append(tr_sum / period)
    return atrs


def compute_returns(candles):
    return [(candles[i]["c"] - candles[i-1]["c"]) / candles[i-1]["c"] * 100
            for i in range(1, len(candles))]


def compute_volume_ratio(candles, short=5, long=20):
    """Short-term vs long-term volume ratio — detects accumulation."""
    ratios = []
    for i in range(long, len(candles)):
        short_vol = mean(c["v"] for c in candles[i-short:i])
        long_vol = mean(c["v"] for c in candles[i-long:i])
        ratios.append(short_vol / long_vol if long_vol > 0 else 1)
    return ratios


def find_vol_spikes(atrs, threshold=2.0):
    """Find ATR spikes >2x baseline."""
    baseline = mean(atrs)
    spikes = []
    for i, a in enumerate(atrs):
        if a > baseline * threshold:
            spikes.append({"index": i, "atr": a, "ratio": a / baseline})
    return spikes


def cross_correlation(x, y, max_lag=24):
    """Find max correlation and optimal lag between two series."""
    n = min(len(x), len(y))
    x, y = x[-n:], y[-n:]
    best_corr = 0
    best_lag = 0

    for lag in range(-max_lag, max_lag + 1):
        if lag < 0:
            xs, ys = x[-lag:], y[:lag]
        elif lag > 0:
            xs, ys = x[:-lag], y[lag:]
        else:
            xs, ys = x, y

        if len(xs) < 20:
            continue
        try:
            c = correlation(xs, ys)
            if abs(c) > abs(best_corr):
                best_corr = c
                best_lag = lag
        except Exception:
            continue

    return best_corr, best_lag


print(f"\n{'='*65}")
print(f"  XRP VOLATILITY RESEARCH — {datetime.now().strftime('%H:%M:%S')}")
print(f"{'='*65}")

# ── 1. Fetch all data ────────────────────────────────────────────
print(f"\n[1] Fetching 30 days of 1h candles for 15 pairs...")
all_klines = {}
for sym in PAIRS:
    candles = fetch_klines(sym, "1h", 30)
    if candles:
        all_klines[sym] = candles
        print(f"  {sym}: {len(candles)} candles", file=sys.stderr)

# Focus on XRP
xrp = all_klines.get("XRPUSDT", [])
if len(xrp) < 100:
    print("Not enough XRP data")
    sys.exit(1)

xrp_atr = compute_atr_series(xrp)
xrp_returns = compute_returns(xrp)
xrp_vol_ratio = compute_volume_ratio(xrp)
xrp_closes = [c["c"] for c in xrp]

print(f"\n  XRP stats:")
print(f"  Price range: ${min(xrp_closes):.4f} - ${max(xrp_closes):.4f}")
print(f"  Avg ATR(14): {mean(xrp_atr):.6f}")
print(f"  Max ATR: {max(xrp_atr):.6f}")
print(f"  Volatility (std returns): {stdev(xrp_returns):.2f}%")

# ── 2. Cross-pair correlations ───────────────────────────────────
print(f"\n[2] Cross-pair ATR correlations (with lag)...")
correlations = []

for sym in PAIRS:
    if sym == "XRPUSDT" or sym not in all_klines:
        continue
    candles = all_klines[sym]
    other_atr = compute_atr_series(candles)

    corr, lag = cross_correlation(xrp_atr[14:], other_atr[14:])
    lag_label = f"{abs(lag)}h {'before' if lag < 0 else 'after'} XRP"

    correlations.append({
        "pair": sym, "correlation": round(corr, 3),
        "lag_hours": lag, "lag_desc": lag_label,
        "leads_xrp": lag < 0,
    })

correlations.sort(key=lambda x: abs(x["correlation"]), reverse=True)

print(f"  {'Pair':<12s} {'Corr':>8s} {'Lag':>8s} {'Relation':<20s}")
print(f"  {'-'*50}")
for c in correlations[:8]:
    leader = "LEADS XRP" if c["leads_xrp"] else "LAGS XRP"
    print(f"  {c['pair']:<12s} {c['correlation']:>+7.3f} {c['lag_hours']:>+6d}h {leader:<20s}")

# ── 3. BTC dominance effect ──────────────────────────────────────
print(f"\n[3] BTC dominance effect on XRP vol...")
btc = all_klines.get("BTCUSDT", [])
if btc:
    btc_returns = compute_returns(btc)
    btc_vol_ratio = compute_volume_ratio(btc)

    # Correlation: BTC volume spike → XRP ATR spike
    min_len = min(len(xrp_vol_ratio), len(btc_vol_ratio))
    btc_xrp_vol_corr, vlag = cross_correlation(xrp_vol_ratio[-min_len:], btc_vol_ratio[-min_len:], 12)
    print(f"  BTC volume ratio -> XRP vol correlation: {btc_xrp_vol_corr:+.3f} (lag: {vlag}h)")

    # When BTC moves >2%, what happens to XRP ATR next?
    btc_big_moves = []
    for i in range(1, len(btc_returns)):
        if abs(btc_returns[i]) > 2.0:
            # Check XRP ATR in next 4 hours
            xrp_idx = i + 14  # ATR offset
            if xrp_idx + 4 < len(xrp_atr):
                future_atr = max(xrp_atr[xrp_idx:xrp_idx+4])
                baseline_atr = mean(xrp_atr[max(0, xrp_idx-24):xrp_idx])
                btc_big_moves.append({
                    "btc_move": btc_returns[i],
                    "xrp_atr_spike": future_atr / baseline_atr if baseline_atr > 0 else 1,
                })

    if btc_big_moves:
        avg_spike = mean(m["xrp_atr_spike"] for m in btc_big_moves)
        spike_count = sum(1 for m in btc_big_moves if m["xrp_atr_spike"] > 1.5)
        print(f"  BTC moves >2% count: {len(btc_big_moves)}")
        print(f"  Avg XRP ATR spike after BTC move: {avg_spike:.2f}x")
        print(f"  Significant XRP vol spikes (>1.5x): {spike_count}/{len(btc_big_moves)} ({spike_count/len(btc_big_moves)*100:.0f}%)")

# ── 4. Order book analysis ───────────────────────────────────────
print(f"\n[4] Order book depth analysis...")
ob = fetch_orderbook_snapshot("XRPUSDT")
if ob:
    print(f"  Bid/Ask ratio (top 100): {ob['bid_ask_ratio']:.2f}")
    print(f"  Depth imbalance: {ob['depth_imbalance']:+.3f}")
    print(f"  Spread: {ob['spread_pct']:.4f}%")
    print(f"  Total bid depth (100lv): ${ob['total_bid_100']:,.0f}")
    print(f"  Total ask depth (100lv): ${ob['total_ask_100']:,.0f}")

    if ob['depth_imbalance'] > 0.1:
        print(f"  [SIGNAL] Buy-side dominant — bullish pressure")
    elif ob['depth_imbalance'] < -0.1:
        print(f"  [SIGNAL] Sell-side dominant — bearish pressure")
    else:
        print(f"  [NEUTRAL] Balanced order book")

# ── 5. Stablecoin flow proxy ─────────────────────────────────────
print(f"\n[5] Stablecoin flow (USDC/USDT deviation)...")
for sp in STABLECOIN_PROXIES:
    try:
        r = requests.get(f"{BINANCE}/ticker/price?symbol={sp}", timeout=10,
                         headers={"User-Agent": UA})
        if r.status_code == 200:
            px = float(r.json()["price"])
            dev = abs(px - 1.0) * 100
            flow = "INFLOW (buying pressure)" if px > 1.0005 else ("OUTFLOW (selling)" if px < 0.9995 else "NEUTRAL")
            print(f"  {sp}: ${px:.4f} (dev: {dev:.2f}%) — {flow}")
    except Exception:
        pass

# ── 6. Volume profile: accumulation detection ─────────────────────
print(f"\n[6] Volume profile — accumulation before breakout...")
recent_vol_ratio = xrp_vol_ratio[-24:] if xrp_vol_ratio else []
if recent_vol_ratio:
    avg_recent = mean(recent_vol_ratio)
    accumulating = avg_recent > 1.1  # 10% above average volume
    print(f"  Recent volume ratio (5/20): {avg_recent:.2f}")
    if accumulating:
        print(f"  [SIGNAL] Above-average volume — possible accumulation before move")

# Recent ATR trend
if len(xrp_atr) >= 48:
    recent_atr = xrp_atr[-24:]
    older_atr = xrp_atr[-48:-24]
    atr_trend = (mean(recent_atr) / mean(older_atr) - 1) * 100 if older_atr else 0
    print(f"  ATR trend (24h vs 48h ago): {atr_trend:+.1f}%")

# ── 7. Key findings ──────────────────────────────────────────────
print(f"\n{'='*65}")
print(f"  KEY FINDINGS")
print(f"{'='*65}")

# Top correlations
top_corr = correlations[:3]
print(f"\n  TOP XRP VOL DRIVERS:")
for c in top_corr:
    direction = "LEADS" if c["leads_xrp"] else "LAGS"
    print(f"    {c['pair']}: corr={c['corr']:+.3f}, {direction} by {abs(c['lag_hours'])}h")

# Vol spike triggers
print(f"\n  XRP VOL SPIKE TRIGGERS:")
print(f"    1. BTC moves >2% → XRP vol spikes {avg_spike:.1f}x in next 4h" if btc_big_moves else "    (BTC data needed)")
print(f"    2. Order book imbalance >0.1 → directional pressure")
print(f"    3. Volume ratio >1.1 over 24h → accumulation signal")
print(f"    4. ATR trend {atr_trend:+.0f}% — {'expanding' if atr_trend > 0 else 'contracting'} vol regime")

# Save
results = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "xrp_price": xrp_closes[-1] if xrp_closes else 0,
    "xrp_atr": mean(xrp_atr) if xrp_atr else 0,
    "xrp_volatility": stdev(xrp_returns) if xrp_returns else 0,
    "cross_correlations": correlations,
    "btc_spike_effect": {
        "avg_xrp_atr_multiplier": avg_spike if btc_big_moves else 0,
        "count": len(btc_big_moves) if btc_big_moves else 0,
    } if btc_big_moves else {},
    "orderbook": ob,
    "volume_ratio_recent": avg_recent if recent_vol_ratio else 0,
    "atr_trend_pct": atr_trend if xrp_atr else 0,
}
OUT.write_text(json.dumps(results, indent=2, ensure_ascii=False))
print(f"\n  Full data saved: {OUT}")
