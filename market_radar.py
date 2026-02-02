import requests
import json
import os
from datetime import datetime, timezone, timedelta

# ========= CONFIG =========

BINANCE_SPOT = "https://api.binance.com/api/v3/klines"
BINANCE_FUTURES = "https://fapi.binance.com/fapi/v1/premiumIndex"
FNG_API = "https://api.alternative.me/fng/"
COINGECKO_GLOBAL = "https://api.coingecko.com/api/v3/global"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

STATE_FILE = "last_state.json"
META_FILE = "meta_state.json"
SNAPSHOT_FILE = "last_snapshot.json"

FEAR_DEEP = 30
FEAR_EUPHORIA = 75

FUNDING_RETAIL = 0.02
FUNDING_EUPHORIA = 0.05

VOLUME_PRESTART = 1.2
VOLUME_START = 1.5
VOLUME_RETAIL = 1.8
VOLUME_CAPITULATION = 2.2
VOLUME_NORMAL = 1.2

GLITCH_DAYS = 4

# Candle-confirm thresholds
WICK_RATIO_CONFIRM = 0.55
RANGE_SPIKE_MULT = 1.25

# Smart alert thresholds
CONFIDENCE_DELTA_ALERT = 15
HEALTH_DELTA_ALERT = 15
BIAS_SCORE_DELTA_ALERT = 12  # if bias score shifts a lot, notify even if state is same

# Bias thresholds + smoothing
BIAS_BULL_SCORE = 65
BIAS_BEAR_SCORE = 35
BIAS_HYSTERESIS_RUNS = 2


# ========= HELPERS =========

def utc_now():
    return datetime.now(timezone.utc)


def safe_get_json(url, params=None, timeout=10):
    try:
        r = requests.get(url, params=params, timeout=timeout)
        return r.json()
    except Exception as e:
        print(f"[WARN] Failed request {url}: {e}")
        return None


# ========= DATA FETCH =========

def get_fear_greed():
    data = safe_get_json(FNG_API)
    if not data or "data" not in data:
        return 50
    return int(data["data"][0]["value"])


def get_daily_klines(symbol="BTCUSDT"):
    params = {"symbol": symbol, "interval": "1d", "limit": 60}
    data = safe_get_json(BINANCE_SPOT, params=params)
    if isinstance(data, list) and len(data) > 0 and isinstance(data[0], list):
        return data
    print(f"[WARN] Invalid kline data for {symbol}: {data}")
    return []


def get_funding_rate(symbol="BTCUSDT"):
    data = safe_get_json(BINANCE_FUTURES, params={"symbol": symbol})
    if not data:
        return 0.0

    if isinstance(data, dict) and "lastFundingRate" in data:
        try:
            return float(data["lastFundingRate"])
        except:
            return 0.0

    if isinstance(data, list):
        for item in data:
            if item.get("symbol") == symbol:
                try:
                    return float(item.get("lastFundingRate", 0.0))
                except:
                    return 0.0

    return 0.0


def get_coingecko_global():
    """
    Free global market snapshot.
    We use only what is stable + useful:
    - total market cap change 24h (usd)
    - btc dominance (%)
    """
    data = safe_get_json(COINGECKO_GLOBAL, timeout=12)
    if not data or "data" not in data:
        return {
            "mcap_change_24h_pct_usd": None,
            "btc_dominance_pct": None
        }

    d = data["data"]
    mcap_change = d.get("market_cap_change_percentage_24h_usd", None)
    dominance = None
    if isinstance(d.get("market_cap_percentage"), dict):
        dominance = d["market_cap_percentage"].get("btc", None)

    return {
        "mcap_change_24h_pct_usd": mcap_change,
        "btc_dominance_pct": dominance
    }


# ========= FEATURE ENGINE =========

def get_trend(klines):
    if not klines or len(klines) < 15:
        return "RANGE"

    closes = [float(k[4]) for k in klines]
    if closes[-1] > closes[-5] > closes[-10]:
        return "UP"
    if closes[-1] < closes[-5] < closes[-10]:
        return "DOWN"
    return "RANGE"


