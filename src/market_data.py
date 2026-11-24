"""Objective market ground truth fetcher using free, unauthenticated APIs."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
from pydantic import BaseModel, Field

from src.config import MarketDataConfig

logger = logging.getLogger(__name__)

# Common ticker -> CoinGecko ID mapping to avoid unnecessary search calls
_COMMON_COINGECKO_IDS: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "BNB": "binancecoin",
    "XRP": "ripple",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "AVAX": "avalanche-2",
    "LINK": "chainlink",
    "DOT": "polkadot",
    "SUI": "sui",
    "TON": "the-open-network",
    "ARB": "arbitrum",
    "OP": "optimism",
    "NEAR": "near",
    "APT": "aptos",
    "UNI": "uniswap",
    "AAVE": "aave",
    "MKR": "maker",
    "INJ": "injective-protocol",
    "RENDER": "render-token",
    "RNDR": "render-token",
    "FET": "fetch-ai",
    "TAO": "bittensor",
    "ONDO": "ondo-finance",
    "PENDLE": "pendle",
    "PEPE": "pepe",
    "WIF": "dogwifcoin",
    "BONK": "bonk",
    "SHIB": "shiba-inu",
    "TIA": "celestia",
    "SEI": "sei-network",
    "HYPE": "hyperliquid",
    "ENA": "ethena",
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


class PerpAssetMetrics(BaseModel):
    symbol: str
    funding_rate_8h_pct: float | None = None
    mark_price: float | None = None
    open_interest_contracts: float | None = None
    open_interest_usd: float | None = None


class LeverageMetrics(BaseModel):
    btc: PerpAssetMetrics = Field(default_factory=lambda: PerpAssetMetrics(symbol="BTCUSDT"))
    eth: PerpAssetMetrics = Field(default_factory=lambda: PerpAssetMetrics(symbol="ETHUSDT"))


class LiquidityMetrics(BaseModel):
    total_stablecoin_mcap_usd: float | None = None
    stablecoin_7d_change_usd: float | None = None
    stablecoin_7d_change_pct: float | None = None
    defi_tvl_usd: float | None = None
    defi_tvl_7d_change_usd: float | None = None
    defi_tvl_7d_change_pct: float | None = None


class SentimentMetrics(BaseModel):
    fear_greed_value: int | None = None
    fear_greed_label: str | None = None
    fear_greed_7d_avg: float | None = None


class MarketSnapshot(BaseModel):
    fetched_at_utc: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    macro: MacroMetrics = Field(default_factory=MacroMetrics)
    leverage: LeverageMetrics = Field(default_factory=LeverageMetrics)
    liquidity: LiquidityMetrics = Field(default_factory=LiquidityMetrics)
    sentiment: SentimentMetrics = Field(default_factory=SentimentMetrics)
    altcoins: list[AssetMetrics] = Field(default_factory=list)
    source_status: dict[str, str] = Field(default_factory=dict)


class MarketDataFetcher:
    """Fetches macro, perp leverage, stablecoin/TVL liquidity, sentiment, and altcoin metrics."""

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
            headers={"Accept": "application/json", "User-Agent": "CryptoBotPipeline/1.0"},
        )
        self._sleep = sleep_fn

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

    def fetch_snapshot(self, altcoin_tickers: list[str] | None = None) -> MarketSnapshot:
        """Collect all market data categories with independent per-source fallbacks."""
        snapshot = MarketSnapshot()
        snapshot.macro, macro_status = self._fetch_macro()
        snapshot.source_status["macro"] = macro_status

        snapshot.leverage, lev_status = self._fetch_leverage()
        snapshot.source_status["leverage"] = lev_status

        snapshot.liquidity, liq_status = self._fetch_liquidity()
        snapshot.source_status["liquidity"] = liq_status

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

        # 1. CoinGecko global (Total Market Cap, BTC.D, ETH.D)
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

        # 2. CoinGecko BTC & ETH spot metrics (includes 7d change)
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
            status_parts.append("coingecko_spot:ok")
        else:
            # Fallback to Binance public spot API
            btc_bn = self._fetch_binance_spot_asset("BTC")
            eth_bn = self._fetch_binance_spot_asset("ETH")
            if btc_bn.price_usd is not None:
                macro.btc = btc_bn
            if eth_bn.price_usd is not None:
                macro.eth = eth_bn
            fallback_ok = btc_bn.price_usd is not None or eth_bn.price_usd is not None
            status_parts.append("binance_spot_fallback:ok" if fallback_ok else "spot:failed")

        return macro, ", ".join(status_parts)

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

    def _fetch_leverage(self) -> tuple[LeverageMetrics, str]:
        ep = self.config.endpoints
        lev = LeverageMetrics()
        ok_count = 0

        for attr, pair in (("btc", "BTCUSDT"), ("eth", "ETHUSDT")):
            prem = self._get_json(f"{ep.binance_futures_base}/premiumIndex", params={"symbol": pair})
            oi = self._get_json(f"{ep.binance_futures_base}/openInterest", params={"symbol": pair})

            mark_price = _to_float(prem.get("markPrice")) if isinstance(prem, dict) else None
            raw_funding = _to_float(prem.get("lastFundingRate")) if isinstance(prem, dict) else None
            funding_pct = round(raw_funding * 100.0, 5) if raw_funding is not None else None
            oi_contracts = _to_float(oi.get("openInterest")) if isinstance(oi, dict) else None
            oi_usd = (
                round(oi_contracts * mark_price, 2)
                if oi_contracts is not None and mark_price is not None
                else None
            )

            if funding_pct is not None or oi_contracts is not None:
                ok_count += 1

            setattr(
                lev,
                attr,
                PerpAssetMetrics(
                    symbol=pair,
                    funding_rate_8h_pct=funding_pct,
                    mark_price=mark_price,
                    open_interest_contracts=oi_contracts,
                    open_interest_usd=oi_usd,
                ),
            )

        status = "ok" if ok_count == 2 else ("partial" if ok_count == 1 else "failed")
        return lev, status

    def _fetch_liquidity(self) -> tuple[LiquidityMetrics, str]:
        ep = self.config.endpoints
        liq = LiquidityMetrics()
        parts: list[str] = []

        # DefiLlama Stablecoin Supply
        stables = self._get_json(ep.defillama_stablecoins)
        if isinstance(stables, list) and len(stables) >= 8:
            latest_val = _extract_stablecoin_usd(stables[-1])
            prev_val = _extract_stablecoin_usd(stables[-8])
            if latest_val is not None:
                liq.total_stablecoin_mcap_usd = round(latest_val, 2)
                if prev_val and prev_val > 0:
                    delta = latest_val - prev_val
                    liq.stablecoin_7d_change_usd = round(delta, 2)
                    liq.stablecoin_7d_change_pct = round((delta / prev_val) * 100.0, 3)
                parts.append("stablecoins:ok")
            else:
                parts.append("stablecoins:parse_error")
        else:
            parts.append("stablecoins:failed")

        # DefiLlama DeFi TVL
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
                parts.append("defi_tvl:parse_error")
        else:
            parts.append("defi_tvl:failed")

        return liq, ", ".join(parts)

    def _fetch_sentiment(self) -> tuple[SentimentMetrics, str]:
        ep = self.config.endpoints
        sent = SentimentMetrics()
        fng = self._get_json(ep.fear_and_greed, params={"limit": 7})
        if isinstance(fng, dict) and isinstance(fng.get("data"), list) and fng["data"]:
            entries = fng["data"]
            latest = entries[0]
            try:
                sent.fear_greed_value = int(latest.get("value"))
            except (TypeError, ValueError):
                sent.fear_greed_value = None
            sent.fear_greed_label = latest.get("value_classification")
            vals = []
            for row in entries:
                try:
                    vals.append(int(row.get("value")))
                except (TypeError, ValueError):
                    continue
            if vals:
                sent.fear_greed_7d_avg = round(sum(vals) / len(vals), 1)
            return sent, "ok"
        return sent, "failed"

    def fetch_altcoin_metrics(self, tickers: list[str], max_tickers: int = 10) -> list[AssetMetrics]:
        """Look up 24h/7d price & volume for extracted altcoin tickers via CoinGecko + Binance fallback."""
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

        # Resolve unknown tickers via CoinGecko /search (best-effort)
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

        # Fallback to Binance spot for any ticker missing from CoinGecko
        for sym in cleaned_tickers:
            if sym not in results_by_sym:
                bn_asset = self._fetch_binance_spot_asset(sym)
                results_by_sym[sym] = bn_asset

        return [results_by_sym[sym] for sym in cleaned_tickers]


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


def _extract_stablecoin_usd(entry: Any) -> float | None:
    if not isinstance(entry, dict):
        return None
    circ = entry.get("totalCirculatingUSD") or entry.get("totalCirculating") or {}
    if isinstance(circ, dict):
        return _to_float(circ.get("peggedUSD"))
    return None
