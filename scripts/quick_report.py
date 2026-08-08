"""Quick analysis — live data + scraped intel. CoinGecko API key enabled."""
import json, os, sys, requests
from datetime import datetime, timezone
from pathlib import Path

_KEY_FILE = Path(__file__).parent.parent / "COINGECKO_KEY.txt"
_CG_KEY = _KEY_FILE.read_text().strip() if _KEY_FILE.exists() else ""
_CG_HEADERS = {"x-cg-demo-api-key": _CG_KEY, "Accept": "application/json"} if _CG_KEY else {}

# ── previously scraped insider signals ─────────────────────────────────────
INSIDER_SIGNALS = {
    "bearish": [
        {"signal": "Strategy (MSTR) first-ever BTC sale", "weight": -0.25, "source": "crypto.news"},
        {"signal": "CLARITY Act stalling at 37% odds", "weight": -0.15, "source": "crypto.news"},
        {"signal": "FalconX 10% staff cuts", "weight": -0.08, "source": "crypto.news"},
        {"signal": "Coldcard exploit: up to 2,055 BTC stolen", "weight": -0.10, "source": "crypto.news"},
        {"signal": "FBI agent stole crypto, asked ChatGPT escape plan", "weight": -0.05, "source": "crypto.news"},
        {"signal": "Boltz halts swaps due to AI attacks", "weight": -0.05, "source": "crypto.news"},
        {"signal": "Tyler Williams exits Treasury as CLARITY stalls", "weight": -0.07, "source": "crypto.news"},
    ],
    "bullish": [
        {"signal": "BlackRock $311B money market onchain", "weight": +0.15, "source": "crypto.news"},
        {"signal": "Mastercard completes BVNK acquisition (stablecoins)", "weight": +0.10, "source": "crypto.news"},
        {"signal": "Cardano +22% whales add 240M ADA", "weight": +0.08, "source": "crypto.news"},
        {"signal": "XRP enters DeFi via RLUSD + Morpho/Flare", "weight": +0.10, "source": "crypto.news"},
        {"signal": "Italy Intesa rotation BTC->ETH (94% cut IBIT, 3x ETHB)", "weight": +0.12, "source": "crypto.news"},
        {"signal": "Ripple bank pilots going live", "weight": +0.08, "source": "crypto.news"},
        {"signal": "South Korea confirms Jan 2027 crypto tax launch", "weight": +0.05, "source": "crypto.news"},
        {"signal": "Nigeria sets 1% crypto tax — legitimization", "weight": +0.05, "source": "crypto.news"},
    ]
}


def get_coingecko_data():
    """Live market data from CoinGecko with API key (demo: 30s lag, 30 req/min)."""
    h = _CG_HEADERS

    # Market overview
    try:
        r = requests.get("https://api.coingecko.com/api/v3/global", headers=h, timeout=10)
        if r.status_code == 200:
            d = r.json()["data"]
            overview = {
                "btc_dominance": d.get("market_cap_percentage", {}).get("btc", 0),
                "eth_dominance": d.get("market_cap_percentage", {}).get("eth", 0),
                "mcap_change_24h": d.get("market_cap_change_percentage_24h_usd", 0),
                "total_mcap": d.get("total_market_cap", {}).get("usd", 0),
                "total_volume_24h": d.get("total_volume", {}).get("usd", 0),
                "active_cryptos": d.get("active_cryptocurrencies", 0),
                "btc_price": d.get("market_cap_percentage", {}).get("btc", 0),
                "eth_price": d.get("market_cap_percentage", {}).get("eth", 0),
            }
    except Exception:
        overview = {}

    # Core coins — keep it lean for slow networks
    COINS = "bitcoin,ethereum,solana,cardano,xrp"
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": COINS,
                "vs_currencies": "usd",
                "include_24hr_change": "true",
                "include_7d_change": "true",
                "include_market_cap": "true",
                "include_24hr_vol": "true",
            },
            headers=h,
            timeout=10,
        )
        if r.status_code == 200:
            coins = {}
            for cid, info in r.json().items():
                if isinstance(info, dict):
                    coins[cid] = {
                        "usd": info.get("usd", 0),
                        "change_24h": info.get("usd_24h_change", 0),
                        "change_7d": info.get("usd_7d_change", 0),
                        "market_cap": info.get("usd_market_cap", 0),
                        "volume_24h": info.get("usd_24h_vol", 0),
                    }
            overview["coins"] = coins
    except Exception:
        overview["coins"] = {}

    return overview


