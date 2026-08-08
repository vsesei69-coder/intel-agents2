"""Genetic Parameter Optimizer — 100x faster than grid search.

Population of 30 individuals, 15 generations = 450 evaluations vs 4608+.
Each individual = parameter set for backtest.
Fitness = PnL penalized by drawdown and overtrading.
Tournament selection, uniform crossover, gaussian mutation, elitism top-3.
"""

import json, random, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from statistics import mean, stdev
import requests

BINANCE = "https://api.binance.com/api/v3"
UA = "GenOpt/1.0"
OUT = Path(__file__).parent.parent / "backtest_results"
OUT.mkdir(exist_ok=True)
BANKROLL = 1000.0
TAKER_FEE = 0.0004; SLIPPAGE = 0.001; FUNDING_RATE = 0.0001

POP_SIZE = 10
GENERATIONS = 5
ELITISM = 2
TOURNAMENT = 5
MUTATION_RATE = 0.15
MUTATION_SIGMA = 0.05

random.seed(42)


def fetch_klines(symbol, interval, days):
    all_klines = []
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
                all_klines.append({"o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                                   "c": float(k[4]), "v": float(k[5]),
                                   "t": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc)})
            start_ms = int(batch[-1][0]) + 1
            time.sleep(0.15)
        except Exception: break
    return all_klines


def bb(cl, p=20, m=2.0):
    if len(cl) < p: return None
    r = cl[-p:]; sm = mean(r); s = stdev(r) if len(r) > 1 else 0
    return {"pos": (cl[-1] - sm + m * s) / (s * 2 * m) * 100 if s > 0 else 50}

def rsi_calc(cl, p=14):
    if len(cl) < p + 1: return 50
    g, l = [], []
    for i in range(len(cl) - p, len(cl)):
        d = cl[i] - cl[i - 1]
        (g if d > 0 else l).append(abs(d))
        (l if d > 0 else g).append(0)
    ag, al = mean(g) if g else 0, mean(l) if l else 0
    return 100 - (100 / (1 + ag / al)) if al > 0 else 100

def atr_calc(cd, p=14):
    if len(cd) < p + 1: return 0
    trs = [max(cd[i]["h"] - cd[i]["l"], abs(cd[i]["h"] - cd[i - 1]["c"]), abs(cd[i]["l"] - cd[i - 1]["c"])) for i in range(1, len(cd))]
    return mean(trs[-p:])

def costs(size, hours, lev):
    n = size / lev
    return n * TAKER_FEE * 2 + size * SLIPPAGE + size * FUNDING_RATE * (hours / 8)


def random_params():
    return {
        "leverage": random.choice([20, 25, 30, 35, 40, 50]),
        "conf_min": round(random.uniform(0.50, 0.70), 2),
        "max_pos_pct": round(random.uniform(0.05, 0.20), 2),
        "bb_low": random.randint(15, 30),
        "bb_high": random.randint(70, 85),
        "rsi_low": random.randint(30, 42),
        "rsi_high": random.randint(58, 70),
        "tp_atr": round(random.uniform(1.0, 3.0), 1),
        "sl_atr": round(random.uniform(1.0, 2.5), 1),
        "trail_act": round(random.uniform(1.01, 1.025), 3),
        "trail_off": round(random.uniform(0.985, 0.995), 3),
    }


def mutate(params):
    child = params.copy()
    for key in child:
        if random.random() < MUTATION_RATE:
            if key in ("leverage", "bb_low", "bb_high", "rsi_low", "rsi_high"):
                child[key] = max(1, child[key] + random.choice([-5, -1, 1, 5]))
            elif key in ("conf_min", "max_pos_pct", "tp_atr", "sl_atr", "trail_act", "trail_off"):
                delta = random.gauss(0, MUTATION_SIGMA)
                child[key] = max(0.01, min(0.99, child[key] + delta))
    return child


def crossover(p1, p2):
    child = {}
    for key in p1:
        child[key] = p1[key] if random.random() < 0.5 else p2[key]
    return child


