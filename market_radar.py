"""
API Diagnostic Script
Run this once to identify exactly which API is failing and why.
Usage: python diagnose.py
"""

import requests
import json
import time

# ================= ENDPOINTS =================

TESTS = {
    "Fear & Greed": {
        "url": "https://api.alternative.me/fng/",
        "params": None,
        "extract": lambda d: f"Value={d['data'][0]['value']}, Label={d['data'][0]['value_classification']}"
    },
    "Binance Futures - fundingRate": {
        "url": "https://fapi.binance.com/fapi/v1/fundingRate",
        "params": {"symbol": "BTCUSDT", "limit": 1},
        "extract": lambda d: f"Rate={d[0]['fundingRate']}, Time={d[0]['fundingTime']}"
    },
    "Binance Futures - premiumIndex (old)": {
        "url": "https://fapi.binance.com/fapi/v1/premiumIndex",
        "params": {"symbol": "BTCUSDT"},
        "extract": lambda d: f"lastFundingRate={d['lastFundingRate']}"
    },
    "CoinGecko - BTC Global Dominance": {
        "url": "https://api.coingecko.com/api/v3/global",
        "params": None,
        "extract": lambda d: f"BTC Dominance={d['data']['market_cap_percentage']['btc']:.2f}%"
    },
    "CoinGecko - BTC OHLC (30d)": {
        "url": "https://api.coingecko.com/api/v3/coins/bitcoin/ohlc",
        "params": {"vs_currency": "usd", "days": "30"},
        "extract": lambda d: f"Rows={len(d)}, Last close={d[-1][4]}"
    },
    "CoinGecko - ETH OHLC (30d)": {
        "url": "https://api.coingecko.com/api/v3/coins/ethereum/ohlc",
        "params": {"vs_currency": "usd", "days": "30"},
        "extract": lambda d: f"Rows={len(d)}, Last close={d[-1][4]}"
    },
}

# ================= RUNNER =================

def test_endpoint(name, url, params, extract_fn):
    print(f"\n{'='*55}")
    print(f"TEST: {name}")
    print(f"URL:  {url}")
    if params:
        print(f"Params: {params}")
    print("-" * 55)

    try:
        r = requests.get(url, params=params, timeout=15)
        print(f"Status Code : {r.status_code}")
        print(f"Headers     : {dict(r.headers).get('Content-Type', 'N/A')}")

        if r.status_code == 200:
            try:
                data = r.json()
                extracted = extract_fn(data)
                print(f"✅ SUCCESS")
                print(f"Data        : {extracted}")
            except Exception as e:
                print(f"⚠️  PARSE ERROR: {e}")
                print(f"Raw (first 300 chars): {r.text[:300]}")

        elif r.status_code == 429:
            retry_after = r.headers.get("Retry-After", "unknown")
            print(f"❌ RATE LIMITED (429)")
            print(f"Retry-After : {retry_after}s")
            print(f"Response    : {r.text[:200]}")

        elif r.status_code == 403:
            print(f"❌ FORBIDDEN (403) — Likely blocked by geo or API key required")
            print(f"Response    : {r.text[:200]}")

        elif r.status_code in (502, 503):
            print(f"❌ SERVER ERROR ({r.status_code}) — Temporary, retry later")

        else:
            print(f"❌ UNEXPECTED STATUS: {r.status_code}")
            print(f"Response    : {r.text[:200]}")

    except requests.exceptions.Timeout:
        print(f"❌ TIMEOUT — Server did not respond within 15s")

    except requests.exceptions.ConnectionError as e:
        print(f"❌ CONNECTION ERROR — {e}")
        print("   → Likely cause: DNS failure, firewall, or GitHub Actions IP block")

    except Exception as e:
        print(f"❌ UNEXPECTED ERROR — {e}")


def main():
    print("=" * 55)
    print("  LIQUIDITY BOT — API DIAGNOSTIC")
    print("=" * 55)
    print("Testing each endpoint individually...")
    print("Note: CoinGecko calls are spaced 8s apart to avoid 429s")

    results = {}

    for i, (name, cfg) in enumerate(TESTS.items()):
        # Space out CoinGecko calls to respect rate limits
        if i > 0 and "CoinGecko" in name:
            print(f"\n⏳ Waiting 8s before next CoinGecko call...")
            time.sleep(8)

        test_endpoint(name, cfg["url"], cfg["params"], cfg["extract"])
        results[name] = True  # placeholder — read output manually

    print(f"\n{'='*55}")
    print("DIAGNOSTIC COMPLETE")
    print("Check each result above for ✅ or ❌")
    print("Common fixes:")
    print("  429 on CoinGecko  → Add delay between calls (already in V4.0)")
    print("  403 on Binance    → GitHub Actions IP may be geo-blocked")
    print("  ConnectionError   → GitHub Actions outbound network restriction")
    print("  Timeout           → Endpoint is slow, increase timeout")
    print("=" * 55)


if __name__ == "__main__":
    main()
