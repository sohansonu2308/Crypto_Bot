"""Tests for StateStore, Telegram 4,096-char chunking, Pydantic v2 analyzer schemas, and --dry-run."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from src.analyzer import (
    AltcoinRealityItem,
    BaselineSynthesis,
    DailyReport,
    Pass1Digest,
    Pass2RealityCheck,
    RollingStateUpdate,
    WeeklySummary,
)
from src.config import AppConfig, LLMConfig, PathsConfig, TelegramConfig
from src.main import run_pipeline
from src.market_data import MarketSnapshot
from src.notifier import TELEGRAM_MAX_MESSAGE_LENGTH, TelegramNotifier, chunk_telegram_message
from src.state_store import StateStore, VerdictRecord
from src.telegram_ingest import TelegramMessage


def test_state_store_checkpoints_major_ticker_exclusion_and_rolling_memory(tmp_path):
    store = StateStore(tmp_path / "state.json")
    state = store.load()
    assert state.channel_checkpoints == {}

    store.update_checkpoints(state, {"@alpha": 150, "beta": 200})
    store.update_checkpoints(state, {"alpha": 120})  # monotonic: should not go backward
    assert state.channel_checkpoints == {"alpha": 150, "beta": 200}

    # ETH, BTC, SOL, USDT must be excluded automatically from recurring_shilled_tickers
    store.merge_memory(
        state,
        macro_themes=["ETF inflows", "Stablecoin expansion"],
        recurring_shilled_tickers=["$MEME1", "meme1", "ETH", "BTC", "SOL", "TIBBIR"],
        channel_credibility_notes={"@alpha": "Mostly macro charts"},
    )
    for day in range(1, 65):
        store.append_verdict(
            state,
            VerdictRecord(
                date=f"2026-01-{day:02d}",
                phase="Stealth Accumulation",
                confidence=60,
                bull_ignition_score=52,
                direct_answer="Not yet.",
            ),
        )

    store.save(state)
    reloaded = store.load()
    assert reloaded.channel_checkpoints["alpha"] == 150
    assert reloaded.memory.recurring_shilled_tickers == ["MEME1", "TIBBIR"]
    assert len(reloaded.verdict_history) == 60


def test_telegram_chunking_respects_4096_limit_and_plain_text_fallback():
    paragraph = "Signal line with objective metrics and verification details. " * 25
    long_text = "\n\n".join([f"Section {i}\n{paragraph}" for i in range(10)])
    assert len(long_text) > TELEGRAM_MAX_MESSAGE_LENGTH

    chunks = chunk_telegram_message(long_text, max_length=TELEGRAM_MAX_MESSAGE_LENGTH)
    assert len(chunks) >= 2
    assert all(len(c) <= TELEGRAM_MAX_MESSAGE_LENGTH for c in chunks)

    attempts: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content.decode("utf-8"))
        attempts.append(body)
        if body.get("parse_mode") == "Markdown":
            return httpx.Response(400, json={"ok": False, "description": "can't parse entities"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    notifier = TelegramNotifier(bot_token="tok", chat_id="123", http_client=client)
    res = notifier.send_message("Short *broken markdown")
    assert len(res) == 1
    assert attempts[0]["parse_mode"] == "Markdown"
    assert "parse_mode" not in attempts[1]


class _FakeLLM:
    def complete_structured(self, *, model, system_prompt, user_prompt, response_model, temperature=None):
        if response_model is WeeklySummary:
            return WeeklySummary(week_label="2026-W47", macro_themes=["BTC breakout"], top_mentioned_tickers=["TAO"])
        if response_model is BaselineSynthesis:
            return BaselineSynthesis(
                baseline_phase="Stealth Accumulation",
                confidence_score=74,
                executive_summary="Pure fiat stables stabilizing while retail apathy deepens.",
                macro_themes=["Pure fiat stabilization"],
                active_altcoin_narratives=["AI", "RWA"],
                recurring_shilled_tickers=["SHILLX", "ETH"],
                channel_credibility_notes={"alpha": "High promo noise"},
                key_watchpoints=["Watch Coinbase Spot Premium > +4 bps"],
            )
        if response_model is Pass1Digest:
            return Pass1Digest(
                deduplicated_themes=["BTC spot breakout"],
                top_altcoin_tickers=["TAO", "ONDO"],
                active_sectors=["AI", "RWA"],
                coordinated_shilling_flags=["SHILLX posted identically on 2 channels"],
                unverified_claims=["Unconfirmed Tier-1 listing rumor"],
                sentiment_extremes=["High retail apathy"],
                channel_summaries={"alpha": "Pushing VIP funnel despite quiet tape"},
            )
        if response_model is Pass2RealityCheck:
            return Pass2RealityCheck(
                macro_alignment="BTC reclaiming 20D & 50D SMA on Coinbase premium.",
                leverage_trap_assessment="Hyperliquid funding flat; disbelief rally intact.",
                liquidity_assessment="USDT+USDC 7d supply accelerating higher.",
                altcoin_data_verification=[
                    AltcoinRealityItem(
                        ticker="TAO",
                        telegram_narrative="Strong AI bid",
                        market_reality="+8.2% 7d on expanding spot volume",
                        verdict="Backed by Volume",
                    )
                ],
                playbook_stage_match="Phase 1 BTC leadership",
                confirmed_catalysts=["Positive Coinbase Spot Premium + USDT+USDC expansion"],
                debunked_or_unverified_claims=["Tier-1 listing rumor unverified"],
            )
        if response_model is DailyReport:
            return DailyReport(
                report_date_utc="2026-11-24",
                macro_phase="Early Bull Ignition",
                confidence_score=79,
                bull_run_direct_answer="Yes — early bull ignition is supported by Coinbase spot buying, uncrowded Hyperliquid funding, and positive USDT+USDC velocity.",
                what_must_flip_for_bull_run=["BTC daily close above 200D SMA", "Sustained >+0.5% weekly USDT+USDC growth"],
                altcoin_rotation_stage="BTC leadership with selective institutional majors strength",
                real_volume_sectors=["AI", "RWA"],
                pure_hype_sectors=["Micro-cap Memes"],
                altcoin_pulse_summary="Institutional majors lead while Telegram micro-cap shills lag.",
                verified_signals=["Positive 7d USDT+USDC net issuance", "Positive Coinbase Spot Premium"],
                manipulation_watch=["Ignore SHILLX coordinated cross-channel pump"],
                channel_pulse={"alpha": "Heavy VIP promo spam; contrarian apathy remains supportive."},
                rolling_state_update=RollingStateUpdate(
                    macro_themes=["Spot-led BTC breakout"],
                    active_altcoin_narratives=["AI", "RWA"],
                    recurring_shilled_tickers=["SHILLX", "ETH"],
                    channel_credibility_notes={"alpha": "Promotional funnel"},
                ),
            )
        raise AssertionError(f"Unexpected response_model: {response_model}")


class _FakeScraper:
    def fetch_backfill(self, channels, days):
        return {
            "alpha": [
                TelegramMessage(
                    channel="alpha",
                    message_id=501,
                    timestamp_utc=datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc),
                    text="BTC spot inflows accelerating",
                    views=4000,
                )
            ]
        }

    def fetch_daily(self, checkpoints, channels):
        return {
            "alpha": [
                TelegramMessage(
                    channel="alpha",
                    message_id=502,
                    timestamp_utc=datetime(2026, 11, 24, 1, 0, tzinfo=timezone.utc),
                    text="Join VIP group! Watch SHILLX and TAO",
                    views=2000,
                )
            ]
        }

    def close(self):
        pass


class _FakeMarketFetcher:
    def fetch_snapshot(self, altcoin_tickers=None):
        return MarketSnapshot()

    def close(self):
        pass


class _RecordingNotifier(TelegramNotifier):
    def __init__(self, reports_dir):
        super().__init__(bot_token="tok", chat_id="123", reports_dir=reports_dir)
        self.sent_messages: list[str] = []

    def send_message(self, text: str) -> list[dict]:
        self.sent_messages.append(text)
        return [{"ok": True}]


def test_pipeline_dry_run_has_no_side_effects(tmp_path):
    state_file = tmp_path / "data" / "state.json"
    reports_dir = tmp_path / "reports"
    cfg = AppConfig(
        telegram=TelegramConfig(channels=["alpha"], bot_token="tok", chat_id="123"),
        llm=LLMConfig(api_key="key"),
        paths=PathsConfig(state_file=state_file, reports_dir=reports_dir, playbook_file=tmp_path / "none.md"),
    )
    notifier = _RecordingNotifier(reports_dir)

    md = run_pipeline(
        mode="daily",
        dry_run=True,
        config=cfg,
        scraper=_FakeScraper(),
        market_fetcher=_FakeMarketFetcher(),
        llm_client=_FakeLLM(),
        state_store=StateStore(state_file),
        notifier=notifier,
    )

    assert "Early Bull Ignition" in md
    assert "Deterministic Bull-Ignition Scorecard" in md
    assert not state_file.exists()
    assert not reports_dir.exists()
    assert notifier.sent_messages == []


def test_pipeline_backfill_and_daily_persist_state_and_reports(tmp_path):
    state_file = tmp_path / "data" / "state.json"
    reports_dir = tmp_path / "reports"
    cfg = AppConfig(
        telegram=TelegramConfig(channels=["alpha"], bot_token="tok", chat_id="123"),
        llm=LLMConfig(api_key="key"),
        paths=PathsConfig(state_file=state_file, reports_dir=reports_dir, playbook_file=tmp_path / "none.md"),
    )
    notifier = _RecordingNotifier(reports_dir)
    store = StateStore(state_file)

    run_pipeline(
        mode="backfill",
        dry_run=False,
        config=cfg,
        scraper=_FakeScraper(),
        market_fetcher=_FakeMarketFetcher(),
        llm_client=_FakeLLM(),
        state_store=store,
        notifier=notifier,
    )
    assert (reports_dir / "initial_baseline.md").exists()
    loaded_after_bf = store.load()
    assert loaded_after_bf.channel_checkpoints["alpha"] == 501
    assert "ETH" not in loaded_after_bf.memory.recurring_shilled_tickers
    assert "alpha" in loaded_after_bf.channel_baselines
    assert len(notifier.sent_messages) == 1

    run_pipeline(
        mode="daily",
        dry_run=False,
        config=cfg,
        scraper=_FakeScraper(),
        market_fetcher=_FakeMarketFetcher(),
        llm_client=_FakeLLM(),
        state_store=store,
        notifier=notifier,
    )
    loaded_after_daily = store.load()
    assert loaded_after_daily.channel_checkpoints["alpha"] == 502
    assert loaded_after_daily.memory.recurring_shilled_tickers == ["SHILLX"]
    assert loaded_after_daily.verdict_history[-1].bull_ignition_score is not None
    assert len(notifier.sent_messages) == 2
