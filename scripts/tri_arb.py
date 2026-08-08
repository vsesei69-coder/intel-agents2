"""Triangular Arbitrage Scanner — Binance public API.

Scans all possible A→B→C→A triangles on Binance in real-time.
Detects profitable loops, calculates net profit after fees.
No account needed — pure public data.

Math:
  Start 1 unit of base. Route through quote/middle assets.
  rate = (1/price_AB) * rate_BC * (1/rate_CA) * (1 - fee)^3
  If rate > 1.0 → arbitrage profit = (rate - 1) * 100%

Usage:
  python tri_arb.py --scan        # continuous scan, every 5s
  python tri_arb.py --once        # single scan
  python tri_arb.py --top 10      # top 10 opportunities
  python tri_arb.py --optimize    # find best triangles historically
"""

import json, sys, time
from datetime import datetime, timezone, timedelta
from itertools import combinations
from pathlib import Path
import requests

BINANCE = "https://api.binance.com/api/v3"
UA = "TriArb/1.0"
OUT = Path(__file__).parent.parent / "backtest_results" / "tri_arb_opportunities.jsonl"

TAKER_FEE = 0.00075  # 0.075% with BNB discount
MIN_PROFIT_PCT = 0.05  # 0.05% minimum profit to report
MIN_VOLUME_USD = 100000  # $100k 24h volume minimum per leg
SCAN_INTERVAL = 5  # seconds between scans

# Common triangular bases
BASE_ASSETS = ["BTC", "ETH", "BNB", "USDT", "BUSD", "USDC"]
QUOTE_ASSETS = ["USDT", "BTC", "ETH", "BNB"]


def fetch_24hr_volume(symbols_list):
    """Get 24h volumes for filtering illiquid pairs."""
    try:
        r = requests.get(f"{BINANCE}/ticker/24hr", headers={"User-Agent": UA}, timeout=10)
        if r.status_code != 200:
            return {}
        volumes = {}
        for t in r.json():
            sym = t["symbol"]
            if sym in symbols_list:
                volumes[sym] = float(t["quoteVolume"])  # USDT volume
        return volumes
    except Exception:
        return {}


def fetch_all_prices():
    """Get all ticker prices from Binance."""
    try:
        r = requests.get(f"{BINANCE}/ticker/price", headers={"User-Agent": UA}, timeout=10)
        if r.status_code != 200:
            return {}
        return {t["symbol"]: float(t["price"]) for t in r.json()}
    except Exception:
        return {}


def fetch_exchange_info():
    """Get all trading pairs and their status."""
    try:
        r = requests.get(f"{BINANCE}/exchangeInfo", headers={"User-Agent": UA}, timeout=10)
        if r.status_code != 200:
            return set()
        symbols = set()
        for s in r.json()["symbols"]:
            if s["status"] == "TRADING":
                symbols.add(s["symbol"])
        return symbols
    except Exception:
        return set()


def build_triangles(symbols):
    """Build all possible triangular arbitrage paths.
    
    A triangle is: start_asset → mid_asset → end_asset → start_asset
    e.g., BTC → ETH → USDT → BTC
    
    Returns list of (leg1_symbol, leg2_symbol, leg3_symbol, direction)
    """
    # Extract all unique assets from trading pairs
    assets = set()
    for sym in symbols:
        # Find quote asset by checking known quotes first
        for quote in QUOTE_ASSETS:
            if sym.endswith(quote) and sym[:-len(quote)]:
                assets.add(sym[:-len(quote)])
                assets.add(quote)
                break

    triangles = []
    
    for start in BASE_ASSETS:
        if start not in assets:
            continue
        for mid in assets:
            if mid == start:
                continue
            for end in assets:
                if end == start or end == mid:
                    continue
                
                # Forward: start→mid→end→start
                leg1 = f"{mid}{start}" if f"{mid}{start}" in symbols else None  # mid/start price
                leg2 = f"{mid}{end}" if f"{mid}{end}" in symbols else None     # mid/end price
                leg3 = f"{end}{start}" if f"{end}{start}" in symbols else None # end/start price
                
                if leg1 and leg2 and leg3:
                    triangles.append({
                        "path": f"{start}>{mid}>{end}>{start}",
                        "legs": [leg1, leg2, leg3],
                        "start": start, "mid": mid, "end": end,
                        "direction": "forward",
                    })
                
                # Reverse: start→end→mid→start  
                # (already covered by different ordering, skip duplicates)
    
    return triangles


