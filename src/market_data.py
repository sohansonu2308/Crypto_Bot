"""Institutional-grade objective market ground truth fetcher (geo-block-resilient)."""

from __future__ import annotations

import csv
import io
import logging
import statistics
import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
from pydantic import BaseModel, Field

from src.config import MarketDataConfig

logger = logging.getLogger(__name__)

# Fixed 12-asset Liquid Institutional Majors Basket (evaluated every run regardless of Telegram hype)
BENCHMARK_MAJORS_CG_IDS: dict[str, str] = {
    "SOL": "solana",
    "BNB": "binancecoin",
    "XRP": "ripple",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "AVAX": "avalanche-2",
    "LINK": "chainlink",
    "SUI": "sui",
    "TON": "the-open-network",
    "AAVE": "aave",
    "TAO": "bittensor",
    "ONDO": "ondo-finance",
}

_COMMON_COINGECKO_IDS: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    **BENCHMARK_MAJORS_CG_IDS,
    "DOT": "polkadot",
    "ARB": "arbitrum",
    "OP": "optimism",
    "NEAR": "near",
    "APT": "aptos",
    "UNI": "uniswap",
    "MKR": "maker",
    "INJ": "injective-protocol",
    "RENDER": "render-token",
    "RNDR": "render-token",
    "FET": "fetch-ai",
    "PENDLE": "pendle",
    "PEPE": "pepe",
    "WIF": "dogwifcoin",
    "BONK": "bonk",
    "SHIB": "shiba-inu",
    "TIA": "celestia",
    "SEI": "sei-network",
    "HYPE": "hyperliquid",
    "ENA": "ethena",
    "VIRTUAL": "virtual-protocol",
    "JUP": "jupiter-exchange-solana",
    "LTC": "litecoin",
    "BCH": "bitcoin-cash",
    "TRX": "tron",
    "ATOM": "cosmos",
    "FIL": "filecoin",
}


class AssetMetrics(BaseModel):
    symbol: str
    price_usd: float | None = None
    change_24h_pct: float | None = None
    change_7d_pct: float | None = None
    volume_24h_usd: float | None = None
    market_cap_usd: float | None = None
    source: str = "unavailable"


class MacroMetrics(BaseModel):
    btc: AssetMetrics = Field(default_factory=lambda: AssetMetrics(symbol="BTC"))
    eth: AssetMetrics = Field(default_factory=lambda: AssetMetrics(symbol="ETH"))
    total_market_cap_usd: float | None = None
    total_market_cap_change_24h_pct: float | None = None
    btc_dominance_pct: float | None = None
    eth_dominance_pct: float | None = None


class PriceStructureMetrics(BaseModel):
    btc_sma20: float | None = None
    btc_sma50: float | None = None
    btc_sma200: float | None = None
    btc_dist_sma20_pct: float | None = None
    btc_dist_sma50_pct: float | None = None
    btc_dist_sma200_pct: float | None = None
    btc_sma200_slope_14d_pct: float | None = None
    btc_donchian_20d_pct: float | None = None
    btc_higher_low_structure: bool | None = None
    spot_volume_24h_usd: float | None = None
    spot_volume_20d_avg_usd: float | None = None
    spot_volume_20d_ratio: float | None = None
    eth_btc_ratio: float | None = None
    eth_btc_14d_change_pct: float | None = None
    eth_btc_30d_change_pct: float | None = None


class SpotFlowMetrics(BaseModel):
    coinbase_btc_usd: float | None = None
    offshore_btc_usdt: float | None = None
    coinbase_premium_bps: float | None = None
    kraken_usdt_usd: float | None = None
    usdt_peg_deviation_bps: float | None = None


class PerpAssetMetrics(BaseModel):
    symbol: str
    funding_rate_8h_pct: float | None = None
    mark_price: float | None = None
    oracle_price: float | None = None
    perp_basis_bps: float | None = None
    open_interest_contracts: float | None = None
    open_interest_usd: float | None = None
    day_notional_volume_usd: float | None = None
    source: str = "unavailable"


class OptionsMetrics(BaseModel):
    btc_dvol_index: float | None = None
    btc_put_call_oi_ratio: float | None = None
    btc_put_call_vol_24h_ratio: float | None = None


class LeverageMetrics(BaseModel):
    btc: PerpAssetMetrics = Field(default_factory=lambda: PerpAssetMetrics(symbol="BTC"))
    eth: PerpAssetMetrics = Field(default_factory=lambda: PerpAssetMetrics(symbol="ETH"))
    options: OptionsMetrics = Field(default_factory=OptionsMetrics)


class LiquidityMetrics(BaseModel):
    # Pure fiat dry powder (USDT + USDC combined, excluding synthetic yield stables like USDe)
    fiat_stablecoin_mcap_usd: float | None = None
    fiat_stablecoin_7d_change_usd: float | None = None
    fiat_stablecoin_7d_change_pct: float | None = None
    fiat_stablecoin_30d_change_pct: float | None = None
    fiat_stablecoin_7d_accel_pct: float | None = None
    # Total all-stablecoin mcap (for reference)
    total_stablecoin_mcap_usd: float | None = None
    stablecoin_7d_change_usd: float | None = None
    stablecoin_7d_change_pct: float | None = None
    # On-chain DeFi TVL
    defi_tvl_usd: float | None = None
    defi_tvl_7d_change_usd: float | None = None
    defi_tvl_7d_change_pct: float | None = None
    # US Federal Reserve Net Dollar Liquidity (WALCL - TGA - RRP)
    fed_net_liquidity_usd_bn: float | None = None
    fed_net_liquidity_4w_change_usd_bn: float | None = None
    fed_net_liquidity_4w_change_pct: float | None = None


class BreadthMetrics(BaseModel):
    sample_size: int = 0
    majors_positive_24h_pct: float | None = None
    majors_positive_7d_pct: float | None = None
    majors_outperforming_btc_7d_pct: float | None = None
    median_7d_change_pct: float | None = None
    leaders_7d: list[str] = Field(default_factory=list)
    laggards_7d: list[str] = Field(default_factory=list)


