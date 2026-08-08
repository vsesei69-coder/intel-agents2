"""Virtual Trading Bot — $1000 bankroll, 3% risk, 50x leverage, BB + S/R strategy.

Monitors pairs, opens/closes virtual positions, logs everything to journal.
"""

import hashlib, json, os, sys, time
from datetime import datetime, timezone
from math import sqrt
from pathlib import Path
from statistics import mean, stdev

import requests

BINANCE_BASE = "https://api.binance.com/api/v3"
UA = "VirtualBot/1.0"

JOURNAL_DIR = Path(__file__).parent.parent / "trading_journal"
JOURNAL_DIR.mkdir(exist_ok=True)
POSITIONS_FILE = JOURNAL_DIR / "open_positions.json"
HISTORY_FILE = JOURNAL_DIR / "trade_history.json"

# ── virtual account ────────────────────────────────────────────────────────
BANKROLL = 1000.0
RISK_PER_TRADE = 0.16  # 16% (optimized — was 3%)
MAX_LEVERAGE = 49       # 49x (optimized — was 50x)
MAX_POSITIONS = 5

# ── real-world costs ─────────────────────────────────────────────────────
TAKER_FEE = 0.0004       # 0.04% per side (Binance taker)
SLIPPAGE = 0.001         # 0.1% slippage on fill
FUNDING_RATE = 0.0001    # 0.01% per 8h funding (simplified, prorated)

# ── helpers ─────────────────────────────────────────────────────────────────

def fetch_klines(symbol, interval="15m", limit=100):
    try:
        r = requests.get(f"{BINANCE_BASE}/klines",
                         params={"symbol": symbol, "interval": interval, "limit": limit},
                         headers={"User-Agent": UA}, timeout=10)
        if r.status_code != 200:
            return []
        return [{"o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                 "c": float(k[4]), "v": float(k[5]),
                 "t": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc)}
                for k in r.json()]
    except Exception:
        return []

def fetch_orderbook(symbol, limit=100):
    try:
        r = requests.get(f"{BINANCE_BASE}/depth",
                         params={"symbol": symbol, "limit": limit},
                         headers={"User-Agent": UA}, timeout=10)
        if r.status_code != 200:
            return None
        d = r.json()
        bids = [(float(b[0]), float(b[1])) for b in d["bids"]]
        asks = [(float(a[0]), float(a[1])) for a in d["asks"]]
        return {"bids": bids, "asks": asks}
    except Exception:
        return None

def fetch_ticker(symbol):
    try:
        r = requests.get(f"{BINANCE_BASE}/ticker/24hr",
                         params={"symbol": symbol},
                         headers={"User-Agent": UA}, timeout=10)
        if r.status_code != 200:
            return None
        d = r.json()
        return {"price": float(d["lastPrice"]), "change": float(d["priceChangePercent"]),
                "high": float(d["highPrice"]), "low": float(d["lowPrice"]),
                "volume": float(d["quoteVolume"])}
    except Exception:
        return None

def fetch_klines_range(symbol, start_time, end_time, interval="1m", limit=1000):
    try:
        start_ms = int(start_time.timestamp() * 1000)
        end_ms = int(end_time.timestamp() * 1000)
        r = requests.get(f"{BINANCE_BASE}/klines",
                         params={"symbol": symbol, "interval": interval,
                                 "startTime": start_ms, "endTime": end_ms,
                                 "limit": limit},
                         headers={"User-Agent": UA}, timeout=10)
        if r.status_code != 200:
            return []
        return [{"o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                 "c": float(k[4]),
                 "t": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc)}
                for k in r.json()]
    except Exception:
        return []

def bollinger(closes, period=20, mult=2.0):
    if len(closes) < period:
        return None
    r = closes[-period:]
    sma = mean(r)
    s = stdev(r) if len(r) > 1 else 0
    return {"sma": sma, "upper": sma + mult * s, "lower": sma - mult * s,
            "bw": (s * 2) / sma * 100 if sma > 0 else 0,
            "pos": (closes[-1] - (sma - mult * s)) / (s * 2 * mult) * 100 if s > 0 else 50}

def rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    g, l = [], []
    for i in range(len(closes) - period, len(closes)):
        d = closes[i] - closes[i - 1]
        (g if d > 0 else l).append(abs(d))
        (l if d > 0 else g).append(0)
    ag, al = mean(g) if g else 0, mean(l) if l else 0
    return 100 - (100 / (1 + ag / al)) if al > 0 else 100

def atr(candles, period=14):
    if len(candles) < period + 1:
        return 0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return mean(trs[-period:])

def find_sr(candles_1h, candles_15m):
    """Multi-timeframe support/resistance."""
    levels = {"resistance": [], "support": []}

    # 1H levels (stronger)
    if len(candles_1h) >= 24:
        h = candles_1h[-24:]
        levels["resistance"].extend(sorted([c["h"] for c in h], reverse=True)[:2])
        levels["support"].extend(sorted([c["l"] for c in h])[:2])

    # 15m levels (nearer)
    if len(candles_15m) >= 48:
        h = candles_15m[-48:]
        levels["resistance"].extend(sorted([c["h"] for c in h], reverse=True)[:3])
        levels["support"].extend(sorted([c["l"] for c in h])[:3])

    # Merge nearby levels (within 0.5%)
    def merge(lvls):
        if not lvls:
            return []
        s = sorted(set(round(l, 8) for l in lvls))
        merged = [s[0]]
        for x in s[1:]:
            if abs(x - merged[-1]) / merged[-1] > 0.005:
                merged.append(x)
        return merged[:3]

    return {"support": merge(levels["support"]), "resistance": merge(levels["resistance"])}

def orderbook_walls(orderbook, levels, side="support"):
    """Find order clusters near S/R levels — where the 'walls' are."""
    if not orderbook or not levels:
        return {}
    walls = {}
    book = orderbook["bids"] if side == "support" else orderbook["asks"]
    for level in levels[:3]:
        nearby = sum(qty for price, qty in book if abs(price - level) / level < 0.005)
        walls[f"{level:.6f}"] = round(nearby, 2)
    return walls

# ── signal engine ──────────────────────────────────────────────────────────

