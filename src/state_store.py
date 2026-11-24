"""Lightweight JSON state manager persisted in data/state.json."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from src.config import normalize_channel_name

# Core liquid majors, stablecoins, and TradFi macro symbols that must NEVER be recorded as "shilled micro-cap tickers"
EXCLUDED_MAJOR_TICKERS: frozenset[str] = frozenset(
    {
        "BTC",
        "BITCOIN",
        "ETH",
        "SOL",
        "BNB",
        "XRP",
        "USDT",
        "USDC",
        "DAI",
        "USDE",
        "USD",
        "EUR",
        "GBP",
        "GOLD",
        "SILVER",
        "XAU",
        "XAG",
        "OIL",
        "WTI",
        "BRENT",
        "SPX",
        "NDX",
        "DJI",
        "QQQ",
        "SPY",
        "IWM",
        "TLT",
        "DXY",
        "YIELDS",
        "US10Y",
        "NVDA",
        "AAPL",
        "MSFT",
        "TSLA",
        "META",
        "GOOGL",
        "AMZN",
        "COIN",
        "MSTR",
        "URA",
        "XLE",
        "COAL",
        "FOMC",
        "CPI",
        "GDP",
    }
)


class ChannelBaseline(BaseModel):
    baseline_posts_per_day: float = 0.0
    baseline_median_views: float = 0.0
    sample_messages: int = 0


class VerdictRecord(BaseModel):
    date: str
    phase: str
    confidence: int = Field(ge=0, le=100)
    bull_ignition_score: int | None = None
    direct_answer: str


class RollingMemory(BaseModel):
    macro_themes: list[str] = Field(default_factory=list)
    active_altcoin_narratives: list[str] = Field(default_factory=list)
    recurring_shilled_tickers: list[str] = Field(default_factory=list)
    channel_credibility_notes: dict[str, str] = Field(default_factory=dict)


class BotState(BaseModel):
    version: int = 2
    initialized: bool = False
    last_updated_utc: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    channel_checkpoints: dict[str, int] = Field(default_factory=dict)
    channel_baselines: dict[str, ChannelBaseline] = Field(default_factory=dict)
    memory: RollingMemory = Field(default_factory=RollingMemory)
    verdict_history: list[VerdictRecord] = Field(default_factory=list)


def clean_shilled_tickers(tickers: list[str], limit: int = 20) -> list[str]:
    """Normalize shilled ticker symbols and strip out core majors/stablecoins."""
    cleaned_list: list[str] = []
    seen: set[str] = set()
    for raw in tickers:
        sym = raw.strip().upper().lstrip("$")
        if not sym or sym in EXCLUDED_MAJOR_TICKERS or sym in seen:
            continue
        seen.add(sym)
        cleaned_list.append(sym)
        if len(cleaned_list) >= limit:
            break
    return cleaned_list


class StateStore:
    """Manages loading, updating, and atomically saving `data/state.json`."""

    MAX_HISTORY_DAYS = 60
    MAX_LIST_ITEMS = 20

    def __init__(self, path: str | Path = "data/state.json") -> None:
        self.path = Path(path)

    def load(self) -> BotState:
        if not self.path.exists():
            return BotState()
        with self.path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        state = BotState.model_validate(raw)
        # Sanitize any legacy major tickers (e.g., ETH) from recurring_shilled_tickers on load
        state.memory.recurring_shilled_tickers = clean_shilled_tickers(
            state.memory.recurring_shilled_tickers, self.MAX_LIST_ITEMS
        )
        return state

    def save(self, state: BotState) -> None:
        state.version = 2
        state.last_updated_utc = datetime.now(timezone.utc).isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".json.tmp")
        payload = json.dumps(state.model_dump(mode="json"), indent=2, ensure_ascii=False)
        with tmp_path.open("w", encoding="utf-8") as f:
            f.write(payload + "\n")
        os.replace(tmp_path, self.path)

    def update_checkpoints(self, state: BotState, latest_ids: dict[str, int]) -> BotState:
        """Advance per-channel `last_message_id` checkpoints monotonically."""
        for raw_ch, msg_id in latest_ids.items():
            ch = normalize_channel_name(raw_ch)
            prev = state.channel_checkpoints.get(ch, 0)
            if msg_id > prev:
                state.channel_checkpoints[ch] = msg_id
        return state

    def update_channel_baselines(
        self,
        state: BotState,
        baselines: dict[str, ChannelBaseline],
    ) -> BotState:
        for raw_ch, baseline in baselines.items():
            state.channel_baselines[normalize_channel_name(raw_ch)] = baseline
        return state

    def merge_memory(
        self,
        state: BotState,
        *,
        macro_themes: list[str] | None = None,
        active_altcoin_narratives: list[str] | None = None,
        recurring_shilled_tickers: list[str] | None = None,
        channel_credibility_notes: dict[str, str] | None = None,
        replace: bool = False,
    ) -> BotState:
        """Merge or replace rolling 60-day condensed narrative memory."""
        if replace:
            state.memory = RollingMemory(
                macro_themes=_dedupe_cap(macro_themes or [], self.MAX_LIST_ITEMS),
                active_altcoin_narratives=_dedupe_cap(active_altcoin_narratives or [], self.MAX_LIST_ITEMS),
                recurring_shilled_tickers=clean_shilled_tickers(
                    recurring_shilled_tickers or [],
                    self.MAX_LIST_ITEMS,
                ),
                channel_credibility_notes={
                    normalize_channel_name(k): v.strip()
                    for k, v in (channel_credibility_notes or {}).items()
                    if v and v.strip()
                },
            )
            return state

        if macro_themes is not None:
            state.memory.macro_themes = _dedupe_cap(
                macro_themes + state.memory.macro_themes, self.MAX_LIST_ITEMS
            )
        if active_altcoin_narratives is not None:
            state.memory.active_altcoin_narratives = _dedupe_cap(
                active_altcoin_narratives + state.memory.active_altcoin_narratives, self.MAX_LIST_ITEMS
            )
        if recurring_shilled_tickers is not None:
            state.memory.recurring_shilled_tickers = clean_shilled_tickers(
                recurring_shilled_tickers + state.memory.recurring_shilled_tickers,
                self.MAX_LIST_ITEMS,
            )
        if channel_credibility_notes:
            for raw_ch, note in channel_credibility_notes.items():
                if note and note.strip():
                    state.memory.channel_credibility_notes[normalize_channel_name(raw_ch)] = note.strip()

        return state

    def append_verdict(self, state: BotState, record: VerdictRecord) -> BotState:
        """Record today's bull-run verdict (replacing same-day entry if re-run) and cap to 60 days."""
        filtered = [v for v in state.verdict_history if v.date != record.date]
        filtered.append(record)
        filtered.sort(key=lambda v: v.date)
        state.verdict_history = filtered[-self.MAX_HISTORY_DAYS :]
        return state


def _dedupe_cap(items: list[str], limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        cleaned = item.strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key not in seen:
            seen.add(key)
            result.append(cleaned)
        if len(result) >= limit:
            break
    return result
