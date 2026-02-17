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

def vol_ratio(k):
    v=vols(k)
    if len(v)<21:return 1
    avg=sum(v[-21:-1])/20
    return v[-1]/avg if avg>0 else 1

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

# ================= ALT MOMENTUM SCORE =================

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

# ================= LIQUIDITY VECTOR =================

def liquidity_vector(lag):
    f=funding()
    fr=fear()

    if lag in["MID_LAG","LATE_LAG"] and f<=0 and fr<30:
        return"UPWARD_HUNT"

    if lag=="EARLY_LAG":
        return"DOWNWARD_HUNT"

    return"ABSORBING"

# ================= MACRO FLOW =================

def macro_flow():
    d=klines("BTCUSDT","1d",30)
    tr=trend(d)
    dom=dominance()

    if tr=="RANGE" and dom<56:return"PRE_EXPANSION"
    if tr=="UP":return"EXPANSION"
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
    liq=liquidity_vector(lag)
    macro=macro_flow()
    alt_score=alt_momentum()

    guide={
        "EARLY_LAG":"No trades yet.",
        "MID_LAG":"Spot accumulation allowed.",
        "LATE_LAG":"Prepare for expansion.",
        "NONE":"No lag."
    }

    msg=f"""📡 V3.1 INTELLIGENCE REPORT

Lag Phase: {lag}
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
