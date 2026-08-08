"""Batch Backtest — test each agent on its active pairs, multi-TF, then optimize.

Runs: backtest → best TF found → optimize params → save config.
"""

import json, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).parent
OUT = SCRIPTS.parent / "backtest_results"
OUT.mkdir(exist_ok=True)

TESTS = [
    # (agent_name, symbol, days, timeframes, strategy_type)
    ("Trend (#1)", "UNIUSDT", 30, ["1h", "4h"], "trend"),
    ("Grid (#2)", "BTCUSDT", 14, ["15m", "1h"], "grid"),
    ("Grid (#2)", "ETHUSDT", 14, ["15m", "1h"], "grid"),
    ("Grid (#2)", "SOLUSDT", 14, ["15m", "1h"], "grid"),
    ("Max Grid (#3)", "ETHUSDT", 14, ["15m", "1h"], "grid"),
    ("Max Grid (#3)", "AVAXUSDT", 14, ["15m", "1h"], "grid"),
    ("Corridor (#4)", "ADAUSDT", 14, ["15m", "30m"], "grid"),
    ("Corridor (#4)", "UNIUSDT", 14, ["15m", "30m"], "grid"),
    ("XRP (#5)", "XRPUSDT", 14, ["15m", "30m", "1h"], "grid"),
    ("Stoch (#6)", "ETHUSDT", 14, ["15m", "1h"], "trend"),
]

results = {}
total = len(TESTS)
done = 0

print(f"\n{'='*65}")
print(f"  BATCH BACKTEST — {total} configs")
print(f"{'='*65}")

for agent, symbol, days, tfs, stype in TESTS:
    for tf in tfs:
        done += 1
        print(f"\n[{done}/{total * max(len(tfs), 1)}] {agent} on {symbol} {tf} ({days}d)...")

        try:
            r = subprocess.run(
                [sys.executable, str(SCRIPTS / "backtest_engine.py"),
                 "--strategy", stype, "--symbol", symbol, "--days", str(days), "--tf", tf],
                capture_output=True, text=True, timeout=300
            )
            # Parse PnL from output
            for line in r.stdout.splitlines():
                if "PnL:" in line and "ROI" in line:
                    parts = line.strip().split()
                    pnl_str = parts[1].replace("$", "").replace(",", "")
                    roi_str = parts[2].replace("(", "").replace("%", "")
                    try:
                        pnl = float(pnl_str)
                        roi = float(roi_str)
                        key = f"{agent}_{symbol}_{tf}"
                        results[key] = {"pnl": pnl, "roi": roi}
                        icon = "+" if pnl > 0 else ""
                        print(f"  {key}: {icon}${pnl:,.0f} ({roi:+.1f}%)")
                    except ValueError:
                        print(f"  {key}: parse error")

            if r.stderr:
                err_lines = [l for l in r.stderr.splitlines() if l.strip()]
                if err_lines and "Warning" not in err_lines[0]:
                    print(f"  [stderr] {r.stderr[:200]}")

        except subprocess.TimeoutExpired:
            print(f"  {agent} {symbol} {tf}: TIMEOUT")
        except Exception as e:
            print(f"  {agent} {symbol} {tf}: ERROR {e}")

# Summary
print(f"\n{'='*65}")
print(f"  BATCH RESULTS")
print(f"{'='*65}")
print(f"  {'Config':<35s} {'PnL':>10s} {'ROI':>8s}")
print(f"  {'-'*55}")

sorted_results = sorted(results.items(), key=lambda x: x[1]["pnl"], reverse=True)
for name, r in sorted_results:
    icon = "[+]" if r["pnl"] > 0 else "[ ]"
    print(f"  {icon} {name:<32s} ${r['pnl']:>+8,.0f} {r['roi']:>+7.1f}%")

# Best per agent
print(f"\n  BEST PER AGENT:")
for agent, _, _, tfs, _ in TESTS:
    agent_results = {k: v for k, v in results.items() if k.startswith(agent)}
    if agent_results:
        best = max(agent_results.items(), key=lambda x: x[1]["pnl"])
        print(f"    {agent}: {best[0]} — ${best[1]['pnl']:+,.0f} ({best[1]['roi']:+.1f}%)")

# Save
report = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "results": {k: v for k, v in results.items()},
}
with open(OUT / f"batch_results_{datetime.now().strftime('%Y%m%d_%H%M')}.json", "w", encoding="utf-8") as f:
    json.dump(report, f, indent=2, ensure_ascii=False)
print(f"\n  Report saved to backtest_results/")
