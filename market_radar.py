import requests
import json
import os
from datetime import datetime, timezone, timedelta

# ========= CONFIG =========

BINANCE_SPOT = "https://api.binance.com/api/v3/klines"
BINANCE_FUTURES = "https://fapi.binance.com/fapi/v1/premiumIndex"
FNG_API = "https://api.alternative.me/fng/"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

STATE_FILE = "last_state.json"
META_FILE = "meta_state.json"

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
WICK_RATIO_CONFIRM = 0.55  # >55% wick dominance means fakeout/stop-hunt like behavior
RANGE_SPIKE_MULT = 1.25    # range vs avg range


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


def get_daily_klines():
    params = {"symbol": "BTCUSDT", "interval": "1d", "limit": 60}
    data = safe_get_json(BINANCE_SPOT, params=params)

    if isinstance(data, list) and len(data) > 0 and isinstance(data[0], list):
        return data

    print(f"[WARN] Invalid kline data: {data}")
    return []


def get_funding_rate():
    data = safe_get_json(BINANCE_FUTURES, params={"symbol": "BTCUSDT"})
    if not data:
        return 0.0

    if isinstance(data, dict) and "lastFundingRate" in data:
        try:
            return float(data["lastFundingRate"])
        except:
            return 0.0

    if isinstance(data, list):
        for item in data:
            if item.get("symbol") == "BTCUSDT":
                try:
                    return float(item.get("lastFundingRate", 0.0))
                except:
                    return 0.0

    return 0.0


# ========= FEATURE ENGINE =========

def get_trend(klines):
    if not klines or len(klines) < 15:
        return "RANGE"

    try:
        closes = [float(k[4]) for k in klines]
        if closes[-1] > closes[-5] > closes[-10]:
            return "UP"
        if closes[-1] < closes[-5] < closes[-10]:
            return "DOWN"
        return "RANGE"
    except Exception as e:
        print(f"[WARN] Trend calc failed: {e}")
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

    try:
        vols = [float(k[5]) for k in klines]
        avg_20 = sum(vols[-21:-1]) / 20
        if avg_20 <= 0:
            return 1.0
        return vols[-1] / avg_20
    except Exception as e:
        print(f"[WARN] Volume ratio calc failed: {e}")
        return 1.0


def get_recent_change_pct(klines, days=5):
    if not klines or len(klines) < days + 1:
        return 0.0

    try:
        closes = [float(k[4]) for k in klines]
        old = closes[-(days + 1)]
        new = closes[-1]
        if old == 0:
            return 0.0
        return ((new - old) / old) * 100
    except Exception as e:
        print(f"[WARN] Recent change calc failed: {e}")
        return 0.0


def candle_metrics_from_kline(k):
    """
    kline format:
    [open_time, open, high, low, close, volume, close_time, ...]
    """
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
    """
    How big today's range is vs average range.
    """
    if not klines or len(klines) < lookback + 2:
        return 1.0

    try:
        ranges = []
        for k in klines[-(lookback + 1):-1]:
            m = candle_metrics_from_kline(k)
            ranges.append(m["range"])
        avg_range = sum(ranges) / len(ranges)
        last_range = candle_metrics_from_kline(klines[-1])["range"]
        if avg_range <= 0:
            return 1.0
        return last_range / avg_range
    except Exception as e:
        print(f"[WARN] Range multiplier calc failed: {e}")
        return 1.0


# ========= META STATE =========

def load_meta():
    if os.path.exists(META_FILE):
        try:
            return json.load(open(META_FILE))
        except:
            return {}
    return {}


def save_meta(meta):
    json.dump(meta, open(META_FILE, "w"))


def parse_dt(s):
    try:
        return datetime.fromisoformat(s)
    except:
        return None


# ========= GLITCH WINDOW (Hybrid) =========

def glitch_watch_active(meta):
    """Time-based 4-day watch window."""
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
    """
    Candle-confirmed glitch logic:
      - BEAR_GLITCH: green candle with big wicks / rejection during bear/chop
      - BULL_GLITCH: red candle with big wicks / stop-hunt during bull
    """
    if not glitch_watch_active(meta):
        meta["glitch_confirmed"] = False
        return meta

    direction = get_glitch_direction(meta)
    wick_ratio = last_candle["wick_ratio"]

    bear_confirm = (
        direction == "BEAR_GLITCH"
        and regime in ["BEAR_MODE", "CHOP_MODE"]
        and last_candle["green"]
        and wick_ratio >= WICK_RATIO_CONFIRM
    )

    bull_confirm = (
        direction == "BULL_GLITCH"
        and regime == "BULL_MODE"
        and last_candle["red"]
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

    # Only penalize HEALTH if glitch is CONFIRMED by candles
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


# ========= STATE ENGINE (V2.3 Hybrid Candle GLITCH) =========

def detect_market_state():
    fear = get_fear_greed()
    funding = get_funding_rate()
    klines = get_daily_klines()

    trend = get_trend(klines)
    regime = get_regime(trend)
    volume_ratio = get_volume_ratio(klines)
    change_5d = get_recent_change_pct(klines, days=5)

    meta = load_meta()

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

    save_meta(meta)

    g_watch = glitch_watch_active(meta)
    g_conf = glitch_confirmed(meta)
    g_dir = get_glitch_direction(meta)

    confidence = compute_confidence(trend, fear, funding, volume_ratio, retail_entry)
    health = compute_health(regime, fear, funding, capitulation_risk, retail_entry, absorption, g_conf, g_dir)

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
        "range_mult": range_mult
    }


# ========= NOTIFICATION =========

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


def load_last_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE)).get("state")
        except:
            return None
    return None


def save_state(state):
    json.dump({"state": state}, open(STATE_FILE, "w"))


# ========= MAIN =========

def main():
    r = detect_market_state()
    state = r["state"]
    last_state = load_last_state()

    if state != last_state:
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

        msg = (
            f"📡 MARKET STATE UPDATE (V2.3)\n\n"
            f"State: {r['state']}\n"
            f"Regime: {r['regime']}\n"
            f"Trend: {r['trend']}\n"
            f"5D Change: {r['change_5d']:.2f}%\n\n"
            f"Confidence: {r['confidence']}/100\n"
            f"Health: {r['health']}/100\n"
            f"Action: {action_map.get(r['state'], 'Stand by.')}\n\n"
            f"Fear & Greed: {r['fear']}\n"
            f"Funding: {r['funding']:.4f}\n"
            f"Volume Ratio: {r['volume_ratio']:.2f}\n"
            f"Wick Ratio: {r['wick_ratio']:.2f}\n"
            f"Range Mult: {r['range_mult']:.2f}\n\n"
            f"{glitch_line}\n\n"
            f"Time: {utc_now().isoformat()}"
        )

        send_telegram(msg)
        save_state(state)


if __name__ == "__main__":
    main()
