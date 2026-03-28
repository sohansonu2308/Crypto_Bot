"""
Liquidity Lag Market Phase Interpreter — V4.7
Runs every 4 hours via GitHub Actions.

Concept: Price reacts to liquidity changes with a delay (liquidity lag).
This bot identifies where the market sits in that lag cycle and reports
the macro phase, rotation, and directional pressure.
"""

import os
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

# ================= CONFIG =================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT   = os.getenv("TELEGRAM_CHAT_ID")

# API Endpoints
_FNG            = "https://api.alternative.me/fng/"
_CG_GLOBAL      = "https://api.coingecko.com/api/v3/global"
_CG_OHLC        = "https://api.coingecko.com/api/v3/coins/{}/ohlc"
_OIL_FEED       = "https://query1.finance.yahoo.com/v8/finance/chart/CL=F"
_SPX_FEED       = "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC"

# RSS feeds — no API key needed, no geo-blocks
# Using crypto-native sources that cover geopolitical + macro events
# and are accessible globally without geo-restrictions
_RSS_FEEDS = [
    # CoinTelegraph — covers war, sanctions, Fed, regulations affecting crypto
    "https://cointelegraph.com/rss",
    # CryptoPanic — aggregates macro + geopolitical news impacting markets
    "https://cryptopanic.com/news/rss/",
    # CoinDesk — Fed, rates, institutional macro
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
]