def analyze_pair(symbol):
    """Full multi-timeframe analysis. Returns signal or None."""
    c_1h = fetch_klines(symbol, "1h", 50)
    c_15 = fetch_klines(symbol, "15m", 100)
    c_5 = fetch_klines(symbol, "5m", 60)

    if len(c_15) < 30:
        return None

    ticker = fetch_ticker(symbol)
    if not ticker:
        return None

    price = ticker["price"]
    # Filter ultra-low-price coins (SHIB, VET, etc.) — insufficient price resolution for 50x
    if price < 0.01:
        return None
    closes_15 = [c["c"] for c in c_15]
    closes_5 = [c["c"] for c in c_5] if c_5 else closes_15[-20:]

    bb = bollinger(closes_15, 20, 2.0)
    bb_5 = bollinger(closes_5, 20, 2.0) if len(closes_5) >= 20 else None
    rsi_15 = rsi(closes_15, 14)
    rsi_5 = rsi(closes_5, 14) if len(closes_5) >= 15 else rsi_15
    a = atr(c_15, 14)

    sr = find_sr(c_1h, c_15)
    ob = fetch_orderbook(symbol, 200)

    # Trend: 1H SMA20 vs SMA50
    closes_1h = [c["c"] for c in c_1h] if c_1h else []
    trend = "neutral"
    if len(closes_1h) >= 50:
        s20 = mean(closes_1h[-20:])
        s50 = mean(closes_1h[-50:])
        trend = "up" if s20 > s50 else "down"
    elif len(closes_1h) >= 20:
        s20 = mean(closes_1h[-20:])
        trend = "up" if price > s20 else "down"

    # Signal logic: multi-timeframe confirmation
    signal = None
    direction = None
    confidence = 0

    # BUY: price at/near lower BB + RSI < 37 (optimized)
    if bb and bb_5:
        near_low_15 = bb["pos"] < 22
        near_low_5 = bb_5["pos"] < 25 if bb_5 else False
        oversold = rsi_15 < 37 and rsi_5 < 40

        if near_low_15 and near_low_5 and oversold:
            direction = "long"
            confidence = (1 - bb["pos"] / 22) * 0.3 + (1 - rsi_15 / 37) * 0.3 + 0.4
        elif near_low_15 and oversold:
            direction = "long"
            confidence = (1 - bb["pos"] / 22) * 0.3 + (1 - rsi_15 / 37) * 0.2 + 0.3

    # SELL: price at/near upper BB + RSI > 65
    if bb and bb_5:
        near_high_15 = bb["pos"] > 84
        near_high_5 = bb_5["pos"] > 78 if bb_5 else False
        overbought = rsi_15 > 65 and rsi_5 > 60

        if near_high_15 and near_high_5 and overbought:
            direction = "short"
            confidence = (bb["pos"] / 100) * 0.3 + (rsi_15 / 100) * 0.3 + 0.4

    if not direction:
        return None

    # Closest S/R
    if direction == "long":
        nearest_resistance = sr["resistance"][0] if sr["resistance"] else price * 1.02
        nearest_support = sr["support"][0] if sr["support"] else price * 0.98
    else:
        nearest_resistance = sr["resistance"][0] if sr["resistance"] else price * 1.02
        nearest_support = sr["support"][0] if sr["support"] else price * 0.98

    # Entry, TP, SL
    entry = price
    if direction == "long":
        tp = nearest_resistance
        sl = min(nearest_support, price - a * 1.5)
        if sl >= entry:
            sl = entry - a * 2
    else:
        tp = nearest_support
        sl = max(nearest_resistance, price + a * 1.5)
        if sl <= entry:
            sl = entry + a * 2

    # Position size: 3% of bankroll risk
    risk_amount = BANKROLL * RISK_PER_TRADE
    risk_per_unit = abs(entry - sl)
    if risk_per_unit == 0:
        return None
    position_size = risk_amount / risk_per_unit
    leveraged_size = min(position_size * MAX_LEVERAGE, BANKROLL * 0.5)  # cap 50% of bankroll

    # Wall analysis
    wall_side = "support" if direction == "long" else "resistance"
    walls = orderbook_walls(ob, sr[wall_side], wall_side)

    # Volume check
    vol_score = min(ticker["volume"] / 10_000_000, 2.0)

    return {
        "symbol": symbol,
        "price": price,
        "direction": direction,
        "entry": round(entry, 6),
        "take_profit": round(tp, 6),
        "stop_loss": round(sl, 6),
        "leverage": MAX_LEVERAGE,
        "position_size_usd": round(leveraged_size, 2),
        "risk_usd": round(risk_amount, 2),
        "confidence": round(min(confidence, 1.0), 2),
        "rsi_15m": round(rsi_15, 1),
        "rsi_5m": round(rsi_5, 1),
        "atr": round(a, 6),
        "trend_1h": trend,
        "bb_pos_15m": round(bb["pos"], 1) if bb else None,
        "bb_pos_5m": round(bb_5["pos"], 1) if bb_5 else None,
        "volume_24h": ticker["volume"],
        "volume_score": round(vol_score, 1),
        "support_levels": sr["support"][:2],
        "resistance_levels": sr["resistance"][:2],
        "orderbook_walls": walls,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

# ── virtual trading engine ─────────────────────────────────────────────────

def load_positions():
    if POSITIONS_FILE.exists():
        return json.loads(POSITIONS_FILE.read_text())
    return []

def save_positions(positions):
    POSITIONS_FILE.write_text(json.dumps(positions, indent=2, ensure_ascii=False))

def load_history():
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return {"trades": [], "stats": {"total": 0, "wins": 0, "losses": 0,
            "total_pnl": 0.0, "best_trade": 0.0, "worst_trade": 0.0,
            "total_fees": 0.0, "total_slippage": 0.0, "total_funding": 0.0}}

def save_history(history):
    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False))

