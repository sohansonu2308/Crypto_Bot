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

# Analyst-aligned thresholds
FEAR_DEEP = 30
FEAR_EUPHORIA = 75

FUNDING_RETAIL = 0.02
FUNDING_EUPHORIA = 0.05

VOLUME_PRESTART = 1.2
VOLUME_START = 1.5
VOLUME_RETAIL = 1.8
VOLUME_CAPITULATION = 2.2
VOLUME_NORMAL = 1.2

# Glitch Window (Four Day Window)
GLITCH_DAYS = 4


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
    params = {"symbol": "BTCUSDT", "interval": "1d", "limit": 40}
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


def compute_health(trend, fear, funding, volume_ratio, capitulation_risk, retail_flag, absorption, glitch_active):
    score = 50

    if retail_flag:
        score -= 20

    if capitulation_risk:
        score -= 25

    if glitch_active:
        score -= 15  # key: reduce risk during "deceptive pump" window

    if funding < 0.0:
        score += 10
    elif funding < FUNDING_RETAIL:
        score += 5

    if absorption:
        score += 20

    if trend == "DOWN" and not absorption:
        score -= 10

    if fear < FEAR_DEEP:
        score -= 5

    return max(0, min(score, 100))


# ========= GLITCH WINDOW =========

def glitch_active(meta):
    """
    GLITCH window = 4 days after a high-risk regime appears.
    During this time: avoid trusting short-term pumps.
    """
    start = meta.get("glitch_start_utc")
    if not start:
        return False

    dt = parse_dt(start)
    if not dt:
        return False

    return utc_now() <= dt + timedelta(days=GLITCH_DAYS)


def start_glitch(meta):
    meta["glitch_start_utc"] = utc_now().isoformat()
    return meta


def stop_glitch(meta):
    meta.pop("glitch_start_utc", None)
    return meta


# ========= STATE ENGINE (V2.1 with GLITCH WINDOW) =========

def detect_market_state():
    fear = get_fear_greed()
    funding = get_funding_rate()
    klines = get_daily_klines()

    trend = get_trend(klines)
    volume_ratio = get_volume_ratio(klines)
    change_5d = get_recent_change_pct(klines, days=5)

    meta = load_meta()

    retail_entry = (funding >= FUNDING_RETAIL and volume_ratio >= VOLUME_RETAIL)

    capitulation_risk = (
        fear < FEAR_DEEP
        and volume_ratio >= VOLUME_CAPITULATION
        and trend in ["DOWN", "RANGE"]
        and change_5d < -3.0
    )

    lag_window_active = (
        fear < FEAR_DEEP
        and funding <= FUNDING_RETAIL
        and trend in ["DOWN", "RANGE"]
        and not capitulation_risk
    )

    # Glitch window activation logic:
    # if we detect ANY high-risk regime, start glitch window (if not already active)
    high_risk_regime = capitulation_risk or lag_window_active or retail_entry

    if high_risk_regime and not glitch_active(meta):
        meta = start_glitch(meta)

    # Absorption detection
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
        # Once absorption is detected, glitch window can be safely turned off early
        meta = stop_glitch(meta)

    g_active = glitch_active(meta)
    save_meta(meta)

    confidence = compute_confidence(trend, fear, funding, volume_ratio, retail_entry)
    health = compute_health(trend, fear, funding, volume_ratio, capitulation_risk, retail_entry, absorption, g_active)

    # Primary State Selection
    if capitulation_risk:
        state = "CAPITULATION_RISK"
    elif absorption:
        state = "ABSORPTION_DETECTED"
    elif g_active:
        state = "GLITCH_WINDOW_ACTIVE"
    elif lag_window_active:
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

    return state, confidence, health, fear, funding, volume_ratio, trend, change_5d, g_active


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
    state, confidence, health, fear, funding, vol, trend, change_5d, g_active = detect_market_state()
    last_state = load_last_state()

    if state != last_state:
        action_map = {
            "CAPITULATION_RISK": "Bloodbath risk. NO leverage. Wait.",
            "ABSORPTION_DETECTED": "Absorption detected. Begin real spot accumulation.",
            "GLITCH_WINDOW_ACTIVE": "GLITCH window active (4D). Ignore pumps/wicks. Risk-off.",
            "LAG_WINDOW_ACTIVE": "Lag window active. Patience. No aggressive longs.",
            "DEEP_FEAR": "Accumulate slowly. x2 max (spot preferred).",
            "PRE_START": "Accumulate. No aggression.",
            "START_CONFIRMED": "Hold/add on pullbacks. x3 allowed.",
            "LIQUIDITY_TRAP": "DO NOTHING. Bounce likely traps. Expect pullback.",
            "EUPHORIA": "Scale out. Protect capital.",
            "NEUTRAL": "Stand by."
        }

        extra = ""
        if g_active:
            extra = "\n⚠️ GLITCH: Time-based lag zone. Price can fake you out."

        msg = (
            f"📡 MARKET STATE UPDATE (V2.1)\n\n"
            f"State: {state}\n"
            f"Trend: {trend}\n"
            f"5D Change: {change_5d:.2f}%\n\n"
            f"Confidence: {confidence}/100\n"
            f"Health: {health}/100\n"
            f"Action: {action_map.get(state, 'Stand by.')}\n\n"
            f"Fear & Greed: {fear}\n"
            f"Funding: {funding:.4f}\n"
            f"Volume Ratio: {vol:.2f}"
            f"{extra}\n\n"
            f"Time: {utc_now().isoformat()}"
        )

        send_telegram(msg)
        save_state(state)


if __name__ == "__main__":
    main()