# Keywords that trigger a risk alert
# War/conflict triggers
_WAR_KEYWORDS = [
    "war", "attack", "strike", "missile", "invasion", "conflict",
    "military", "bomb", "airstrike", "sanction", "nuclear",
    "iran", "russia", "ukraine", "israel", "hamas", "nato",
]
# Fed/rates triggers
_FED_KEYWORDS = [
    "fed", "federal reserve", "interest rate", "rate hike", "rate cut",
    "fomc", "powell", "inflation", "cpi", "recession", "gdp",
]

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

    # --- Oil Price WTI (D Man war progress bar) ---
    try:
        oil_raw = _fetch(_OIL_FEED, {"interval": "1d", "range": "2d"})
        oil_cls = oil_raw["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        oil_cls = [c for c in oil_cls if c is not None]
        if len(oil_cls) >= 2:
            _mkt["oil_price"]   = round(float(oil_cls[-1]), 2)
            _mkt["oil_chg_pct"] = round(((oil_cls[-1] - oil_cls[-2]) / oil_cls[-2]) * 100, 2)
        else:
            _mkt["oil_price"] = _mkt["oil_chg_pct"] = 0.0
    except Exception:
        _mkt["oil_price"] = _mkt["oil_chg_pct"] = 0.0
        print("[WARNING] Oil price unavailable")

    # --- SPX closes (BTC/SPX correlation — D Man: same liquidity drivers) ---
    try:
        spx_raw = _fetch(_SPX_FEED, {"interval": "1d", "range": "30d"})
        spx_cls = spx_raw["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        _mkt["spx_closes"] = [c for c in spx_cls if c is not None]
    except Exception:
        _mkt["spx_closes"] = []
        print("[WARNING] SPX data unavailable")


# Convenience accessors — read from cache, never re-fetch

def fear()       -> int:   return _mkt.get("fear",        50)
def funding()    -> float: return _mkt.get("funding",     0.0)
def dominance()  -> float: return _mkt.get("dominance",   50.0)
def oil_price()  -> float: return _mkt.get("oil_price",   0.0)
def oil_chg()    -> float: return _mkt.get("oil_chg_pct", 0.0)
def spx_closes() -> list:  return _mkt.get("spx_closes",  [])


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


def days_since_btc_peak(n: int = 30) -> int:
    """
    Count how many 4h candles (CoinGecko granularity) have passed since
    BTC made its highest close in the last n closes.
    Used to detect the BTC→ALT rotation window (2–7 days after BTC peaks).
    Returns -1 if data is insufficient.
    """
    closes = _data["btc_closes"]
    if len(closes) < n:
        return -1
    window    = closes[-n:]
    peak_idx  = window.index(max(window))          # index of peak within window
    candles_since = (n - 1) - peak_idx             # candles since peak
    # CoinGecko 30d OHLC returns ~6 candles/day (4h each)
    days = round(candles_since / 6)
    return days


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

    if (dom > 58 or fr >= 70) and tr == "UP":
        # D Man: "last stages of liquidity = major sign of bull market"
        # High greed (retail euphoria) OR high dominance while price rising = distribution risk
        return "PEAKING"

    # D Man: oil above $95 = Fed trapped = suppress bull signals
    oil_high = oil_price() > 95 and oil_price() > 0

    if fr < 30 and dom < 56 and not oil_high:
        return "REVERSING"

    if fr < 30 and dom < 56 and oil_high:
        return "DRAINING"  # oil override — war suppressing liquidity recovery

    if tr == "DOWN" and fr > 35:
        return "DRAINING"

    return "NEUTRAL"


def rotation_phase() -> str:
    """
    Where is capital flowing?

    D Man: "When BTC has peaked, last two days or a week — alts peak."
    BUT this only applies during bull expansion — NOT during macro downtrends.

    ALT_WINDOW_OPEN requires ALL THREE conditions to be true:
      1. BTC peaked 2–7 days ago (timing)
      2. ETH/BTC ratio holding or rising (alts not bleeding vs BTC)
      3. BTC dominance flat or falling (capital actually rotating out)

    If dominance is rising and ETH/BTC is falling, alts are just
    following BTC down — that is NOT a rotation, that is a correlated dump.

    BTC_LED         — Bitcoin leading, alts lagging or falling with BTC
    ALT_WINDOW_OPEN — All 3 conditions met — genuine alt rotation window
    TRANSITION      — ETH/BTC trending up, early rotation signs
    ALT_EXPANSION   — Full altcoin expansion, dominance below 55
    """
    ethbtc = _data["ethbtc"]
    tr     = trend_from(ethbtc)
    dom    = dominance()
    days   = days_since_btc_peak()

    # Check if ETH/BTC is holding or improving (alts not bleeding vs BTC)
    ethbtc_holding = pct_change(ethbtc, 3) >= 0  # ETH/BTC flat or up over last 3 candles

    # Check if dominance is flat or falling (capital rotating away from BTC)
    # We approximate this by checking if current dominance < recent dominance
    # Using the closes of BTC dominance is not available, so we use a soft threshold:
    # dominance below 57 = not in full BTC dominance surge mode
    dom_not_surging = dom < 57

    # D Man's timing rule — only fires if alts are genuinely responding
    if 2 <= days <= 7 and ethbtc_holding and dom_not_surging:
        return "ALT_WINDOW_OPEN"

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


def high_conviction_setup(lag: str, stage: str, rot: str) -> str:
    """
    D Man's highest conviction setup:
    REVERSING stage + LATE_LAG phase = liquidity turning while price
    still has room to run. This is the core contrarian bull signal.
    Also flags the alt rotation window when BTC has peaked.

    D Man asset hierarchy (from his positioning message):
    PRIMARY  — ETH + ERC chain alts (surviving alts with real ecosystems)
    BASE     — BTC
    SECONDARY — SOL, Hyperliquid

    Returns a special label or empty string if no setup detected.
    """
    if stage == "REVERSING" and lag == "LATE_LAG":
        return (
            "⚡ PRIME SETUP: Liquidity reversing + compression ending\n"
            "   Focus: ETH + ERC alts (primary) → SOL, HYPE (secondary) → BTC (base)"
        )
    if stage == "REVERSING" and lag == "MID_LAG":
        return (
            "🔥 HIGH CONVICTION: Liquidity reversing + absorption phase\n"
            "   Action: Accumulate ETH + surviving ERC alts. BTC as base."
        )
    if rot == "ALT_WINDOW_OPEN":
        return (
            "🔄 ALT ROTATION WINDOW: BTC peaked 2–7 days ago\n"
            "   Watch: ETH/ERC alts first, then SOL + Hyperliquid"
        )
    return ""


def macro_flow(stage: str) -> str:
    """High-level macro flow label derived from liquidity stage."""
    mapping = {
        "REVERSING": "PRE_EXPANSION",
        "PEAKING":   "EXPANSION",
        "BUILDING":  "ACCUMULATING",
        "DRAINING":  "LATE_CYCLE",
    }
    return mapping.get(stage, "ACCUMULATING")


# ================= OIL PROGRESS BAR =================

def oil_progress_bar() -> str:
    """D Man: Oil price is the war progress bar."""
    price = oil_price()
    chg   = oil_chg()
    if price == 0.0:
        return "N/A"
    if price < 75:
        label = "WAR RESOLVED — Bull signal zone"
        icon  = "🟢"
    elif price < 85:
        label = "DE-ESCALATING — Watch for D Man bull flip"
        icon  = "🟡"
    elif price < 95:
        label = "STALEMATE — No resolution yet"
        icon  = "🟠"
    elif price < 110:
        label = "ESCALATING — Iran pressure active"
        icon  = "🔴"
    else:
        label = "MAXIMUM PAIN — Hormuz severely disrupted"
        icon  = "🚨"
    return f"{icon} ${price} ({chg:+.1f}% today) — {label}"


# ================= BTC/SPX CORRELATION =================

def btc_spx_correlation() -> dict:
    """
    D Man: BTC and SPX share same liquidity drivers.
    BTC may now be LEADING stocks — use as early warning signal.
    """
    btc = _data.get("btc_closes", [])
    spx = spx_closes()
    if len(btc) < 10 or len(spx) < 10:
        return {"correlation": 0.0, "label": "Insufficient data"}
    n = 10
    b = btc[-n:]
    s = spx[-n:]
    b_mean = sum(b) / n
    s_mean = sum(s) / n
    num = sum((b[i] - b_mean) * (s[i] - s_mean) for i in range(n))
    den = (sum((b[i] - b_mean)**2 for i in range(n)) *
           sum((s[i] - s_mean)**2 for i in range(n))) ** 0.5
    corr = round(num / den, 2) if den != 0 else 0.0
    btc_3d = pct_change(btc, 3)
    spx_3d = pct_change(spx, 3) if len(spx) >= 4 else 0.0
    corr_pct = int(abs(corr) * 100)
    if abs(btc_3d) > abs(spx_3d) and btc_3d * spx_3d > 0:
        label = f"BTC LEADING SPX ({corr_pct}% corr) — BTC moves first"
    elif abs(spx_3d) > abs(btc_3d) and btc_3d * spx_3d > 0:
        label = f"BTC LAGGING SPX ({corr_pct}% corr) — stocks move first"
    elif corr > 0.7:
        label = f"BTC SYNCED with SPX ({corr_pct}% corr) — moving together"
    elif corr < 0.3:
        label = f"BTC DECOUPLING from SPX ({corr_pct}% corr) — temporary"
    else:
        label = f"BTC/SPX MIXED ({corr_pct}% corr)"
    return {"correlation": corr, "label": label}


# ================= WHALE ACCUMULATION SIGNAL =================

def whale_accumulation() -> str:
    """
    D Man: Whales accumulate predictably during retail panic.
    Three convergent signals = whale accumulation active.
    """
    fr     = fear()
    ethbtc = _data.get("ethbtc", [])
    dom    = dominance()
    signals = 0
    details = []
    if fr <= 20:
        signals += 1
        details.append(f"Fear {fr} — retail capitulating")
    if len(ethbtc) >= 6:
        chg = pct_change(ethbtc, 5)
        if chg >= -1.0:
            signals += 1
            details.append(f"ETH/BTC holding ({chg:+.1f}%)")
    if dom < 60:
        signals += 1
        details.append(f"Dom {dom:.1f}% — capital not fleeing alts")
    if signals == 3:
        return "🐋 WHALE ACCUMULATION: All 3 signals — " + " | ".join(details)
    elif signals == 2:
        return "👀 ACCUMULATION WATCH: 2/3 signals — " + " | ".join(details)
    return ""


# ================= ETH ECOSYSTEM MONITOR =================

def eth_ecosystem_signal() -> str:
    """
    D Man + Shitcap + Technical analyst all independently called
    ETH ecosystem as the primary expansion target.
    """
    ethbtc = _data.get("ethbtc", [])
    btc    = _data.get("btc_closes", [])
    eth    = _data.get("eth_closes", [])
    if len(ethbtc) < 10 or len(btc) < 6 or len(eth) < 6:
        return ""
    eth_vs_btc = pct_change(ethbtc, 5)
    tr         = trend_from(ethbtc)
    dom        = dominance()
    if tr == "UP" and eth_vs_btc > 0 and dom < 57:
        return (f"💎 ETH ECOSYSTEM STRONG: ETH/BTC trending up "
                f"({eth_vs_btc:+.1f}%) — primary expansion target activating")
    if eth_vs_btc >= -2.0 and pct_change(eth, 5) >= pct_change(btc, 5):
        return (f"📈 ETH HOLDING: ETH/BTC {eth_vs_btc:+.1f}% — "
                f"institutional base building quietly")
    return ""


# ================= GEOPOLITICAL RISK MONITOR =================

def _parse_rss(url: str) -> list[dict]:
    """
    Fetch and parse an RSS feed.
    Returns list of {title, summary} dicts for recent items.
    Uses raw requests so it goes through our existing _fetch retry logic.
    """
    try:
        r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)
        items = []
        for item in root.iter("item"):
            title = item.findtext("title") or ""
            desc  = item.findtext("description") or ""
            items.append({"title": title.strip(), "summary": desc.strip()})
            if len(items) >= 20:   # only check last 20 headlines per feed
                break
        return items
    except Exception:
        return []


def geo_risk_monitor() -> list[dict]:
    """
    Scan RSS feeds for war/conflict and Fed/rates headlines.
    Only returns items that match risk keywords — silent otherwise.

    Each returned item: {title, category}
    category = "⚔️ WAR/CONFLICT" or "🏦 FED/RATES"
    """
    alerts = []
    seen   = set()   # deduplicate across feeds

    for feed_url in _RSS_FEEDS:
        items = _parse_rss(feed_url)
        for item in items:
            title_lower = item["title"].lower()

            # Skip duplicates
            if title_lower in seen:
                continue
            seen.add(title_lower)

            # Check war keywords
            if any(kw in title_lower for kw in _WAR_KEYWORDS):
                alerts.append({
                    "title":    item["title"],
                    "category": "⚔️ WAR/CONFLICT"
                })
                continue

            # Check Fed keywords
            if any(kw in title_lower for kw in _FED_KEYWORDS):
                alerts.append({
                    "title":    item["title"],
                    "category": "🏦 FED/RATES"
                })

    # Cap at 5 most relevant alerts to keep message clean
    return alerts[:5]


# ================= BOTTOM DETECTOR =================

def bottom_detector() -> str:
    """
    Rothschild framework: detect when the crash bottom is likely in.
    D Man: "Immediate after" phase — the long entry after the short plays out.
    Arcanum: "Bargain Area / Lifetime Opportunity Area"

    Bottom forms when multiple signals converge simultaneously:
    1. Fear extreme (≤15) — retail fully capitulated
    2. BTC structure holding — no new lows forming
    3. Whale accumulation active — institutions buying quietly
    4. Oil stabilising or falling — war pressure easing
    5. Funding proxy negative — shorts dominant = squeeze fuel loaded

    Scoring: 5 signals = PRIME BOTTOM, 4 = LIKELY BOTTOM, 3 = WATCH
    Silent below 3 signals.
    """
    fr      = fear()
    btc     = _data.get("btc_closes", [])
    f_rate  = funding()
    oil     = oil_price()
    oil_chg_val = oil_chg()
    ethbtc  = _data.get("ethbtc", [])
    dom     = dominance()

    score   = 0
    details = []

    # Signal 1: Fear extreme — retail fully capitulated
    if fr <= 15:
        score += 1
        details.append(f"Fear {fr} — full capitulation")

    # Signal 2: BTC structure holding — last candle not making new lows
    if len(btc) >= 6:
        recent_low  = min(btc[-5:])
        current     = btc[-1]
        if current >= recent_low * 0.99:  # within 1% of recent low = holding
            score += 1
            details.append("BTC holding structure")

    # Signal 3: Whale accumulation — 2+ signals active
    whale_signals = 0
    if fr <= 20:
        whale_signals += 1
    if len(ethbtc) >= 6 and pct_change(ethbtc, 5) >= -1.0:
        whale_signals += 1
    if dom < 60:
        whale_signals += 1
    if whale_signals >= 2:
        score += 1
        details.append(f"Whale signals {whale_signals}/3 active")

    # Signal 4: Oil stabilising or falling — war pressure easing
    if oil > 0 and oil_chg_val <= 0:
        score += 1
        details.append(f"Oil stabilising (${oil} {oil_chg_val:+.1f}%)")

    # Signal 5: Funding negative — shorts loaded = short squeeze fuel
    if f_rate <= 0:
        score += 1
        details.append("Funding negative — squeeze fuel ready")

    # Return signal based on score
    sig_str = " | ".join(details)
    if score >= 5:
        return (
            "🎯 PRIME BOTTOM: All 5 signals active\n"
            f"   {sig_str}\n"
            "   D Man's IMMEDIATE AFTER phase — Arcanum's BARGAIN AREA\n"
            "   Deploy: ETH primary, SOL secondary, ERC alts follow"
        )
    elif score >= 4:
        return (
            "🟢 LIKELY BOTTOM: 4/5 signals active\n"
            f"   {sig_str}\n"
            "   Prepare positions. Wait for 5th signal before full deploy."
        )
    elif score >= 3:
        return (
            "👀 BOTTOM WATCH: 3/5 signals active\n"
            f"   {sig_str}\n"
            "   Not yet. Monitor closely next 4h run."
        )
    return ""


# ================= NARRATIVE vs REALITY FILTER =================

def narrative_vs_reality() -> str:
    """
    Rothschild framework: detect when official narratives diverge from
    what oil price and market structure actually say.

    D Man: "The only talk they will have is with fists and weapons."
    Shitcap: "You will see what they want you to see."

    Three divergence patterns to detect:

    FAKE PEACE — Peace narrative + oil still high
    Price pumps on ceasefire/talks headlines but oil unchanged above $95
    = Whales de-risking into retail buying the narrative
    = Manipulation pump, not real resolution

    REAL BOTTOM — Crash narrative + whale accumulation active
    Mainstream says panic/crisis but smart money is quietly accumulating
    = Actual bottom formation regardless of headline fear

    MANIPULATION PUMP — BTC pumping + fear still extreme + oil unchanged
    Price moved up significantly but underlying conditions unchanged
    = Short opportunity (D Man: "any up move I'll look to short")

    Silent when no divergence detected — only fires on real signals.
    """
    fr          = fear()
    oil         = oil_price()
    oil_c       = oil_chg()
    btc         = _data.get("btc_closes", [])
    ethbtc      = _data.get("ethbtc", [])
    dom         = dominance()

    # BTC short term pump — 3 candle change
    btc_3d_chg  = pct_change(btc, 3) if len(btc) >= 4 else 0.0

    # Whale accumulation check
    whale_count = 0
    if fr <= 20:              whale_count += 1
    if len(ethbtc) >= 6 and pct_change(ethbtc, 5) >= -1.0: whale_count += 1
    if dom < 60:              whale_count += 1

    alerts = []

    # Pattern 1: FAKE PEACE SIGNAL
    # RSS has peace/ceasefire keywords AND oil still above $95
    # BTC pumped 3%+ but oil unchanged = narrative not confirmed by reality
    if oil > 95 and btc_3d_chg > 3.0:
        alerts.append(
            f"⚠️ FAKE PEACE SIGNAL DETECTED\n"
            f"   BTC pumped {btc_3d_chg:+.1f}% but oil at ${oil} — war still ongoing\n"
            f"   Rothschild rule: official narrative != oil reality\n"
            f"   Do NOT chase this pump. Whales are distributing."
        )

    # Pattern 2: REAL BOTTOM
    if fr <= 15 and whale_count >= 2 and oil > 0 and oil_c <= 1.0:
        alerts.append(
            f"✅ REALITY CONFIRMS BOTTOM\n"
            f"   Fear {fr} + {whale_count}/3 whale signals + oil stabilising\n"
            f"   Narrative says panic. Reality says accumulation.\n"
            f"   This is Arcanum's Bargain Area forming."
        )

    # Pattern 3: MANIPULATION PUMP
    if btc_3d_chg > 5.0 and fr <= 25 and oil > 90:
        alerts.append(
            f"🚨 MANIPULATION PUMP DETECTED\n"
            f"   BTC +{btc_3d_chg:.1f}% but Fear={fr}, Oil=${oil}\n"
            f"   D Man: Any up move I will look to SHORT\n"
            f"   Whales de-risking. Do not buy this move."
        )

    if alerts:
        return "\n".join(alerts)
    return ""


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
    "EARLY_LAG":  "⛔ No trades. Damage phase active. Wait for structure.",
    "MID_LAG":    "🟡 Accumulate spot. Focus: ETH + surviving ERC alts. No leverage.",
    "LATE_LAG":   "🟢 Prepare for expansion. ETH/ERC primary. SOL/HYPE secondary. BTC base.",
    "LAG_ACTIVE": "🔵 Lag detected. Phase unclear — observe ETH/BTC ratio for direction.",
    "NONE":       "⚪ No lag signal. Market not in fear zone. Monitor for re-entry.",
}


