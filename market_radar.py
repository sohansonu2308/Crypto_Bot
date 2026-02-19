"""
Liquidity Lag Market Phase Interpreter — V4.1
Runs every 4 hours via GitHub Actions.

Concept: Price reacts to liquidity changes with a delay (liquidity lag).
This bot identifies where the market sits in that lag cycle and reports
the macro phase, rotation, and directional pressure.
"""

import os
import time
import requests
from datetime import datetime, timezone

# ================= CONFIG =================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT   = os.getenv("TELEGRAM_CHAT_ID")

# API Endpoints
_FNG            = "https://api.alternative.me/fng/"
_CG_GLOBAL      = "https://api.coingecko.com/api/v3/global"
_CG_OHLC        = "https://api.coingecko.com/api/v3/coins/{}/ohlc"

# CoinGecko coin IDs for symbols we track
COIN_MAP = {
    "BTCUSDT": "bitcoin",
    "ETHUSDT": "ethereum",
}

# ================= STATE (single-run cache) =================
# All market data and derived values are computed once per run
# and stored here, so no API is called more than once.

_cache = {}   # raw API responses  — keyed by url+params string
_data  = {}   # processed closes, ratios etc.
_mkt   = {}   # fear, funding, dominance — fetched once, reused everywhere

API_HEALTH = "OK"   # mutated to DEGRADED if any fetch fails


# ================= HTTP LAYER =================

def _fetch(url: str, params: dict = None, retries: int = 3) -> dict | list | None:
    """
    Reliable GET with exponential backoff.
    Handles 429 rate-limit responses explicitly with a longer wait.
    Returns parsed JSON or None on total failure.
    """
    cache_key = url + str(sorted((params or {}).items()))
    if cache_key in _cache:
        return _cache[cache_key]

    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=12)

            if r.status_code == 200:
                result = r.json()
                _cache[cache_key] = result
                return result

            if r.status_code == 429:
                # Rate limited — wait longer before retrying
                wait = 20 * (attempt + 1)
                time.sleep(wait)
                continue

            if r.status_code in (502, 503, 504):
                # Transient server error — short backoff
                time.sleep(3 * (attempt + 1))
                continue

        except requests.exceptions.Timeout:
            time.sleep(2 * (attempt + 1))
        except requests.exceptions.RequestException:
            time.sleep(2 * (attempt + 1))

    return None


# ================= OHLC / KLINES =================

def _fetch_ohlc(symbol: str) -> list:
    """
    Fetch daily OHLC from CoinGecko.
    Returns list of [timestamp, open, high, low, close] entries.
    On failure marks API_HEALTH as DEGRADED and returns [].
    """
    global API_HEALTH

    coin = COIN_MAP.get(symbol)
    if not coin:
        API_HEALTH = "DEGRADED"
        return []

    data = _fetch(_CG_OHLC.format(coin), {"vs_currency": "usd", "days": "30"})

    if isinstance(data, list) and len(data) > 0:
        return data   # each row: [timestamp_ms, open, high, low, close]

    API_HEALTH = "DEGRADED"
    return []


def _closes(ohlc: list) -> list[float]:
    """Extract close prices from CoinGecko OHLC rows."""
    out = []
    for row in ohlc:
        if isinstance(row, (list, tuple)) and len(row) >= 5:
            try:
                out.append(float(row[4]))
            except (ValueError, TypeError):
                pass
    return out


# ================= MARKET SIGNALS (fetched once) =================

def _load_market_signals():
    """
    Fetch fear, funding rate proxy, and BTC dominance exactly once per run.
    Results stored in _mkt so engines can read them without extra calls.
    Funding rate is derived from CoinGecko intraday OHLC — no geo-blocked APIs needed.
    """
    global API_HEALTH

    # --- Fear & Greed ---
    fng = _fetch(_FNG)
    try:
        _mkt["fear"] = int(fng["data"][0]["value"])
    except (TypeError, KeyError, IndexError):
        _mkt["fear"] = 50
        API_HEALTH = "DEGRADED"

    # --- Funding Rate Proxy (derived from CoinGecko — no separate API needed) ---
    # Binance Futures (451) and Bybit (403) are both geo-blocked from this region.
    # Proxy logic: funding rate is conceptually negative when price is falling and
    # the market is oversold — i.e. shorts are dominant and paying longs.
    # We approximate this using BTC's 24h price change from the OHLC data already loaded.
    # If the last close is below the open of the same candle → bearish pressure → proxy < 0
    # If last close is above the open → bullish pressure → proxy > 0
    # This preserves the only condition that uses funding: `f <= 0` in liquidity_vector().
    try:
        btc_ohlc = _fetch(_CG_OHLC.format("bitcoin"), {"vs_currency": "usd", "days": "2"})
        if isinstance(btc_ohlc, list) and len(btc_ohlc) >= 2:
            # Use [-2] (last fully completed candle) instead of [-1] which may be mid-formation.
            # CoinGecko free tier returns 4h candles for days<=2, so [-2] is always closed.
            last = btc_ohlc[-2]   # [timestamp, open, high, low, close]
            open_price  = float(last[1])
            close_price = float(last[4])
            # Normalise to a small decimal like a real funding rate
            _mkt["funding"] = (close_price - open_price) / open_price
        else:
            _mkt["funding"] = 0.0
    except (TypeError, KeyError, IndexError, ValueError):
        _mkt["funding"] = 0.0
        print("[WARNING] Funding proxy unavailable — using neutral default 0.0")

    # --- BTC Dominance (CoinGecko global) ---
    cg = _fetch(_CG_GLOBAL)
    try:
        _mkt["dominance"] = float(cg["data"]["market_cap_percentage"]["btc"])
    except (TypeError, KeyError):
        _mkt["dominance"] = 50.0
        API_HEALTH = "DEGRADED"


