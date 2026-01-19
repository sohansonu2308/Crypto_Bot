import requests
import json
import os
from datetime import datetime

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
VOLUME_NORMAL = 1.2


# ========= DATA FETCH =========

def get_fear_greed():
    r = requests.get(FNG_API, timeout=10)
    return int(r.json()["data"][0]["value"])


def get_daily_klines():
    params = {"symbol": "BTCUSDT", "interval": "1d", "limit": 30}
    r = requests.get(BINANCE_SPOT, params=params, timeout=10)
    return r.json()


def get_funding_rate():
    try:
        r = requests.get(
            BINANCE_FUTURES,
            params={"symbol": "BTCUSDT"},
            timeout=10
        )
        data = r.json()

        # Case 1: normal dict response
        if isinstance(data, dict) and "lastFundingRate" in data:
            return float(data["lastFundingRate"])

        # Case 2: list response (sometimes happens)
        if isinstance(data, list):
            for item in data:
                if item.get("symbol") == "BTCUSDT":
                    return float(item.get("lastFundingRate", 0.0))

        # Any other unexpected response
        return 0.0

    except Exception as e:
        # Fail safe: treat as neutral funding
        print(f"[WARN] Funding rate fetch failed: {e}")
        return 0.0



# ========= FEATURE ENGINE =========

def get_trend(klines):
    closes = [float(k[4]) for k in klines]
    if closes[-1] > closes[-5] > closes[-10]:
        return "UP"
    if closes[-1] < closes[-5] < closes[-10]:
        return "DOWN"
    return "RANGE"


def get_volume_ratio(klines):
    volumes = [float(k[5]) for k in klines]
    avg_20 = sum(volumes[-21:-1]) / 20
    return volumes[-1] / avg_20


# ========= META STATE =========

def load_meta():
    if os.path.exists(META_FILE):
        return json.load(open(META_FILE))
    return {}


def save_meta(meta):
    json.dump(meta, open(META_FILE, "w"))


# ========= CONFIDENCE =========

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


# ========= STATE ENGINE =========

def detect_market_state():
    fear = get_fear_greed()
    funding = get_funding_rate()
    klines = get_daily_klines()

    trend = get_trend(klines)
    volume_ratio = get_volume_ratio(klines)

    meta = load_meta()
    last_retail = meta.get("retail_recent", False)

    retail_entry = (
        trend == "UP"
        and funding >= FUNDING_RETAIL
        and volume_ratio >= VOLUME_RETAIL
    )

    post_sweep = (
        last_retail
        and funding < FUNDING_RETAIL
        and volume_ratio <= VOLUME_NORMAL
        and trend == "UP"
    )

    if retail_entry:
        meta["retail_recent"] = True
    elif post_sweep:
        meta["retail_recent"] = False

    save_meta(meta)

    confidence = compute_confidence(
        trend, fear, funding, volume_ratio, retail_entry
    )

    if fear < FEAR_DEEP and funding <= 0:
        state = "DEEP_FEAR"
    elif post_sweep:
        state = "POST_SWEEP_OPPORTUNITY"
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

    return state, confidence, fear, funding, volume_ratio


# ========= NOTIFICATION =========

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }
    requests.post(url, json=payload, timeout=10)


def load_last_state():
    if os.path.exists(STATE_FILE):
        return json.load(open(STATE_FILE)).get("state")
    return None


def save_state(state):
    json.dump({"state": state}, open(STATE_FILE, "w"))


# ========= MAIN =========

def main():
    state, confidence, fear, funding, vol = detect_market_state()
    last_state = load_last_state()

    if state != last_state:
        action_map = {
            "DEEP_FEAR": "Accumulate slowly. x2 max.",
            "PRE_START": "Accumulate. No aggression.",
            "START_CONFIRMED": "Hold/add on pullbacks. x3 allowed.",
            "LIQUIDITY_TRAP": "DO NOTHING. Expect pullback.",
            "POST_SWEEP_OPPORTUNITY": "Best R/R zone. Controlled adds.",
            "EUPHORIA": "Scale out. Protect capital.",
            "NEUTRAL": "Stand by."
        }

        msg = (
            f"📡 MARKET STATE UPDATE\n\n"
            f"State: {state}\n"
            f"Confidence: {confidence}/100\n"
            f"Action: {action_map[state]}\n\n"
            f"Fear & Greed: {fear}\n"
            f"Funding: {funding:.4f}\n"
            f"Volume Ratio: {vol:.2f}\n\n"
            f"Time: {datetime.utcnow()} UTC"
        )

        send_telegram(msg)
        save_state(state)


if __name__ == "__main__":
    main()

