"""Trade verifier — cross-references trade_history.json against real Binance candles.

For each closed trade, fetches 1m klines between opened_at and closed_at,
checks if the price actually reached the claimed TP during that window.
Reports: VALID (price reached TP in the window), STALE (24h high/low false fill),
NO_DATA (klines unavailable), and summary statistics.
"""

import json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path

import requests

BINANCE_BASE = "https://api.binance.com/api/v3"
UA = "TradeVerifier/1.0"
HISTORY = Path(__file__).parent.parent / "trading_journal" / "trade_history.json"


def fetch_klines_range(symbol, start_ms, end_ms):
    try:
        r = requests.get(f"{BINANCE_BASE}/klines",
                         params={"symbol": symbol, "interval": "1m",
                                 "startTime": start_ms, "endTime": end_ms,
                                 "limit": 1000},
                         headers={"User-Agent": UA}, timeout=15)
        if r.status_code != 200:
            return None
        return [{"o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                 "c": float(k[4]),
                 "t": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc)}
                for k in r.json()]
    except Exception as e:
        return None


def verify_trade(trade):
    symbol = trade["symbol"]
    direction = trade["direction"]
    tp = trade["take_profit"]
    sl = trade["stop_loss"]
    opened_at = datetime.fromisoformat(trade["opened_at"].replace("Z", "+00:00"))
    closed_at_str = trade.get("closed_at")
    if not closed_at_str:
        return {"status": "OPEN", "note": "trade still open"}

    closed_at = datetime.fromisoformat(closed_at_str.replace("Z", "+00:00"))
    start_ms = int(opened_at.timestamp() * 1000)
    end_ms = int(closed_at.timestamp() * 1000) + 60_000  # +1 min buffer

    klines = fetch_klines_range(symbol, start_ms, end_ms)
    if klines is None:
        return {"status": "NO_DATA", "note": "Binance API failed"}
    if not klines:
        return {"status": "NO_DATA", "note": "no klines in range"}

    window_high = max(c["h"] for c in klines)
    window_low = min(c["l"] for c in klines)

    tp_reached = False
    sl_reached = False
    tp_candle = None
    sl_candle = None

    for c in klines:
        if direction == "long":
            if c["h"] >= tp and not tp_reached:
                tp_reached = True
                tp_candle = c["t"]
            if c["l"] <= sl and not sl_reached:
                sl_reached = True
                sl_candle = c["t"]
        else:  # short
            if c["l"] <= tp and not tp_reached:
                tp_reached = True
                tp_candle = c["t"]
            if c["h"] >= sl and not sl_reached:
                sl_reached = True
                sl_candle = c["t"]

    claimed_hit_tp = trade.get("hit_tp", False)
    claimed_hit_sl = trade.get("hit_sl", False)

    # Determine which event happened first (more realistic)
    if tp_reached and sl_reached:
        if tp_candle and sl_candle:
            actual_hit = "tp" if tp_candle <= sl_candle else "sl"
        else:
            actual_hit = "tp"
    elif tp_reached:
        actual_hit = "tp"
    elif sl_reached:
        actual_hit = "sl"
    else:
        actual_hit = "none"

    claimed_hit = "tp" if claimed_hit_tp else ("sl" if claimed_hit_sl else "none")

    duration_min = (closed_at - opened_at).total_seconds() / 60

    if claimed_hit == "tp" and tp_reached:
        return {
            "status": "VALID_TP",
            "symbol": symbol, "direction": direction,
            "tp": tp, "window_high": window_high, "window_low": window_low,
            "tp_reached": True, "tp_time": tp_candle.isoformat() if tp_candle else None,
            "duration_min": round(duration_min, 1),
            "closed_at_claimed": closed_at_str,
        }
    elif claimed_hit == "sl" and sl_reached:
        return {
            "status": "VALID_SL",
            "symbol": symbol, "direction": direction,
            "sl": sl, "window_high": window_high, "window_low": window_low,
            "sl_reached": True, "sl_time": sl_candle.isoformat() if sl_candle else None,
            "duration_min": round(duration_min, 1),
            "closed_at_claimed": closed_at_str,
        }
    elif claimed_hit in ("tp", "sl") and not tp_reached and not sl_reached:
        return {
            "status": "FALSE_FILL",
            "symbol": symbol, "direction": direction,
            "claimed": claimed_hit,
            "tp": tp, "sl": sl,
            "window_high": window_high, "window_low": window_low,
            "tp_reached": tp_reached, "sl_reached": sl_reached,
            "duration_min": round(duration_min, 1),
            "note": f"claimed {claimed_hit} but price never reached it in window",
        }
    elif claimed_hit == "tp" and not tp_reached and sl_reached:
        return {
            "status": "WRONG_DIRECTION",
            "symbol": symbol, "direction": direction,
            "claimed": "tp", "actual_first": "sl",
            "tp": tp, "sl": sl,
            "window_high": window_high, "window_low": window_low,
            "duration_min": round(duration_min, 1),
        }
    else:
        return {
            "status": "UNKNOWN",
            "symbol": symbol, "direction": direction,
            "claimed": claimed_hit, "actual_first": actual_hit,
        }