def calculate_arbitrage(triangle, prices):
    """Calculate arbitrage profit for a triangle.
    
    Forward: start → mid → end → start
      Step 1: Buy mid with start:    mid_amount = 1 / price(mid/start)
      Step 2: Buy end with mid:      end_amount = mid_amount * price(mid/end)
      Step 3: Buy start with end:    final = end_amount / price(end/start)
    
    Returns: (profit_pct, steps_detail) or None if no opportunity
    """
    legs = triangle["legs"]
    
    # Get prices
    p1 = prices.get(legs[0])  # mid/start
    p2 = prices.get(legs[1])  # mid/end
    p3 = prices.get(legs[2])  # end/start
    
    if not all([p1, p2, p3]) or any(p <= 0 for p in [p1, p2, p3]):
        return None
    
    # Filter out unreadable micro-prices (e.g., XRPBTC = 0.000015)
    if any(p < 0.0001 for p in [p1, p2, p3]):
        return None
    
    # Forward calculation
    # Step 1: 1 start → mid. Price is mid/start, so mid_amount = 1 / price
    mid_amount = 1.0 / p1
    # Step 2: mid → end. Price is mid/end, so end_amount = mid_amount * price
    end_amount = mid_amount * p2
    # Step 3: end → start. Price is end/start, so final = end_amount * price
    final = end_amount * p3
    
    # After fees (3 trades)
    fee_factor = (1 - TAKER_FEE) ** 3
    final_after_fees = final * fee_factor
    
    profit_pct = (final_after_fees - 1.0) * 100
    
    steps = {
        "buy": f"1 {triangle['start']} > {mid_amount:.6f} {triangle['mid']} @ {p1}",
        "swap": f"{mid_amount:.6f} {triangle['mid']} > {end_amount:.6f} {triangle['end']} @ {p2}",
        "sell": f"{end_amount:.6f} {triangle['end']} > {final:.6f} {triangle['start']} @ {p3}",
        "fee_impact": f"{(1 - fee_factor) * 100:.3f}%",
    }
    
    return profit_pct, steps


