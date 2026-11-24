"""Multi-stage LLM analysis pipeline using structured Pydantic outputs."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from src.config import AppConfig
from src.llm_client import LLMClient
from src.market_data import MarketSnapshot
from src.state_store import BotState
from src.telegram_ingest import TelegramMessage

logger = logging.getLogger(__name__)

BullRunPhase = Literal[
    "Accumulation",
    "Early Ignition",
    "Confirmed Bull Trend",
    "Bull Trap / Fakeout",
    "Distribution / Noise",
]


class WeeklySummary(BaseModel):
    week_label: str
    macro_themes: list[str] = Field(default_factory=list)
    dominant_altcoin_narratives: list[str] = Field(default_factory=list)
    top_mentioned_tickers: list[str] = Field(default_factory=list)
    suspicious_shilling_or_rumors: list[str] = Field(default_factory=list)
    channel_observations: dict[str, str] = Field(default_factory=dict)


class BaselineSynthesis(BaseModel):
    baseline_phase: BullRunPhase
    confidence_score: int = Field(ge=0, le=100)
    executive_summary: str
    macro_themes: list[str] = Field(default_factory=list)
    active_altcoin_narratives: list[str] = Field(default_factory=list)
    recurring_shilled_tickers: list[str] = Field(default_factory=list)
    channel_credibility_notes: dict[str, str] = Field(default_factory=dict)
    key_watchpoints: list[str] = Field(default_factory=list)


class Pass1Digest(BaseModel):
    deduplicated_themes: list[str] = Field(default_factory=list)
    top_altcoin_tickers: list[str] = Field(default_factory=list, max_length=10)
    active_sectors: list[str] = Field(default_factory=list)
    coordinated_shilling_flags: list[str] = Field(default_factory=list)
    unverified_claims: list[str] = Field(default_factory=list)
    sentiment_extremes: list[str] = Field(default_factory=list)
    channel_summaries: dict[str, str] = Field(default_factory=dict)


class AltcoinRealityItem(BaseModel):
    ticker: str
    telegram_narrative: str
    market_reality: str
    verdict: Literal["Backed by Volume", "Mixed / Early", "Hype / Unverified", "No Data"]


class Pass2RealityCheck(BaseModel):
    macro_alignment: str
    leverage_trap_assessment: str
    liquidity_assessment: str
    altcoin_data_verification: list[AltcoinRealityItem] = Field(default_factory=list)
    playbook_stage_match: str
    confirmed_catalysts: list[str] = Field(default_factory=list)
    debunked_or_unverified_claims: list[str] = Field(default_factory=list)


class RollingStateUpdate(BaseModel):
    macro_themes: list[str] = Field(default_factory=list)
    active_altcoin_narratives: list[str] = Field(default_factory=list)
    recurring_shilled_tickers: list[str] = Field(default_factory=list)
    channel_credibility_notes: dict[str, str] = Field(default_factory=dict)


class DailyReport(BaseModel):
    report_date_utc: str
    macro_phase: BullRunPhase
    confidence_score: int = Field(ge=0, le=100)
    bull_run_direct_answer: str
    altcoin_rotation_stage: str
    real_volume_sectors: list[str] = Field(default_factory=list)
    pure_hype_sectors: list[str] = Field(default_factory=list)
    altcoin_pulse_summary: str
    verified_signals: list[str] = Field(default_factory=list)
    manipulation_watch: list[str] = Field(default_factory=list)
    channel_pulse: dict[str, str] = Field(default_factory=dict)
    rolling_state_update: RollingStateUpdate = Field(default_factory=RollingStateUpdate)


def _format_messages_for_prompt(messages: list[TelegramMessage], max_chars: int = 36_000) -> str:
    """Serialize Telegram messages into a compact prompt block within a character budget."""
    lines: list[str] = []
    total_chars = 0
    for msg in messages:
        if not msg.text and not msg.has_media:
            continue
        snippet = msg.text.replace("\n", " ").strip()
        if len(snippet) > 400:
            snippet = snippet[:397] + "..."
        meta_parts = [f"@{msg.channel}#{msg.message_id}", msg.timestamp_utc.strftime("%Y-%m-%d %H:%M")]
        if msg.views_raw:
            meta_parts.append(f"views={msg.views_raw}")
        if msg.forwarded_from:
            meta_parts.append(f"fwd={msg.forwarded_from}")
        if msg.has_media:
            meta_parts.append(f"media={','.join(msg.media_types)}")
        line = f"[{' | '.join(meta_parts)}] {snippet}"
        if total_chars + len(line) + 1 > max_chars:
            lines.append("... [truncated older/excess messages for token budget]")
            break
        lines.append(line)
        total_chars += len(line) + 1
    return "\n".join(lines) if lines else "(No messages available in this window.)"


def _group_messages_by_week(messages_by_channel: dict[str, list[TelegramMessage]]) -> list[tuple[str, list[TelegramMessage]]]:
    buckets: dict[str, list[TelegramMessage]] = defaultdict(list)
    for msgs in messages_by_channel.values():
        for msg in msgs:
            iso_year, iso_week, _ = msg.timestamp_utc.isocalendar()
            label = f"{iso_year}-W{iso_week:02d}"
            buckets[label].append(msg)

    ordered: list[tuple[str, list[TelegramMessage]]] = []
    for label in sorted(buckets.keys()):
        week_msgs = sorted(buckets[label], key=lambda m: (m.timestamp_utc, m.message_id))
        ordered.append((label, week_msgs))
    return ordered


class CryptoAnalyzer:
    """Runs Backfill synthesis and the 3-pass Daily Analysis pipeline."""

    def __init__(self, config: AppConfig, llm: LLMClient) -> None:
        self.config = config
        self.llm = llm
        self.playbook_text = self._load_playbook(config.paths.playbook_file)

    @staticmethod
    def _load_playbook(path: Path) -> str:
        if path.exists():
            return path.read_text(encoding="utf-8")
        logger.warning("Playbook file not found at %s; continuing without playbook context.", path)
        return ""

    def run_backfill(
        self,
        messages_by_channel: dict[str, list[TelegramMessage]],
        market_snapshot: MarketSnapshot,
    ) -> BaselineSynthesis:
        """Chunk 60-day history by week with fast_model, then synthesize baseline with reasoning_model."""
        weekly_groups = _group_messages_by_week(messages_by_channel)
        weekly_summaries: list[WeeklySummary] = []

        fast_system = (
            "You are a disciplined crypto market analyst summarizing one week of public Telegram "
            "channel chatter. Separate recurring macro themes and altcoin narratives from "
            "coordinated shilling, fake rumors, and engagement farming."
        )

        for week_label, week_msgs in weekly_groups:
            prompt = (
                f"Week: {week_label}\n"
                f"Message count: {len(week_msgs)}\n\n"
                f"Telegram Messages:\n{_format_messages_for_prompt(week_msgs)}"
            )
            summary = self.llm.complete_structured(
                model=self.config.llm.fast_model,
                system_prompt=fast_system,
                user_prompt=prompt,
                response_model=WeeklySummary,
            )
            summary.week_label = week_label
            weekly_summaries.append(summary)

        reasoning_system = (
            "You are a senior crypto macro strategist building an initial 60-day baseline state. "
            "Cross-reference the 2-month Telegram weekly digests against the current objective "
            "market snapshot and the historical bull-run playbook. Be skeptical of retail hype."
        )
        synthesis_prompt = (
            f"Historical Bull Run Playbook:\n{self.playbook_text}\n\n"
            f"Current Objective Market Snapshot:\n{market_snapshot.model_dump_json(indent=2)}\n\n"
            f"Weekly Telegram Digests ({len(weekly_summaries)} weeks):\n"
            f"{json.dumps([w.model_dump(mode='json') for w in weekly_summaries], indent=2, ensure_ascii=False)}"
        )

        return self.llm.complete_structured(
            model=self.config.llm.reasoning_model,
            system_prompt=reasoning_system,
            user_prompt=synthesis_prompt,
            response_model=BaselineSynthesis,
        )

    def run_daily_pass1(
        self,
        messages_by_channel: dict[str, list[TelegramMessage]],
        state: BotState,
    ) -> Pass1Digest:
        """Pass 1 (fast_model): Deduplicate posts, extract top 5-10 altcoin tickers, flag shilling/FOMO."""
        all_messages = sorted(
            [m for msgs in messages_by_channel.values() for m in msgs],
            key=lambda m: (m.timestamp_utc, m.message_id),
        )
        channels_list = list(messages_by_channel.keys()) or self.config.telegram.channels

        system_prompt = (
            "You are Pass 1 of a crypto signal filter. Given today's raw public Telegram posts:\n"
            "1. Deduplicate overlapping cross-channel posts into concise themes.\n"
            "2. Extract up to 10 most-mentioned altcoin ticker symbols (uppercase, excluding BTC/ETH/USDT/USDC).\n"
            "3. Identify active sectors (e.g., AI, RWA, DeFi, Memes, L1/L2).\n"
            "4. Flag coordinated cross-channel shilling, unverified insider/ETF/listing claims, and FOMO/FUD spikes.\n"
            "5. Provide a 1-sentence raw summary per channel."
        )
        user_prompt = (
            f"Tracked Channels: {channels_list}\n"
            f"Existing Rolling Memory:\n{state.memory.model_dump_json(indent=2)}\n\n"
            f"Today's New Telegram Messages ({len(all_messages)} total):\n"
            f"{_format_messages_for_prompt(all_messages)}"
        )

        return self.llm.complete_structured(
            model=self.config.llm.fast_model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_model=Pass1Digest,
        )

    def run_daily_pass2(
        self,
        pass1: Pass1Digest,
        market_snapshot: MarketSnapshot,
        state: BotState,
    ) -> Pass2RealityCheck:
        """Pass 2 (reasoning_model): Cross-check Telegram claims & tickers against hard market data & playbook."""
        system_prompt = (
            "You are Pass 2 (Reality Check) of a crypto bull-run verification engine. "
            "Your job is to ruthlessly audit Telegram chatter against objective market data "
            "(spot volume, BTC.D, perp funding/OI, stablecoin 7d supply, DeFi TVL, Fear & Greed, "
            "and actual altcoin 24h/7d price & volume) and the historical bull-run playbook. "
            "Explicitly separate genuine spot-backed moves from leverage traps and Telegram exit-liquidity hype."
        )
        user_prompt = (
            f"Historical Bull Run Playbook:\n{self.playbook_text}\n\n"
            f"Rolling 60-Day State Memory:\n{state.memory.model_dump_json(indent=2)}\n\n"
            f"Recent Verdict History (last 7):\n"
            f"{json.dumps([v.model_dump(mode='json') for v in state.verdict_history[-7:]], indent=2)}\n\n"
            f"Pass 1 Telegram Digest:\n{pass1.model_dump_json(indent=2)}\n\n"
            f"Objective Market Ground Truth:\n{market_snapshot.model_dump_json(indent=2)}"
        )

        return self.llm.complete_structured(
            model=self.config.llm.reasoning_model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_model=Pass2RealityCheck,
        )

    def run_daily_pass3(
        self,
        pass1: Pass1Digest,
        pass2: Pass2RealityCheck,
        market_snapshot: MarketSnapshot,
        state: BotState,
        report_date_utc: str | None = None,
    ) -> DailyReport:
        """Pass 3 (reasoning_model): Produce the final daily briefing and compact rolling state update."""
        date_str = report_date_utc or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        system_prompt = (
            "You are Pass 3 (Chief Strategist) generating the daily crypto briefing. "
            "Deliver an objective, high-signal report with:\n"
            "1. Macro Bull Run Verdict: classify into exactly one of "
            "['Accumulation', 'Early Ignition', 'Confirmed Bull Trend', 'Bull Trap / Fakeout', 'Distribution / Noise'], "
            "assign a 0-100 confidence score, and directly answer: "
            "'Based on today's move, is this actually the start of a bull run or not?'\n"
            "2. Altcoin Rotation & Narrative Pulse: sectors gaining real volume vs. pure Telegram hype, and rotation stage.\n"
            "3. Verified Signals: real catalysts backed by hard market data.\n"
            "4. Manipulation & Fake News Watch: specific rumors, shilled coins, or manipulated narratives to ignore and why.\n"
            "5. Channel Pulse: 1-line summary per channel comparing narrative vs. reality.\n"
            "6. Compact rolling_state_update to merge into the 60-day memory."
        )
        user_prompt = (
            f"Report Date (UTC): {date_str}\n\n"
            f"Historical Playbook Summary Reference:\n{self.playbook_text}\n\n"
            f"Pass 1 Telegram Digest:\n{pass1.model_dump_json(indent=2)}\n\n"
            f"Pass 2 Reality Check:\n{pass2.model_dump_json(indent=2)}\n\n"
            f"Objective Market Snapshot:\n{market_snapshot.model_dump_json(indent=2)}\n\n"
            f"Existing Rolling State Memory:\n{state.memory.model_dump_json(indent=2)}"
        )

        report = self.llm.complete_structured(
            model=self.config.llm.reasoning_model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_model=DailyReport,
        )
        report.report_date_utc = date_str
        return report