def main():
    if not HISTORY.exists():
        print("No trade history found.")
        return

    with open(HISTORY, encoding="utf-8") as f:
        data = json.load(f)

    trades = data.get("trades", [])
    print(f"Verifying {len(trades)} trades against Binance candles...\n")

    results = {"VALID_TP": 0, "VALID_SL": 0, "FALSE_FILL": 0,
               "NO_DATA": 0, "OPEN": 0, "WRONG_DIRECTION": 0, "UNKNOWN": 0}
    false_fills = []
    total = 0

    for i, trade in enumerate(trades):
        if i > 0:
            time.sleep(0.3)  # rate limit

        result = verify_trade(trade)
        status = result["status"]
        results[status] = results.get(status, 0) + 1
        total += 1

        icon = {"VALID_TP": "[OK]", "VALID_SL": "[OK]", "FALSE_FILL": "[!!]",
                "WRONG_DIRECTION": "[!!]", "NO_DATA": "[??]", "OPEN": "[  ]",
                "UNKNOWN": "[??]"}.get(status, "[  ]")

        symbol = result.get("symbol", trade.get("symbol", "?"))
        detail = ""
        if status == "FALSE_FILL":
            note = result.get("note", "")
            detail = f" — {note}"
            false_fills.append(result)
        elif status == "WRONG_DIRECTION":
            detail = f" — claimed TP but SL hit first at {result.get('sl'):.6f}"

        print(f"  {icon} #{i+1:3d} {symbol:10s} {status:16s}{detail}")

    print(f"\n{'='*60}")
    print(f"  VERIFICATION SUMMARY — {total} trades")
    print(f"  Valid (TP+SL):    {results['VALID_TP'] + results['VALID_SL']:4d}  ({((results['VALID_TP'] + results['VALID_SL']) / total * 100) if total else 0:.0f}%)")
    print(f"  False fills:      {results['FALSE_FILL']:4d}  ({results['FALSE_FILL'] / total * 100 if total else 0:.0f}%)")
    print(f"  Wrong direction:  {results['WRONG_DIRECTION']:4d}")
    print(f"  No data:          {results['NO_DATA']:4d}")
    print(f"  Open/Unknown:     {results['OPEN'] + results['UNKNOWN']:4d}")

    if false_fills:
        print(f"\n  FALSE FILLS (price never reached claimed TP/SL):")
        for ff in false_fills[:10]:
            print(f"    {ff['symbol']:10s} {ff['direction']:6s} "
                  f"claimed={ff['claimed']} tp={ff.get('tp',0):.6f} "
                  f"window_high={ff['window_high']:.6f} window_low={ff['window_low']:.6f} "
                  f"dur={ff['duration_min']}m")

    # Save report
    report_path = HISTORY.parent / "verification_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({"results": results, "false_fills": false_fills,
                   "total": total, "timestamp": datetime.now(timezone.utc).isoformat()},
                  f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  Report saved: {report_path}")

    return results


if __name__ == "__main__":
    main()
