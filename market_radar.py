import requests, os
from datetime import datetime, timezone

BINANCE="https://api.binance.com/api/v3/klines"
FUTURES="https://fapi.binance.com/fapi/v1/premiumIndex"
FNG="https://api.alternative.me/fng/"
COINGECKO="https://api.coingecko.com/api/v3/global"

TOKEN=os.getenv("TELEGRAM_TOKEN")
CHAT=os.getenv("TELEGRAM_CHAT_ID")

# ================= HELPERS =================

def now():
    return datetime.now(timezone.utc).isoformat()

def get(url,params=None):
    try:
        return requests.get(url,params=params,timeout=10).json()
    except:
        return None

def klines(sym,tf,lim):
    return get(BINANCE,{"symbol":sym,"interval":tf,"limit":lim}) or []

def closes(k): return [float(x[4]) for x in k]
def vols(k): return [float(x[5]) for x in k]

# ================= DATA =================

def fear():
    d=get(FNG)
    return int(d["data"][0]["value"]) if d else 50

def funding():
    d=get(FUTURES,{"symbol":"BTCUSDT"})
    try:return float(d["lastFundingRate"])
    except:return 0.0

def dominance():
    g=get(COINGECKO)
    try:return float(g["data"]["market_cap_percentage"]["btc"])
    except:return 50.0

# ================= STRUCTURE =================

def trend(k):
    c=closes(k)
    if len(c)<10:return"RANGE"
    if c[-1]>c[-5]>c[-10]:return"UP"
    if c[-1]<c[-5]<c[-10]:return"DOWN"
    return"RANGE"

def pct(c,n):
    if len(c)<n+1:return 0
    return((c[-1]-c[-n-1])/c[-n-1])*100

# ================= LAG ENGINE =================

def lag_phase():
    d=klines("BTCUSDT","1d",40)
    if len(d)<20:return"NONE"

    tr=trend(d)
    change=pct(closes(d),5)
    fr=fear()

    if fr>35:return"NONE"
    if tr=="DOWN" and change<-6:return"EARLY_LAG"
    if tr=="RANGE":return"MID_LAG"
    if tr in["RANGE","UP"]:return"LATE_LAG"
    return"LAG_ACTIVE"

# ================= ROTATION ENGINE =================

def rotation_phase():
    ethbtc=klines("ETHBTC","1d",20)
    if len(ethbtc)<10:return"BTC_LED"

    tr=trend(ethbtc)
    dom=dominance()

    if tr=="UP" and dom<55:return"ALT_EXPANSION"
    if tr=="UP":return"TRANSITION"
    return"BTC_LED"

# ================= ALT MOMENTUM =================

def alt_momentum():
    ethbtc=klines("ETHBTC","1d",15)
    btc=klines("BTCUSDT","1d",15)
    eth=klines("ETHUSDT","1d",15)

    if len(ethbtc)<10:return 50

    score=50

    if trend(ethbtc)=="UP":score+=15
    if pct(closes(eth),5)>pct(closes(btc),5):score+=15

    dom=dominance()
    if dom<55:score+=10
    if dom>58:score-=10

    return max(0,min(100,score))

# ================= NEW LIQUIDITY STAGE ENGINE =================

def liquidity_stage():
    d=klines("BTCUSDT","1d",30)
    fr=fear()
    f=funding()
    dom=dominance()

    tr=trend(d)

    # BUILDING = early cycle fear but not trending
    if fr<35 and tr=="DOWN":
        return"BUILDING"

    # PEAKING = dominance high + trend up
    if dom>58 and tr=="UP":
        return"PEAKING"

    # REVERSING = fear low + dominance falling
    if fr<30 and dom<56:
        return"REVERSING"

    # DRAINING = trend down but fear rising
    if tr=="DOWN" and fr>35:
        return"DRAINING"

    return"NEUTRAL"

# ================= LIQUIDITY VECTOR =================

def liquidity_vector(lag,stage):
    f=funding()
    fr=fear()

    if stage=="REVERSING" and lag in["MID_LAG","LATE_LAG"]:
        return"UPWARD_HUNT"

    if lag=="EARLY_LAG":
        return"DOWNWARD_HUNT"

    if fr<30 and f<=0:
        return"ABSORBING"

    return"NEUTRAL"

# ================= MACRO FLOW =================

def macro_flow(stage):
    if stage=="REVERSING":
        return"PRE_EXPANSION"
    if stage=="PEAKING":
        return"EXPANSION"
    if stage=="BUILDING":
        return"ACCUMULATING"
    return"ACCUMULATING"

# ================= TELEGRAM =================

def send(msg):
    if not TOKEN or not CHAT:return
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id":CHAT,"text":msg}
    )

# ================= MAIN =================

def main():
    lag=lag_phase()
    rot=rotation_phase()
    stage=liquidity_stage()
    liq=liquidity_vector(lag,stage)
    macro=macro_flow(stage)
    alt_score=alt_momentum()

    guide={
        "EARLY_LAG":"No trades yet.",
        "MID_LAG":"Spot accumulation allowed.",
        "LATE_LAG":"Prepare for expansion.",
        "NONE":"No lag."
    }

    msg=f"""📡 V3.2 LIQUIDITY INTELLIGENCE

Lag Phase: {lag}
Liquidity Stage: {stage}
Rotation Phase: {rot}
Liquidity Vector: {liq}
Macro Flow: {macro}

Alt Momentum Score: {alt_score}/100

Guidance: {guide.get(lag)}

Fear: {fear()}
Funding: {funding():.4f}
BTC Dominance: {dominance():.2f}

Time: {now()}
"""

    send(msg)

if __name__=="__main__":
    main()
