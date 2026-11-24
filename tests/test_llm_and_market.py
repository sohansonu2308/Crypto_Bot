"""Tests for OpenAI-compatible LLMClient and v2 institutional MarketDataFetcher."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from openai import BadRequestError, RateLimitError
from pydantic import BaseModel

from src.config import LLMConfig, MarketDataConfig
from src.llm_client import LLMClient, extract_json_payload
from src.market_data import MarketDataFetcher


class SampleSchema(BaseModel):
    verdict: str
    score: int


def _fake_completion(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def test_extract_json_payload_strips_markdown_fences_and_prose():
    raw = 'Here is the result:\n```json\n{"verdict": "Early Bull Ignition", "score": 78}\n```\nHope this helps!'
    assert extract_json_payload(raw) == {"verdict": "Early Bull Ignition", "score": 78}


def test_llm_client_json_schema_fallback_and_429_retry(monkeypatch):
    calls: list[dict] = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            req = httpx.Request("POST", "https://custom.provider/v1/chat/completions")
            resp = httpx.Response(400, request=req, json={"error": "json_schema unsupported"})
            raise BadRequestError("json_schema unsupported", response=resp, body=None)
        if len(calls) == 2:
            req = httpx.Request("POST", "https://custom.provider/v1/chat/completions")
            resp = httpx.Response(429, request=req, json={"error": "rate limited"})
            raise RateLimitError("rate limited", response=resp, body=None)
        return _fake_completion('```json\n{"verdict": "Stealth Accumulation", "score": 65}\n```')

    fake_openai = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    monkeypatch.setattr("src.llm_client.time.sleep", lambda _: None)

    cfg = LLMConfig(
        base_url="https://custom.provider/v1",
        api_key="test-key",
        fast_model="gpt-6-mini",
        reasoning_model="gpt-6",
        max_retries=3,
    )
    client = LLMClient(cfg, client=fake_openai)
    result = client.complete_structured(
        model="gpt-6",
        system_prompt="Analyze.",
        user_prompt="Data.",
        response_model=SampleSchema,
    )

    assert result == SampleSchema(verdict="Stealth Accumulation", score=65)
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]
    assert "response_format" not in calls[2]


def test_v2_institutional_market_data_fetcher():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "coingecko.com/api/v3/global" in url:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "total_market_cap": {"usd": 3_200_000_000_000},
                        "market_cap_change_percentage_24h_usd": 2.4,
                        "market_cap_percentage": {"btc": 57.5, "eth": 13.2},
                    }
                },
            )
        if "coingecko.com/api/v3/coins/markets" in url and request.url.params.get("ids") == "bitcoin,ethereum":
            # Force Hyperliquid fallback for BTC & ETH spot
            return httpx.Response(429, json={"error": "rate limit"})
        if "coingecko.com/api/v3/coins/markets" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "solana",
                        "current_price": 210.5,
                        "price_change_percentage_24h_in_currency": 6.2,
                        "price_change_percentage_7d_in_currency": 14.1,
                        "total_volume": 5_500_000_000,
                        "market_cap": 98_000_000_000,
                    },
                    {
                        "id": "chainlink",
                        "current_price": 18.0,
                        "price_change_percentage_24h_in_currency": 3.0,
                        "price_change_percentage_7d_in_currency": 8.0,
                        "total_volume": 800_000_000,
                        "market_cap": 11_000_000_000,
                    },
                ],
            )
        if "hyperliquid.xyz/info" in url:
            return httpx.Response(
                200,
                json=[
                    {"universe": [{"name": "BTC"}, {"name": "ETH"}, {"name": "HYPE"}]},
                    [
                        {
                            "funding": "0.0000125",
                            "openInterest": "38000.0",
                            "prevDayPx": "96000.0",
                            "dayNtlVlm": "4500000000.0",
                            "premium": "-0.00025",
                            "oraclePx": "98500.0",
                            "markPx": "98475.0",
                        },
                        {
                            "funding": "0.0000100",
                            "openInterest": "450000.0",
                            "prevDayPx": "3350.0",
                            "dayNtlVlm": "2100000000.0",
                            "premium": "0.00010",
                            "oraclePx": "3450.0",
                            "markPx": "3450.3",
                        },
                        {
                            "funding": "0.0000200",
                            "openInterest": "1000000.0",
                            "prevDayPx": "25.0",
                            "dayNtlVlm": "300000000.0",
                            "premium": "0.0",
                            "oraclePx": "28.0",
                            "markPx": "28.0",
                        },
                    ],
                ],
            )
        if "kraken.com/0/public/OHLC" in url:
            pair = request.url.params.get("pair")
            base_px = 80000.0 if pair == "XBTUSD" else 2800.0
            rows = []
            for i in range(220):
                px = base_px + i * (100.0 if pair == "XBTUSD" else 3.5)
                rows.append([1700000000 + i * 86400, str(px - 50), str(px + 100), str(px - 100), str(px), str(px), "5000.0", 1000])
            return httpx.Response(200, json={"error": [], "result": {pair: rows, "last": 1}})
        if "coinbase.com/products/BTC-USD/candles" in url:
            rows = [[1700000000 + i * 86400, 80000 + i * 100 - 100, 80000 + i * 100 + 100, 80000 + i * 100, 80000 + i * 100, 6000.0] for i in range(220)]
            return httpx.Response(200, json=rows)
        if "coinbase.com/products/BTC-USD/ticker" in url:
            return httpx.Response(200, json={"price": "98620.0"})
        if "kraken.com/0/public/Ticker" in url:
            return httpx.Response(
                200,
                json={"error": [], "result": {"USDTZUSD": {"c": ["1.00040"]}, "XBTUSDT": {"c": ["98500.0"]}}},
            )
        if "deribit.com/api/v2/public/ticker" in url and "BTC_USDT" in url:
            return httpx.Response(200, json={"result": {"last_price": 98500.0}})
        if "deribit.com/api/v2/public/get_volatility_index_data" in url:
            return httpx.Response(200, json={"result": {"data": [[1790000000000, 45.0, 48.0, 44.0, 46.5]]}})
        if "deribit.com/api/v2/public/get_book_summary_by_currency" in url:
            return httpx.Response(
                200,
                json={
                    "result": [
                        {"instrument_name": "BTC-28DEC26-100000-C", "open_interest": 2000.0, "volume": 300.0},
                        {"instrument_name": "BTC-28DEC26-80000-P", "open_interest": 1100.0, "volume": 150.0},
                    ]
                },
            )
        if "stablecoins.llama.fi" in url:
            sc_id = request.url.params.get("stablecoin")
            base = 120_000_000_000 if sc_id == "1" else 50_000_000_000
            rows = [
                {"date": str(1700000000 + i * 86400), "totalCirculatingUSD": {"peggedUSD": base + (i * i * 20_000_000)}}
                for i in range(40)
            ]
            return httpx.Response(200, json=rows)
        if "historicalChainTvl" in url:
            rows = [{"tvl": 100_000_000_000 + i * 1_000_000_000} for i in range(15)]
            return httpx.Response(200, json=rows)
        if "fredgraph.csv" in url:
            series_id = request.url.params.get("id")
            if series_id == "WALCL":
                lines = ["DATE,WALCL"] + [f"2026-10-{i+1:02d},{6800000 + i * 25000}" for i in range(10)]
            elif series_id == "WTREGEN":
                lines = ["DATE,WTREGEN"] + [f"2026-10-{i+1:02d},750000" for i in range(10)]
            else:
                lines = ["DATE,RRPONTSYD"] + [f"2026-10-{i+1:02d},120.0" for i in range(25)]
            return httpx.Response(200, text="\n".join(lines))
        if "alternative.me/fng" in url:
            data = [{"value": "34", "value_classification": "Fear"}] + [
                {"value": "22", "value_classification": "Extreme Fear"} for _ in range(7)
            ]
            return httpx.Response(200, json={"data": data})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = MarketDataFetcher(MarketDataConfig(max_retries=1), http_client=client, sleep_fn=lambda _: None)
    snap = fetcher.fetch_snapshot(altcoin_tickers=["$SOL", "UNKNOWN_HL", "HYPE"])

    assert snap.macro.btc.source == "hyperliquid"
    assert snap.macro.btc.price_usd == 98475.0
    assert snap.structure.btc_sma200 is not None
    assert snap.structure.btc_dist_sma50_pct is not None and snap.structure.btc_dist_sma50_pct > 0
    assert snap.spot_flows.coinbase_premium_bps == pytest.approx(8.18, abs=0.1)
    assert snap.spot_flows.usdt_peg_deviation_bps == pytest.approx(4.0, abs=0.1)
    assert snap.leverage.btc.source == "hyperliquid"
    assert snap.leverage.btc.funding_rate_8h_pct == pytest.approx(0.01)
    assert snap.leverage.btc.perp_basis_bps == pytest.approx(-2.5)
    assert snap.leverage.options.btc_dvol_index == 46.5
    assert snap.leverage.options.btc_put_call_oi_ratio == pytest.approx(0.55)
    assert snap.liquidity.fiat_stablecoin_7d_change_pct is not None and snap.liquidity.fiat_stablecoin_7d_change_pct > 0
    assert snap.liquidity.fiat_stablecoin_7d_accel_pct is not None and snap.liquidity.fiat_stablecoin_7d_accel_pct > 0
    assert snap.liquidity.fed_net_liquidity_4w_change_pct is not None and snap.liquidity.fed_net_liquidity_4w_change_pct > 0
    assert snap.sentiment.fear_greed_7d_delta == 12.0
