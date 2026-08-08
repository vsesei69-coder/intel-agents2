"""Backtest Engine — runs any strategy against historical Binance data.

Feeds historical klines through the same per-cycle fill engine used in live trading.
Produces: total PnL, win rate, max drawdown, Sharpe ratio, trades/day.

Usage:
  python backtest_engine.py --strategy trend --symbol BTCUSDT --days 30 --tf 15m
  python backtest_engine.py --strategy grid --symbol ETHUSDT --days 7 --tf 5m
  python backtest_engine.py --all  # test all strategies on multiple pairs/TFs
"""

import json, os, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from statistics import mean, stdev

import requests

BINANCE_BASE = "https://api.binance.com/api/v3"
UA = "BacktestEngine/1.0"

OUTPUT_DIR = Path(__file__).parent.parent / "backtest_results"
OUTPUT_DIR.mkdir(exist_ok=True)

# Import strategy functions
sys.path.insert(0, str(Path(__file__).parent))


def fetch_historical(symbol, interval, days):
    """Fetch historical klines in batches (Binance limit: 1000 per request)."""
    all_klines = []
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)

    while start_ms < end_ms:
        try:
            r = requests.get(f"{BINANCE_BASE}/klines",
                             params={"symbol": symbol, "interval": interval,
                                     "startTime": start_ms, "endTime": end_ms, "limit": 1000},
                             headers={"User-Agent": UA}, timeout=15)
            if r.status_code != 200:
                break
            batch = r.json()
            if not batch:
                break
            for k in batch:
                all_klines.append({
                    "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]),
                    "v": float(k[5]),
                    "t": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
                })
            start_ms = int(batch[-1][0]) + 1
            time.sleep(0.2)
        except Exception as e:
            print(f"  Fetch error: {e}", file=sys.stderr)
            break

    return all_klines


# ══════════════════════════════════════════════════════════════════════
# Strategy adapters — use same signal logic as live agents
# ══════════════════════════════════════════════════════════════════════

def compute_bollinger(closes, period=20, mult=2.0):
    if len(closes) < period:
        return None
    r = closes[-period:]
    sma = mean(r)
    s = stdev(r) if len(r) > 1 else 0
    return {"sma": sma, "upper": sma + mult * s, "lower": sma - mult * s,
            "bw": (s * 2) / sma * 100 if sma > 0 else 0,
            "pos": (closes[-1] - sma + mult * s) / (s * 2 * mult) * 100 if s > 0 else 50}


def compute_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    g, l = [], []
    for i in range(len(closes) - period, len(closes)):
        d = closes[i] - closes[i - 1]
        (g if d > 0 else l).append(abs(d))
        (l if d > 0 else g).append(0)
    ag, al = mean(g) if g else 0, mean(l) if l else 0
    return 100 - (100 / (1 + ag / al)) if al > 0 else 100


def compute_atr(candles, period=14):
    if len(candles) < period + 1:
        return 0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return mean(trs[-period:])


# ══════════════════════════════════════════════════════════════════════
# Backtest Engine
# ══════════════════════════════════════════════════════════════════════

TAKER_FEE = 0.0004
SLIPPAGE = 0.001
FUNDING_RATE = 0.0001
BANKROLL = 1000.0
LEVERAGE = 50
RISK_PER_TRADE = 0.03


class BacktestResult:
    def __init__(self):
        self.trades = []
        self.total_pnl = 0.0
        self.total_fees = 0.0
        self.total_slip = 0.0
        self.total_fund = 0.0
        self.wins = 0
        self.losses = 0
        self.equity_curve = [BANKROLL]
        self.max_drawdown_pct = 0.0
        self.peak_equity = BANKROLL


def compute_costs(size_usd, hours_open):
    entry_notional = size_usd / LEVERAGE
    fee = entry_notional * TAKER_FEE * 2
    slip = size_usd * SLIPPAGE
    fund = size_usd * FUNDING_RATE * (hours_open / 8)
    return fee + slip + fund


