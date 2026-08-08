"""Fast optimizer — targeted search around best known parameters."""
import json, sys, time
from datetime import datetime, timezone, timedelta
from itertools import product
from pathlib import Path
from statistics import mean, stdev
import requests

BINANCE_BASE = "https://api.binance.com/api/v3"
UA = "FastOpt/1.0"
OUTPUT_DIR = Path(__file__).parent.parent / "backtest_results"
OUTPUT_DIR.mkdir(exist_ok=True)
BANKROLL = 1000.0
TAKER_FEE = 0.0004
SLIPPAGE = 0.001
FUNDING_RATE = 0.0001


def fetch_historical(symbol, interval, days):
    all_klines = []
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    while start_ms < end_ms:
        try:
            r = requests.get(f"{BINANCE_BASE}/klines",
                             params={"symbol": symbol, "interval": interval,
                                     "startTime": start_ms, "endTime": end_ms, "limit": 1000},
                             headers={"User-Agent": UA}, timeout=15)
            if r.status_code != 200: break
            batch = r.json()
            if not batch: break
            for k in batch:
                all_klines.append({
                    "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]),
                    "v": float(k[5]), "t": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
                })
            start_ms = int(batch[-1][0]) + 1
            time.sleep(0.15)
        except Exception: break
    return all_klines


def bb(cl, p=20, m=2.0):
    if len(cl) < p: return None
    r = cl[-p:]; sm = mean(r); s = stdev(r) if len(r) > 1 else 0
    return {"pos": (cl[-1]-sm+m*s)/(s*2*m)*100 if s>0 else 50, "bw": s*2/sm*100 if sm>0 else 0}

def rsi_calc(cl, p=14):
    if len(cl) < p+1: return 50
    g, l = [], []
    for i in range(len(cl)-p, len(cl)):
        d = cl[i]-cl[i-1]; (g if d>0 else l).append(abs(d)); (l if d>0 else g).append(0)
    ag, al = mean(g) if g else 0, mean(l) if l else 0
    return 100-(100/(1+ag/al)) if al>0 else 100

def atr_calc(cd, p=14):
    if len(cd) < p+1: return 0
    trs = [max(cd[i]["h"]-cd[i]["l"], abs(cd[i]["h"]-cd[i-1]["c"]), abs(cd[i]["l"]-cd[i-1]["c"])) for i in range(1, len(cd))]
    return mean(trs[-p:])

def costs(size_usd, hours_open, leverage):
    notion = size_usd / leverage
    return notion * TAKER_FEE * 2 + size_usd * SLIPPAGE + size_usd * FUNDING_RATE * (hours_open / 8)


def run_backtest(symbol, klines, params):
    LEV = params["leverage"]; C = params["conf_min"]; MP = params["max_pos_pct"]
    BL = params["bb_low"]; BH = params["bb_high"]; RL = params["rsi_low"]; RH = params["rsi_high"]
    TA = params["tp_atr"]; SA = params["sl_atr"]; TRa = params["trail_act"]; TRo = params["trail_off"]
    trades = []; equity = BANKROLL; peak = BANKROLL; max_dd = 0.0; positions = []

    for i in range(50, len(klines)):
        window = klines[max(0,i-100):i+1]; c = klines[i]; px = c["c"]; cls = [k["c"] for k in window]
        bb_d = bb(cls); r = rsi_calc(cls); a = atr_calc(window)
        if not bb_d or a == 0: continue

        direction = None; conf = 0
        if bb_d["pos"] < BL and r < RL:
            direction = "long"; conf = (1-bb_d["pos"]/BL)*0.4 + (1-r/RL)*0.3 + 0.3
        elif bb_d["pos"] > BH and r > RH:
            direction = "short"; conf = (bb_d["pos"]/100)*0.4 + (r/100)*0.3 + 0.3

        if direction and conf >= C and len(positions) < 3:
            entry = px
            tp = px * (1 + a*TA/px) if direction=="long" else px * (1 - a*TA/px)
            sl = px - a*SA if direction=="long" else px + a*SA
            sz = min(BANKROLL*MP, equity*MP)
            positions.append({"dir":direction,"entry":entry,"tp":tp,"sl":sl,"size":sz,"opened":c["t"],"trail":False,"tsl":None})

        for p in positions[:]:
            if p["dir"]=="long":
                if not p["trail"] and px >= p["entry"]*TRa: p["trail"]=True
                if p["trail"]:
                    ns=px*TRo
                    if p["tsl"] is None or ns>p["tsl"]: p["tsl"]=ns
                eff=p["tsl"] if p["tsl"] else p["sl"]
            else:
                if not p["trail"] and px <= p["entry"]*(2-TRa): p["trail"]=True
                if p["trail"]:
                    ns=px*(2-TRo)
                    if p["tsl"] is None or ns<p["tsl"]: p["tsl"]=ns
                eff=p["tsl"] if p["tsl"] else p["sl"]

            done=False; ex=None
            if p["dir"]=="long":
                if c["h"]>=p["tp"]: ex=p["tp"]*(1-SLIPPAGE); done=True
                elif c["l"]<=eff: ex=eff*(1-SLIPPAGE); done=True
            else:
                if c["l"]<=p["tp"]: ex=p["tp"]*(1+SLIPPAGE); done=True
                elif c["h"]>=eff: ex=eff*(1+SLIPPAGE); done=True

            if done and ex:
                pct = (ex-p["entry"])/p["entry"] if p["dir"]=="long" else (p["entry"]-ex)/p["entry"]
                gross = p["size"]*pct*LEV
                hrs = max((c["t"]-p["opened"]).total_seconds()/3600, 0)
                cost = costs(p["size"], hrs, LEV)
                net = gross-cost
                trades.append({"pnl":net})
                equity+=net; peak=max(peak,equity)
                max_dd=max(max_dd,(peak-equity)/peak*100 if peak>0 else 0)
                positions.remove(p)
            elif (c["t"]-p["opened"]).total_seconds()>172800:
                ex=px*(1-SLIPPAGE) if p["dir"]=="long" else px*(1+SLIPPAGE)
                pct = (ex-p["entry"])/p["entry"] if p["dir"]=="long" else (p["entry"]-ex)/p["entry"]
                gross=p["size"]*pct*LEV; cost=costs(p["size"],48,LEV); net=gross-cost
                trades.append({"pnl":net}); equity+=net
                positions.remove(p)

    total_pnl = sum(t["pnl"] for t in trades)
    wins = sum(1 for t in trades if t["pnl"]>0)
    losses = sum(1 for t in trades if t["pnl"]<=0)
    wr = wins/len(trades)*100 if trades else 0
    dd_p = max(0.1, max_dd)/100
    sharpe = (total_pnl/BANKROLL)/dd_p if dd_p>0 else 0
    score = total_pnl*(1-max_dd/200) - len(trades)*0.5
    return {"params":params,"pnl":total_pnl,"trades":len(trades),"wins":wins,"losses":losses,"wr":wr,"max_dd":max_dd,"sharpe":sharpe,"score":score,"equity":BANKROLL+total_pnl}