def compute_analysis(data):
    """Combine insider signals + live prices into predictions."""
    # Aggregate weights
    bearish_score = sum(s["weight"] for s in INSIDER_SIGNALS["bearish"])
    bullish_score = sum(s["weight"] for s in INSIDER_SIGNALS["bullish"])

    # Live price adjustment from coin data
    live_adj = 0.0
    coins = data.get("coins", {}) if isinstance(data, dict) else {}
    for coin, info in coins.items():
        if isinstance(info, dict):
            change = info.get("change_24h", 0) or 0
            if change != 0:
                live_adj += 0.02 if change > 0 else -0.02

    total_bearish = abs(bearish_score)
    total_bullish = bullish_score
    net = bullish_score + bearish_score + live_adj  # bearish_score is negative

    # Probability calculation
    total_weight = total_bearish + total_bullish
    prob_bullish = total_bullish / total_weight if total_weight > 0 else 0.5
    raw_confidence = min(total_weight, 1.0)

    # Adjust confidence by net direction agreement
    if abs(net) > abs(bearish_score) * 0.5 or abs(net) > abs(bullish_score) * 0.5:
        confidence = min(raw_confidence * 1.2, 1.0)
    else:
        confidence = raw_confidence * 0.7  # mixed signals

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "net_score": round(net, 3),
        "bullish_weight": round(total_bullish, 3),
        "bearish_weight": round(total_bearish, 3),
        "probability_bullish": round(prob_bullish, 3),
        "confidence": round(confidence, 3),
        "direction": "bullish" if net > 0.1 else ("bearish" if net < -0.1 else "neutral"),
        "signals": {
            "bearish": [s["signal"] for s in INSIDER_SIGNALS["bearish"]],
            "bullish": [s["signal"] for s in INSIDER_SIGNALS["bullish"]],
        },
    }


def generate_predictions(analysis, data):
    """Generate specific asset predictions."""
    coins = data.get("coins", {}) if isinstance(data, dict) else {}
    predictions = []

    # BTC
    btc = coins.get("bitcoin", {})
    predictions.append({
        "asset": "BTC",
        "current_price": btc.get("usd", 0),
        "change_24h": btc.get("change_24h", 0),
        "direction": "down" if analysis["net_score"] < 0 else "sideways",
        "probability": analysis["probability_bullish"],
        "target_range": "$58K-$64K" if analysis["net_score"] < 0 else "$64K-$68K",
        "timeframe": "1-2 weeks",
        "key_factors": [
            "MSTR first-ever BTC sale",
            "CLARITY Act legislative risk",
            "BlackRock onchain (long-term positive)",
        ],
    })

    # ETH
    eth = coins.get("ethereum", {})
    predictions.append({
        "asset": "ETH",
        "current_price": eth.get("usd", 0),
        "change_24h": eth.get("change_24h", 0),
        "direction": "up",
        "probability": 0.70,
        "target_range": "$1,900-$2,100",
        "timeframe": "1-2 weeks",
        "key_factors": [
            "Intesa Sanpaolo tripled ETH position",
            "Institutional rotation BTC->ETH",
            "DeFi ecosystem expansion",
        ],
    })

    # XRP
    xrp = coins.get("xrp", {})
    predictions.append({
        "asset": "XRP",
        "current_price": xrp.get("usd", 0),
        "change_24h": xrp.get("change_24h", 0),
        "direction": "up",
        "probability": 0.60,
        "target_range": "$1.10-$1.25",
        "timeframe": "2-4 weeks",
        "key_factors": [
            "RLUSD DeFi integration (Morpho/Flare)",
            "Ripple bank pilots live",
            "First real DeFi utility beyond speculation",
        ],
    })

    # ADA
    ada = coins.get("cardano", {})
    predictions.append({
        "asset": "ADA",
        "current_price": ada.get("usd", 0),
        "change_24h": ada.get("change_24h", 0),
        "direction": "up",
        "probability": 0.65,
        "target_range": "$0.22-$0.26",
        "timeframe": "1-2 weeks",
        "key_factors": [
            "Whales added 240M ADA",
            "+22% momentum break",
            "Ecosystem development activity",
        ],
    })

    # SOL
    sol = coins.get("solana", {})
    predictions.append({
        "asset": "SOL",
        "current_price": sol.get("usd", 0),
        "change_24h": sol.get("change_24h", 0),
        "direction": "sideways-down",
        "probability": 0.45,
        "target_range": "$70-$78",
        "timeframe": "1-2 weeks",
        "key_factors": [
            "Market-wide pressure from BTC",
            "Memecoin activity cooling",
            "No strong catalyst near-term",
        ],
    })

    return predictions