def backtest_trend(symbol, klines, tf="15m"):
    """Backtest Agent #1 — Trend strategy (BB + RSI + multi-TF)."""
    result = BacktestResult()
    positions = []
    warmup = 50

    for i in range(warmup, len(klines)):
        window = klines[max(0, i-100):i+1]
        candle = klines[i]
        price = candle["c"]
        closes = [c["c"] for c in window]

        # Generate signal
        bb = compute_bollinger(closes, 20, 2.0)
        r = compute_rsi(closes, 14)
        a = compute_atr(window, 14)
        if not bb or a == 0:
            continue

        direction = None
        confidence = 0
        if bb["pos"] < 25 and r < 40:
            direction = "long"
            confidence = (1 - bb["pos"]/25) * 0.3 + (1 - r/40) * 0.3 + 0.4
        elif bb["pos"] > 75 and r > 60:
            direction = "short"
            confidence = (bb["pos"]/100) * 0.3 + (r/100) * 0.3 + 0.4

        if not direction or confidence < 0.5:
            continue

        # Entry/TP/SL
        entry = price
        if direction == "long":
            tp = price * 1.015
            sl = price - a * 1.5
        else:
            tp = price * 0.985
            sl = price + a * 1.5

        risk_amount = BANKROLL * RISK_PER_TRADE
        risk_per_unit = abs(entry - sl)
        if risk_per_unit == 0:
            continue
        size_usd = min((risk_amount / risk_per_unit) * LEVERAGE, BANKROLL * 0.5)

        pos = {
            "direction": direction, "entry": entry, "tp": tp, "sl": sl,
            "size_usd": size_usd, "opened_at": candle["t"], "filled": True,
            "trailing_active": False, "trailing_sl": None
        }
        positions.append(pos)

        # Check existing positions against future candles
        for p in positions[:]:
            if p.get("closed"):
                continue

            # Trailing stop
            if p["direction"] == "long":
                if not p["trailing_active"] and price >= p["entry"] * 1.015:
                    p["trailing_active"] = True
                if p["trailing_active"]:
                    new_sl = price * 0.992
                    if p["trailing_sl"] is None or new_sl > p["trailing_sl"]:
                        p["trailing_sl"] = new_sl
                effective_sl = p["trailing_sl"] if p["trailing_sl"] else p["sl"]
            else:
                if not p["trailing_active"] and price <= p["entry"] * 0.985:
                    p["trailing_active"] = True
                if p["trailing_active"]:
                    new_sl = price * 1.008
                    if p["trailing_sl"] is None or new_sl < p["trailing_sl"]:
                        p["trailing_sl"] = new_sl
                effective_sl = p["trailing_sl"] if p["trailing_sl"] else p["sl"]

            # Check close
            closed = False
            exit_price = None
            hit_tp = False

            if p["direction"] == "long":
                if candle["h"] >= p["tp"]:
                    exit_price = p["tp"] * (1 - SLIPPAGE)
                    hit_tp = True
                    closed = True
                elif candle["l"] <= effective_sl:
                    exit_price = effective_sl * (1 - SLIPPAGE)
                    closed = True
            else:
                if candle["l"] <= p["tp"]:
                    exit_price = p["tp"] * (1 + SLIPPAGE)
                    hit_tp = True
                    closed = True
                elif candle["h"] >= effective_sl:
                    exit_price = effective_sl * (1 + SLIPPAGE)
                    closed = True

            if closed and exit_price:
                p["closed"] = True
                p["exit_price"] = exit_price
                p["closed_at"] = candle["t"]

                if p["direction"] == "long":
                    gross_pct = (exit_price - p["entry"]) / p["entry"]
                else:
                    gross_pct = (p["entry"] - exit_price) / p["entry"]
                gross_pnl = p["size_usd"] * gross_pct * LEVERAGE

                hours = (candle["t"] - p["opened_at"]).total_seconds() / 3600
                costs = compute_costs(p["size_usd"], hours)
                net_pnl = gross_pnl - costs

                result.trades.append({
                    "symbol": symbol, "direction": p["direction"],
                    "entry": p["entry"], "exit": exit_price,
                    "pnl": net_pnl, "hit_tp": hit_tp,
                    "opened": p["opened_at"].isoformat(),
                    "closed": candle["t"].isoformat(),
                })
                result.total_pnl += net_pnl
                if net_pnl > 0:
                    result.wins += 1
                else:
                    result.losses += 1

                result.equity_curve.append(BANKROLL + result.total_pnl)
                positions.remove(p)

    # Finalize stats
    if result.equity_curve:
        peak = result.equity_curve[0]
        for eq in result.equity_curve:
            peak = max(peak, eq)
            dd = (peak - eq) / peak * 100 if peak > 0 else 0
            result.max_drawdown_pct = max(result.max_drawdown_pct, dd)

    return result