class SentimentMetrics(BaseModel):
    fear_greed_value: int | None = None
    fear_greed_label: str | None = None
    fear_greed_7d_avg: float | None = None
    fear_greed_7d_delta: float | None = None


class MarketSnapshot(BaseModel):
    fetched_at_utc: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    macro: MacroMetrics = Field(default_factory=MacroMetrics)
    structure: PriceStructureMetrics = Field(default_factory=PriceStructureMetrics)
    spot_flows: SpotFlowMetrics = Field(default_factory=SpotFlowMetrics)
    leverage: LeverageMetrics = Field(default_factory=LeverageMetrics)
    liquidity: LiquidityMetrics = Field(default_factory=LiquidityMetrics)
    breadth: BreadthMetrics = Field(default_factory=BreadthMetrics)
    sentiment: SentimentMetrics = Field(default_factory=SentimentMetrics)
    altcoins: list[AssetMetrics] = Field(default_factory=list)
    source_status: dict[str, str] = Field(default_factory=dict)


class MarketDataFetcher:
    """Fetches institutional spot structure, fiat flows, perps/options, breadth, and sentiment."""

    def __init__(
        self,
        config: MarketDataConfig,
        http_client: httpx.Client | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._owns_client = http_client is None
        self.http_client = http_client or httpx.Client(
            timeout=config.timeout_seconds,
            follow_redirects=True,
            headers={"Accept": "application/json", "User-Agent": "CryptoBotWhale/2.0"},
        )
        self._sleep = sleep_fn
        self._hl_cache: dict[str, dict[str, Any]] | None = None

    def close(self) -> None:
        if self._owns_client:
            self.http_client.close()

    def __enter__(self) -> MarketDataFetcher:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _get_json(self, url: str, params: dict[str, Any] | None = None) -> Any | None:
        for attempt in range(self.config.max_retries):
            try:
                resp = self.http_client.get(url, params=params)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 429:
                    self._sleep(min(15.0, 3.0 * (attempt + 1)))
                    continue
                if resp.status_code in (500, 502, 503, 504):
                    self._sleep(1.5 * (attempt + 1))
                    continue
                logger.warning("HTTP %s from %s", resp.status_code, url)
                return None
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("Error fetching %s (attempt %d): %s", url, attempt + 1, exc)
                self._sleep(1.0 * (attempt + 1))
        return None

    def _post_json(self, url: str, payload: dict[str, Any]) -> Any | None:
        for attempt in range(self.config.max_retries):
            try:
                resp = self.http_client.post(url, json=payload)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (429, 500, 502, 503, 504):
                    self._sleep(1.5 * (attempt + 1))
                    continue
                logger.warning("HTTP %s POST %s", resp.status_code, url)
                return None
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("Error POST %s (attempt %d): %s", url, attempt + 1, exc)
                self._sleep(1.0 * (attempt + 1))
        return None

    def _get_text(self, url: str, params: dict[str, Any] | None = None) -> str | None:
        for attempt in range(self.config.max_retries):
            try:
                resp = self.http_client.get(url, params=params)
                if resp.status_code == 200:
                    return resp.text
                if resp.status_code in (429, 500, 502, 503, 504):
                    self._sleep(1.5 * (attempt + 1))
                    continue
                return None
            except httpx.HTTPError:
                self._sleep(1.0 * (attempt + 1))
        return None

    def _get_hyperliquid_universe(self) -> dict[str, dict[str, Any]]:
        """Fetch and cache Hyperliquid's full perp universe (150+ coins) in one single call."""
        if self._hl_cache is not None:
            return self._hl_cache
        raw = self._post_json(self.config.endpoints.hyperliquid_info, {"type": "metaAndAssetCtxs"})
        mapping: dict[str, dict[str, Any]] = {}
        if (
            isinstance(raw, list)
            and len(raw) >= 2
            and isinstance(raw[0], dict)
            and isinstance(raw[0].get("universe"), list)
            and isinstance(raw[1], list)
        ):
            universe = raw[0]["universe"]
            ctxs = raw[1]
            for meta, ctx in zip(universe, ctxs):
                if isinstance(meta, dict) and isinstance(ctx, dict) and meta.get("name"):
                    mapping[str(meta["name"]).upper()] = ctx
        self._hl_cache = mapping
        return mapping

    def fetch_snapshot(self, altcoin_tickers: list[str] | None = None) -> MarketSnapshot:
        """Collect all institutional market categories with independent per-source fallbacks."""
        self._hl_cache = None  # Refresh per snapshot run
        snapshot = MarketSnapshot()

        snapshot.macro, macro_status = self._fetch_macro()
        snapshot.source_status["macro"] = macro_status

        snapshot.structure, struct_status = self._fetch_price_structure()
        snapshot.source_status["structure"] = struct_status

        snapshot.spot_flows, flows_status = self._fetch_spot_flows()
        snapshot.source_status["spot_flows"] = flows_status

        snapshot.leverage, lev_status = self._fetch_leverage()
        snapshot.source_status["leverage"] = lev_status

        snapshot.liquidity, liq_status = self._fetch_liquidity()
        snapshot.source_status["liquidity"] = liq_status

        snapshot.breadth, breadth_status = self._fetch_benchmark_breadth(
            btc_7d_pct=snapshot.macro.btc.change_7d_pct
        )
        snapshot.source_status["breadth"] = breadth_status

        snapshot.sentiment, sent_status = self._fetch_sentiment()
        snapshot.source_status["sentiment"] = sent_status

        if altcoin_tickers:
            snapshot.altcoins = self.fetch_altcoin_metrics(altcoin_tickers)
            snapshot.source_status["altcoins"] = f"ok ({len(snapshot.altcoins)} tickers)"
        else:
            snapshot.source_status["altcoins"] = "skipped (no tickers)"

        return snapshot

    def _fetch_macro(self) -> tuple[MacroMetrics, str]:
        ep = self.config.endpoints
        macro = MacroMetrics()
        status_parts: list[str] = []

        cg_global = self._get_json(f"{ep.coingecko_base}/global")
        if isinstance(cg_global, dict) and isinstance(cg_global.get("data"), dict):
            gdata = cg_global["data"]
            macro.total_market_cap_usd = _to_float((gdata.get("total_market_cap") or {}).get("usd"))
            macro.total_market_cap_change_24h_pct = _to_float(gdata.get("market_cap_change_percentage_24h_usd"))
            dom = gdata.get("market_cap_percentage") or {}
            macro.btc_dominance_pct = _to_float(dom.get("btc"))
            macro.eth_dominance_pct = _to_float(dom.get("eth"))
            status_parts.append("coingecko_global:ok")
        else:
            status_parts.append("coingecko_global:failed")

        cg_markets = self._get_json(
            f"{ep.coingecko_base}/coins/markets",
            params={
                "vs_currency": "usd",
                "ids": "bitcoin,ethereum",
                "price_change_percentage": "24h,7d",
            },
        )
        if isinstance(cg_markets, list) and cg_markets:
            by_id = {item.get("id"): item for item in cg_markets if isinstance(item, dict)}
            if "bitcoin" in by_id:
                macro.btc = _parse_coingecko_market("BTC", by_id["bitcoin"])
            if "ethereum" in by_id:
                macro.eth = _parse_coingecko_market("ETH", by_id["ethereum"])

        if macro.btc.price_usd is not None and macro.eth.price_usd is not None:
            status_parts.append("coingecko_spot:ok")
        else:
            # Geo-block-safe fallback chain: Hyperliquid -> Binance Spot
            hl = self._get_hyperliquid_universe()
            if macro.btc.price_usd is None and "BTC" in hl:
                macro.btc = _parse_hl_spot_fallback("BTC", hl["BTC"])
            if macro.eth.price_usd is None and "ETH" in hl:
                macro.eth = _parse_hl_spot_fallback("ETH", hl["ETH"])

            if macro.btc.price_usd is not None and macro.eth.price_usd is not None:
                status_parts.append("hyperliquid_fallback:ok")
            else:
                if macro.btc.price_usd is None:
                    btc_bn = self._fetch_binance_spot_asset("BTC")
                    if btc_bn.price_usd is not None:
                        macro.btc = btc_bn
                if macro.eth.price_usd is None:
                    eth_bn = self._fetch_binance_spot_asset("ETH")
                    if eth_bn.price_usd is not None:
                        macro.eth = eth_bn
                fallback_ok = macro.btc.price_usd is not None or macro.eth.price_usd is not None
                status_parts.append("binance_spot_fallback:ok" if fallback_ok else "spot:failed")

        return macro, ", ".join(status_parts)

    def _fetch_price_structure(self) -> tuple[PriceStructureMetrics, str]:
        """Compute 20D/50D/200D SMAs, breakout percentile, Spot Volume Z-ratio, and ETH/BTC RS."""
        ep = self.config.endpoints
        struct = PriceStructureMetrics()
        parts: list[str] = []

        # Primary 200D+ daily OHLCV from Kraken (returns up to 720 daily candles, US-friendly)
        btc_candles = self._fetch_kraken_daily_ohlcv("XBTUSD")
        source_used = "kraken"
        if len(btc_candles) < 55:
            cb_candles = self._fetch_coinbase_daily_ohlcv("BTC-USD")
            if len(cb_candles) > len(btc_candles):
                btc_candles = cb_candles
                source_used = "coinbase"

        # Also fetch Coinbase daily candles to combine Coinbase + Kraken real USD spot volume!
        cb_btc_candles = self._fetch_coinbase_daily_ohlcv("BTC-USD") if source_used == "kraken" else btc_candles

        if len(btc_candles) >= 25:
            closes = [c["close"] for c in btc_candles]
            highs = [c["high"] for c in btc_candles]
            lows = [c["low"] for c in btc_candles]
            last_close = closes[-1]

            struct.btc_sma20 = round(statistics.mean(closes[-20:]), 2)
            struct.btc_dist_sma20_pct = round(((last_close / struct.btc_sma20) - 1.0) * 100.0, 2)

            if len(closes) >= 50:
                struct.btc_sma50 = round(statistics.mean(closes[-50:]), 2)
                struct.btc_dist_sma50_pct = round(((last_close / struct.btc_sma50) - 1.0) * 100.0, 2)

            if len(closes) >= 200:
                struct.btc_sma200 = round(statistics.mean(closes[-200:]), 2)
                struct.btc_dist_sma200_pct = round(((last_close / struct.btc_sma200) - 1.0) * 100.0, 2)
                if len(closes) >= 214:
                    prev_sma200 = statistics.mean(closes[-214:-14])
                    if prev_sma200 > 0:
                        struct.btc_sma200_slope_14d_pct = round(
                            ((struct.btc_sma200 / prev_sma200) - 1.0) * 100.0, 3
                        )

            # 20-day Donchian channel percentile (using prior 20 completed days + today)
            window_high = max(highs[-21:])
            window_low = min(lows[-21:])
            if window_high > window_low:
                struct.btc_donchian_20d_pct = round(
                    ((last_close - window_low) / (window_high - window_low)) * 100.0, 1
                )

            # Higher-low check: minimum low of last 10 days vs minimum low of days -20..-10
            if len(lows) >= 21:
                recent_low = min(lows[-10:])
                prior_low = min(lows[-20:-10])
                struct.btc_higher_low_structure = recent_low > prior_low

            # Spot USD volume ratio (use latest completed full candle [-2] + live [-1] max vs 20d avg)
            usd_vols = self._combine_daily_usd_volumes(btc_candles, cb_btc_candles)
            if len(usd_vols) >= 22:
                avg_20d = statistics.mean(usd_vols[-22:-2])
                # Use max of today's in-progress candle and yesterday's completed candle so 02:30 UTC runs aren't skewed by a 2.5-hour partial candle!
                active_vol = max(usd_vols[-1], usd_vols[-2])
                struct.spot_volume_24h_usd = round(active_vol, 2)
                struct.spot_volume_20d_avg_usd = round(avg_20d, 2)
                if avg_20d > 0:
                    struct.spot_volume_20d_ratio = round(active_vol / avg_20d, 2)

            parts.append(f"btc_ohlcv({source_used}):ok")
        else:
            parts.append("btc_ohlcv:failed")

        # ETH/BTC 14d & 30d relative strength
        eth_candles = self._fetch_kraken_daily_ohlcv("ETHUSD")
        if len(eth_candles) < 35:
            eth_candles = self._fetch_coinbase_daily_ohlcv("ETH-USD")

        if len(eth_candles) >= 32 and len(btc_candles) >= 32:
            eth_closes = [c["close"] for c in eth_candles]
            btc_closes = [c["close"] for c in btc_candles]
            if btc_closes[-1] > 0:
                curr_ratio = eth_closes[-1] / btc_closes[-1]
                struct.eth_btc_ratio = round(curr_ratio, 5)
                r14 = eth_closes[-15] / btc_closes[-15] if btc_closes[-15] > 0 else None
                r30 = eth_closes[-31] / btc_closes[-31] if btc_closes[-31] > 0 else None
                if r14 and r14 > 0:
                    struct.eth_btc_14d_change_pct = round(((curr_ratio / r14) - 1.0) * 100.0, 2)
                if r30 and r30 > 0:
                    struct.eth_btc_30d_change_pct = round(((curr_ratio / r30) - 1.0) * 100.0, 2)
                parts.append("eth_btc:ok")
        else:
            parts.append("eth_btc:failed")

        return struct, ", ".join(parts)

    def _fetch_kraken_daily_ohlcv(self, pair: str) -> list[dict[str, float]]:
        data = self._get_json(
            f"{self.config.endpoints.kraken_base}/OHLC",
            params={"pair": pair, "interval": 1440},
        )
        if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
            return []
        for key, rows in data["result"].items():
            if key == "last" or not isinstance(rows, list):
                continue
            candles: list[dict[str, float]] = []
            for r in rows:
                if isinstance(r, list) and len(r) >= 7:
                    ts, o, h, l, c, _vwap, vol = r[:7]
                    cf = _to_float(c)
                    vf = _to_float(vol)
                    if cf is not None and vf is not None:
                        candles.append(
                            {
                                "ts": float(ts),
                                "open": float(o),
                                "high": float(h),
                                "low": float(l),
                                "close": cf,
                                "usd_vol": cf * vf,
                            }
                        )
            candles.sort(key=lambda x: x["ts"])
            return candles
        return []

    def _fetch_coinbase_daily_ohlcv(self, product_id: str) -> list[dict[str, float]]:
        rows = self._get_json(
            f"{self.config.endpoints.coinbase_base}/products/{product_id}/candles",
            params={"granularity": 86400},
        )
        if not isinstance(rows, list):
            return []
        candles: list[dict[str, float]] = []
        for r in rows:
            # Coinbase schema: [time, low, high, open, close, volume]
            if isinstance(r, list) and len(r) >= 6:
                ts, low, high, open_p, close_p, vol = r[:6]
                cf = _to_float(close_p)
                vf = _to_float(vol)
                if cf is not None and vf is not None:
                    candles.append(
                        {
                            "ts": float(ts),
                            "open": float(open_p),
                            "high": float(high),
                            "low": float(low),
                            "close": cf,
                            "usd_vol": cf * vf,
                        }
                    )
        candles.sort(key=lambda x: x["ts"])
        return candles

    @staticmethod
    def _combine_daily_usd_volumes(
        primary: list[dict[str, float]],
        secondary: list[dict[str, float]],
    ) -> list[float]:
        if not secondary or secondary is primary:
            return [c["usd_vol"] for c in primary]
        sec_by_day = {int(c["ts"] // 86400): c["usd_vol"] for c in secondary}
        combined: list[float] = []
        for c in primary:
            day_key = int(c["ts"] // 86400)
            combined.append(c["usd_vol"] + sec_by_day.get(day_key, 0.0))
        return combined

    def _fetch_spot_flows(self) -> tuple[SpotFlowMetrics, str]:
        """Compute Coinbase BTC-USD Spot Premium (bps) and Kraken USDT/USD Peg Deviation (bps)."""
        ep = self.config.endpoints
        flows = SpotFlowMetrics()
        parts: list[str] = []

        cb = self._get_json(f"{ep.coinbase_base}/products/BTC-USD/ticker")
        if isinstance(cb, dict):
            cb_bid = _to_float(cb.get("bid"))
            cb_ask = _to_float(cb.get("ask"))
            if cb_bid and cb_ask:
                flows.coinbase_btc_usd = round((cb_bid + cb_ask) / 2.0, 2)
            else:
                flows.coinbase_btc_usd = _to_float(cb.get("price"))

        kr = self._get_json(f"{ep.kraken_base}/Ticker", params={"pair": "USDTZUSD,XBTUSDT"})
        kr_btc_usdt: float | None = None
        if isinstance(kr, dict) and isinstance(kr.get("result"), dict):
            res = kr["result"]
            usdt_obj = res.get("USDTZUSD") or {}
            usdt_px = _kraken_mid_or_last(usdt_obj)
            if usdt_px is not None:
                flows.kraken_usdt_usd = round(usdt_px, 5)
                flows.usdt_peg_deviation_bps = round((usdt_px - 1.0) * 10_000.0, 2)
                parts.append("usdt_peg:ok")
            xbt_usdt = res.get("XBTUSDT") or {}
            kr_btc_usdt = _kraken_mid_or_last(xbt_usdt)

        # Use simultaneous Kraken XBTUSDT order-book midprice first (never stale); fallback to Deribit BTC_USDT
        flows.offshore_btc_usdt = kr_btc_usdt
        if flows.offshore_btc_usdt is None:
            db_usdt = self._get_json(f"{ep.deribit_base}/ticker", params={"instrument_name": "BTC_USDT"})
            if isinstance(db_usdt, dict) and isinstance(db_usdt.get("result"), dict):
                flows.offshore_btc_usdt = _to_float(db_usdt["result"].get("last_price")) or _to_float(
                    db_usdt["result"].get("index_price")
                )

        if flows.coinbase_btc_usd and flows.offshore_btc_usdt and flows.offshore_btc_usdt > 0:
            fx_rate = flows.kraken_usdt_usd if flows.kraken_usdt_usd and flows.kraken_usdt_usd > 0 else 1.0
            offshore_usd_equiv = flows.offshore_btc_usdt * fx_rate
            flows.coinbase_premium_bps = round(
                ((flows.coinbase_btc_usd - offshore_usd_equiv) / offshore_usd_equiv) * 10_000.0,
                2,
            )
            parts.append("coinbase_premium:ok")
        else:
            parts.append("coinbase_premium:failed")

        return flows, ", ".join(parts)

    def _fetch_leverage(self) -> tuple[LeverageMetrics, str]:
        """Fetch geo-block-free perps & options via Hyperliquid + Deribit (Binance fallback)."""
        ep = self.config.endpoints
        lev = LeverageMetrics()
        parts: list[str] = []

        # 1. Primary Perp Positioning: Hyperliquid (works globally including US GitHub runners)
        hl = self._get_hyperliquid_universe()
        hl_ok = 0
        for attr, sym in (("btc", "BTC"), ("eth", "ETH")):
            ctx = hl.get(sym)
            if ctx:
                hourly_funding = _to_float(ctx.get("funding"))
                funding_8h_pct = round(hourly_funding * 8.0 * 100.0, 5) if hourly_funding is not None else None
                mark_px = _to_float(ctx.get("markPx"))
                oracle_px = _to_float(ctx.get("oraclePx"))
                premium = _to_float(ctx.get("premium"))
                basis_bps = round(premium * 10_000.0, 2) if premium is not None else None
                oi_coins = _to_float(ctx.get("openInterest"))
                oi_usd = round(oi_coins * mark_px, 2) if oi_coins is not None and mark_px is not None else None
                day_vol = _to_float(ctx.get("dayNtlVlm"))

                setattr(
                    lev,
                    attr,
                    PerpAssetMetrics(
                        symbol=sym,
                        funding_rate_8h_pct=funding_8h_pct,
                        mark_price=mark_px,
                        oracle_price=oracle_px,
                        perp_basis_bps=basis_bps,
                        open_interest_contracts=oi_coins,
                        open_interest_usd=oi_usd,
                        day_notional_volume_usd=round(day_vol, 2) if day_vol is not None else None,
                        source="hyperliquid",
                    ),
                )
                hl_ok += 1

        if hl_ok == 2:
            parts.append("hyperliquid_perps:ok")
        else:
            # Fallback 1: Deribit Perpetuals
            db_ok = 0
            for attr, inst in (("btc", "BTC-PERPETUAL"), ("eth", "ETH-PERPETUAL")):
                if getattr(lev, attr).funding_rate_8h_pct is not None:
                    continue
                tkr = self._get_json(f"{ep.deribit_base}/ticker", params={"instrument_name": inst})
                if isinstance(tkr, dict) and isinstance(tkr.get("result"), dict):
                    res = tkr["result"]
                    f8h = _to_float(res.get("funding_8h"))
                    mark = _to_float(res.get("mark_price"))
                    idx = _to_float(res.get("index_price"))
                    oi_usd = _to_float(res.get("open_interest"))
                    basis = ((mark - idx) / idx * 10_000.0) if mark and idx and idx > 0 else None
                    setattr(
                        lev,
                        attr,
                        PerpAssetMetrics(
                            symbol=inst,
                            funding_rate_8h_pct=round(f8h * 100.0, 5) if f8h is not None else None,
                            mark_price=mark,
                            oracle_price=idx,
                            perp_basis_bps=round(basis, 2) if basis is not None else None,
                            open_interest_usd=oi_usd,
                            source="deribit",
                        ),
                    )
                    db_ok += 1
            if db_ok > 0:
                parts.append("deribit_perps_fallback:ok")
            else:
                # Last-resort fallback: Binance Futures
                bn_ok = self._populate_binance_futures_fallback(lev)
                parts.append("binance_perps_fallback:ok" if bn_ok else "perps:failed")

        # 2. Institutional Options Skew & Implied Volatility (Deribit DVOL + Put/Call OI)
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - 5 * 86_400 * 1000
        dvol_resp = self._get_json(
            f"{ep.deribit_base}/get_volatility_index_data",
            params={
                "currency": "BTC",
                "resolution": "86400",
                "start_timestamp": start_ms,
                "end_timestamp": now_ms,
            },
        )
        if (
            isinstance(dvol_resp, dict)
            and isinstance(dvol_resp.get("result"), dict)
            and isinstance(dvol_resp["result"].get("data"), list)
            and dvol_resp["result"]["data"]
        ):
            latest_row = dvol_resp["result"]["data"][-1]
            if isinstance(latest_row, list) and len(latest_row) >= 5:
                lev.options.btc_dvol_index = _to_float(latest_row[4])

        opt_book = self._get_json(
            f"{ep.deribit_base}/get_book_summary_by_currency",
            params={"currency": "BTC", "kind": "option"},
        )
        if isinstance(opt_book, dict) and isinstance(opt_book.get("result"), list) and opt_book["result"]:
            put_oi = 0.0
            call_oi = 0.0
            put_vol = 0.0
            call_vol = 0.0
            for item in opt_book["result"]:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("instrument_name", ""))
                oi = _to_float(item.get("open_interest")) or 0.0
                vol = _to_float(item.get("volume")) or 0.0
                if name.endswith("-P"):
                    put_oi += oi
                    put_vol += vol
                elif name.endswith("-C"):
                    call_oi += oi
                    call_vol += vol
            if call_oi > 0:
                lev.options.btc_put_call_oi_ratio = round(put_oi / call_oi, 3)
            if call_vol > 0:
                lev.options.btc_put_call_vol_24h_ratio = round(put_vol / call_vol, 3)
            parts.append("deribit_options:ok")
        else:
            parts.append("deribit_options:failed")

        return lev, ", ".join(parts)

    def _populate_binance_futures_fallback(self, lev: LeverageMetrics) -> bool:
        ep = self.config.endpoints
        ok = False
        for attr, pair in (("btc", "BTCUSDT"), ("eth", "ETHUSDT")):
            if getattr(lev, attr).funding_rate_8h_pct is not None:
                continue
            prem = self._get_json(f"{ep.binance_futures_base}/premiumIndex", params={"symbol": pair})
            oi = self._get_json(f"{ep.binance_futures_base}/openInterest", params={"symbol": pair})
            mark_price = _to_float(prem.get("markPrice")) if isinstance(prem, dict) else None
            idx_price = _to_float(prem.get("indexPrice")) if isinstance(prem, dict) else None
            raw_funding = _to_float(prem.get("lastFundingRate")) if isinstance(prem, dict) else None
            funding_pct = round(raw_funding * 100.0, 5) if raw_funding is not None else None
            oi_contracts = _to_float(oi.get("openInterest")) if isinstance(oi, dict) else None
            oi_usd = (
                round(oi_contracts * mark_price, 2)
                if oi_contracts is not None and mark_price is not None
                else None
            )
            basis = ((mark_price - idx_price) / idx_price * 10_000.0) if mark_price and idx_price else None
            if funding_pct is not None or oi_contracts is not None:
                ok = True
                setattr(
                    lev,
                    attr,
                    PerpAssetMetrics(
                        symbol=pair,
                        funding_rate_8h_pct=funding_pct,
                        mark_price=mark_price,
                        oracle_price=idx_price,
                        perp_basis_bps=round(basis, 2) if basis is not None else None,
                        open_interest_contracts=oi_contracts,
                        open_interest_usd=oi_usd,
                        source="binance_futures",
                    ),
                )
        return ok

    def _fetch_binance_spot_asset(self, symbol: str) -> AssetMetrics:
        ep = self.config.endpoints
        pair = f"{symbol.upper()}USDT"
        data = self._get_json(f"{ep.binance_spot_base}/ticker/24hr", params={"symbol": pair})
        if not isinstance(data, dict) or "lastPrice" not in data:
            return AssetMetrics(symbol=symbol.upper(), source="unavailable")
        return AssetMetrics(
            symbol=symbol.upper(),
            price_usd=_to_float(data.get("lastPrice")),
            change_24h_pct=_to_float(data.get("priceChangePercent")),
            change_7d_pct=None,
            volume_24h_usd=_to_float(data.get("quoteVolume")),
            source="binance_spot",
        )

    def _fetch_liquidity(self) -> tuple[LiquidityMetrics, str]:
        ep = self.config.endpoints
        liq = LiquidityMetrics()
        parts: list[str] = []

        # 1. Pure Fiat Dry Powder: USDT (id=1) + USDC (id=2)
        usdt_series = self._get_json(ep.defillama_stablecoins, params={"stablecoin": 1})
        usdc_series = self._get_json(ep.defillama_stablecoins, params={"stablecoin": 2})
        fiat_series = _sum_stablecoin_series(usdt_series, usdc_series)
        if len(fiat_series) >= 31:
            curr = fiat_series[-1]
            d7 = fiat_series[-8]
            d14 = fiat_series[-15]
            d30 = fiat_series[-31]
            liq.fiat_stablecoin_mcap_usd = round(curr, 2)
            if d7 > 0:
                chg7 = ((curr - d7) / d7) * 100.0
                liq.fiat_stablecoin_7d_change_usd = round(curr - d7, 2)
                liq.fiat_stablecoin_7d_change_pct = round(chg7, 3)
                if d14 > 0:
                    prev_chg7 = ((d7 - d14) / d14) * 100.0
                    liq.fiat_stablecoin_7d_accel_pct = round(chg7 - prev_chg7, 3)
            if d30 > 0:
                liq.fiat_stablecoin_30d_change_pct = round(((curr - d30) / d30) * 100.0, 3)
            parts.append("fiat_stables:ok")
        else:
            parts.append("fiat_stables:failed")

        # 2. All-stablecoin total (context)
        stables_all = self._get_json(ep.defillama_stablecoins)
        if isinstance(stables_all, list) and len(stables_all) >= 8:
            latest_val = _extract_stablecoin_usd(stables_all[-1])
            prev_val = _extract_stablecoin_usd(stables_all[-8])
            if latest_val is not None:
                liq.total_stablecoin_mcap_usd = round(latest_val, 2)
                if prev_val and prev_val > 0:
                    delta = latest_val - prev_val
                    liq.stablecoin_7d_change_usd = round(delta, 2)
                    liq.stablecoin_7d_change_pct = round((delta / prev_val) * 100.0, 3)
                parts.append("all_stables:ok")
        else:
            parts.append("all_stables:failed")

        # 3. DefiLlama DeFi TVL
        tvl_hist = self._get_json(ep.defillama_tvl)
        if isinstance(tvl_hist, list) and len(tvl_hist) >= 8:
            latest_tvl = _to_float(tvl_hist[-1].get("tvl")) if isinstance(tvl_hist[-1], dict) else None
            prev_tvl = _to_float(tvl_hist[-8].get("tvl")) if isinstance(tvl_hist[-8], dict) else None
            if latest_tvl is not None:
                liq.defi_tvl_usd = round(latest_tvl, 2)
                if prev_tvl and prev_tvl > 0:
                    delta = latest_tvl - prev_tvl
                    liq.defi_tvl_7d_change_usd = round(delta, 2)
                    liq.defi_tvl_7d_change_pct = round((delta / prev_tvl) * 100.0, 3)
                parts.append("defi_tvl:ok")
        else:
            parts.append("defi_tvl:failed")

        # 4. US Federal Reserve Net Dollar Liquidity (WALCL - TGA - RRP) via free FRED CSVs
        fed_ok = self._populate_fed_net_liquidity(liq)
        parts.append("fed_liquidity:ok" if fed_ok else "fed_liquidity:failed")

        return liq, ", ".join(parts)

    def _populate_fed_net_liquidity(self, liq: LiquidityMetrics) -> bool:
        base = self.config.endpoints.fred_csv_base
        walcl = _parse_fred_csv(self._get_text(base, params={"id": "WALCL"}))  # Millions USD (weekly)
        tga = _parse_fred_csv(self._get_text(base, params={"id": "WTREGEN"}))  # Millions USD (weekly)
        rrp = _parse_fred_csv(self._get_text(base, params={"id": "RRPONTSYD"}))  # Billions USD (daily)

        if len(walcl) < 5 or len(tga) < 5 or len(rrp) < 5:
            return False

        # Compute latest vs 4-weeks-ago (~5 weekly observations back)
        latest_net_bn = (walcl[-1][1] / 1000.0) - (tga[-1][1] / 1000.0) - rrp[-1][1]
        prev_rrp = rrp[-21][1] if len(rrp) >= 21 else rrp[0][1]
        prev_net_bn = (walcl[-5][1] / 1000.0) - (tga[-5][1] / 1000.0) - prev_rrp

        liq.fed_net_liquidity_usd_bn = round(latest_net_bn, 2)
        if prev_net_bn > 0:
            delta_bn = latest_net_bn - prev_net_bn
            liq.fed_net_liquidity_4w_change_usd_bn = round(delta_bn, 2)
            liq.fed_net_liquidity_4w_change_pct = round((delta_bn / prev_net_bn) * 100.0, 3)
        return True

    def _fetch_benchmark_breadth(self, btc_7d_pct: float | None) -> tuple[BreadthMetrics, str]:
        """Evaluate the fixed 12-asset Liquid Institutional Majors Basket."""
        ep = self.config.endpoints
        breadth = BreadthMetrics()
        ids_param = ",".join(BENCHMARK_MAJORS_CG_IDS.values())
        cg_data = self._get_json(
            f"{ep.coingecko_base}/coins/markets",
            params={
                "vs_currency": "usd",
                "ids": ids_param,
                "price_change_percentage": "24h,7d",
            },
        )
        inv_map = {v: k for k, v in BENCHMARK_MAJORS_CG_IDS.items()}
        items: list[tuple[str, float | None, float | None]] = []

        if isinstance(cg_data, list) and cg_data:
            for row in cg_data:
                if not isinstance(row, dict):
                    continue
                sym = inv_map.get(str(row.get("id", "")))
                if not sym:
                    continue
                c24 = _to_float(
                    row.get("price_change_percentage_24h_in_currency", row.get("price_change_percentage_24h"))
                )
                c7 = _to_float(row.get("price_change_percentage_7d_in_currency"))
                items.append((sym, c24, c7))

        if not items:
            # Fallback to Hyperliquid 24h changes if CoinGecko rate-limited
            hl = self._get_hyperliquid_universe()
            for sym in BENCHMARK_MAJORS_CG_IDS:
                if sym in hl:
                    m = _parse_hl_spot_fallback(sym, hl[sym])
                    items.append((sym, m.change_24h_pct, None))
            if not items:
                return breadth, "failed"

        breadth.sample_size = len(items)
        vals_24 = [c24 for _, c24, _ in items if c24 is not None]
        vals_7d = [(sym, c7) for sym, _, c7 in items if c7 is not None]

        if vals_24:
            breadth.majors_positive_24h_pct = round(
                (sum(1 for v in vals_24 if v > 0) / len(vals_24)) * 100.0, 1
            )
        if vals_7d:
            only_7d = [v for _, v in vals_7d]
            breadth.majors_positive_7d_pct = round(
                (sum(1 for v in only_7d if v > 0) / len(only_7d)) * 100.0, 1
            )
            breadth.median_7d_change_pct = round(statistics.median(only_7d), 2)
            if btc_7d_pct is not None:
                breadth.majors_outperforming_btc_7d_pct = round(
                    (sum(1 for v in only_7d if v > btc_7d_pct) / len(only_7d)) * 100.0, 1
                )
            sorted_7d = sorted(vals_7d, key=lambda x: x[1], reverse=True)
            breadth.leaders_7d = [f"{s} ({v:+.1f}%)" for s, v in sorted_7d[:3]]
            breadth.laggards_7d = [f"{s} ({v:+.1f}%)" for s, v in sorted_7d[-3:]]

        return breadth, f"ok ({breadth.sample_size} majors)"

    def _fetch_sentiment(self) -> tuple[SentimentMetrics, str]:
        ep = self.config.endpoints
        sent = SentimentMetrics()
        fng = self._get_json(ep.fear_and_greed, params={"limit": 8})
        if isinstance(fng, dict) and isinstance(fng.get("data"), list) and fng["data"]:
            entries = fng["data"]
            latest = entries[0]
            try:
                sent.fear_greed_value = int(latest.get("value"))
            except (TypeError, ValueError):
                sent.fear_greed_value = None
            sent.fear_greed_label = latest.get("value_classification")
            vals: list[int] = []
            for row in entries[:7]:
                try:
                    vals.append(int(row.get("value")))
                except (TypeError, ValueError):
                    continue
            if vals:
                sent.fear_greed_7d_avg = round(sum(vals) / len(vals), 1)
            if sent.fear_greed_value is not None and len(entries) >= 8:
                try:
                    val_7d_ago = int(entries[7].get("value"))
                    sent.fear_greed_7d_delta = float(sent.fear_greed_value - val_7d_ago)
                except (TypeError, ValueError):
                    pass
            return sent, "ok"
        return sent, "failed"

    def fetch_altcoin_metrics(self, tickers: list[str], max_tickers: int = 10) -> list[AssetMetrics]:
        """Look up 24h/7d price & volume for extracted altcoin tickers (CoinGecko -> Hyperliquid -> Binance)."""
        cleaned_tickers: list[str] = []
        seen: set[str] = set()
        for raw in tickers:
            sym = raw.strip().upper().lstrip("$")
            if not sym or sym in ("BTC", "ETH", "USDT", "USDC", "USD") or sym in seen:
                continue
            seen.add(sym)
            cleaned_tickers.append(sym)
            if len(cleaned_tickers) >= max_tickers:
                break

        if not cleaned_tickers:
            return []

        ep = self.config.endpoints
        cg_id_map: dict[str, str] = {}
        unknown_syms: list[str] = []

        for sym in cleaned_tickers:
            if sym in _COMMON_COINGECKO_IDS:
                cg_id_map[sym] = _COMMON_COINGECKO_IDS[sym]
            else:
                unknown_syms.append(sym)

        for sym in unknown_syms[:5]:
            search = self._get_json(f"{ep.coingecko_base}/search", params={"query": sym})
            if isinstance(search, dict) and isinstance(search.get("coins"), list):
                for coin in search["coins"]:
                    if isinstance(coin, dict) and str(coin.get("symbol", "")).upper() == sym:
                        if coin.get("id"):
                            cg_id_map[sym] = str(coin["id"])
                            break

        results_by_sym: dict[str, AssetMetrics] = {}

        if cg_id_map:
            ids_param = ",".join(dict.fromkeys(cg_id_map.values()))
            cg_data = self._get_json(
                f"{ep.coingecko_base}/coins/markets",
                params={
                    "vs_currency": "usd",
                    "ids": ids_param,
                    "price_change_percentage": "24h,7d",
                },
            )
            if isinstance(cg_data, list):
                by_id = {item.get("id"): item for item in cg_data if isinstance(item, dict)}
                for sym, cg_id in cg_id_map.items():
                    if cg_id in by_id:
                        results_by_sym[sym] = _parse_coingecko_market(sym, by_id[cg_id])

        # Fallback 1: Hyperliquid Perp Universe (US-friendly), Fallback 2: Binance Spot
        hl = self._get_hyperliquid_universe()
        for sym in cleaned_tickers:
            if sym not in results_by_sym:
                if sym in hl:
                    results_by_sym[sym] = _parse_hl_spot_fallback(sym, hl[sym])
                else:
                    results_by_sym[sym] = self._fetch_binance_spot_asset(sym)

        return [results_by_sym[sym] for sym in cleaned_tickers]


def _kraken_mid_or_last(pair_obj: Any) -> float | None:
    if not isinstance(pair_obj, dict):
        return None
    ask_arr = pair_obj.get("a")
    bid_arr = pair_obj.get("b")
    if isinstance(ask_arr, list) and ask_arr and isinstance(bid_arr, list) and bid_arr:
        ask = _to_float(ask_arr[0])
        bid = _to_float(bid_arr[0])
        if ask and bid and ask > 0 and bid > 0:
            return (ask + bid) / 2.0
    last_arr = pair_obj.get("c")
    if isinstance(last_arr, list) and last_arr:
        return _to_float(last_arr[0])
    return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_coingecko_market(symbol: str, item: dict[str, Any]) -> AssetMetrics:
    return AssetMetrics(
        symbol=symbol.upper(),
        price_usd=_to_float(item.get("current_price")),
        change_24h_pct=_to_float(
            item.get("price_change_percentage_24h_in_currency", item.get("price_change_percentage_24h"))
        ),
        change_7d_pct=_to_float(item.get("price_change_percentage_7d_in_currency")),
        volume_24h_usd=_to_float(item.get("total_volume")),
        market_cap_usd=_to_float(item.get("market_cap")),
        source="coingecko",
    )


def _parse_hl_spot_fallback(symbol: str, ctx: dict[str, Any]) -> AssetMetrics:
    mark = _to_float(ctx.get("markPx"))
    prev = _to_float(ctx.get("prevDayPx"))
    chg_24h = round(((mark - prev) / prev) * 100.0, 2) if mark and prev and prev > 0 else None
    return AssetMetrics(
        symbol=symbol.upper(),
        price_usd=mark,
        change_24h_pct=chg_24h,
        change_7d_pct=None,
        volume_24h_usd=_to_float(ctx.get("dayNtlVlm")),
        source="hyperliquid",
    )


def _extract_stablecoin_usd(entry: Any) -> float | None:
    if not isinstance(entry, dict):
        return None
    circ = entry.get("totalCirculatingUSD") or entry.get("totalCirculating") or {}
    if isinstance(circ, dict):
        return _to_float(circ.get("peggedUSD"))
    return None


def _sum_stablecoin_series(series_a: Any, series_b: Any) -> list[float]:
    if not isinstance(series_a, list) or not isinstance(series_b, list):
        return []
    by_date_a: dict[str, float] = {}
    for row in series_a:
        val = _extract_stablecoin_usd(row)
        dt = str(row.get("date", "")) if isinstance(row, dict) else ""
        if dt and val is not None:
            by_date_a[dt] = val

    combined: list[tuple[int, float]] = []
    for row in series_b:
        val_b = _extract_stablecoin_usd(row)
        dt = str(row.get("date", "")) if isinstance(row, dict) else ""
        if dt and val_b is not None and dt in by_date_a:
            try:
                combined.append((int(dt), by_date_a[dt] + val_b))
            except ValueError:
                continue
    combined.sort(key=lambda x: x[0])
    return [v for _, v in combined]


def _parse_fred_csv(csv_text: str | None) -> list[tuple[str, float]]:
    if not csv_text:
        return []
    rows: list[tuple[str, float]] = []
    reader = csv.reader(io.StringIO(csv_text))
    header_skipped = False
    for line in reader:
        if not header_skipped:
            header_skipped = True
            continue
        if len(line) < 2:
            continue
        val = _to_float(line[1])
        if val is not None:
            rows.append((line[0].strip(), val))
    return rows