# Convenience accessors — read from cache, never re-fetch

def fear()      -> int:   return _mkt.get("fear",      50)
def funding()   -> float: return _mkt.get("funding",   0.0)  # proxy: intraday BTC open/close spread
def dominance() -> float: return _mkt.get("dominance", 50.0)


# ================= DATA LOAD =================

def load_data():
    """
    Load all OHLC data and derived series once.
    Pre-compute closes and ETH/BTC ratio so engines don't repeat work.
    """
    btc_ohlc = _fetch_ohlc("BTCUSDT")
    eth_ohlc = _fetch_ohlc("ETHUSDT")

    _data["btc_closes"] = _closes(btc_ohlc)
    _data["eth_closes"] = _closes(eth_ohlc)

    # ETH/BTC ratio series — computed once, used by rotation + alt momentum
    btc_c = _data["btc_closes"]
    eth_c = _data["eth_closes"]
    pairs  = min(len(btc_c), len(eth_c))
    _data["ethbtc"] = [
        eth_c[i] / btc_c[i]
        for i in range(pairs)
        if btc_c[i] != 0
    ]

    _load_market_signals()


# ================= TECHNICAL HELPERS =================

def trend_from(series: list[float]) -> str:
    """
    Simple 3-point trend detection using closes at index -1, -5, -10.
    Requires at least 10 data points; returns RANGE otherwise.
    """
    if len(series) < 10:
        return "RANGE"
    if series[-1] > series[-5] > series[-10]:
        return "UP"
    if series[-1] < series[-5] < series[-10]:
        return "DOWN"
    return "RANGE"


def pct_change(series: list[float], n: int) -> float:
    """Percentage change of last close vs n candles ago."""
    if len(series) < n + 1:
        return 0.0
    return ((series[-1] - series[-n - 1]) / series[-n - 1]) * 100


# ================= ENGINES =================

def lag_phase() -> str:
    """
    Where is the market in the liquidity lag cycle?

    Lag only activates when fear ≤ 35 (distress zone).
    At fear > 35 there is no meaningful lag signal — returns NONE.

    EARLY_LAG  — Panic / damage phase, price still falling hard
    MID_LAG    — Absorption, price ranging, smart money quietly accumulating
    LATE_LAG   — Compression before expansion, structure improving
    LAG_ACTIVE — Fallback: lag present but phase unclear
    NONE       — No lag signal (market not in fear)
    """
    btc    = _data["btc_closes"]
    tr     = trend_from(btc)
    fr     = fear()
    change = pct_change(btc, 5)

    if fr > 35:
        return "NONE"

    if tr == "DOWN" and change < -6:
        return "EARLY_LAG"

    if tr == "RANGE":
        return "MID_LAG"

    if tr == "UP":          # fixed: was unreachable due to duplicate RANGE check
        return "LATE_LAG"

    return "LAG_ACTIVE"


def liquidity_stage() -> str:
    """
    Where is the macro liquidity cycle?

    BUILDING   — Liquidity increasing, accumulation zone
    PEAKING    — Risk-on phase, BTC-led, near a potential top
    REVERSING  — Liquidity turning while price may still rise (contrarian buy signal)
    DRAINING   — Late-cycle exhaustion
    NEUTRAL    — No clear stage
    """
    btc = _data["btc_closes"]
    tr  = trend_from(btc)
    fr  = fear()
    dom = dominance()

    if fr < 35 and tr == "DOWN":
        return "BUILDING"

    if dom > 58 and tr == "UP":
        return "PEAKING"

    if fr < 30 and dom < 56:        # fixed: removed stray quote mark
        return "REVERSING"

    if tr == "DOWN" and fr > 35:
        return "DRAINING"

    return "NEUTRAL"


