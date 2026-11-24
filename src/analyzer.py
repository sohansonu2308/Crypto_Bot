"""Multi-stage institutional LLM analysis pipeline anchored to deterministic quant scoring."""

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
from src.quant_scoring import (
    BullIgnitionScorecard,
    CycleRegime,
    compute_bull_ignition_scorecard,
)
from src.state_store import BotState, clean_shilled_tickers
from src.telegram_ingest import (
    TelegramMessage,
    TelegramTelemetrySummary,
    is_promo_or_hindsight_spam,
)

logger = logging.getLogger(__name__)

BullRunPhase = CycleRegime


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
    what_must_flip_for_bull_run: list[str] = Field(default_factory=list)
    altcoin_rotation_stage: str
    real_volume_sectors: list[str] = Field(default_factory=list)
    pure_hype_sectors: list[str] = Field(default_factory=list)
    altcoin_pulse_summary: str
    verified_signals: list[str] = Field(default_factory=list)
    manipulation_watch: list[str] = Field(default_factory=list)
    channel_pulse: dict[str, str] = Field(default_factory=dict)
    rolling_state_update: RollingStateUpdate = Field(default_factory=RollingStateUpdate)


def _format_messages_for_prompt(messages: list[TelegramMessage], max_chars: int = 36_000) -> str:
    """Serialize Telegram messages into a compact prompt block, tagging VIP/promo spam."""
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
        if is_promo_or_hindsight_spam(msg.text):
            meta_parts.append("PROMO_OR_HINDSIGHT=true")
        line = f"[{' | '.join(meta_parts)}] {snippet}"
        if total_chars + len(line) + 1 > max_chars:
            lines.append("... [truncated older/excess messages for token budget]")
            break
        lines.append(line)
        total_chars += len(line) + 1
    return "\n".join(lines) if lines else "(No messages available in this window.)"


