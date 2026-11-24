"""Deterministic 0-100 Institutional Bull-Ignition Scorecard & 6-Phase Regime Classifier."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from src.market_data import MarketSnapshot
from src.telegram_ingest import TelegramTelemetrySummary

CycleRegime = Literal[
    "Markdown / Liquidity Drain",
    "Capitulation / Bottoming Watch",
    "Stealth Accumulation",
    "Early Bull Ignition",
    "Confirmed Bull Trend",
    "Bull Trap / Distribution",
]


class PillarScore(BaseModel):
    name: str
    score: float
    max_score: float
    notes: list[str] = Field(default_factory=list)


class BullIgnitionScorecard(BaseModel):
    total_score: int = Field(ge=0, le=100)
    quant_suggested_phase: CycleRegime
    bull_trap_risk_flag: bool = False
    disbelief_rally_flag: bool = False
    liquidity_pillar: PillarScore
    spot_structure_pillar: PillarScore
    derivatives_pillar: PillarScore
    breadth_rotation_pillar: PillarScore
    contrarian_sentiment_pillar: PillarScore
    bullish_confirmations: list[str] = Field(default_factory=list)
    bearish_blockers: list[str] = Field(default_factory=list)


def compute_bull_ignition_scorecard(
    snapshot: MarketSnapshot,
    telemetry: TelegramTelemetrySummary | None = None,
) -> BullIgnitionScorecard:
    """Evaluate all 5 institutional pillars into an auditable 0-100 Bull-Ignition Score."""
    confirmations: list[str] = []
    blockers: list[str] = []

    # ------------------------------------------------------------------
    # Pillar 1: Fiat Liquidity & Macro Wind (Max 25 pts)
    # ------------------------------------------------------------------
    liq = snapshot.liquidity
    flows = snapshot.spot_flows
    p1 = 0.0
    p1_notes: list[str] = []

    fiat_7d = liq.fiat_stablecoin_7d_change_pct
    if fiat_7d is None:
        fiat_7d = liq.stablecoin_7d_change_pct

    if fiat_7d is not None:
        if fiat_7d >= 0.5:
            p1 += 10.0
            msg = f"Pure fiat stables (USDT+USDC) expanding +{fiat_7d:.2f}% (7d)"
            p1_notes.append(msg)
            confirmations.append(msg)
        elif fiat_7d > 0.0:
            p1 += 6.5
            p1_notes.append(f"Pure fiat stables (USDT+USDC) slightly positive +{fiat_7d:.2f}% (7d)")
        elif fiat_7d > -0.35:
            p1 += 3.0
            p1_notes.append(f"Fiat stables nearly flat ({fiat_7d:.2f}% 7d)")
        else:
            msg = f"Fiat stablecoin supply contracting ({fiat_7d:.2f}% 7d) — net capital outflow"
            p1_notes.append(msg)
            blockers.append(msg)

    accel = liq.fiat_stablecoin_7d_accel_pct
    if accel is not None:
        if accel >= 0.35:
            p1 += 6.0
            msg = f"2nd-derivative stablecoin velocity accelerating (+{accel:.2f}% WoW)"
            p1_notes.append(msg)
            confirmations.append(msg)
        elif accel > 0.0:
            p1 += 3.5
            p1_notes.append(f"Stablecoin outflows decelerating / improving (+{accel:.2f}% WoW)")
        else:
            p1_notes.append(f"Stablecoin velocity decelerating ({accel:.2f}% WoW)")

    peg_bps = flows.usdt_peg_deviation_bps
    if peg_bps is not None:
        if peg_bps >= 1.5:
            p1 += 5.0
            msg = f"USDT trading above peg ({peg_bps:+.1f} bps) — active fiat minting demand"
            p1_notes.append(msg)
            confirmations.append(msg)
        elif peg_bps >= -3.0:
            p1 += 3.0
            p1_notes.append(f"USDT peg neutral ({peg_bps:+.1f} bps)")
        elif peg_bps >= -7.0:
            p1 += 1.0
            p1_notes.append(f"USDT slight discount ({peg_bps:+.1f} bps)")
        else:
            msg = f"USDT trading at {peg_bps:+.1f} bps discount — active USD fiat redemptions"
            p1_notes.append(msg)
            blockers.append(msg)

    fed_4w = liq.fed_net_liquidity_4w_change_pct
    if fed_4w is not None:
        if fed_4w >= 0.4:
            p1 += 4.0
            p1_notes.append(f"US Fed Net Liquidity expanding (+{fed_4w:.2f}% 4w)")
        elif fed_4w >= -0.25:
            p1 += 2.0
            p1_notes.append(f"US Fed Net Liquidity stable ({fed_4w:+.2f}% 4w)")
        else:
            p1_notes.append(f"US Fed Net Liquidity contracting ({fed_4w:+.2f}% 4w)")

    p1 = min(25.0, round(p1, 1))

    # ------------------------------------------------------------------
    # Pillar 2: BTC Spot Structure, Volume & Coinbase Premium (Max 25 pts)
    # ------------------------------------------------------------------
    st = snapshot.structure
    p2 = 0.0
    p2_notes: list[str] = []

    above_20 = st.btc_dist_sma20_pct is not None and st.btc_dist_sma20_pct > 0
    above_50 = st.btc_dist_sma50_pct is not None and st.btc_dist_sma50_pct > 0
    above_200 = st.btc_dist_sma200_pct is not None and st.btc_dist_sma200_pct > 0

    if above_20:
        p2 += 3.5
    if above_50:
        p2 += 4.0
    if above_200:
        p2 += 2.5

    if above_20 and above_50:
        msg = f"BTC reclaimed 20D ({st.btc_dist_sma20_pct:+.1f}%) & 50D ({st.btc_dist_sma50_pct:+.1f}%) SMAs"
        p2_notes.append(msg)
        confirmations.append(msg)
    elif st.btc_dist_sma50_pct is not None and st.btc_dist_sma50_pct < 0:
        msg = f"BTC below 50D SMA ({st.btc_dist_sma50_pct:+.1f}%)"
        p2_notes.append(msg)
        blockers.append(msg)

    donchian = st.btc_donchian_20d_pct
    if donchian is not None:
        if donchian >= 85.0:
            p2 += 4.0
            p2_notes.append(f"BTC breaking top of 20D range ({donchian:.0f}th percentile)")
        elif donchian >= 55.0:
            p2 += 2.0
        elif donchian <= 20.0:
            p2_notes.append(f"BTC near 20D lows ({donchian:.0f}th percentile)")

    if st.btc_higher_low_structure is True:
        p2 += 1.0
        p2_notes.append("10D higher-low structure intact")

    vol_ratio = st.spot_volume_20d_ratio
    btc_24h = snapshot.macro.btc.change_24h_pct or 0.0
    if vol_ratio is not None:
        if vol_ratio >= 1.6 and btc_24h > 0:
            p2 += 5.0
            msg = f"Institutional spot volume expansion ({vol_ratio:.2f}x 20D avg on green candle)"
            p2_notes.append(msg)
            confirmations.append(msg)
        elif vol_ratio >= 1.15 and btc_24h > 0:
            p2 += 3.5
            p2_notes.append(f"Healthy spot volume ({vol_ratio:.2f}x 20D avg)")
        elif btc_24h > 2.5 and vol_ratio < 0.85:
            p2_notes.append(f"Low-volume bounce warning ({vol_ratio:.2f}x 20D avg)")
            blockers.append(f"Price bounce lacks spot volume ({vol_ratio:.2f}x 20D avg)")
        else:
            p2 += 1.5

    cb_bps = flows.coinbase_premium_bps
    if cb_bps is not None:
        if cb_bps >= 4.0:
            p2 += 5.0
            msg = f"Positive Coinbase Spot Premium ({cb_bps:+.1f} bps) — US spot buyers leading"
            p2_notes.append(msg)
            confirmations.append(msg)
        elif cb_bps >= 0.0:
            p2 += 3.0
            p2_notes.append(f"Coinbase Spot Premium neutral/positive ({cb_bps:+.1f} bps)")
        elif cb_bps <= -6.0:
            msg = f"Negative Coinbase Spot Premium ({cb_bps:+.1f} bps) — US spot lagging/selling"
            p2_notes.append(msg)
            blockers.append(msg)
        else:
            p2 += 1.0

    p2 = min(25.0, round(p2, 1))

    # ------------------------------------------------------------------
    # Pillar 3: Derivatives Disbelief & Options Skew (Max 20 pts)
    # ------------------------------------------------------------------
    lev = snapshot.leverage
    p3 = 0.0
    p3_notes: list[str] = []
    disbelief_flag = False
    bull_trap_flag = False

    f8h = lev.btc.funding_rate_8h_pct
    basis = lev.btc.perp_basis_bps
    btc_7d = snapshot.macro.btc.change_7d_pct or 0.0

    if f8h is not None:
        if f8h <= 0.005 and (btc_24h > 1.5 or above_20):
            # Classic whale disbelief signal: price rising while shorts pay longs or funding dead flat!
            p3 += 10.0
            disbelief_flag = True
            msg = f"Disbelief rally setup: BTC advancing while 8h perp funding is uncrowded ({f8h:+.4f}%)"
            p3_notes.append(msg)
            confirmations.append(msg)
        elif -0.015 <= f8h <= 0.015:
            p3 += 7.0
            p3_notes.append(f"Clean, uncrowded BTC 8h funding ({f8h:+.4f}%)")
        elif f8h > 0.035:
            bull_trap_flag = True
            msg = f"Overheated perp long funding ({f8h:+.4f}% 8h) — crowded leverage risk"
            p3_notes.append(msg)
            blockers.append(msg)
        else:
            p3 += 4.0

    if basis is not None:
        if basis <= 0.0 and (btc_24h > 1.0 or above_20):
            p3 += 5.0
            p3_notes.append(f"Perps trading below spot ({basis:+.1f} bps) — spot-led bid")
        elif -8.0 <= basis <= 8.0:
            p3 += 3.5
            p3_notes.append(f"Balanced perp-to-oracle basis ({basis:+.1f} bps)")
        elif basis > 15.0 and (cb_bps is not None and cb_bps < 0):
            bull_trap_flag = True
            msg = f"Offshore perp squeeze warning: perp basis +{basis:.1f} bps while Coinbase spot at discount"
            p3_notes.append(msg)
            blockers.append(msg)
        else:
            p3 += 1.5

    pc_oi = lev.options.btc_put_call_oi_ratio
    if pc_oi is not None:
        if 0.40 <= pc_oi <= 0.70:
            p3 += 5.0
            p3_notes.append(f"Constructive Deribit BTC Put/Call OI ratio ({pc_oi:.2f})")
        elif pc_oi > 0.85:
            p3 += 2.5
            p3_notes.append(f"Defensive/hedged options positioning (Put/Call OI {pc_oi:.2f})")
        else:
            p3 += 3.0

    # Detect counter-trend squeeze trap
    if btc_24h >= 4.0 and (fiat_7d is not None and fiat_7d < -0.3) and (cb_bps is not None and cb_bps < -3.0):
        bull_trap_flag = True
        blockers.append("Sharp bounce occurring alongside negative Coinbase premium and shrinking fiat stables")

    p3 = min(20.0, round(p3, 1))

    # ------------------------------------------------------------------
    # Pillar 4: Breadth & Capital Rotation Order (Max 15 pts)
    # ------------------------------------------------------------------
    br = snapshot.breadth
    p4 = 0.0
    p4_notes: list[str] = []

    btc_d = snapshot.macro.btc_dominance_pct
    if btc_d is not None and btc_d >= 54.0 and (btc_7d > 0 or above_20):
        p4 += 5.0
        p4_notes.append(f"Healthy Phase-1 BTC leadership (BTC.D {btc_d:.1f}%)")
    elif btc_d is not None:
        p4 += 2.5

    pos_7d = br.majors_positive_7d_pct
    if pos_7d is not None:
        if pos_7d >= 65.0:
            p4 += 7.0
            msg = f"Broad institutional majors participation ({pos_7d:.0f}% of top-12 majors green 7d)"
            p4_notes.append(msg)
            confirmations.append(msg)
        elif pos_7d >= 40.0:
            p4 += 4.0
            p4_notes.append(f"Selective majors breadth ({pos_7d:.0f}% positive 7d)")
        else:
            msg = f"Weak liquid majors breadth (only {pos_7d:.0f}% of top-12 majors positive 7d)"
            p4_notes.append(msg)
            blockers.append(msg)

    if st.eth_btc_14d_change_pct is not None and st.eth_btc_14d_change_pct > 1.5:
        p4 += 3.0
        p4_notes.append(f"ETH/BTC stabilizing (+{st.eth_btc_14d_change_pct:.1f}% 14d)")
    elif st.eth_btc_14d_change_pct is not None and st.eth_btc_14d_change_pct >= -2.0:
        p4 += 1.5

    p4 = min(15.0, round(p4, 1))

    # ------------------------------------------------------------------
    # Pillar 5: Contrarian Sentiment & Telegram Apathy (Max 15 pts)
    # ------------------------------------------------------------------
    sent = snapshot.sentiment
    p5 = 0.0
    p5_notes: list[str] = []

    fg = sent.fear_greed_value
    fg_delta = sent.fear_greed_7d_delta or 0.0
    if fg is not None:
        if 20 <= fg <= 48 and fg_delta >= 4:
            # Exiting extreme fear upward = highest-probability early bull signal
            p5 += 9.0
            msg = f"Fear & Greed inflecting upward out of fear ({fg}, {fg_delta:+.0f} vs 7d ago)"
            p5_notes.append(msg)
            confirmations.append(msg)
        elif fg < 20:
            p5 += 6.0
            p5_notes.append(f"Extreme Fear ({fg}) — seller exhaustion zone, awaiting upward turn")
        elif 48 < fg <= 68:
            p5 += 6.5
            p5_notes.append(f"Constructive mid-cycle sentiment (F&G {fg})")
        elif fg > 78:
            p5 += 1.0
            bull_trap_flag = True
            msg = f"Extreme Greed euphoria (F&G {fg}) — late-stage distribution risk"
            p5_notes.append(msg)
            blockers.append(msg)
        else:
            p5 += 4.5

    if telemetry is not None:
        apathy = telemetry.overall_views_apathy_ratio
        if apathy is not None:
            if apathy <= 0.70:
                p5 += 6.0
                msg = f"High retail Telegram apathy (views {apathy:.2f}x of 60d median) — contrarian bottom signal"
                p5_notes.append(msg)
                confirmations.append(msg)
            elif apathy <= 1.05:
                p5 += 3.5
                p5_notes.append(f"Normal Telegram engagement ({apathy:.2f}x of 60d median)")
            elif apathy >= 1.6 and fg is not None and fg > 70:
                p5_notes.append(f"Retail FOMO view spike ({apathy:.2f}x of 60d median)")
            else:
                p5 += 2.0
        else:
            p5 += 3.0
    else:
        p5 += 3.0

    p5 = min(15.0, round(p5, 1))

    total_score = int(round(min(100.0, max(0.0, p1 + p2 + p3 + p4 + p5))))
    phase = _classify_regime(
        total_score=total_score,
        snapshot=snapshot,
        bull_trap_flag=bull_trap_flag,
        above_20=above_20,
        above_50=above_50,
        above_200=above_200,
        fiat_7d=fiat_7d,
    )

    return BullIgnitionScorecard(
        total_score=total_score,
        quant_suggested_phase=phase,
        bull_trap_risk_flag=bull_trap_flag,
        disbelief_rally_flag=disbelief_flag,
        liquidity_pillar=PillarScore(name="Fiat Liquidity & Macro", score=p1, max_score=25.0, notes=p1_notes),
        spot_structure_pillar=PillarScore(name="BTC Spot Structure & Demand", score=p2, max_score=25.0, notes=p2_notes),
        derivatives_pillar=PillarScore(name="Derivatives Disbelief & Skew", score=p3, max_score=20.0, notes=p3_notes),
        breadth_rotation_pillar=PillarScore(name="Breadth & Capital Rotation", score=p4, max_score=15.0, notes=p4_notes),
        contrarian_sentiment_pillar=PillarScore(name="Contrarian Sentiment & Apathy", score=p5, max_score=15.0, notes=p5_notes),
        bullish_confirmations=confirmations,
        bearish_blockers=blockers,
    )


def _classify_regime(
    *,
    total_score: int,
    snapshot: MarketSnapshot,
    bull_trap_flag: bool,
    above_20: bool,
    above_50: bool,
    above_200: bool,
    fiat_7d: float | None,
) -> CycleRegime:
    fg = snapshot.sentiment.fear_greed_value
    accel = snapshot.liquidity.fiat_stablecoin_7d_accel_pct

    # 1. Explicit Bull Trap / Late Distribution check
    if bull_trap_flag and (fg is None or fg > 30):
        return "Bull Trap / Distribution"

    # 2. Confirmed Bull Trend
    if total_score >= 74 and above_50 and above_200 and (fiat_7d is None or fiat_7d >= 0):
        return "Confirmed Bull Trend"

    # 3. Early Bull Ignition
    if total_score >= 62 and above_20 and (above_50 or (snapshot.structure.btc_donchian_20d_pct or 0) >= 85):
        return "Early Bull Ignition"

    # 4. Stealth Accumulation
    if total_score >= 45 and (fiat_7d is not None and fiat_7d >= -0.25) and (fg is None or fg <= 52):
        return "Stealth Accumulation"

    # 5. Capitulation / Bottoming Watch vs Markdown / Liquidity Drain
    if fg is not None and fg <= 20:
        # Distinguish active waterfall drain from bottoming stabilization
        if (accel is not None and accel > 0) or (snapshot.structure.btc_higher_low_structure is True):
            return "Capitulation / Bottoming Watch"
        if fiat_7d is not None and fiat_7d < -0.5:
            return "Markdown / Liquidity Drain"
        return "Capitulation / Bottoming Watch"

    if not above_50 and (fiat_7d is None or fiat_7d < 0):
        return "Markdown / Liquidity Drain"

    return "Stealth Accumulation" if total_score >= 42 else "Markdown / Liquidity Drain"