def get_regime(trend):
    if trend == "UP":
        return "BULL_MODE"
    if trend == "DOWN":
        return "BEAR_MODE"
    return "CHOP_MODE"


def get_volume_ratio(klines):
    if not klines or len(klines) < 25:
        return 1.0

    vols = [float(k[5]) for k in klines]
    avg_20 = sum(vols[-21:-1]) / 20
    if avg_20 <= 0:
        return 1.0
    return vols[-1] / avg_20


def get_recent_change_pct(klines, days=5):
    if not klines or len(klines) < days + 1:
        return 0.0
    closes = [float(k[4]) for k in klines]
    old = closes[-(days + 1)]
    new = closes[-1]
    if old == 0:
        return 0.0
    return ((new - old) / old) * 100


def candle_metrics_from_kline(k):
    o = float(k[1])
    h = float(k[2])
    l = float(k[3])
    c = float(k[4])

    body = abs(c - o)
    rng = max(1e-9, (h - l))
    wick = rng - body
    wick_ratio = wick / rng

    green = c >= o
    red = c < o

    return {
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "range": rng,
        "body": body,
        "wick": wick,
        "wick_ratio": wick_ratio,
        "green": green,
        "red": red
    }


def get_range_multiplier(klines, lookback=20):
    if not klines or len(klines) < lookback + 2:
        return 1.0

    ranges = []
    for k in klines[-(lookback + 1):-1]:
        m = candle_metrics_from_kline(k)
        ranges.append(m["range"])

    avg_range = sum(ranges) / len(ranges)
    last_range = candle_metrics_from_kline(klines[-1])["range"]
    if avg_range <= 0:
        return 1.0
    return last_range / avg_range


# ========= STORAGE =========

def load_json_file(path):
    if os.path.exists(path):
        try:
            return json.load(open(path))
        except:
            return {}
    return {}


def save_json_file(path, data):
    json.dump(data, open(path, "w"))


def parse_dt(s):
    try:
        return datetime.fromisoformat(s)
    except:
        return None


# ========= GLITCH WINDOW (Hybrid) =========

def glitch_watch_active(meta):
    start = meta.get("glitch_start_utc")
    if not start:
        return False
    dt = parse_dt(start)
    if not dt:
        return False
    return utc_now() <= dt + timedelta(days=GLITCH_DAYS)


def start_glitch_watch(meta, direction):
    meta["glitch_start_utc"] = utc_now().isoformat()
    meta["glitch_direction"] = direction
    return meta


def stop_glitch(meta):
    meta.pop("glitch_start_utc", None)
    meta.pop("glitch_direction", None)
    meta.pop("glitch_confirmed", None)
    return meta


def get_glitch_direction(meta):
    return meta.get("glitch_direction", "UNKNOWN")


def confirm_glitch_if_needed(meta, regime, last_candle, range_mult):
    if not glitch_watch_active(meta):
        meta["glitch_confirmed"] = False
        return meta

    direction = get_glitch_direction(meta)
    wick_ratio = last_candle.get("wick_ratio", 0.0)

    bear_confirm = (
        direction == "BEAR_GLITCH"
        and regime in ["BEAR_MODE", "CHOP_MODE"]
        and last_candle.get("green", False)
        and wick_ratio >= WICK_RATIO_CONFIRM
    )

    bull_confirm = (
        direction == "BULL_GLITCH"
        and regime == "BULL_MODE"
        and last_candle.get("red", False)
        and wick_ratio >= WICK_RATIO_CONFIRM
    )

    if bear_confirm or bull_confirm or range_mult >= RANGE_SPIKE_MULT:
        meta["glitch_confirmed"] = True
    else:
        meta["glitch_confirmed"] = False

    return meta


def glitch_confirmed(meta):
    return bool(meta.get("glitch_confirmed", False))


# ========= SCORES =========