# Per-ticker signal mapping
INSIDER_SIGNALS_TICKERS = {
    "BTC": {"bearish": 0.25, "bullish": 0.15},
    "ETH": {"bearish": 0.05, "bullish": 0.20},
    "XRP": {"bearish": 0.02, "bullish": 0.15},
    "ADA": {"bearish": 0.02, "bullish": 0.10},
    "SOL": {"bearish": 0.08, "bullish": 0.03},
}


def main():
    print("[*] Fetching live prices from CoinGecko (API key)...", file=sys.stderr)
    data = get_coingecko_data()

    print("[*] Computing analysis from insider signals...", file=sys.stderr)
    analysis = compute_analysis(data)

    print("[*] Generating predictions...", file=sys.stderr)
    predictions = generate_predictions(analysis, data)

    output = {
        "analysis": analysis,
        "predictions": predictions,
    }

    print(json.dumps(output, indent=2, ensure_ascii=False))

    # Pretty summary
    print(f"\n{'='*60}")
    print(f"  INTEL REPORT — {analysis['timestamp'][:19]}")
    print(f"{'='*60}")
    print(f"  Direction: {analysis['direction'].upper()}")
    print(f"  Net score: {analysis['net_score']:.3f}")
    print(f"  Bullish weight: {analysis['bullish_weight']:.3f}")
    print(f"  Bearish weight: {analysis['bearish_weight']:.3f}")
    print(f"  Market prob: {analysis['probability_bullish']:.1%} bullish")
    print(f"  Confidence: {analysis['confidence']:.1%}")
    print(f"\n  LIVE PRICES (CoinGecko demo, ~30s lag):")
    coins = data.get("coins", {})
    for cid, label in [("bitcoin","BTC"), ("ethereum","ETH"), ("solana","SOL"),
                       ("cardano","ADA"), ("xrp","XRP"), ("dogecoin","DOGE")]:
        c = coins.get(cid, {})
        if c:
            ch = c.get("change_24h", 0)
            sign = "+" if ch > 0 else ""
            print(f"    {label:<6} ${c.get('usd',0):<10,.2f} {sign}{ch:.1f}%")
    print(f"    Market cap: ${data.get('total_mcap',0)/1e12:.2f}T | 24h change: {data.get('mcap_change_24h',0):.1f}%")
    print(f"\n  SIGNALS:")
    print(f"  --- Bearish ({len(analysis['signals']['bearish'])}):")
    for s in analysis["signals"]["bearish"]:
        print(f"    - {s}")
    print(f"  --- Bullish ({len(analysis['signals']['bullish'])}):")
    for s in analysis["signals"]["bullish"]:
        print(f"    + {s}")

    print(f"\n  PREDICTIONS:")
    print(f"  {'Asset':<6} {'Direction':<16} {'Prob':<8} {'Target':<16} {'Timeframe'}")
    print(f"  {'-'*6} {'-'*16} {'-'*8} {'-'*16} {'-'*10}")
    for p in predictions:
        if p["current_price"]:
            print(f"  {p['asset']:<6} {p['direction']:<16} {p['probability']:.0%}      "
                  f"${p['current_price']:<5.0f}->{p['target_range']:<8} {p['timeframe']}")
        else:
            print(f"  {p['asset']:<6} {p['direction']:<16} {p['probability']:.0%}      "
                  f"{p['target_range']:<16} {p['timeframe']}")


if __name__ == "__main__":
    main()