# ================= MAIN =================

def main():
    # Step 1: Load all data (one pass, everything cached)
    load_data()

    # Step 2: Run engines + all monitors
    geo_alerts  = geo_risk_monitor()
    oil_bar     = oil_progress_bar()
    btc_spx     = btc_spx_correlation()
    whale_sig   = whale_accumulation()
    eth_sig     = eth_ecosystem_signal()
    bottom_sig  = bottom_detector()
    narrative   = narrative_vs_reality()
    lag    = lag_phase()
    stage  = liquidity_stage()
    rot    = rotation_phase()
    vec    = liquidity_vector(lag, stage)
    macro  = macro_flow(stage)
    score  = alt_momentum_score()
    combo  = high_conviction_setup(lag, stage, rot)
    d_peak = days_since_btc_peak()

    # Step 3: Read cached market values for display
    fr  = fear()
    f   = funding()
    dom = dominance()
    # ETH/BTC 5-candle change — D Man's primary rotation indicator
    ethbtc     = _data.get("ethbtc", [])
    ethbtc_chg = pct_change(ethbtc, 5) if len(ethbtc) >= 6 else 0.0

    # Step 4: Build report
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    msg = (
        f"<b>📡 LIQUIDITY LAG INTELLIGENCE — V4.7</b>\n"
        f"<i>{ts}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔧 <b>API Health:</b> {API_HEALTH}\n\n"
        f"<b>[ CYCLE PHASES ]</b>\n"
        f"  Lag Phase:        <b>{lag}</b>\n"
        f"  Liquidity Stage:  <b>{stage}</b>\n"
        f"  Macro Flow:       <b>{macro}</b>\n\n"
        f"<b>[ FLOW & ROTATION ]</b>\n"
        f"  Rotation Phase:   <b>{rot}</b> (BTC peak: {d_peak}d ago)\n"
        f"  Liquidity Vector: <b>{vec}</b>\n"
        f"  Alt Momentum:     <b>{score}/100</b>\n\n"
        f"<b>[ RAW SIGNALS ]</b>\n"
        f"  Fear & Greed:      {fr}/100\n"
        f"  Funding Rate:      {f:.4f}\n"
        f"  BTC Dominance:     {dom:.2f}%\n"
        f"  ETH/BTC 5d Change: {ethbtc_chg:+.2f}%\n\n"
        f"<b>[ 🛢 WAR PROGRESS BAR ]</b>\n"
        f"  {oil_bar}\n\n"
        f"<b>[ 📊 BTC/SPX ]</b>\n"
        f"  {btc_spx['label']}\n\n"
        f"<b>[ GUIDANCE ]</b>\n"
        f"  {LAG_GUIDANCE.get(lag, 'No guidance available.')}\n"
        f"  Days since BTC peak: <b>{d_peak}d</b>\n"
        + (f"\n<b>[ ⚡ SIGNAL ]</b>\n  {combo}\n" if combo else "")
        + (f"\n<b>[ 🐋 WHALE ]</b>\n  {whale_sig}\n" if whale_sig else "")
        + (f"\n<b>[ 💎 ETH ]</b>\n  {eth_sig}\n" if eth_sig else "")
        + (f"\n<b>[ 🎯 BOTTOM SIGNAL ]</b>\n  {bottom_sig}\n" if bottom_sig else "")
        + (f"\n<b>[ 🕊️ NARRATIVE vs REALITY ]</b>\n  {narrative}\n" if narrative else "")
        + (
            "\n<b>[ 🌍 RISK ALERTS ]</b>\n" +
            "\n".join(f"  {a['category']}: {a['title']}" for a in geo_alerts) + "\n"
            if geo_alerts else ""
        )
        + "━━━━━━━━━━━━━━━━━━━━━"
    )

    # Step 5: Send
    send_telegram(msg)
    print(msg)   # also print for GitHub Actions logs


if __name__ == "__main__":
    main()