def compute_confidence(trend, fear, funding, volume_ratio, retail_flag):
    score = 0

    if trend == "UP":
        score += 25
    elif trend == "RANGE":
        score += 10

    if FEAR_DEEP <= fear <= 55:
        score += 20
    else:
        score += 5

    if volume_ratio > VOLUME_START:
        score += 20
    elif volume_ratio > VOLUME_PRESTART:
        score += 10

    if funding < FUNDING_RETAIL:
        score += 20
    elif funding < FUNDING_EUPHORIA:
        score += 10

    if retail_flag:
        score -= 25

    return max(0, min(score, 100))


def compute_health(regime, fear, funding, capitulation_risk, retail_flag, absorption, glitch_conf, g_dir):
    score = 50

    if retail_flag:
        score -= 20

    if capitulation_risk:
        score -= 25

    if glitch_conf:
        if g_dir == "BEAR_GLITCH":
            score -= 18
        elif g_dir == "BULL_GLITCH":
            score -= 8
        else:
            score -= 12

    if funding < 0.0:
        score += 10
    elif funding < FUNDING_RETAIL:
        score += 5

    if absorption:
        score += 20

    if fear < FEAR_DEEP:
        score -= 5

    if regime == "BEAR_MODE" and not absorption:
        score -= 7
    elif regime == "BULL_MODE" and not retail_flag:
        score += 5

    return max(0, min(score, 100))


# ========= BTC BIAS =========

def compute_btc_bias_score(trend, fear, funding, volume_ratio, change_5d):
    score = 50

    if trend == "UP":
        score += 20
    elif trend == "DOWN":
        score -= 20

    if 35 <= fear <= 65:
        score += 15
    elif fear < FEAR_DEEP:
        score -= 10
    elif fear > FEAR_EUPHORIA:
        score -= 10

    if funding < 0:
        score += 10
    elif funding < FUNDING_RETAIL:
        score += 5
    elif funding > FUNDING_EUPHORIA:
        score -= 15

    if VOLUME_PRESTART <= volume_ratio <= 1.9:
        score += 10
    elif volume_ratio > VOLUME_RETAIL:
        score -= 10

    if change_5d > 2.0:
        score += 5
    elif change_5d < -2.0:
        score -= 5

    return max(0, min(score, 100))


def bias_from_score(score):
    if score >= BIAS_BULL_SCORE:
        return "BULLISH"
    if score <= BIAS_BEAR_SCORE:
        return "BEARISH"
    return "NEUTRAL"


def update_bias_with_hysteresis(meta, prefix, next_bias):
    """
    prefix: 'btc' or 'mkt'
    """
    bias_key = f"{prefix}_bias"
    pending_key = f"{prefix}_bias_pending"
    pending_count_key = f"{prefix}_bias_pending_count"

    current_bias = meta.get(bias_key, "NEUTRAL")
    pending = meta.get(pending_key, None)
    pending_count = int(meta.get(pending_count_key, 0))

    if next_bias == current_bias:
        meta[pending_key] = None
        meta[pending_count_key] = 0
        return meta

    if pending != next_bias:
        meta[pending_key] = next_bias
        meta[pending_count_key] = 1
    else:
        meta[pending_count_key] = pending_count + 1

    if meta[pending_count_key] >= BIAS_HYSTERESIS_RUNS:
        meta[bias_key] = next_bias
        meta[pending_key] = None
        meta[pending_count_key] = 0

    return meta


# ========= MARKET BIAS (CoinGecko Global) =========

def compute_market_bias_score(coingecko):
    """
    Uses global market cap change + dominance.
    Free and stable-ish.
    """
    score = 50

    mcap_change = coingecko.get("mcap_change_24h_pct_usd", None)
    btc_dom = coingecko.get("btc_dominance_pct", None)

    # market cap change: positive -> risk on
    if isinstance(mcap_change, (int, float)):
        if mcap_change > 2.0:
            score += 20
        elif mcap_change > 0.5:
            score += 10
        elif mcap_change < -2.0:
            score -= 20
        elif mcap_change < -0.5:
            score -= 10

    # dominance:
    # higher dominance -> BTC-led / defensive
    # lower dominance -> risk-on broad market (alts participating)
    if isinstance(btc_dom, (int, float)):
        if btc_dom > 55:
            score -= 5  # more defensive
        elif btc_dom < 50:
            score += 5  # more broad risk appetite

    return max(0, min(score, 100))