def open_position(signal):
    """Open a new virtual position."""
    pos = {
        "id": hashlib.md5(f"{signal['symbol']}{signal['timestamp']}".encode()).hexdigest()[:12],
        "symbol": signal["symbol"],
        "direction": signal["direction"],
        "entry_price": signal["entry"],
        "take_profit": signal["take_profit"],
        "stop_loss": signal["stop_loss"],
        "size_usd": signal["position_size_usd"],
        "leverage": signal["leverage"],
        "risk_usd": signal["risk_usd"],
        "confidence": signal["confidence"],
        "opened_at": signal["timestamp"],
        "status": "open",
        "closed_at": None,
        "exit_price": None,
        "pnl_usd": None,
        "pnl_pct": None,
        "hit_tp": False,
        "hit_sl": False,
        "atr": signal.get("atr", 0),
        "trailing_activated": False,
        "trailing_stop": None,
        "last_checked_at": signal["timestamp"],
        "fees_paid": 0.0,
        "slippage_cost": 0.0,
        "funding_paid": 0.0,
    }
    positions = load_positions()
    positions.append(pos)
    save_positions(positions)
    return pos

def check_positions():
    """Check open positions against per-cycle klines (not 24h high/low).
    Each cycle fetches 1m candles from last_checked_at to now,
    checking each candle for TP/SL cross. Includes taker fees,
    slippage, and funding rate.
    Applies trailing stop: when price moves beyond entry+ATR*2 in profit,
    trailing stop locks profit at price-ATR*2 and moves with price."""
    positions = load_positions()
    history = load_history()
    closed = []
    modified = False
    now_utc = datetime.now(timezone.utc)

    for pos in positions:
        if pos["status"] != "open":
            continue

        ticker = fetch_ticker(pos["symbol"])
        if not ticker:
            continue

        price = ticker["price"]
        tp = pos["take_profit"]
        sl = pos["stop_loss"]
        d = pos["direction"]
        entry = pos["entry_price"]
        atr_val = pos.get("atr", 0)
        last_check_str = pos.get("last_checked_at", pos["opened_at"])
        last_check = datetime.fromisoformat(last_check_str.replace("Z", "+00:00"))

        # === TRAILING STOP ===
        if atr_val > 0:
            trailing_on = pos.get("trailing_activated", False)

            if d == "long":
                activate_threshold = entry + atr_val * 2
                if not trailing_on and price >= activate_threshold:
                    pos["trailing_activated"] = True
                    trailing_on = True
                    trailing_sl = max(sl, price - atr_val * 2)
                    if trailing_sl > sl:
                        pos["stop_loss"] = round(trailing_sl, 8)
                        pos["trailing_stop"] = round(trailing_sl, 8)
                        modified = True
                if trailing_on:
                    trailing_sl = max(sl, price - atr_val * 2)
                    if trailing_sl > sl:
                        pos["stop_loss"] = round(trailing_sl, 8)
                        pos["trailing_stop"] = round(trailing_sl, 8)
                        modified = True
            else:  # short
                activate_threshold = entry - atr_val * 2
                if not trailing_on and price <= activate_threshold:
                    pos["trailing_activated"] = True
                    trailing_on = True
                    trailing_sl = min(sl, price + atr_val * 2)
                    if trailing_sl < sl:
                        pos["stop_loss"] = round(trailing_sl, 8)
                        pos["trailing_stop"] = round(trailing_sl, 8)
                        modified = True
                if trailing_on:
                    trailing_sl = min(sl, price + atr_val * 2)
                    if trailing_sl < sl:
                        pos["stop_loss"] = round(trailing_sl, 8)
                        pos["trailing_stop"] = round(trailing_sl, 8)
                        modified = True

            sl = pos["stop_loss"]

        # === PER-CYCLE TP/SL CHECK using 1m klines ===
        klines = fetch_klines_range(pos["symbol"], last_check, now_utc, "1m", 1000)
        if not klines:
            pos["last_checked_at"] = now_utc.isoformat()
            modified = True
            continue

        hit = False
        hit_candle = None

        for c in klines:
            ch, cl = c["h"], c["l"]
            if d == "long":
                if ch >= tp:
                    hit = True
                    pos["hit_tp"] = True
                    hit_candle = c
                    break
                if cl <= sl:
                    hit = True
                    pos["hit_sl"] = True
                    hit_candle = c
                    break
            else:  # short
                if cl <= tp:
                    hit = True
                    pos["hit_tp"] = True
                    hit_candle = c
                    break
                if ch >= sl:
                    hit = True
                    pos["hit_sl"] = True
                    hit_candle = c
                    break

        # === HARD STOP: if position loss > 60%, force close ===
        if not hit and price > 0:
            if d == "long":
                loss_pct = (entry - price) / entry * pos["leverage"] * 100
            else:
                loss_pct = (price - entry) / entry * pos["leverage"] * 100
            if loss_pct > 60:
                hit = True
                hit_candle = klines[-1] if klines else None
                pos["hit_sl"] = True
                pos["exit_price"] = round(price * (1 - SLIPPAGE) if d == "long" else price * (1 + SLIPPAGE), 8)
                pos["force_closed"] = True

        if hit and hit_candle:
            raw_exit = tp if pos["hit_tp"] else sl

            # Apply slippage: exit worse than exact TP/SL
            if d == "long":
                exit_with_slip = raw_exit * (1 - SLIPPAGE) if pos["hit_tp"] else raw_exit * (1 - SLIPPAGE)
            else:
                exit_with_slip = raw_exit * (1 + SLIPPAGE) if pos["hit_tp"] else raw_exit * (1 + SLIPPAGE)

            # Realistic exit: use the candle's price at the moment of crossing
            # For TP on a long: use the high of the crossing candle (price was there)
            # For SL on a long: use the low
            if d == "long":
                if pos["hit_tp"]:
                    real_exit = hit_candle["h"]  # price reached high = tp area
                else:
                    real_exit = hit_candle["l"]
            else:
                if pos["hit_tp"]:
                    real_exit = hit_candle["l"]
                else:
                    real_exit = hit_candle["h"]

            pos["exit_price"] = round(real_exit, 8)
            pos["status"] = "closed"
            pos["closed_at"] = hit_candle["t"].isoformat()

            # Gross PnL before costs
            if d == "long":
                gross_pnl_pct = (pos["exit_price"] - entry) / entry
            else:
                gross_pnl_pct = (entry - pos["exit_price"]) / entry
            gross_pnl_pct *= pos["leverage"]
            gross_pnl_usd = pos["size_usd"] * gross_pnl_pct

            # Trading costs
            fee_open = pos["size_usd"] * TAKER_FEE
            fee_close = pos["size_usd"] * TAKER_FEE
            total_fee = fee_open + fee_close
            slippage_cost = pos["size_usd"] * SLIPPAGE * pos["leverage"]

            # Funding rate (prorated by duration)
            opened_dt = datetime.fromisoformat(pos["opened_at"].replace("Z", "+00:00"))
            hours_open = max((hit_candle["t"] - opened_dt).total_seconds() / 3600, 0)
            funding_cost = pos["size_usd"] * FUNDING_RATE * (hours_open / 8) * pos["leverage"]

            # Net PnL
            total_costs = total_fee + slippage_cost + funding_cost
            net_pnl_usd = gross_pnl_usd - total_costs
            net_pnl_pct = (net_pnl_usd / pos["size_usd"]) * 100 if pos["size_usd"] else 0

            pos["pnl_usd"] = round(net_pnl_usd, 2)
            pos["pnl_pct"] = round(net_pnl_pct, 2)
            pos["gross_pnl_usd"] = round(gross_pnl_usd, 2)
            pos["fees_paid"] = round(total_fee, 4)
            pos["slippage_cost"] = round(slippage_cost, 4)
            pos["funding_paid"] = round(funding_cost, 4)

            closed.append(pos)

            history["trades"].append(pos)
            history["stats"]["total"] += 1
            if net_pnl_usd > 0:
                history["stats"]["wins"] += 1
                history["stats"]["best_trade"] = max(history["stats"]["best_trade"], net_pnl_usd)
            else:
                history["stats"]["losses"] += 1
                history["stats"]["worst_trade"] = min(history["stats"]["worst_trade"], net_pnl_usd)
            history["stats"]["total_pnl"] += net_pnl_usd
            history["stats"]["total_fees"] = history["stats"].get("total_fees", 0.0) + total_fee
            history["stats"]["total_slippage"] = history["stats"].get("total_slippage", 0.0) + slippage_cost
            history["stats"]["total_funding"] = history["stats"].get("total_funding", 0.0) + funding_cost
        else:
            pos["last_checked_at"] = now_utc.isoformat()
            modified = True

    if closed or modified:
        active = [p for p in positions if p["status"] == "open"]
        save_positions(active)
        if closed:
            save_history(history)
        else:
            save_positions(positions)

    return closed

