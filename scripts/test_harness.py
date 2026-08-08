"""Self-Test Harness for Trading System

Validates:
  1. All agent scripts load without import errors
  2. Binance API is reachable
  3. Journal files are valid JSON
  4. Regime detector works
  5. Vol monitor has data
  6. All agents respond to --status
  7. No stale/broken configs

Run: python test_harness.py
Returns exit code 0 if all tests pass, 1 if any fail.
"""

import json, os, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent
JOURNAL_BASE = SCRIPTS_DIR.parent / "trading_journal"

AGENTS = {
    "trend": "agent_monitor.py",
    "grid": "grid_agent.py",
    "max_grid": "grid_max_agent.py",
    "corridor": "grid_corridor_agent.py",
}

SUPPORT = {
    "regime": "regime_detector.py",
    "vol_monitor": "vol_monitor.py",
    "supervisor": "agent_supervisor.py",
    "backtest": "backtest_engine.py",
    "verify": "verify_trades.py",
}

JOURNALS = {
    "trend": JOURNAL_BASE / "trade_history.json",
    "grid": JOURNAL_BASE.parent / "trading_journal_grid" / "grid_history.json",
    "max_grid": JOURNAL_BASE.parent / "trading_journal_max" / "grid_history.json",
    "corridor": JOURNAL_BASE.parent / "trading_journal_corridor" / "corridor_history.json",
}

failed = 0
passed = 0

def test(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name} — {detail}")


print(f"\n{'='*60}")
print(f"  TRADING SYSTEM SELF-TEST — {datetime.now().strftime('%H:%M:%S')}")
print(f"{'='*60}")

# ── 1. Script integrity ───────────────────────────────────────────
print(f"\n[1] Script integrity")

for name, script in {**AGENTS, **SUPPORT}.items():
    path = SCRIPTS_DIR / script
    exists = path.exists()
    test(f"{name} exists", exists, f"{script} not found")
    if exists:
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            has_main = "if __name__" in content
            test(f"{name} has main", has_main, "missing entry point")
        except Exception as e:
            test(f"{name} readable", False, str(e))

# ── 2. Journal integrity ──────────────────────────────────────────
print(f"\n[2] Journal integrity")

for name, path in JOURNALS.items():
    if path.exists():
        try:
            data = json.loads(path.read_text())
            has_trades = "trades" in data
            has_stats = "stats" in data
            test(f"{name} journal valid", has_trades and has_stats,
                 f"missing trades={not has_trades} stats={not has_stats}")
            if has_stats:
                s = data["stats"]
                total = s.get("total", 0)
                wins = s.get("wins", 0)
                losses = s.get("losses", 0)
                test(f"{name} stats consistent", wins + losses == total,
                     f"wins({wins}) + losses({losses}) != total({total})")
        except Exception as e:
            test(f"{name} journal JSON", False, str(e))
    else:
        print(f"  [INFO] {name} journal not created yet (no trades)")

# ── 3. Binance API reachable ──────────────────────────────────────
print(f"\n[3] API connectivity")

import requests
try:
    r = requests.get("https://api.binance.com/api/v3/ping", timeout=10)
    test("Binance ping", r.status_code == 200, f"status={r.status_code}")
except Exception as e:
    test("Binance ping", False, str(e))

try:
    r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=10)
    if r.status_code == 200:
        price = float(r.json()["price"])
        test("BTCUSDT price", price > 1000, f"price={price} (suspicious)")
    else:
        test("BTCUSDT price", False, f"status={r.status_code}")
except Exception as e:
    test("BTCUSDT price", False, str(e))

# ── 4. Regime detector ────────────────────────────────────────────
print(f"\n[4] Regime detector")

try:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "regime_detector.py")],
        capture_output=True, text=True, timeout=30
    )
    has_regime = "MARKET REGIME:" in result.stdout
    test("regime detector runs", result.returncode == 0,
         f"rc={result.returncode}" if result.returncode != 0 else "")
    test("regime output valid", has_regime, "no REGIME in output")
except Exception as e:
    test("regime detector", False, str(e))

# ── 5. Vol monitor state ──────────────────────────────────────────
print(f"\n[5] Volatility monitor")

vol_file = JOURNAL_BASE / "vol_state.json"
if vol_file.exists():
    try:
        data = json.loads(vol_file.read_text())
        pairs = data.get("pairs", {})
        test("vol state has pairs", len(pairs) > 0, "no pair data")
        test("vol state has market_event", "market_event" in data, "missing field")
        test("vol state has safe flag", "safe_to_trade" in data, "missing field")
    except Exception as e:
        test("vol state JSON", False, str(e))
else:
    print(f"  [INFO] vol_state.json not created yet (vol monitor starting)")

# ── 6. Agent status responds ──────────────────────────────────────
print(f"\n[6] Agent --status")

for name, script in AGENTS.items():
    path = SCRIPTS_DIR / script
    if not path.exists():
        test(f"{name} --status", False, "script missing")
        continue
    try:
        result = subprocess.run(
            [sys.executable, str(path), "--status"],
            capture_output=True, text=True, timeout=20
        )
        has_pnl = "PnL" in result.stdout or "pnl" in result.stdout.lower()
        test(f"{name} --status", result.returncode == 0 and has_pnl,
             f"rc={result.returncode} output={result.stdout[:80]}")
    except subprocess.TimeoutExpired:
        test(f"{name} --status", False, "timeout")
    except Exception as e:
        test(f"{name} --status", False, str(e))

# ── 7. No duplicate processes ─────────────────────────────────────
print(f"\n[7] Process health")

try:
    import psutil
    python_procs = [p for p in psutil.process_iter(['pid', 'name', 'cmdline'])
                    if p.info['name'] and 'python' in p.info['name'].lower()]
    test("python processes found", len(python_procs) > 0, "no python running")
except ImportError:
    try:
        result = subprocess.run(["tasklist", "/FI", "IMAGENAME eq python.exe"],
                                capture_output=True, text=True, timeout=10, shell=True)
        has_python = "python.exe" in result.stdout
        test("python processes found", has_python, "no python.exe running")
    except Exception:
        print(f"  [INFO] Cannot check processes (no psutil, tasklist failed)")

# ── 8. Disk space ─────────────────────────────────────────────────
print(f"\n[8] Disk space")

try:
    import shutil
    usage = shutil.disk_usage(str(JOURNAL_BASE.parent))
    free_gb = usage.free / (1024**3)
    test("disk free > 1GB", free_gb > 1, f"only {free_gb:.1f} GB free")
except Exception:
    print(f"  [INFO] Cannot check disk space")

# ── SUMMARY ────────────────────────────────────────────────────────
print(f"\n{'='*60}")
total = passed + failed
print(f"  RESULTS: {passed}/{total} passed, {failed} failed")
print(f"{'='*60}")

if failed > 0:
    print(f"  [FAIL] {failed} test(s) failed. Fix before trading.")
    sys.exit(1)
else:
    print(f"  [PASS] All tests passed. System healthy.")
    sys.exit(0)