# ========= INTERPRETATION =========

def interpret_bias(btc_bias, mkt_bias):
    if btc_bias == "BULLISH" and mkt_bias == "BULLISH":
        return "FULL RISK-ON (broad market participation)"
    if btc_bias == "BULLISH" and mkt_bias != "BULLISH":
        return "BTC-LED RALLY (early cycle / defensive risk-on)"
    if btc_bias != "BULLISH" and mkt_bias == "BULLISH":
        return "ALT RISK-ON (speculative rotation risk)"
    if btc_bias == "BEARISH" and mkt_bias == "BEARISH":
        return "FULL RISK-OFF"
    return "MIXED / UNCLEAR"


# ========= STATE ENGINE =========

def detect_market_state():
    fear = get_fear_greed()
    funding = get_funding_rate("BTCUSDT")
    klines = get_daily_klines("BTCUSDT")

    trend = get_trend(klines)
    regime = get_regime(trend)
    volume_ratio = get_volume_ratio(klines)
    change_5d = get_recent_change_pct(klines, days=5)

    meta = load_json_file(META_FILE)

    # Coingecko global snapshot
    cg = get_coingecko_global()

    # Candle metrics
    last_candle = candle_metrics_from_kline(klines[-1]) if klines else {
        "wick_ratio": 0.0, "green": False, "red": False, "range": 0, "body": 0
    }
    range_mult = get_range_multiplier(klines, lookback=20)

    retail_entry = (funding >= FUNDING_RETAIL and volume_ratio >= VOLUME_RETAIL)

    capitulation_risk = (
        fear < FEAR_DEEP
        and volume_ratio >= VOLUME_CAPITULATION
        and trend in ["DOWN", "RANGE"]
        and change_5d < -3.0
    )

    bear_lag_window = (
        fear < FEAR_DEEP
        and funding <= FUNDING_RETAIL
        and trend in ["DOWN", "RANGE"]
        and not capitulation_risk
    )

    bull_lag_window = (
        regime == "BULL_MODE"
        and funding < FUNDING_RETAIL
        and fear < FEAR_EUPHORIA
        and change_5d < 1.0
    )

    high_risk = capitulation_risk or bear_lag_window or bull_lag_window or retail_entry
    if high_risk and not glitch_watch_active(meta):
        if regime == "BEAR_MODE" or bear_lag_window or capitulation_risk:
            meta = start_glitch_watch(meta, "BEAR_GLITCH")
        elif regime == "BULL_MODE":
            meta = start_glitch_watch(meta, "BULL_GLITCH")
        else:
            meta = start_glitch_watch(meta, "BEAR_GLITCH")

    meta = confirm_glitch_if_needed(meta, regime, last_candle, range_mult)

    prev_capitulation = meta.get("capitulation_recent", False)
    if capitulation_risk:
        meta["capitulation_recent"] = True

    absorption = (
        prev_capitulation
        and volume_ratio <= VOLUME_NORMAL
        and funding < FUNDING_RETAIL
        and trend in ["RANGE", "UP"]
    )

    if absorption:
        meta["capitulation_recent"] = False
        meta = stop_glitch(meta)

    # ---------- BIAS MODES ----------
    btc_bias_score = compute_btc_bias_score(trend, fear, funding, volume_ratio, change_5d)

    # instant bias (raw)
    btc_bias_instant = bias_from_score(btc_bias_score)

    # confirmed bias (hysteresis)
    meta = update_bias_with_hysteresis(meta, "btc", btc_bias_instant)
    btc_bias_confirmed = meta.get("btc_bias", "NEUTRAL")

    mkt_bias_score = compute_market_bias_score(cg)

    # instant bias (raw)
    mkt_bias_instant = bias_from_score(mkt_bias_score)

    # confirmed bias (hysteresis)
    meta = update_bias_with_hysteresis(meta, "mkt", mkt_bias_instant)
    mkt_bias_confirmed = meta.get("mkt_bias", "NEUTRAL")

    bias_mode = interpret_bias(btc_bias_confirmed, mkt_bias_confirmed)

    save_json_file(META_FILE, meta)

    g_watch = glitch_watch_active(meta)
    g_conf = glitch_confirmed(meta)
    g_dir = get_glitch_direction(meta)

    confidence = compute_confidence(trend, fear, funding, volume_ratio, retail_entry)
    health = compute_health(regime, fear, funding, capitulation_risk, retail_entry, absorption, g_conf, g_dir)

    # ---------- STATE ----------
    if capitulation_risk:
        state = "CAPITULATION_RISK"
    elif absorption:
        state = "ABSORPTION_DETECTED"
    elif g_watch and g_conf:
        state = "GLITCH_WINDOW_ACTIVE"
    elif bear_lag_window or bull_lag_window:
        state = "LAG_WINDOW_ACTIVE"
    elif fear < FEAR_DEEP and funding <= 0:
        state = "DEEP_FEAR"
    elif retail_entry:
        state = "LIQUIDITY_TRAP"
    elif trend == "UP" and volume_ratio > VOLUME_START and funding < FUNDING_RETAIL:
        state = "START_CONFIRMED"
    elif trend == "UP" and volume_ratio > VOLUME_PRESTART:
        state = "PRE_START"
    elif fear > FEAR_EUPHORIA and funding >= FUNDING_EUPHORIA:
        state = "EUPHORIA"
    else:
        state = "NEUTRAL"

    return {
        "state": state,
        "regime": regime,
        "trend": trend,
        "confidence": confidence,
        "health": health,
        "fear": fear,
        "funding": funding,
        "volume_ratio": volume_ratio,
        "change_5d": change_5d,
        "glitch_watch": g_watch,
        "glitch_confirmed": g_conf,
        "glitch_direction": g_dir,
        "wick_ratio": last_candle.get("wick_ratio", 0.0),
        "range_mult": range_mult,

        # Bias fields
        "btc_bias_score": btc_bias_score,
        "btc_bias_instant": btc_bias_instant,
        "btc_bias_confirmed": btc_bias_confirmed,

        "mkt_bias_score": mkt_bias_score,
        "mkt_bias_instant": mkt_bias_instant,
        "mkt_bias_confirmed": mkt_bias_confirmed,

        "bias_mode": bias_mode,

        # CoinGecko snapshot
        "cg_mcap_change_24h": cg.get("mcap_change_24h_pct_usd", None),
        "cg_btc_dominance": cg.get("btc_dominance_pct", None),
    }