def fitness_func(klines, params):
    lev = params["leverage"]; c_min = params["conf_min"]; mp = params["max_pos_pct"]
    bl = params["bb_low"]; bh = params["bb_high"]; rl = params["rsi_low"]; rh = params["rsi_high"]
    ta = params["tp_atr"]; sa = params["sl_atr"]; tr_a = params["trail_act"]; tr_o = params["trail_off"]

    trades = []; equity = BANKROLL; peak = BANKROLL; max_dd = 0; positions = []

    for i in range(50, len(klines)):
        window = klines[max(0, i - 100):i + 1]; c = klines[i]; px = c["c"]; cls = [k["c"] for k in window]
        bb_d = bb(cls); r = rsi_calc(cls); a = atr_calc(window)
        if not bb_d or a == 0: continue

        direction = None; conf = 0
        if bb_d["pos"] < bl and r < rl: direction = "long"; conf = (1 - bb_d["pos"] / bl) * 0.4 + (1 - r / rl) * 0.3 + 0.3
        elif bb_d["pos"] > bh and r > rh: direction = "short"; conf = (bb_d["pos"] / 100) * 0.4 + (r / 100) * 0.3 + 0.3

        if direction and conf >= c_min and len(positions) < 3:
            tp = px * (1 + a * ta / px) if direction == "long" else px * (1 - a * ta / px)
            sl = px - a * sa if direction == "long" else px + a * sa
            sz = min(BANKROLL * mp, equity * mp)
            positions.append({"d": direction, "e": px, "tp": tp, "sl": sl, "sz": sz, "op": c["t"], "tr": False, "tsl": None})

        for p in positions[:]:
            if p["d"] == "long":
                if not p["tr"] and px >= p["e"] * tr_a: p["tr"] = True
                if p["tr"]:
                    ns = px * tr_o
                    if p["tsl"] is None or ns > p["tsl"]: p["tsl"] = ns
                ef = p["tsl"] if p["tsl"] else p["sl"]
            else:
                if not p["tr"] and px <= p["e"] * (2 - tr_a): p["tr"] = True
                if p["tr"]:
                    ns = px * (2 - tr_o)
                    if p["tsl"] is None or ns < p["tsl"]: p["tsl"] = ns
                ef = p["tsl"] if p["tsl"] else p["sl"]

            done = False; ex = None
            if p["d"] == "long":
                if c["h"] >= p["tp"]: ex = p["tp"] * (1 - SLIPPAGE); done = True
                elif c["l"] <= ef: ex = ef * (1 - SLIPPAGE); done = True
            else:
                if c["l"] <= p["tp"]: ex = p["tp"] * (1 + SLIPPAGE); done = True
                elif c["h"] >= ef: ex = ef * (1 + SLIPPAGE); done = True

            if done and ex:
                pct = (ex - p["e"]) / p["e"] if p["d"] == "long" else (p["e"] - ex) / p["e"]
                gross = p["sz"] * pct * lev
                hrs = max((c["t"] - p["op"]).total_seconds() / 3600, 0)
                net = gross - costs(p["sz"], hrs, lev)
                trades.append({"pnl": net})
                equity += net; peak = max(peak, equity)
                max_dd = max(max_dd, (peak - equity) / peak * 100 if peak > 0 else 0)
                positions.remove(p)
            elif (c["t"] - p["op"]).total_seconds() > 172800:
                ex = px * (1 - SLIPPAGE) if p["d"] == "long" else px * (1 + SLIPPAGE)
                pct = (ex - p["e"]) / p["e"] if p["d"] == "long" else (p["e"] - ex) / p["e"]
                net = p["sz"] * pct * lev - costs(p["sz"], 48, lev)
                trades.append({"pnl": net}); equity += net
                positions.remove(p)

    total_pnl = sum(t["pnl"] for t in trades)
    fitness = total_pnl * (1 - max_dd / 200) - len(trades) * 0.5
    return {"pnl": total_pnl, "trades": len(trades), "max_dd": max_dd, "fitness": fitness, "equity": equity}