def backtest_grid(symbol, klines, grid_levels=5, grid_step_pct=0.003):
    """Backtest Agent #2 — Grid strategy (scale-in levels)."""
    result = BacktestResult()
    grids = []
    warmup = 50
    cooldown = 0

    for i in range(warmup, len(klines)):
        window = klines[max(0, i-100):i+1]
        candle = klines[i]
        price = candle["c"]
        closes = [c["c"] for c in window]

        bb = compute_bollinger(closes, 20, 2.0)
        r = compute_rsi(closes, 14)
        a = compute_atr(window, 14)
        if not bb or a == 0:
            continue

        # Open grid signal
        if len(grids) < 3 and cooldown <= 0:
            direction = None
            confidence = 0
            if bb["pos"] < 30 and r < 40:
                direction = "long"
                confidence = 0.6
            elif bb["pos"] > 70 and r > 60:
                direction = "short"
                confidence = 0.6

            if direction and confidence >= 0.5:
                grid_step = price * grid_step_pct
                levels_list = []
                for j in range(grid_levels):
                    entry_lvl = price - grid_step * (j+1) if direction == "long" else price + grid_step * (j+1)
                    tp_lvl = price if direction == "long" else price
                    levels_list.append({
                        "entry": entry_lvl, "tp": tp_lvl,
                        "size_usd": 70 * (0.85 ** j),  # scaled
                        "filled": False, "tp_hit": False,
                    })
                sl = levels_list[-1]["entry"] - a * 2 if direction == "long" else levels_list[-1]["entry"] + a * 2
                grids.append({
                    "direction": direction, "levels": levels_list,
                    "sl": sl, "opened_at": candle["t"],
                })
                cooldown = 20  # candles cooldown

        cooldown = max(0, cooldown - 1)

        # Check grid fills
        for grid in grids[:]:
            all_resolved = True
            grid_pnl = 0

            # Global SL
            sl_hit = (grid["direction"] == "long" and candle["l"] <= grid["sl"]) or \
                     (grid["direction"] == "short" and candle["h"] >= grid["sl"])

            for lvl in grid["levels"]:
                if lvl["tp_hit"]:
                    continue
                all_resolved = False

                if not lvl["filled"]:
                    if grid["direction"] == "long" and candle["l"] <= lvl["entry"]:
                        lvl["filled"] = True
                        lvl["fill_price"] = candle["l"]
                    elif grid["direction"] == "short" and candle["h"] >= lvl["entry"]:
                        lvl["filled"] = True
                        lvl["fill_price"] = candle["h"]

                if lvl["filled"] and not lvl["tp_hit"]:
                    if sl_hit:
                        lvl["tp_hit"] = True
                        lvl["exit_price"] = candle["l"] if grid["direction"] == "long" else candle["h"]
                    elif (grid["direction"] == "long" and candle["h"] >= lvl["tp"]) or \
                         (grid["direction"] == "short" and candle["l"] <= lvl["tp"]):
                        lvl["tp_hit"] = True
                        lvl["exit_price"] = lvl["tp"] * (1 - SLIPPAGE) if grid["direction"] == "long" else lvl["tp"] * (1 + SLIPPAGE)

                    if lvl["tp_hit"] and lvl.get("fill_price"):
                        pct = (lvl["exit_price"] - lvl["fill_price"]) / lvl["fill_price"] if grid["direction"] == "long" \
                              else (lvl["fill_price"] - lvl["exit_price"]) / lvl["fill_price"]
                        gross = lvl["size_usd"] * pct * LEVERAGE
                        costs = compute_costs(lvl["size_usd"], 1)
                        lvl["pnl"] = gross - costs
                        grid_pnl += lvl["pnl"]

            # Grid complete
            if all_resolved or sl_hit:
                result.trades.append({
                    "symbol": symbol, "direction": grid["direction"],
                    "pnl": grid_pnl,
                    "levels_filled": sum(1 for l in grid["levels"] if l.get("pnl") is not None),
                    "opened": grid["opened_at"].isoformat(),
                    "closed": candle["t"].isoformat(),
                })
                result.total_pnl += grid_pnl
                if grid_pnl > 0:
                    result.wins += 1
                else:
                    result.losses += 1
                result.equity_curve.append(BANKROLL + result.total_pnl)
                grids.remove(grid)

    if result.equity_curve:
        peak = result.equity_curve[0]
        for eq in result.equity_curve:
            peak = max(peak, eq)
            dd = (peak - eq) / peak * 100 if peak > 0 else 0
            result.max_drawdown_pct = max(result.max_drawdown_pct, dd)

    return result