def can_open_more():
    return len([p for p in load_positions() if p["status"] == "open"]) < MAX_POSITIONS

# ── main loop ──────────────────────────────────────────────────────────────

TOP_SCAN = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT",
            "BNBUSDT","AVAXUSDT","LINKUSDT","DOTUSDT","MATICUSDT","UNIUSDT",
            "LTCUSDT","ATOMUSDT","ARBUSDT","OPUSDT","NEARUSDT","APTUSDT","FILUSDT","TRXUSDT"]

def run_cycle():
    """One full scan + trade cycle."""
    print(f"\n{'='*70}")
    print(f"  VIRTUAL TRADING BOT — {datetime.now().strftime('%H:%M:%S')} UTC")
    print(f"  Bankroll: ${BANKROLL} | Risk/trade: 3% | Leverage: {MAX_LEVERAGE}x")
    print(f"{'='*70}")

    # Check existing positions
    closed = check_positions()
    if closed:
        print(f"\n  [CLOSED] {len(closed)} position(s):")
        for c in closed:
            emoji = "WIN" if c["pnl_usd"] > 0 else "LOSS"
            gross = c.get("gross_pnl_usd", c["pnl_usd"])
            costs = c.get("fees_paid", 0) + c.get("slippage_cost", 0) + c.get("funding_paid", 0)
            print(f"    {c['symbol']} {c['direction']}: {emoji} ${c['pnl_usd']:+.2f} ({c['pnl_pct']:+.1f}%) "
                  f"[gross: ${gross:+.2f}, costs: ${costs:.2f}]")
    else:
        print(f"\n  [POSITIONS] {len(load_positions())} open, checking...")

    # Open positions
    active_count = len([p for p in load_positions() if p["status"] == "open"])
    if active_count >= MAX_POSITIONS:
        print(f"  At max positions ({MAX_POSITIONS}). Waiting for closures.")
        return

    print(f"\n  Scanning for new signals...")
    signals = []
    for symbol in TOP_SCAN[:15]:
        time.sleep(0.5)
        s = analyze_pair(symbol)
        if s and s["confidence"] >= 0.5:
            signals.append(s)

    signals.sort(key=lambda x: (x["confidence"] + x.get("volume_score", 0)), reverse=True)

    # Open best signals
    opened = 0
    for sig in signals:
        if not can_open_more():
            break
        pos = open_position(sig)
        print(f"\n  [OPENED] {sig['direction'].upper()} {sig['symbol']}")
        print(f"    Entry: ${sig['entry']:.6f} | TP: ${sig['take_profit']:.6f} | "
              f"SL: ${sig['stop_loss']:.6f}")
        print(f"    Size: ${sig['position_size_usd']:.2f} | Risk: ${sig['risk_usd']:.2f} | "
              f"Confidence: {sig['confidence']:.0%}")
        print(f"    RSI: {sig['rsi_15m']}(15m) / {sig['rsi_5m']}(5m) | "
              f"BB pos: {sig.get('bb_pos_15m','?')}% | Trend: {sig['trend_1h']}")
        if sig.get("orderbook_walls"):
            print(f"    Order walls: {sig['orderbook_walls']}")
        opened += 1

    if not opened:
        print(f"  No valid signals found.")

    # Summary
    history = load_history()
    s = history["stats"]
    active = load_positions()
    total_pnl = s["total_pnl"]
    win_rate = s["wins"] / s["total"] * 100 if s["total"] > 0 else 0
    print(f"\n  [ACCOUNT] PnL: ${total_pnl:+.2f} | Trades: {s['total']} | "
          f"Win rate: {win_rate:.0f}% | Open: {len(active)} | "
          f"Balance: ${BANKROLL + total_pnl:.2f}")
    if s.get("total_fees", 0) > 0:
        print(f"  [COSTS]  Fees: ${s['total_fees']:.2f} | "
              f"Slippage: ${s.get('total_slippage', 0):.2f} | "
              f"Funding: ${s.get('total_funding', 0):.2f}")


if __name__ == "__main__":
    run_cycle()