def optimize(symbol, interval, days):
    print(f"\n{'='*55}")
    print(f"  GENETIC OPTIMIZER: {symbol} {interval} ({days}d)")
    print(f"  Pop: {POP_SIZE} x Gen: {GENERATIONS} = {POP_SIZE*GENERATIONS} evals")
    print(f"{'='*55}")

    klines = fetch_klines(symbol, interval, days)
    if len(klines) < 100: return None
    print(f"  {len(klines)} candles loaded")

    # Initial population
    pop = [random_params() for _ in range(POP_SIZE)]
    best_overall = None; best_score = -999999

    for gen in range(GENERATIONS):
        # Evaluate
        scores = []
        for ind in pop:
            r = fitness_func(klines, ind)
            scores.append(r)
            if r["fitness"] > best_score:
                best_score = r["fitness"]
                best_overall = {"params": ind, **r}

        # Sort by fitness
        ranked = sorted(zip(pop, scores), key=lambda x: x[1]["fitness"], reverse=True)
        avg_pnl = mean(s["pnl"] for _, s in ranked)
        best_pnl = ranked[0][1]["pnl"]

        print(f"  Gen {gen+1:2d}: best PnL=${best_pnl:+.0f} avg=${avg_pnl:+.0f} "
              f"fitness={ranked[0][1]['fitness']:.0f}", end="  \r" if gen < GENERATIONS - 1 else "\n")

        # Selection + reproduction
        new_pop = [ranked[i][0] for i in range(ELITISM)]  # elitism
        while len(new_pop) < POP_SIZE:
            t_size = min(TOURNAMENT, len(ranked))
            t1 = max(random.sample(ranked, t_size), key=lambda x: x[1]["fitness"])[0]
            t2 = max(random.sample(ranked, t_size), key=lambda x: x[1]["fitness"])[0]
            child = mutate(crossover(t1, t2))
            new_pop.append(child)
        pop = new_pop

    if not best_overall: return None

    p = best_overall["params"]
    print(f"\n  BEST PARAMS:")
    print(f"  Lev: {p['leverage']}x | Conf: {p['conf_min']} | Pos: {p['max_pos_pct']*100:.0f}%")
    print(f"  BB: {p['bb_low']}/{p['bb_high']} | RSI: {p['rsi_low']}/{p['rsi_high']}")
    print(f"  TP_ATR: {p['tp_atr']} | SL_ATR: {p['sl_atr']} | Trail: {p['trail_act']}/{p['trail_off']}")
    print(f"  PnL: ${best_overall['pnl']:+,.0f} | Trades: {best_overall['trades']} | "
          f"DD: {best_overall['max_dd']:.1f}% | Score: {best_overall['fitness']:.0f}")

    return best_overall


if __name__ == "__main__":
    results = {}
    for sym, tf, d in [("BTCUSDT", "1h", 30), ("ETHUSDT", "1h", 30), ("XRPUSDT", "1h", 14)]:
        r = optimize(sym, tf, d)
        if r:
            results[f"{sym}_{tf}"] = {
                "pnl": r["pnl"], "trades": r["trades"], "max_dd": r["max_dd"],
                "params": r["params"], "score": r["fitness"]
            }

    print(f"\n{'='*55}")
    print(f"  OPTIMIZATION COMPLETE")
    print(f"{'='*55}")
    for name, r in sorted(results.items(), key=lambda x: x[1]["pnl"], reverse=True):
        print(f"  {name:<20} ${r['pnl']:>+8,.0f} | DD {r['max_dd']:>5.1f}% | Lev {r['params']['leverage']}x")

    out = {k: v["params"] for k, v in results.items()}
    out["_ts"] = datetime.now(timezone.utc).isoformat()
    with open(OUT / "genetic_best_params.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
