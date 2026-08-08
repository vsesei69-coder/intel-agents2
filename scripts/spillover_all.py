"""Volatility Spillover Research — run for all agent trading pairs.

Finds lead-lag ATR correlations for: UNI, ETH, SOL, ADA, AVAX, BTC.
Outputs: which pairs lead each target, optimal lag, orderbook status.
"""

import json, sys, time
from datetime import datetime, timezone, timedelta
from statistics import mean, correlation
from pathlib import Path
import requests

BINANCE = "https://api.binance.com/api/v3"
UA = "Spillover/1.0"
OUT = Path(__file__).parent.parent / "backtest_results" / "spillover_all.json"

PAIRS_REF = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT", "AVAXUSDT",
    "LINKUSDT", "DOTUSDT", "UNIUSDT", "ATOMUSDT", "NEARUSDT",
    "FILUSDT", "BNBUSDT", "LTCUSDT", "APTUSDT", "XRPUSDT",
    "MATICUSDT", "OPUSDT", "ARBUSDT", "TRXUSDT", "ETCUSDT",
]

TARGETS = ["UNIUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT", "AVAXUSDT", "BTCUSDT"]


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
                all_data.append({"h": float(k[2]), "l": float(k[3]), "c": float(k[4])})
            start_ms = int(batch[-1][0]) + 1
            time.sleep(0.15)
        except Exception: break
    return all_data


def compute_atr_series(candles, period=14):
    atrs = []
    for i in range(period, len(candles)):
        tr_sum = 0
        for j in range(i - period + 1, i + 1):
            c = candles[j]; pc = candles[j-1]
            tr = max(c["h"]-c["l"], abs(c["h"]-pc["c"]), abs(c["l"]-pc["c"]))
            tr_sum += tr
        atrs.append(tr_sum / period)
    return atrs


def cross_correlation(x, y, max_lag=12):
    n = min(len(x), len(y))
    x, y = x[-n:], y[-n:]
    best_corr, best_lag = 0, 0
    for lag in range(-max_lag, max_lag+1):
        if lag < 0: xs, ys = x[-lag:], y[:lag]
        elif lag > 0: xs, ys = x[:-lag], y[lag:]
        else: xs, ys = x, y
        if len(xs) < 20: continue
        try:
            c = correlation(xs, ys)
            if abs(c) > abs(best_corr): best_corr, best_lag = c, lag
        except Exception: continue
    return best_corr, best_lag


def fetch_ob_imbalance(symbol):
    try:
        r = requests.get(f"{BINANCE}/depth?symbol={symbol}&limit=100",
                         headers={"User-Agent": UA}, timeout=10)
        if r.status_code != 200: return 0
        d = r.json()
        bid_v = sum(float(b[1]) for b in d["bids"][:100])
        ask_v = sum(float(a[1]) for a in d["asks"][:100])
        total = bid_v + ask_v
        return (bid_v - ask_v) / total if total > 0 else 0
    except Exception: return 0


# Fetch all reference data
print("Fetching 30d 1h data for 20 pairs...", file=sys.stderr)
all_data = {}
for sym in PAIRS_REF:
    c = fetch_klines(sym, "1h", 30)
    if c: all_data[sym] = compute_atr_series(c)

results = {}

for target in TARGETS:
    if target not in all_data:
        continue

    target_atr = all_data[target]
    correlations = []

    for ref in PAIRS_REF:
        if ref == target or ref not in all_data:
            continue
        corr, lag = cross_correlation(target_atr, all_data[ref])
        correlations.append({"pair": ref, "corr": round(corr, 3), "lag": lag,
                             "leads": lag < 0, "lags": lag > 0})

    correlations.sort(key=lambda x: abs(x["corr"]), reverse=True)
    leaders = [c for c in correlations if c["leads"] and c["corr"] > 0.6]
    synchronous = [c for c in correlations if c["lag"] == 0 and c["corr"] > 0.6]
    ob = fetch_ob_imbalance(target)

    results[target] = {
        "top_correlations": correlations[:5],
        "leaders": leaders[:3],
        "synchronous": synchronous[:3],
        "orderbook_imbalance": round(ob, 3),
        "atr": round(mean(target_atr[-48:]), 6) if target_atr else 0,
    }

    # Print summary
    print(f"\n{'='*55}")
    print(f"  {target}")
    print(f"  OB imbalance: {ob:+.3f} | {'BULLISH' if ob>0.08 else 'BEARISH' if ob<-0.08 else 'NEUTRAL'}")
    print(f"  Leaders (lead XRP vol):")
    for l in leaders[:3]:
        print(f"    {l['pair']}: +{l['corr']:.3f}, leads by {abs(l['lag'])}h")
    if not leaders:
        print(f"    (no clear leaders above 0.6)")

OUT.write_text(json.dumps(results, indent=2, ensure_ascii=False))
print(f"\n  Saved: {OUT}")