def rotation_phase() -> str:
    """
    Where is capital flowing?

    BTC_LED       — Bitcoin leading, alts lagging
    TRANSITION    — Capital beginning to rotate toward alts
    ALT_EXPANSION — Stronger broad altcoin movement, dominance falling
    """
    ethbtc = _data["ethbtc"]
    tr     = trend_from(ethbtc)
    dom    = dominance()

    if tr == "UP" and dom < 55:
        return "ALT_EXPANSION"

    if tr == "UP":
        return "TRANSITION"

    return "BTC_LED"


def alt_momentum_score() -> int:
    """
    0–100 score for strength of altcoin cycle participation.
    Based on ETH/BTC trend, relative ETH vs BTC performance, and dominance.
    """
    ethbtc = _data["ethbtc"]
    btc    = _data["btc_closes"]
    eth    = _data["eth_closes"]

    if len(ethbtc) < 10:
        return 50

    score = 50

    if trend_from(ethbtc) == "UP":
        score += 15

    if pct_change(eth, 5) > pct_change(btc, 5):
        score += 15

    dom = dominance()
    if dom < 55:
        score += 10
    if dom > 58:
        score -= 10

    return max(0, min(100, score))


def liquidity_vector(lag: str, stage: str) -> str:
    """
    Directional pressure — where large players may be hunting liquidity.

    UPWARD_HUNT   — Smart money hunting stops/liquidity above price
    DOWNWARD_HUNT — Pressure still to the downside
    ABSORBING     — Market absorbing sell pressure quietly
    NEUTRAL       — No clear directional pressure
    """
    fr = fear()
    f  = funding()

    if stage == "REVERSING" and lag in ("MID_LAG", "LATE_LAG"):
        return "UPWARD_HUNT"

    if lag == "EARLY_LAG":
        return "DOWNWARD_HUNT"

    if fr < 30 and f <= 0:
        return "ABSORBING"

    return "NEUTRAL"


def macro_flow(stage: str) -> str:
    """High-level macro flow label derived from liquidity stage."""
    mapping = {
        "REVERSING": "PRE_EXPANSION",
        "PEAKING":   "EXPANSION",
        "BUILDING":  "ACCUMULATING",
        "DRAINING":  "LATE_CYCLE",
    }
    return mapping.get(stage, "ACCUMULATING")


# ================= TELEGRAM =================

def send_telegram(msg: str):
    """Send message to Telegram. Silently skips if credentials not set."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print("[Telegram] Credentials not set — printing to stdout instead.")
        print(msg)
        return

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"[Telegram] Failed: {resp.status_code} — {resp.text}")
    except requests.exceptions.RequestException as e:
        print(f"[Telegram] Exception: {e}")


# ================= GUIDANCE MAP =================

LAG_GUIDANCE = {
    "EARLY_LAG":  "⛔ No trades. Damage phase still active.",
    "MID_LAG":    "🟡 Spot accumulation allowed. Monitor structure.",
    "LATE_LAG":   "🟢 Prepare for expansion. Compression likely ending.",
    "LAG_ACTIVE": "🔵 Lag detected. Phase unclear — observe.",
    "NONE":       "⚪ No lag signal. Market not in fear zone.",
}


# ================= MAIN =================

def main():
    # Step 1: Load all data (one pass, everything cached)
    load_data()

    # Step 2: Run engines (all read from _data and _mkt — no extra API calls)
    lag   = lag_phase()
    stage = liquidity_stage()
    rot   = rotation_phase()
    vec   = liquidity_vector(lag, stage)
    macro = macro_flow(stage)
    score = alt_momentum_score()

    # Step 3: Read cached market values for display
    fr  = fear()
    f   = funding()
    dom = dominance()

    # Step 4: Build report
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    msg = (
        f"<b>📡 LIQUIDITY LAG INTELLIGENCE — V4.1</b>\n"
        f"<i>{ts}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔧 <b>API Health:</b> {API_HEALTH}\n\n"
        f"<b>[ CYCLE PHASES ]</b>\n"
        f"  Lag Phase:        <b>{lag}</b>\n"
        f"  Liquidity Stage:  <b>{stage}</b>\n"
        f"  Macro Flow:       <b>{macro}</b>\n\n"
        f"<b>[ FLOW & ROTATION ]</b>\n"
        f"  Rotation Phase:   <b>{rot}</b>\n"
        f"  Liquidity Vector: <b>{vec}</b>\n"
        f"  Alt Momentum:     <b>{score}/100</b>\n\n"
        f"<b>[ RAW SIGNALS ]</b>\n"
        f"  Fear & Greed:     {fr}/100\n"
        f"  Funding Rate:     {f:.4f}\n"
        f"  BTC Dominance:    {dom:.2f}%\n\n"
        f"<b>[ GUIDANCE ]</b>\n"
        f"  {LAG_GUIDANCE.get(lag, 'No guidance available.')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )

    # Step 5: Send
    send_telegram(msg)
    print(msg)   # also print for GitHub Actions logs


if __name__ == "__main__":
    main()