def print_report(strategy_name, symbol, tf, days, result):
    trades = result.trades
    total = len(trades)
    wr = result.wins / total * 100 if total > 0 else 0
    roi = result.total_pnl / BANKROLL * 100

    avg_win = mean([t["pnl"] for t in trades if t["pnl"] > 0]) if result.wins > 0 else 0
    avg_loss = mean([t["pnl"] for t in trades if t["pnl"] <= 0]) if result.losses > 0 else 0
    profit_factor = abs(sum(t["pnl"] for t in trades if t["pnl"] > 0) / \
                        sum(t["pnl"] for t in trades if t["pnl"] <= 0)) if result.losses > 0 else 999

    daily_trades = total / days if days > 0 else 0

    print(f"\n{'='*65}")
    print(f"  BACKTEST: {strategy_name.upper()} | {symbol} | {tf} | {days} days")
    print(f"{'='*65}")
    print(f"  PnL:         ${result.total_pnl:+,.2f}  ({roi:+.1f}% ROI)")
    print(f"  Trades:      {total} ({result.wins}W/{result.losses}L)  WR: {wr:.0f}%")
    print(f"  Avg Win:     ${avg_win:+,.2f}  |  Avg Loss: ${avg_loss:+,.2f}")
    print(f"  Profit Factor: {profit_factor:.2f}")
    print(f"  Max DD:      {result.max_drawdown_pct:.1f}%")
    print(f"  Trades/day:  {daily_trades:.1f}")
    print(f"  Equity:      ${BANKROLL:,.0f} -> ${BANKROLL + result.total_pnl:,.0f}")


def run_full_suite():
    """Run backtests on all strategies, multiple pairs, multiple TFs."""
    configs = [
        # (strategy, pairs, tfs, days)
        ("trend", ["BTCUSDT", "ETHUSDT", "SOLUSDT"], ["15m", "1h"], 30),
        ("grid", ["BTCUSDT", "ETHUSDT"], ["15m"], 14),
    ]

    all_results = {}

    for strategy, pairs, tfs, days in configs:
        for symbol in pairs:
            for tf in tfs:
                print(f"\n  [{datetime.now().strftime('%H:%M:%S')}] "
                      f"Backtesting {strategy} on {symbol} {tf} ({days}d)...", file=sys.stderr)

                klines = fetch_historical(symbol, tf, days)
                if len(klines) < 100:
                    print(f"    Not enough data ({len(klines)} candles)", file=sys.stderr)
                    continue

                if strategy == "trend":
                    result = backtest_trend(symbol, klines, tf)
                elif strategy == "grid":
                    result = backtest_grid(symbol, klines)
                else:
                    continue

                print_report(strategy, symbol, tf, days, result)
                all_results[f"{strategy}_{symbol}_{tf}"] = {
                    "pnl": result.total_pnl, "trades": len(result.trades),
                    "wr": result.wins / len(result.trades) * 100 if result.trades else 0,
                    "max_dd": result.max_drawdown_pct, "roi": result.total_pnl / BANKROLL * 100,
                }

    # Summary table
    print(f"\n{'='*65}")
    print(f"  SUMMARY")
    print(f"{'='*65}")
    print(f"  {'Config':<30} {'PnL':>8} {'Trades':>7} {'WR':>6} {'MaxDD':>6} {'ROI':>7}")
    print(f"  {'-'*65}")
    for name, r in sorted(all_results.items()):
        print(f"  {name:<30} ${r['pnl']:>+7,.0f} {r['trades']:>6} "
              f"{r['wr']:>5.0f}% {r['max_dd']:>5.1f}% {r['roi']:>+6.0f}%")

    # Save detailed report
    report_path = OUTPUT_DIR / f"backtest_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "bankroll": BANKROLL, "leverage": LEVERAGE,
            "results": {k: v for k, v in all_results.items()},
        }, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  Report saved: {report_path}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--strategy", default="trend", choices=["trend", "grid"])
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--tf", default="15m")
    p.add_argument("--all", action="store_true")
    args = p.parse_args()

    if args.all:
        run_full_suite()
    else:
        print(f"Fetching {args.days} days of {args.symbol} {args.tf} candles...", file=sys.stderr)
        klines = fetch_historical(args.symbol, args.tf, args.days)
        print(f"  Got {len(klines)} candles. Running backtest...", file=sys.stderr)

        if args.strategy == "trend":
            result = backtest_trend(args.symbol, klines, args.tf)
        else:
            result = backtest_grid(args.symbol, klines)

        print_report(args.strategy, args.symbol, args.tf, args.days, result)
