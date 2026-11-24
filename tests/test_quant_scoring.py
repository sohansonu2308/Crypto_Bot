"""Tests for the deterministic 0-100 Bull-Ignition Scorecard across canonical market regimes."""

from __future__ import annotations

from src.market_data import (
    AssetMetrics,
    BreadthMetrics,
    LeverageMetrics,
    LiquidityMetrics,
    MacroMetrics,
    MarketSnapshot,
    OptionsMetrics,
    PerpAssetMetrics,
    PriceStructureMetrics,
    SentimentMetrics,
    SpotFlowMetrics,
)
from src.quant_scoring import compute_bull_ignition_scorecard
from src.telegram_ingest import TelegramTelemetrySummary


def test_late_bear_liquidity_drain_never_misclassified_as_distribution():
    snap = MarketSnapshot(
        macro=MacroMetrics(
            btc=AssetMetrics(symbol="BTC", price_usd=60350.0, change_24h_pct=-2.5, change_7d_pct=-7.0),
            btc_dominance_pct=58.6,
        ),
        structure=PriceStructureMetrics(
            btc_dist_sma20_pct=-8.5,
            btc_dist_sma50_pct=-14.0,
            btc_dist_sma200_pct=-18.0,
            btc_donchian_20d_pct=8.0,
            btc_higher_low_structure=False,
            spot_volume_20d_ratio=0.9,
        ),
        spot_flows=SpotFlowMetrics(
            coinbase_premium_bps=-4.5,
            usdt_peg_deviation_bps=-12.4,
        ),
        leverage=LeverageMetrics(
            btc=PerpAssetMetrics(symbol="BTC", funding_rate_8h_pct=0.0009, perp_basis_bps=-3.5, source="hyperliquid"),
        ),
        liquidity=LiquidityMetrics(
            fiat_stablecoin_7d_change_pct=-1.12,
            fiat_stablecoin_7d_accel_pct=-0.30,
            fed_net_liquidity_4w_change_pct=-0.6,
        ),
        breadth=BreadthMetrics(majors_positive_7d_pct=8.0),
        sentiment=SentimentMetrics(fear_greed_value=11, fear_greed_label="Extreme Fear", fear_greed_7d_delta=-5.0),
    )

    card = compute_bull_ignition_scorecard(snap)
    assert card.quant_suggested_phase == "Markdown / Liquidity Drain"
    assert card.total_score < 40


def test_offshore_perp_squeeze_detected_as_bull_trap():
    snap = MarketSnapshot(
        macro=MacroMetrics(
            btc=AssetMetrics(symbol="BTC", price_usd=68000.0, change_24h_pct=6.5, change_7d_pct=5.0),
            btc_dominance_pct=56.0,
        ),
        structure=PriceStructureMetrics(
            btc_dist_sma20_pct=2.0,
            btc_dist_sma50_pct=-4.0,
            btc_dist_sma200_pct=-8.0,
            btc_donchian_20d_pct=70.0,
            spot_volume_20d_ratio=0.75,
        ),
        spot_flows=SpotFlowMetrics(
            coinbase_premium_bps=-11.0,
            usdt_peg_deviation_bps=-8.5,
        ),
        leverage=LeverageMetrics(
            btc=PerpAssetMetrics(symbol="BTC", funding_rate_8h_pct=0.052, perp_basis_bps=21.0, source="hyperliquid"),
        ),
        liquidity=LiquidityMetrics(
            fiat_stablecoin_7d_change_pct=-0.85,
            fiat_stablecoin_7d_accel_pct=-0.2,
        ),
        breadth=BreadthMetrics(majors_positive_7d_pct=25.0),
        sentiment=SentimentMetrics(fear_greed_value=54, fear_greed_label="Neutral", fear_greed_7d_delta=15.0),
    )

    card = compute_bull_ignition_scorecard(snap)
    assert card.bull_trap_risk_flag is True
    assert card.quant_suggested_phase == "Bull Trap / Distribution"


def test_true_whale_disbelief_ignition_scores_high():
    snap = MarketSnapshot(
        macro=MacroMetrics(
            btc=AssetMetrics(symbol="BTC", price_usd=74500.0, change_24h_pct=4.2, change_7d_pct=9.5),
            btc_dominance_pct=58.2,
        ),
        structure=PriceStructureMetrics(
            btc_dist_sma20_pct=6.4,
            btc_dist_sma50_pct=4.8,
            btc_dist_sma200_pct=-1.5,  # Just below 200D SMA -> Early Bull Ignition (not yet Confirmed Bull Trend)
            btc_donchian_20d_pct=96.0,
            btc_higher_low_structure=True,
            spot_volume_20d_ratio=2.15,
            eth_btc_14d_change_pct=2.1,
        ),
        spot_flows=SpotFlowMetrics(
            coinbase_premium_bps=9.5,
            usdt_peg_deviation_bps=3.2,
        ),
        leverage=LeverageMetrics(
            btc=PerpAssetMetrics(symbol="BTC", funding_rate_8h_pct=0.0015, perp_basis_bps=-1.5, source="hyperliquid"),
            options=OptionsMetrics(btc_dvol_index=48.0, btc_put_call_oi_ratio=0.52),
        ),
        liquidity=LiquidityMetrics(
            fiat_stablecoin_7d_change_pct=0.85,
            fiat_stablecoin_7d_accel_pct=0.55,
            fed_net_liquidity_4w_change_pct=0.65,
        ),
        breadth=BreadthMetrics(majors_positive_7d_pct=75.0),
        sentiment=SentimentMetrics(fear_greed_value=34, fear_greed_label="Fear", fear_greed_7d_delta=14.0),
    )
    telemetry = TelegramTelemetrySummary(total_posts=12, overall_views_apathy_ratio=0.62, overall_promo_spam_ratio=0.1)

    card = compute_bull_ignition_scorecard(snap, telemetry)
    assert card.disbelief_rally_flag is True
    assert card.bull_trap_risk_flag is False
    assert card.total_score >= 75
    assert card.quant_suggested_phase == "Early Bull Ignition"
