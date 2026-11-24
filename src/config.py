"""Configuration loader merging config.yaml defaults with environment variable overrides."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator


def normalize_channel_name(raw: str) -> str:
    """Strip whitespace, URL prefixes, and leading '@' from a Telegram channel handle."""
    cleaned = raw.strip()
    for prefix in (
        "https://t.me/s/",
        "http://t.me/s/",
        "https://t.me/",
        "http://t.me/",
        "t.me/s/",
        "t.me/",
    ):
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    cleaned = cleaned.strip("/")
    cleaned = cleaned.lstrip("@").strip()
    return cleaned


class TelegramConfig(BaseModel):
    channels: list[str] = Field(
        default_factory=lambda: [
            "researchcitadel",
            "shitcapcentral",
            "blockchainwhispersbaby",
            "ProfitCircle_Official",
        ]
    )
    backfill_days: int = 60
    request_delay_min_seconds: float = 1.0
    request_delay_max_seconds: float = 2.5
    max_pages_per_channel: int = 80
    request_timeout_seconds: float = 20.0
    bot_token: str | None = None
    chat_id: str | None = None

    @field_validator("channels", mode="before")
    @classmethod
    def _clean_channels(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            items = [part for part in value.split(",") if part.strip()]
        elif isinstance(value, list):
            items = [str(part) for part in value if str(part).strip()]
        else:
            items = []
        normalized: list[str] = []
        seen: set[str] = set()
        for item in items:
            ch = normalize_channel_name(item)
            if ch and ch.lower() not in seen:
                seen.add(ch.lower())
                normalized.append(ch)
        return normalized


class MarketEndpointsConfig(BaseModel):
    coingecko_base: str = "https://api.coingecko.com/api/v3"
    coinbase_base: str = "https://api.exchange.coinbase.com"
    kraken_base: str = "https://api.kraken.com/0/public"
    hyperliquid_info: str = "https://api.hyperliquid.xyz/info"
    deribit_base: str = "https://www.deribit.com/api/v2/public"
    binance_spot_base: str = "https://api.binance.com/api/v3"
    binance_futures_base: str = "https://fapi.binance.com/fapi/v1"
    defillama_stablecoins: str = "https://stablecoins.llama.fi/stablecoincharts/all"
    defillama_tvl: str = "https://api.llama.fi/v2/historicalChainTvl"
    fear_and_greed: str = "https://api.alternative.me/fng/"
    fred_csv_base: str = "https://fred.stlouisfed.org/graph/fredgraph.csv"


class MarketDataConfig(BaseModel):
    timeout_seconds: float = 15.0
    max_retries: int = 3
    endpoints: MarketEndpointsConfig = Field(default_factory=MarketEndpointsConfig)


class LLMConfig(BaseModel):
    base_url: str = "https://api.openai.com/v1"
    api_key: str | None = None
    fast_model: str = "gpt-6-mini"
    reasoning_model: str = "gpt-6"
    timeout_seconds: float = 90.0
    max_retries: int = 4
    temperature: float = 0.2
    extra_headers: dict[str, str] = Field(default_factory=dict)


class PathsConfig(BaseModel):
    state_file: Path = Path("data/state.json")
    reports_dir: Path = Path("reports")
    playbook_file: Path = Path("knowledge/bull_run_playbook.md")


class AppConfig(BaseModel):
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    market_data: MarketDataConfig = Field(default_factory=MarketDataConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)


def _parse_extra_headers(raw_json: str | None) -> dict[str, str]:
    if not raw_json or not raw_json.strip():
        return {}
    parsed = json.loads(raw_json)
    if not isinstance(parsed, dict):
        raise ValueError("LLM_EXTRA_HEADERS_JSON must decode to a JSON object.")
    return {str(k): str(v) for k, v in parsed.items()}


def load_config(config_path: str | Path = "config.yaml", load_dotenv_file: bool = True) -> AppConfig:
    """Load config.yaml and apply environment variable overrides."""
    if load_dotenv_file:
        load_dotenv(override=False)

    raw_data: dict[str, Any] = {}
    cfg_path = Path(config_path)
    if cfg_path.exists():
        with cfg_path.open("r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
            if isinstance(loaded, dict):
                raw_data = loaded

    tg_data = dict(raw_data.get("telegram") or {})
    mkt_data = dict(raw_data.get("market_data") or {})
    llm_data = dict(raw_data.get("llm") or {})
    paths_data = dict(raw_data.get("paths") or {})

    # Telegram env overrides (reuses existing GitHub secrets: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)
    env_channels = os.getenv("TELEGRAM_CHANNELS")
    if env_channels and env_channels.strip():
        tg_data["channels"] = env_channels
    if os.getenv("BACKFILL_DAYS"):
        tg_data["backfill_days"] = int(os.environ["BACKFILL_DAYS"])

    tg_data["bot_token"] = os.getenv("TELEGRAM_TOKEN") or tg_data.get("bot_token")
    tg_data["chat_id"] = os.getenv("TELEGRAM_CHAT_ID") or tg_data.get("chat_id")

    # LLM env overrides
    if os.getenv("LLM_BASE_URL"):
        llm_data["base_url"] = os.environ["LLM_BASE_URL"].strip()
    if os.getenv("LLM_API_KEY"):
        llm_data["api_key"] = os.environ["LLM_API_KEY"].strip()

    env_fast = os.getenv("LLM_FAST_MODEL", "").strip()
    env_reasoning = os.getenv("LLM_REASONING_MODEL", "").strip()
    if env_fast and env_reasoning:
        llm_data["fast_model"] = env_fast
        llm_data["reasoning_model"] = env_reasoning
    elif env_fast:
        llm_data["fast_model"] = env_fast
        llm_data["reasoning_model"] = env_fast
    elif env_reasoning:
        llm_data["reasoning_model"] = env_reasoning
        llm_data["fast_model"] = env_reasoning

    if os.getenv("LLM_TIMEOUT_SECONDS"):
        llm_data["timeout_seconds"] = float(os.environ["LLM_TIMEOUT_SECONDS"])
    if os.getenv("LLM_MAX_RETRIES"):
        llm_data["max_retries"] = int(os.environ["LLM_MAX_RETRIES"])

    extra_headers_env = os.getenv("LLM_EXTRA_HEADERS_JSON")
    if extra_headers_env is not None:
        llm_data["extra_headers"] = _parse_extra_headers(extra_headers_env)

    return AppConfig(
        telegram=TelegramConfig(**tg_data),
        market_data=MarketDataConfig(**mkt_data),
        llm=LLMConfig(**llm_data),
        paths=PathsConfig(**paths_data),
    )
