"""Intel Betting Engine — unified intelligence pipeline.

Usage:
    python pipeline.py --mode full      # All sources
    python pipeline.py --mode crypto    # Crypto intel only
    python pipeline.py --mode sports    # Sports odds only
    python pipeline.py --mode markets   # Prediction markets only
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

# ── configuration ──────────────────────────────────────────────────────────
BANKROLL = float(os.getenv("INTEL_BANKROLL", "1000.0"))
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
MAX_POSITION_PCT = 0.05
MIN_EDGE = 0.03
MIN_CONFIDENCE = 0.5


# ── data collection ────────────────────────────────────────────────────────

def collect_crypto_intel():
    """Gather crypto intelligence signals."""
    signals = []

    # SEC insider trades via btcnode.uk
    try:
        import requests
        r = requests.get(
            "https://btcnode.uk/api/insider-trades",
            timeout=15,
            headers={"Accept": "application/json"},
        )
        if r.status_code == 200:
            data = r.json()
            for trade in data.get("trades", []):
                signals.append({
                    "source": "sec_insider",
                    "ticker": trade.get("ticker", ""),
                    "direction": "buy" if trade.get("is_purchase") else "sell",
                    "amount": trade.get("amount", 0),
                    "filing_date": trade.get("filing_date", ""),
                    "weight": 0.15,
                })
    except Exception:
        pass

    # Whale tracker — large BTC/ETH transactions
    try:
        r = requests.get(
            "https://api.whale-alert.io/v1/transactions",
            params={"api_key": os.getenv("WHALE_ALERT_KEY", ""), "min_value": 5000000},
            timeout=15,
        )
        if r.status_code == 200:
            for tx in r.json().get("transactions", []):
                direction = tx.get("from_owner_type") == "exchange" and "sell" or "buy"
                signals.append({
                    "source": "whale_alert",
                    "symbol": tx.get("symbol", "BTC"),
                    "direction": direction,
                    "amount_usd": tx.get("amount_usd", 0),
                    "weight": 0.10,
                })
    except Exception:
        pass

    # CoinGecko market overview
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/global",
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json().get("data", {})
            mcap_change = data.get("market_cap_change_percentage_24h_usd", 0)
            btc_dominance = data.get("market_cap_percentage", {}).get("btc", 0)
            signals.append({
                "source": "coingecko",
                "market_cap_24h_change": mcap_change,
                "btc_dominance": btc_dominance,
                "weight": 0.05,
            })
    except Exception:
        pass

    return signals


def collect_prediction_markets():
    """Scan Polymarket for active markets with highest volume."""
    markets = []
    try:
        import requests
        # Polymarket CLOB API (public, no auth needed)
        r = requests.get(
            "https://clob.polymarket.com/markets",
            params={"limit": 50},
            timeout=15,
        )
        if r.status_code == 200:
            for market in r.json():
                tokens = market.get("tokens", [])
                yes_price = 0
                no_price = 0
                for t in tokens:
                    price = float(t.get("price", 0))
                    if "yes" in t.get("outcome", "").lower():
                        yes_price = price
                    elif "no" in t.get("outcome", "").lower():
                        no_price = price
                if not yes_price and len(tokens) == 2:
                    yes_price = float(tokens[0].get("price", 0))
                    no_price = float(tokens[1].get("price", 0))
                if not yes_price:
                    continue
                markets.append({
                    "id": market.get("condition_id", ""),
                    "question": market.get("question", ""),
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "volume_24h": float(market.get("volume", 0)),
                    "liquidity": float(market.get("liquidity", 0)),
                    "end_date": market.get("end_date_iso", ""),
                    "platform": "polymarket",
                })
    except Exception:
        pass

    markets.sort(key=lambda m: m["volume_24h"], reverse=True)
    return markets[:20]


def collect_sports_odds(sport_key="soccer_epl"):
    """Fetch sports odds from The Odds API."""
    odds = []
    if not ODDS_API_KEY:
        return odds

    try:
        import requests
        r = requests.get(
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "eu",
                "markets": "h2h",
                "oddsFormat": "decimal",
            },
            timeout=15,
        )
        if r.status_code == 200:
            for match in r.json():
                bookmakers = match.get("bookmakers", [])
                if len(bookmakers) >= 2:
                    odds.append({
                        "home_team": match.get("home_team", ""),
                        "away_team": match.get("away_team", ""),
                        "commence_time": match.get("commence_time", ""),
                        "bookmakers": [
                            {
                                "name": b.get("title", ""),
                                "home": b["markets"][0]["outcomes"][0].get("price", 0),
                                "away": b["markets"][0]["outcomes"][1].get("price", 0),
                            }
                            for b in bookmakers[:5]
                        ],
                    })
        else:
            remaining = r.headers.get("x-requests-remaining", "N/A")
            if remaining == "0":
                print(f"[WARN] Odds API rate limit reached", file=sys.stderr)
    except Exception as e:
        print(f"[WARN] Odds API error: {e}", file=sys.stderr)

    return odds


# ── analysis ───────────────────────────────────────────────────────────────

def analyze_crypto(signals):
    """Convert crypto signals into probability estimates."""
    if not signals:
        return {"direction": "neutral", "confidence": 0, "probability": 0.5, "reasoning": "No signals"}

    buy_weight = sum(s["weight"] for s in signals if s.get("direction") == "buy")
    sell_weight = sum(s["weight"] for s in signals if s.get("direction") == "sell")

    # Coingecko market cap trend
    for s in signals:
        if s.get("source") == "coingecko":
            mc_change = s.get("market_cap_24h_change", 0)
            if mc_change > 2:
                buy_weight += 0.1
            elif mc_change < -2:
                sell_weight += 0.1
            btc_dom = s.get("btc_dominance", 50)
            if btc_dom > 60:  # high BTC dominance = risk-off
                sell_weight += 0.05

    total = buy_weight + sell_weight
    if total == 0:
        return {"direction": "neutral", "confidence": 0.2, "probability_up": 0.5, "reasoning": "Insufficient signals", "signal_count": len(signals)}
    prob_up = buy_weight / total
    confidence = min(total * 2, 1.0)

    direction = "bullish" if prob_up > 0.55 else ("bearish" if prob_up < 0.45 else "neutral")

    reasoning_parts = []
    insider_buys = sum(1 for s in signals if s.get("source") == "sec_insider" and s.get("direction") == "buy")
    insider_sells = sum(1 for s in signals if s.get("source") == "sec_insider" and s.get("direction") == "sell")

    if insider_buys:
        reasoning_parts.append(f"{insider_buys} SEC insider buy(s)")
    if insider_sells:
        reasoning_parts.append(f"{insider_sells} SEC insider sell(s)")

    whales = [s for s in signals if s.get("source") == "whale_alert"]
    if whales:
        reasoning_parts.append(f"{len(whales)} whale alert(s)")

    return {
        "direction": direction,
        "probability_up": round(prob_up, 3),
        "confidence": round(confidence, 3),
        "signal_count": len(signals),
        "reasoning": ", ".join(reasoning_parts) or "Market data only",
    }


def analyze_markets(markets):
    """Score prediction markets for edge opportunities."""
    decisions = []
    for m in markets:
        if m["volume_24h"] < 1000:
            continue

        yes_prob = m["yes_price"]
        if 0.15 <= yes_prob <= 0.85:
            # Simple edge: look for mispriced underdogs
            model_bias = 0.02  # default small edge toward underdogs
            edge = yes_prob - 0.50
            confidence = min(m["volume_24h"] / 100000, 1.0) * 0.5 + 0.3

            decisions.append({
                "market_id": m["id"],
                "question": m["question"],
                "platform": m["platform"],
                "market_prob": yes_prob,
                "model_prob": yes_prob + model_bias,
                "edge": round(abs(edge) + model_bias, 3),
                "confidence": round(confidence, 3),
                "volume_24h": m["volume_24h"],
                "end_date": m.get("end_date", ""),
            })

    return sorted(decisions, key=lambda d: d["edge"] * d["confidence"], reverse=True)


def analyze_sports(odds_data):
    """Detect arbitrage opportunities across bookmakers."""
    arbs = []
    for match in odds_data:
        bms = match["bookmakers"]
        if len(bms) < 2:
            continue

        best_home = max(bms, key=lambda b: b["home"])
        best_away = max(bms, key=lambda b: b["away"])

        if best_home["name"] != best_away["name"]:
            implied = 1 / best_home["home"] + 1 / best_away["away"]
            if implied < 1.0:
                arb_yield = round((1 - implied), 3)
                arbs.append({
                    "home_team": match["home_team"],
                    "away_team": match["away_team"],
                    "commence_time": match["commence_time"],
                    "bet_bookmaker": best_home["name"],
                    "bet_odds": best_home["home"],
                    "lay_bookmaker": best_away["name"],
                    "lay_odds": best_away["away"],
                    "arbitrage_yield": arb_yield,
                    "action": "ARB" if arb_yield > 0.01 else "OBSERVE",
                })

    return sorted(arbs, key=lambda a: a["arbitrage_yield"], reverse=True)


# ── decision engine ────────────────────────────────────────────────────────

def make_decisions(crypto_analysis, market_decisions, sports_arbs):
    """Combine all signals into final actionable decisions."""
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "bankroll": BANKROLL,
        "max_position": BANKROLL * MAX_POSITION_PCT,
        "decisions": [],
    }

    # Crypto decision
    if crypto_analysis and crypto_analysis["confidence"] > MIN_CONFIDENCE:
        prob = crypto_analysis["probability_up"]
        edge = abs(prob - 0.5)
        action = "BUY" if edge > MIN_EDGE and crypto_analysis["confidence"] > 0.6 else "OBSERVE"
        output["decisions"].append({
            "type": "crypto",
            "market": "BTC/USD directional",
            "direction": crypto_analysis["direction"],
            "probability": prob,
            "edge": round(edge, 3),
            "confidence": crypto_analysis["confidence"],
            "action": action,
            "reasoning": crypto_analysis["reasoning"],
            "signal_count": crypto_analysis["signal_count"],
        })

    # Market decisions
    for md in market_decisions[:10]:
        if md["edge"] >= MIN_EDGE and md["confidence"] >= MIN_CONFIDENCE:
            ev = md["edge"] * md["confidence"]
            size = round(BANKROLL * MAX_POSITION_PCT * ev, 2)
            action = "BUY" if md["edge"] > 0.05 and md["confidence"] > 0.6 else "OBSERVE"
            output["decisions"].append({
                "type": "prediction_market",
                "market": md["question"],
                "platform": md["platform"],
                "model_probability": md["model_prob"],
                "market_probability": md["market_prob"],
                "edge": md["edge"],
                "confidence": md["confidence"],
                "recommended_size": f"${size}",
                "expected_value": round(ev, 4),
                "action": action,
                "volume_24h": md["volume_24h"],
            })

    # Sports arbs
    for arb in sports_arbs:
        if arb["arbitrage_yield"] >= 0.01:
            size = round(BANKROLL * 0.1, 2)
            output["decisions"].append({
                "type": "sports_arbitrage",
                "event": f"{arb['home_team']} vs {arb['away_team']}",
                "arbitrage_yield": arb["arbitrage_yield"],
                "bet_on": f"{arb['bet_bookmaker']} @ {arb['bet_odds']}",
                "lay_on": f"{arb['lay_bookmaker']} @ {arb['lay_odds']}",
                "recommended_size": f"${size}",
                "action": "ARB" if arb["arbitrage_yield"] >= 0.02 else "OBSERVE",
                "commence_time": arb["commence_time"],
            })

    return output


# ── CLI ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Intel Betting Engine")
    parser.add_argument("--mode", choices=["full", "crypto", "sports", "markets"],
                        default="full", help="Analysis mode")
    parser.add_argument("--top-n", type=int, default=10,
                        help="Top N markets to analyze")
    parser.add_argument("--min-ev", type=float, default=0.03,
                        help="Minimum expected value threshold")
    parser.add_argument("--output", choices=["json", "table"],
                        default="json", help="Output format")
    parser.add_argument("--sport", default="soccer_epl",
                        help="Sport key for odds (see The Odds API docs)")

    args = parser.parse_args()

    crypto_analysis = None
    market_decisions = []
    sports_arbs = []

    if args.mode in ("full", "crypto"):
        print("[*] Scanning crypto intel...", file=sys.stderr)
        signals = collect_crypto_intel()
        crypto_analysis = analyze_crypto(signals)
        print(f"   Signals: {len(signals)}, Direction: {crypto_analysis['direction']}, "
              f"Prob: {crypto_analysis['probability_up']}, Conf: {crypto_analysis['confidence']}",
              file=sys.stderr)

    if args.mode in ("full", "markets"):
        print("[*] Scanning prediction markets...", file=sys.stderr)
        markets = collect_prediction_markets()
        market_decisions = analyze_markets(markets)
        print(f"   Markets: {len(markets)}, Candidates: {len(market_decisions)}",
              file=sys.stderr)

    if args.mode in ("full", "sports"):
        print("[*] Scanning sports odds...", file=sys.stderr)
        odds = collect_sports_odds(args.sport)
        sports_arbs = analyze_sports(odds)
        print(f"   Matches: {len(odds)}, Arbs: {len(sports_arbs)}",
              file=sys.stderr)

    result = make_decisions(crypto_analysis, market_decisions, sports_arbs)

    if args.output == "table":
        print_decision_table(result)
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))


def print_decision_table(result):
    """Pretty-print decisions as a table."""
    print(f"\n{'='*70}")
    print(f"  INTEL BETTING ENGINE -- {result['timestamp'][:19]}")
    print(f"  Bankroll: ${result['bankroll']:.2f} | Max position: ${result['max_position']:.2f}")
    print(f"{'='*70}")

    for i, d in enumerate(result["decisions"], 1):
        action_icon = {"BUY": "[BUY]", "ARB": "[ARB]", "OBSERVE": "[WATCH]", "SKIP": "[SKIP]"}.get(d["action"], "")
        print(f"\n  [{i}] {action_icon} {d['action']} | {d.get('type', 'market')}")

        if d["type"] == "crypto":
            print(f"      Market: {d['market']}")
            print(f"      Direction: {d['direction']} | Prob: {d['probability']:.1%}")
            print(f"      Edge: {d['edge']:.3f} | Confidence: {d['confidence']:.3f}")
            print(f"      Reasoning: {d['reasoning']}")

        elif d["type"] == "prediction_market":
            print(f"      Market: {d['market'][:70]}")
            print(f"      Platform: {d['platform']}")
            print(f"      Model: {d['model_probability']:.1%} | Market: {d['market_probability']:.1%}")
            print(f"      Edge: {d['edge']:.3f} | Confidence: {d['confidence']:.3f}")
            print(f"      Size: {d['recommended_size']} | EV: {d['expected_value']:.4f}")

        elif d["type"] == "sports_arbitrage":
            print(f"      Event: {d['event']}")
            print(f"      Arb yield: {d['arbitrage_yield']:.1%}")
            print(f"      Bet: {d['bet_on']} | Lay: {d['lay_on']}")
            print(f"      Size: {d['recommended_size']}")

    print(f"\n{'='*70}")
    buy_count = sum(1 for d in result["decisions"] if d["action"] == "BUY")
    arb_count = sum(1 for d in result["decisions"] if d["action"] == "ARB")
    print(f"  Total decisions: {len(result['decisions'])} (BUY: {buy_count}, ARB: {arb_count})")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
