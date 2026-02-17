import requests, os
from datetime import datetime, timezone

BINANCE="https://api.binance.com/api/v3/klines"
FUTURES="https://fapi.binance.com/fapi/v1/premiumIndex"
FNG="https://api.alternative.me/fng/"
COINGECKO="https://api.coingecko.com/api/v3/global"

TOKEN=os.getenv("TELEGRAM_TOKEN")
CHAT=os.getenv("TELEGRAM_CHAT_ID")

API_HEALTH="OK"
DATA={}

# ================= HELPERS =================

def now():
    return datetime.now(timezone.utc).isoformat()

def get(url,params=None):
    try:
        r=requests.get(url,params=params,timeout=10)
        if r.status_code==200:
            return r.json()
    except:
        pass
    # retry once
    try:
        r=requests.get(url,params=params,timeout=10)
        if r.status_code==200:
            return r.json()
    except:
        pass
    return None

def klines(sym,tf,lim):
    global API_HEALTH
    data=get(BINANCE,{"symbol":sym,"interval":tf,"limit":lim})
    if isinstance(data,list):
        return data
    API_HEALTH="DEGRADED"
    return []

def closes(k):
    out=[]
    if not isinstance(k,list):return out
    for x in k:
        if isinstance(x,list) and len(x)>4:
            try: out.append(float(x[4]))
            except: pass
    return out

# ================= DATA LOAD (NEW CORE) =================

def load_data():
    DATA["btc_d"]=klines("BTCUSDT","1d",40)
    DATA["eth_d"]=klines("ETHUSDT","1d",20)
    DATA["ethbtc_d"]=klines("ETHBTC","1d",20)

# ================= MARKET DATA =================

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

def trend_from(c):
    if len(c)<10:return"RANGE"
    if c[-1]>c[-5]>c[-10]:return"UP"
    if c[-1]<c[-5]<c[-10]:return"DOWN"
    return"RANGE"

def pct(c,n):
    if len(c)<n+1:return 0
    return((c[-1]-c[-n-1])/c[-n-1])*100

# ================= ENGINES =================

def lag_phase():
    btc=closes(DATA["btc_d"])
    tr=trend_from(btc)
    fr=fear()
    change=pct(btc,5)

    if fr>35:return"NONE"
    if tr=="DOWN" and change<-6:return"EARLY_LAG"
    if tr=="RANGE":return"MID_LAG"
    if tr in["RANGE","UP"]:return"LATE_LAG"
    return"LAG_ACTIVE"

def rotation_phase():
    ethbtc=closes(DATA["ethbtc_d"])
    tr=trend_from(ethbtc)
    dom=dominance()

    if tr=="UP" and dom<55:return"ALT_EXPANSION"
    if tr=="UP":return"TRANSITION"
    return"BTC_LED"

def alt_momentum():
    ethbtc=closes(DATA["ethbtc_d"])
    btc=closes(DATA["btc_d"])
    eth=closes(DATA["eth_d"])

    if len(ethbtc)<10:return 50

    score=50

    if trend_from(ethbtc)=="UP":score+=15
    if pct(eth,5)>pct(btc,5):score+=15

    dom=dominance()
    if dom<55:score+=10
    if dom>58:score-=10

    return max(0,min(100,score))

def liquidity_stage():
    btc=closes(DATA["btc_d"])
    tr=trend_from(btc)
    fr=fear()
    dom=dominance()

    if fr<35 and tr=="DOWN":return"BUILDING"
    if dom>58 and tr=="UP":return"PEAKING"
    if fr<30 and dom<56:return"REVERSING"
    if tr=="DOWN" and fr>35:return"DRAINING"
    return"NEUTRAL"

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

def macro_flow(stage):
    if stage=="REVERSING":return"PRE_EXPANSION"
    if stage=="PEAKING":return"EXPANSION"
    if stage=="BUILDING":return"ACCUMULATING"
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
    load_data()

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

    msg=f"""📡 V3.3 LIQUIDITY INTELLIGENCE

API Health: {API_HEALTH}

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