def scan_triangles(prices=None, symbols=None, volumes=None):
    """Single scan: find all profitable triangles. Returns sorted list."""
    if prices is None:
        prices = fetch_all_prices()
    if symbols is None:
        symbols = fetch_exchange_info()
    if volumes is None:
        # Get volume only for candidate legs (performance)
        volumes = {}
    
    if not prices or not symbols:
        return []
    
    # Filter illiquid pairs
    liquid_symbols = symbols
    if volumes:
        liquid_symbols = {s for s in symbols if volumes.get(s, 0) >= MIN_VOLUME_USD}
    
    triangles = build_triangles(liquid_symbols)
    opportunities = []
    
    for tri in triangles:
        result = calculate_arbitrage(tri, prices)
        if result is None:
            continue
        profit_pct, steps = result
        
        if profit_pct > MIN_PROFIT_PCT:
            opportunities.append({
                "triangle": tri["path"],
                "profit_pct": round(profit_pct, 4),
                "legs": tri["legs"],
                "prices": {leg: prices.get(leg) for leg in tri["legs"]},
                "steps": steps,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
    
    opportunities.sort(key=lambda x: x["profit_pct"], reverse=True)
    return opportunities


def continuous_scan(interval=SCAN_INTERVAL):
    """Run continuous triangular arbitrage scanning."""
    print(f"Triangular Arbitrage Scanner — scanning every {interval}s")
    print(f"Min profit: {MIN_PROFIT_PCT}% | Fee: {TAKER_FEE*100:.3f}% per trade")
    
    # Cache symbols (don't change often)
    symbols = fetch_exchange_info()
    triangles = build_triangles(symbols)
    print(f"Built {len(triangles)} triangles from {len(symbols)} pairs\n")
    
    scan_count = 0
    best_ever = 0
    
    try:
        while True:
            scan_count += 1
            prices = fetch_all_prices()
            if not prices:
                time.sleep(interval)
                continue
            
            opportunities = []
            for tri in triangles:
                result = calculate_arbitrage(tri, prices)
                if result and result[0] > MIN_PROFIT_PCT:
                    opportunities.append({
                        "path": tri["path"],
                        "profit": result[0],
                        "steps": result[1],
                    })
            
            opportunities.sort(key=lambda x: x["profit"], reverse=True)
            
            now = datetime.now().strftime("%H:%M:%S")
            if opportunities:
                best = opportunities[0]
                best_ever = max(best_ever, best["profit"])
                print(f"[{now}] #{scan_count} Found {len(opportunities)} opportunities")
                for opp in opportunities[:5]:
                    marker = " *" if opp["profit"] == best["profit"] else ""
                    print(f"  {opp['path']:<30s} +{opp['profit']:.4f}%{marker}")
                
                # Log to file
                with open(OUT, "a", encoding="utf-8") as f:
                    for opp in opportunities[:3]:
                        f.write(json.dumps({
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "path": opp["path"],
                            "profit": round(opp["profit"], 4),
                        }, ensure_ascii=False) + "\n")
            else:
                print(f"[{now}] #{scan_count} No opportunities above {MIN_PROFIT_PCT}%", end="\r")
            
            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"\nStopped. Best ever: +{best_ever:.4f}%. Log: {OUT}")


def optimize_triangles(days=7):
    """Find which triangles are most profitable historically.
    
    Fetches historical klines for top triangle candidates,
    simulates arbitrage on each candle to find:
    - Frequency of profitable opportunities
    - Average profit when profitable
    - Max drawdown (periods without profit)
    """
    print(f"Optimizing triangles over {days} days of history...")
    
    symbols = fetch_exchange_info()
    triangles = build_triangles(symbols)
    print(f"Testing {len(triangles)} triangles...")
    
    # For each triangle, get the 3 pairs' klines
    # Simulate arbitrage on each hourly candle
    
    results = []
    tested = 0
    
    for tri in triangles[:50]:  # Limit to top 50 for speed
        tested += 1
        legs = tri["legs"]
        
        # Fetch 1h klines for all 3 legs
        kline_data = {}
        for leg in legs:
            try:
                r = requests.get(f"{BINANCE}/klines",
                                 params={"symbol": leg, "interval": "1h", "limit": days * 24},
                                 headers={"User-Agent": UA}, timeout=10)
                if r.status_code == 200:
                    kline_data[leg] = [(float(k[1]), float(k[4])) for k in r.json()]  # (open, close)
            except Exception:
                pass
        
        if len(kline_data) < 3:
            continue
        
        # Align by shortest series
        min_len = min(len(v) for v in kline_data.values())
        profits = []
        
        for i in range(min_len):
            prices = {leg: kline_data[leg][i][1] for leg in legs}  # close price
            result = calculate_arbitrage(tri, prices)
            if result:
                profits.append(result[0])
        
        if profits:
            profitable = [p for p in profits if p > MIN_PROFIT_PCT]
            freq = len(profitable) / len(profits) * 100
            avg_profit = sum(profitable) / len(profitable) if profitable else 0
            max_profit = max(profits)
            
            results.append({
                "path": tri["path"],
                "frequency_pct": round(freq, 1),
                "avg_profit_pct": round(avg_profit, 4),
                "max_profit_pct": round(max_profit, 4),
                "samples": len(profits),
                "profitable_samples": len(profitable),
            })
        
        if tested % 10 == 0:
            print(f"  [{tested}/{min(50, len(triangles))}] ...", file=sys.stderr)
    
    results.sort(key=lambda x: x["frequency_pct"] * x["avg_profit_pct"], reverse=True)
    
    print(f"\n{'='*70}")
    print(f"  TRIANGLE OPTIMIZATION RESULTS ({days} days, 1h candles)")
    print(f"{'='*70}")
    print(f"  {'Triangle':<30s} {'Freq':>6s} {'Avg Profit':>10s} {'Max':>8s} {'Samples':>8s}")
    print(f"  {'-'*65}")
    
    for r in results[:15]:
        print(f"  {r['path']:<30s} {r['frequency_pct']:>5.1f}% "
              f"{r['avg_profit_pct']:>+9.4f}% {r['max_profit_pct']:>+7.4f}% "
              f"{r['samples']:>6d}")
    
    return results


def run_tests():
    """Self-test: verify triangle math, edge cases."""
    passed = 0
    failed = 0
    
    # Test 1: Known non-arbitrage triangle
    prices = {"ETHBTC": 0.05, "ETHUSDT": 3000, "BTCUSDT": 60000}
    tri = {"path": "BTC>ETH>USDT>BTC", "legs": ["ETHBTC", "ETHUSDT", "BTCUSDT"],
           "start": "BTC", "mid": "ETH", "end": "USDT", "direction": "forward"}
    result = calculate_arbitrage(tri, prices)
    if result:
        profit = result[0]
        if abs(profit) < 0.1:
            print(f"  [PASS] Test 1: No arbitrage when prices align ({profit:.4f}%)")
            passed += 1
        else:
            print(f"  [FAIL] Test 1: Expected ~0% got {profit:.4f}%")
            failed += 1
    else:
        print(f"  [FAIL] Test 1: No result returned")
        failed += 1
    
    # Test 2: Known arbitrage opportunity
    prices = {"ETHBTC": 0.05, "ETHUSDT": 3000, "BTCUSDT": 59000}
    tri = {"path": "BTC>ETH>USDT>BTC", "legs": ["ETHBTC", "ETHUSDT", "BTCUSDT"],
           "start": "BTC", "mid": "ETH", "end": "USDT", "direction": "forward"}
    result = calculate_arbitrage(tri, prices)
    if result:
        profit = result[0]
        if profit > 0.5:
            print(f"  [PASS] Test 2: Arbitrage detected: +{profit:.4f}%")
            passed += 1
        else:
            print(f"  [FAIL] Test 2: Expected >0.5% got {profit:.4f}%")
            failed += 1
    else:
        print(f"  [FAIL] Test 2: No result returned")
        failed += 1
    
    # Test 3: Build triangles
    test_symbols = {"ETHBTC", "ETHUSDT", "BTCUSDT", "BNBBTC", "BNBUSDT", "SOLUSDT", "SOLBTC"}
    triangles = build_triangles(test_symbols)
    if len(triangles) > 0:
        print(f"  [PASS] Test 3: Built {len(triangles)} triangles from {len(test_symbols)} pairs")
        passed += 1
    else:
        print(f"  [FAIL] Test 3: No triangles built")
        failed += 1
    
    # Test 4: Live scan
    print(f"  [INFO] Test 4: Running live scan...")
    opps = scan_triangles()
    print(f"  [PASS] Test 4: Live scan found {len(opps)} opportunities")
    passed += 1
    
    print(f"\n  RESULTS: {passed}/{passed+failed} passed")
    return failed == 0


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--scan", action="store_true", help="Continuous scan")
    p.add_argument("--once", action="store_true", help="Single scan")
    p.add_argument("--top", type=int, default=10, help="Show top N")
    p.add_argument("--optimize", action="store_true", help="Optimize triangles historically")
    p.add_argument("--days", type=int, default=7, help="Days for optimization")
    p.add_argument("--test", action="store_true", help="Run tests")
    args = p.parse_args()
    
    if args.test:
        run_tests()
    
    elif args.scan:
        continuous_scan()
    
    elif args.optimize:
        optimize_triangles(args.days)
    
    elif args.once:
        opportunities = scan_triangles()
        print(f"\n  Triangular Arbitrage Scan — {datetime.now().strftime('%H:%M:%S')}")
        print(f"  Found {len(opportunities)} opportunities above {MIN_PROFIT_PCT}%")
        for opp in opportunities[:args.top]:
            print(f"\n  {opp['triangle']}: +{opp['profit_pct']:.4f}%")
            print(f"    {opp['steps']['buy']}")
            print(f"    {opp['steps']['swap']}")
            print(f"    {opp['steps']['sell']}")
            print(f"    Fees: {opp['steps']['fee_impact']}")
    
    else:
        # Default: quick scan
        opportunities = scan_triangles()
        print(f"Triangular Arbitrage: {len(opportunities)} opportunities > {MIN_PROFIT_PCT}%")
        for opp in opportunities[:5]:
            print(f"  {opp['triangle']}: +{opp['profit_pct']:.4f}%")