def _group_messages_by_week(
    messages_by_channel: dict[str, list[TelegramMessage]],
) -> list[tuple[str, list[TelegramMessage]]]:
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
    """Runs Backfill synthesis and the 3-pass Whale-Grade Daily Analysis pipeline."""

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
        scorecard: BullIgnitionScorecard | None = None,
    ) -> BaselineSynthesis:
        """Chunk 60-day history by week with fast_model, then synthesize baseline with reasoning_model."""
        quant_card = scorecard or compute_bull_ignition_scorecard(market_snapshot)
        weekly_groups = _group_messages_by_week(messages_by_channel)
        weekly_summaries: list[WeeklySummary] = []

        fast_system = (
            "You are an institutional crypto quant analyst summarizing one week of retail/KOL Telegram "
            "chatter. Treat KOL posts strictly as a contrarian retail sentiment and promotion thermometer—"
            "never treat VIP pitches or self-reported trading wins as objective evidence."
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
            "You are the Head of Macro & Quant Strategy at an institutional crypto desk building a 60-day "
            "baseline. Classify `baseline_phase` into EXACTLY ONE of:\n"
            "['Markdown / Liquidity Drain', 'Capitulation / Bottoming Watch', 'Stealth Accumulation', "
            "'Early Bull Ignition', 'Confirmed Bull Trend', 'Bull Trap / Distribution'].\n"
            "CRITICAL RULE: Never confuse late-bear Markdown/Capitulation (Fear & Greed < 25, negative fiat "
            "stablecoin flows, BTC below 50D SMA) with cycle-top Distribution. Anchor heavily to the "
            "Deterministic Bull-Ignition Scorecard and exclude BTC/ETH/SOL/USDT/USDC from `recurring_shilled_tickers`."
        )
        synthesis_prompt = (
            f"Historical Institutional Playbook:\n{self.playbook_text}\n\n"
            f"Deterministic Bull-Ignition Scorecard (0-100):\n{quant_card.model_dump_json(indent=2)}\n\n"
            f"Current Objective Market Snapshot:\n{market_snapshot.model_dump_json(indent=2)}\n\n"
            f"Weekly Telegram Digests ({len(weekly_summaries)} weeks):\n"
            f"{json.dumps([w.model_dump(mode='json') for w in weekly_summaries], indent=2, ensure_ascii=False)}"
        )

        synthesis = self.llm.complete_structured(
            model=self.config.llm.reasoning_model,
            system_prompt=reasoning_system,
            user_prompt=synthesis_prompt,
            response_model=BaselineSynthesis,
        )
        synthesis.recurring_shilled_tickers = clean_shilled_tickers(synthesis.recurring_shilled_tickers)
        return synthesis

    def run_daily_pass1(
        self,
        messages_by_channel: dict[str, list[TelegramMessage]],
        state: BotState,
        telemetry: TelegramTelemetrySummary | None = None,
    ) -> Pass1Digest:
        """Pass 1 (fast_model): Deduplicate posts, extract non-major altcoin tickers, flag shilling/spam."""
        all_messages = sorted(
            [m for msgs in messages_by_channel.values() for m in msgs],
            key=lambda m: (m.timestamp_utc, m.message_id),
        )
        channels_list = list(messages_by_channel.keys()) or self.config.telegram.channels

        system_prompt = (
            "You are Pass 1 of an institutional crypto contrarian signal filter. Given today's raw Telegram posts:\n"
            "1. Deduplicate overlapping cross-channel posts into concise themes.\n"
            "2. Extract up to 10 most-mentioned speculative altcoin tickers (uppercase; DO NOT include BTC, ETH, USDT, USDC).\n"
            "3. Identify active sectors (e.g., AI, RWA, DeFi, Memes, L1/L2).\n"
            "4. Flag coordinated cross-channel shilling, VIP/paid-funnel hindsight marketing, unverified insider claims, and FOMO/apathy extremes.\n"
            "5. Provide a 1-sentence behavioral summary per channel."
        )
        telemetry_block = (
            f"Quantitative Channel Telemetry (Apathy & Promo Spam):\n{telemetry.model_dump_json(indent=2)}\n\n"
            if telemetry is not None
            else ""
        )
        user_prompt = (
            f"Tracked Channels: {channels_list}\n"
            f"{telemetry_block}"
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
        scorecard: BullIgnitionScorecard | None = None,
        telemetry: TelegramTelemetrySummary | None = None,
    ) -> Pass2RealityCheck:
        """Pass 2 (reasoning_model): Cross-check Telegram claims vs institutional flows, perps, and structure."""
        quant_card = scorecard or compute_bull_ignition_scorecard(market_snapshot, telemetry)
        system_prompt = (
            "You are Pass 2 (Whale Reality Check) of an institutional bull-run detection engine. "
            "Audit Telegram narratives against hard institutional ground truth:\n"
            "- Pure fiat dry powder (USDT+USDC 7d/30d & 2nd-derivative acceleration) + Kraken USDT/USD peg bps + US Fed Net Liquidity.\n"
            "- Multi-timeframe BTC structure (20D/50D/200D SMAs, 20D Donchian breakout, Spot Volume vs 20D avg) + Coinbase Spot Premium bps.\n"
            "- Geo-block-free Hyperliquid/Deribit perp funding, perp-to-oracle basis, DVOL, and Options Put/Call OI ratio.\n"
            "- Fixed 12-asset Liquid Majors Breadth vs. Telegram shill micro-caps."
        )
        telemetry_json = telemetry.model_dump_json(indent=2) if telemetry else "null"
        user_prompt = (
            f"Historical Institutional Playbook:\n{self.playbook_text}\n\n"
            f"Deterministic Bull-Ignition Scorecard (0-100):\n{quant_card.model_dump_json(indent=2)}\n\n"
            f"Telegram Retail Apathy & Promo Telemetry:\n{telemetry_json}\n\n"
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
        scorecard: BullIgnitionScorecard | None = None,
        telemetry: TelegramTelemetrySummary | None = None,
        report_date_utc: str | None = None,
    ) -> DailyReport:
        """Pass 3 (reasoning_model): Produce the final whale-grade daily briefing and rolling state update."""
        date_str = report_date_utc or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        quant_card = scorecard or compute_bull_ignition_scorecard(market_snapshot, telemetry)

        system_prompt = (
            "You are Pass 3 (Head of Whale Macro Strategy) writing the daily bull-run inflection briefing.\n"
            "1. `macro_phase`: Choose EXACTLY ONE of:\n"
            "   ['Markdown / Liquidity Drain', 'Capitulation / Bottoming Watch', 'Stealth Accumulation', "
            "'Early Bull Ignition', 'Confirmed Bull Trend', 'Bull Trap / Distribution'].\n"
            "   Never label low-sentiment selloffs (Fear & Greed < 25) as Distribution; align closely with the "
            "Deterministic Bull-Ignition Scorecard unless there is an overwhelming structural reason.\n"
            "2. Directly answer: 'Based on today's move, is this actually the start of a bull run or not?'\n"
            "3. `what_must_flip_for_bull_run`: List 2-4 concrete, measurable triggers (e.g., USDT+USDC 7d > 0%, "
            "USDT peg > 0 bps, Coinbase premium > +4 bps, BTC reclaiming 50D SMA on >1.5x spot volume) needed next.\n"
            "4. Separate real volume sectors (from the 12-major benchmark & verified alts) from Telegram exit-liquidity hype.\n"
            "5. Never put BTC, ETH, SOL, BNB, XRP, USDT, or USDC into `recurring_shilled_tickers`."
        )
        telemetry_json = telemetry.model_dump_json(indent=2) if telemetry else "null"
        user_prompt = (
            f"Report Date (UTC): {date_str}\n\n"
            f"Deterministic Bull-Ignition Scorecard (0-100):\n{quant_card.model_dump_json(indent=2)}\n\n"
            f"Telegram Retail Apathy & Promo Telemetry:\n{telemetry_json}\n\n"
            f"Historical Playbook Reference:\n{self.playbook_text}\n\n"
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
        report.rolling_state_update.recurring_shilled_tickers = clean_shilled_tickers(
            report.rolling_state_update.recurring_shilled_tickers
        )
        return report