# ========= TELEGRAM =========

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram secrets missing. Skipping notify.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[WARN] Telegram send failed: {e}")


# ========= ALERT ENGINE =========

def should_notify(current, last):
    if not last:
        return True

    if last.get("state") != current.get("state"):
        return True

    if last.get("regime") != current.get("regime"):
        return True

    if last.get("glitch_confirmed") != current.get("glitch_confirmed"):
        return True

    if abs(last.get("confidence", 0) - current.get("confidence", 0)) >= CONFIDENCE_DELTA_ALERT:
        return True

    if abs(last.get("health", 0) - current.get("health", 0)) >= HEALTH_DELTA_ALERT:
        return True

    if abs(last.get("btc_bias_score", 0) - current.get("btc_bias_score", 0)) >= BIAS_SCORE_DELTA_ALERT:
        return True

    if abs(last.get("mkt_bias_score", 0) - current.get("mkt_bias_score", 0)) >= BIAS_SCORE_DELTA_ALERT:
        return True

    # daily heartbeat (UTC)
    today = utc_now().date().isoformat()
    if last.get("heartbeat_day") != today:
        return True

    return False


def build_message(r, is_heartbeat=False):
    glitch_line = ""
    if r["glitch_watch"]:
        if r["glitch_confirmed"]:
            if r["glitch_direction"] == "BEAR_GLITCH":
                glitch_line = "⚠️ GLITCH CONFIRMED (BEAR): Ignore pumps. No chasing."
            elif r["glitch_direction"] == "BULL_GLITCH":
                glitch_line = "⚠️ GLITCH CONFIRMED (BULL): Ignore dumps. Stop-hunt risk."
            else:
                glitch_line = "⚠️ GLITCH CONFIRMED: Lag/whipsaw zone."
        else:
            glitch_line = "🟡 Glitch WATCH: timer active, candle not confirmed yet."

    action_map = {
        "CAPITULATION_RISK": "Bloodbath risk. NO leverage. Wait.",
        "ABSORPTION_DETECTED": "Absorption detected. Begin real spot accumulation.",
        "GLITCH_WINDOW_ACTIVE": "Glitch active. Trade TIME not price.",
        "LAG_WINDOW_ACTIVE": "Lag window active. Patience. Avoid over-risk.",
        "DEEP_FEAR": "Accumulate slowly. x2 max (spot preferred).",
        "PRE_START": "Accumulate. No aggression.",
        "START_CONFIRMED": "Hold/add on pullbacks. x3 allowed.",
        "LIQUIDITY_TRAP": "DO NOTHING. Crowded zone. Expect pullback.",
        "EUPHORIA": "Scale out. Protect capital.",
        "NEUTRAL": "Stand by."
    }

    tag = "✅ HEARTBEAT" if is_heartbeat else "📡 UPDATE"

    # CoinGecko formatting
    cg_line = "Global MCap(24h): N/A | BTC Dom: N/A"
    if isinstance(r.get("cg_mcap_change_24h"), (int, float)) or isinstance(r.get("cg_btc_dominance"), (int, float)):
        mcap = r.get("cg_mcap_change_24h")
        dom = r.get("cg_btc_dominance")
        mcap_s = f"{mcap:.2f}%" if isinstance(mcap, (int, float)) else "N/A"
        dom_s = f"{dom:.2f}%" if isinstance(dom, (int, float)) else "N/A"
        cg_line = f"Global MCap(24h): {mcap_s} | BTC Dom: {dom_s}"

    return (
        f"{tag} (V2.5)\n\n"
        f"State: {r['state']}\n"
        f"Regime: {r['regime']}\n"
        f"Trend: {r['trend']}\n"
        f"5D Change: {r['change_5d']:.2f}%\n\n"
        f"Confidence: {r['confidence']}/100\n"
        f"Health: {r['health']}/100\n"
        f"Action: {action_map.get(r['state'], 'Stand by.')}\n\n"
        f"BTC Bias (Instant): {r['btc_bias_instant']} ({r['btc_bias_score']:.0f}/100)\n"
        f"BTC Bias (Confirmed): {r['btc_bias_confirmed']}\n\n"
        f"Market Bias (Instant): {r['mkt_bias_instant']} ({r['mkt_bias_score']:.0f}/100)\n"
        f"Market Bias (Confirmed): {r['mkt_bias_confirmed']}\n"
        f"Bias Mode: {r['bias_mode']}\n"
        f"{cg_line}\n\n"
        f"Fear & Greed: {r['fear']}\n"
        f"Funding: {r['funding']:.4f}\n"
        f"Volume Ratio: {r['volume_ratio']:.2f}\n"
        f"Wick Ratio: {r['wick_ratio']:.2f}\n"
        f"Range Mult: {r['range_mult']:.2f}\n\n"
        f"{glitch_line}\n\n"
        f"Time: {utc_now().isoformat()}"
    )


def main():
    current = detect_market_state()
    last = load_json_file(SNAPSHOT_FILE)

    notify = should_notify(current, last)

    today = utc_now().date().isoformat()
    is_heartbeat = False
    if not last or last.get("heartbeat_day") != today:
        is_heartbeat = True

    if notify:
        msg = build_message(current, is_heartbeat=is_heartbeat)
        send_telegram(msg)

    # persist heartbeat day for daily keepalive
    current["heartbeat_day"] = today
    save_json_file(SNAPSHOT_FILE, current)
    save_json_file(STATE_FILE, {"state": current["state"]})


if __name__ == "__main__":
    main()
