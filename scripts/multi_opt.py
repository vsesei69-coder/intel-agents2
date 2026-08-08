"""Multi-Strategy Multi-TF Optimizer — runs genetic optimizer for ALL strategies.

Tests each strategy on 15m/1h/4h, finds best TF + optimal params.
"""
import json, sys, time
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).parent
OUT = SCRIPTS.parent / "backtest_results"
OUT.mkdir(exist_ok=True)

# Strategy → symbol mapping
STRATEGIES = {
    "trend":        {"symbol": "BTCUSDT", "tfs": ["15m", "1h", "4h"]},
    "grid":         {"symbol": "BTCUSDT", "tfs": ["15m", "1h"]},
    "max_grid":     {"symbol": "ETHUSDT", "tfs": ["15m", "1h"]},
    "corridor":     {"symbol": "ADAUSDT", "tfs": ["15m", "30m"]},
    "xrp":          {"symbol": "XRPUSDT", "tfs": ["15m", "30m", "1h"]},
    "stoch":        {"symbol": "ETHUSDT", "tfs": ["15m", "1h"]},
    "level_grid":   {"symbol": "BTCUSDT", "tfs": ["1h", "4h"]},
}

results = {}

for strategy, cfg in STRATEGIES.items():
    symbol = cfg["symbol"]
    print(f"\n{'='*60}")
    print(f"  STRATEGY: {strategy.upper()} on {symbol}")
    print(f"{'='*60}")

    best_tf = None
    best_pnl = -999999

    for tf in cfg["tfs"]:
        print(f"\n  --- {tf} ---")
        sys.stdout.flush()

        try:
            import subprocess
            r = subprocess.run(
                [sys.executable, str(SCRIPTS / "genetic_optimizer.py"), "--once",
                 "--symbol", symbol, "--tf", tf, "--days", "14"],
                capture_output=True, text=True, timeout=180
            )
            # Parse PnL from output
            for line in r.stdout.splitlines():
                if "BEST PARAMS:" in line:
                    # Next lines have the data
                    pass
                if "PnL:" in line and "Trades:" in line:
                    # Format: PnL: $+1,391 | Trades: 89 | DD: 19.7% | Score: 1209
                    parts = line.strip().split("|")
                    pnl_str = parts[0].split("$")[1].replace(",", "")
                    try:
                        pnl = float(pnl_str)
                        print(f"  Result: PnL=${pnl:+,.0f} on {tf}")
                        results[f"{strategy}_{tf}"] = {"pnl": pnl, "tf": tf}
                        if pnl > best_pnl:
                            best_pnl = pnl
                            best_tf = tf
                    except:
                        pass

        except subprocess.TimeoutExpired:
            print(f"  {tf}: TIMEOUT")
        except Exception as e:
            print(f"  {tf}: ERROR {e}")

    if best_tf:
        print(f"\n  BEST TF for {strategy}: {best_tf} (PnL=${best_pnl:+,.0f})")

# Final summary
print(f"\n{'='*60}")
print(f"  MULTI-STRATEGY OPTIMIZATION RESULTS")
print(f"{'='*60}")
for name, r in sorted(results.items(), key=lambda x: x[1]["pnl"], reverse=True):
    print(f"  {name:<25s} ${r['pnl']:>+8,.0f}")

with open(OUT / f"multi_strategy_{datetime.now().strftime('%Y%m%d_%H%M')}.json", "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, ensure_ascii=False)
