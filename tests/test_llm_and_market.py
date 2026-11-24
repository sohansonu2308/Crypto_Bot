"""Tests for OpenAI-compatible LLMClient, JSON extraction fallback, retries, and MarketDataFetcher."""

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
    raw = 'Here is the result:\n```json\n{"verdict": "Early Ignition", "score": 78}\n```\nHope this helps!'
    assert extract_json_payload(raw) == {"verdict": "Early Ignition", "score": 78}


def test_llm_client_json_schema_fallback_and_429_retry(monkeypatch):
    calls: list[dict] = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        # 1st call: simulate provider rejecting response_format=json_schema
        if len(calls) == 1:
            req = httpx.Request("POST", "https://custom.provider/v1/chat/completions")
            resp = httpx.Response(400, request=req, json={"error": "json_schema unsupported"})
            raise BadRequestError("json_schema unsupported", response=resp, body=None)
        # 2nd call: simulate 429 rate limit
        if len(calls) == 2:
            req = httpx.Request("POST", "https://custom.provider/v1/chat/completions")
            resp = httpx.Response(429, request=req, json={"error": "rate limited"})
            raise RateLimitError("rate limited", response=resp, body=None)
        # 3rd call: returns JSON wrapped in markdown fences
        return _fake_completion('```json\n{"verdict": "Accumulation", "score": 65}\n```')

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

    assert result == SampleSchema(verdict="Accumulation", score=65)
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]
    assert "response_format" not in calls[2]


def test_market_data_fetcher_with_binance_fallback():
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
        if "coingecko.com/api/v3/coins/markets" in url and "bitcoin" in url:
            # Force fallback to Binance Spot for BTC & ETH
            return httpx.Response(429, json={"error": "rate limit"})
        if "coingecko.com/api/v3/coins/markets" in url and "solana" in url:
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
                    }
                ],
            )
        if "api.binance.com/api/v3/ticker/24hr" in url:
            sym = request.url.params.get("symbol")
            price = "98500.0" if sym == "BTCUSDT" else "3450.0"
            return httpx.Response(
                200,
                json={"lastPrice": price, "priceChangePercent": "3.1", "quoteVolume": "2500000000"},
            )
        if "fapi.binance.com/fapi/v1/premiumIndex" in url:
            return httpx.Response(200, json={"markPrice": "98500.0", "lastFundingRate": "0.0001"})
        if "fapi.binance.com/fapi/v1/openInterest" in url:
            return httpx.Response(200, json={"openInterest": "85000.0"})
        if "stablecoins.llama.fi" in url:
            rows = [{"totalCirculatingUSD": {"peggedUSD": 180_000_000_000 + i * 500_000_000}} for i in range(10)]
            return httpx.Response(200, json=rows)
        if "historicalChainTvl" in url:
            rows = [{"tvl": 100_000_000_000 + i * 1_000_000_000} for i in range(10)]
            return httpx.Response(200, json=rows)
        if "alternative.me/fng" in url:
            return httpx.Response(
                200,
                json={"data": [{"value": "68", "value_classification": "Greed"}, {"value": "62", "value_classification": "Greed"}]},
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = MarketDataFetcher(MarketDataConfig(max_retries=1), http_client=client, sleep_fn=lambda _: None)
    snap = fetcher.fetch_snapshot(altcoin_tickers=["$SOL", "BTC"])

    assert snap.macro.btc.price_usd == 98500.0
    assert snap.macro.btc.source == "binance_spot"
    assert snap.macro.btc_dominance_pct == 57.5
    assert snap.leverage.btc.funding_rate_8h_pct == pytest.approx(0.01)
    assert snap.liquidity.stablecoin_7d_change_pct is not None and snap.liquidity.stablecoin_7d_change_pct > 0
    assert snap.liquidity.defi_tvl_7d_change_pct is not None and snap.liquidity.defi_tvl_7d_change_pct > 0
    assert snap.sentiment.fear_greed_value == 68
    assert len(snap.altcoins) == 1
    assert snap.altcoins[0].symbol == "SOL"
    assert snap.altcoins[0].change_7d_pct == 14.1