def optimize_fast(symbol, interval, days, ptype):
    print(f"\n{'='*50}")
    print(f"  {symbol} {interval} ({days}d) [{ptype}]")
    print(f"{'='*50}")
    klines = fetch_historical(symbol, interval, days)
    if len(klines)<100: return None
    print(f"  {len(klines)} candles")

    # Targeted search — best ranges from full optimizer
    if ptype=="btc":
        combos = list(product(
            [35, 40, 50], [0.55, 0.60, 0.65], [0.08, 0.10, 0.12],
            [20, 22, 25], [75, 78, 80], [35, 38, 40], [60, 62, 65],
            [1.5, 1.8, 2.0], [1.5, 1.8, 2.0], [1.015], [0.992],
        ))
    else:
        combos = list(product(
            [20, 25, 30], [0.55, 0.60, 0.65], [0.06, 0.08, 0.10],
            [15, 18, 20], [80, 82, 85], [32, 35, 38], [62, 65, 68],
            [1.5, 1.8, 2.0], [1.5, 1.8, 2.0], [1.015], [0.992],
        ))

    best = None; bs = -999999
    for idx, combo in enumerate(combos):
        p = {"leverage":combo[0],"conf_min":combo[1],"max_pos_pct":combo[2],
             "bb_low":combo[3],"bb_high":combo[4],"rsi_low":combo[5],"rsi_high":combo[6],
             "tp_atr":combo[7],"sl_atr":combo[8],"trail_act":combo[9],"trail_off":combo[10]}
        r = run_backtest(symbol, klines, p)
        if r["score"]>bs: bs=r["score"]; best=r
    print(f"  Best: PnL=${best['pnl']:+,.0f} | {best['trades']} trades | WR={best['wr']:.0f}% | DD={best['max_dd']:.1f}%")
    print(f"  Config: Lev={best['params']['leverage']}x Conf={best['params']['conf_min']:.2f} MaxPos={best['params']['max_pos_pct']*100:.0f}%")
    print(f"  BB:{best['params']['bb_low']}/{best['params']['bb_high']} RSI:{best['params']['rsi_low']}/{best['params']['rsi_high']}")
    return best


if __name__=="__main__":
    print("FAST OPTIMIZER — targeted search")
    results = {}
    for sym, tf, d, pt in [("BTCUSDT","1h",30,"btc"),("ETHUSDT","1h",30,"alt"),("SOLUSDT","1h",14,"alt"),("BTCUSDT","4h",30,"btc")]:
        r = optimize_fast(sym, tf, d, pt)
        if r: results[f"{sym}_{tf}"] = r

    print(f"\n{'='*50}")
    print(f"  FINAL RESULTS")
    print(f"{'='*50}")
    for name, r in sorted(results.items()):
        print(f"  {name:<20} ${r['pnl']:>+8,.0f} | {r['trades']:>4}t | WR {r['wr']:>4.0f}% | DD {r['max_dd']:>5.1f}% | Lev {r['params']['leverage']}x")

    # Save
    out = {k: v["params"] for k, v in results.items()}
    out["_ts"] = datetime.now(timezone.utc).isoformat()
    with open(OUTPUT_DIR / "best_params.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\n  Saved: best_params.json")
