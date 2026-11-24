"""CLI entrypoint for the whale-grade automated crypto analysis pipeline."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from src.analyzer import CryptoAnalyzer
from src.config import AppConfig, load_config
from src.llm_client import LLMClient
from src.market_data import MarketDataFetcher
from src.notifier import (
    TelegramNotifier,
    format_baseline_report_markdown,
    format_daily_report_markdown,
)
from src.quant_scoring import compute_bull_ignition_scorecard
from src.state_store import StateStore, VerdictRecord
from src.telegram_ingest import (
    TelegramScraper,
    compute_channel_baselines,
    compute_daily_telemetry,
)

logger = logging.getLogger(__name__)


def _latest_ids_by_channel(messages_by_channel: dict[str, list]) -> dict[str, int]:
    latest: dict[str, int] = {}
    for ch, msgs in messages_by_channel.items():
        if msgs:
            latest[ch] = max(m.message_id for m in msgs)
    return latest


def run_pipeline(
    *,
    mode: str,
    dry_run: bool = False,
    config: AppConfig | None = None,
    config_path: str = "config.yaml",
    scraper: TelegramScraper | None = None,
    market_fetcher: MarketDataFetcher | None = None,
    llm_client: LLMClient | None = None,
    state_store: StateStore | None = None,
    notifier: TelegramNotifier | None = None,
) -> str:
    """Execute either `backfill` or `daily` mode and return the rendered Markdown report."""
    cfg = config or load_config(config_path=config_path)
    store = state_store or StateStore(cfg.paths.state_file)
    state = store.load()

    owns_scraper = scraper is None
    owns_market = market_fetcher is None
    owns_notifier = notifier is None

    tg_scraper = scraper or TelegramScraper(cfg.telegram)
    mkt_fetcher = market_fetcher or MarketDataFetcher(cfg.market_data)
    llm = llm_client or LLMClient(cfg.llm)
    analyzer = CryptoAnalyzer(cfg, llm)
    tg_notifier = notifier or TelegramNotifier(
        bot_token=cfg.telegram.bot_token,
        chat_id=cfg.telegram.chat_id,
        reports_dir=cfg.paths.reports_dir,
    )

    try:
        if mode == "backfill":
            logger.info(
                "Starting backfill mode (%d days) for channels: %s",
                cfg.telegram.backfill_days,
                cfg.telegram.channels,
            )
            messages_by_channel = tg_scraper.fetch_backfill(
                channels=cfg.telegram.channels,
                days=cfg.telegram.backfill_days,
            )
            baselines = compute_channel_baselines(
                messages_by_channel,
                backfill_days=cfg.telegram.backfill_days,
            )
            snapshot = mkt_fetcher.fetch_snapshot()
            scorecard = compute_bull_ignition_scorecard(snapshot)
            baseline = analyzer.run_backfill(
                messages_by_channel=messages_by_channel,
                market_snapshot=snapshot,
                scorecard=scorecard,
            )

            message_counts = {ch: len(msgs) for ch, msgs in messages_by_channel.items()}
            report_md = format_baseline_report_markdown(
                baseline=baseline,
                snapshot=snapshot,
                backfill_days=cfg.telegram.backfill_days,
                message_counts=message_counts,
                scorecard=scorecard,
            )

            state.initialized = True
            store.update_checkpoints(state, _latest_ids_by_channel(messages_by_channel))
            store.update_channel_baselines(state, baselines)
            store.merge_memory(
                state,
                macro_themes=baseline.macro_themes,
                active_altcoin_narratives=baseline.active_altcoin_narratives,
                recurring_shilled_tickers=baseline.recurring_shilled_tickers,
                channel_credibility_notes=baseline.channel_credibility_notes,
                replace=True,
            )
            store.append_verdict(
                state,
                VerdictRecord(
                    date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    phase=baseline.baseline_phase,
                    confidence=baseline.confidence_score,
                    bull_ignition_score=scorecard.total_score,
                    direct_answer=baseline.executive_summary[:280],
                ),
            )

            if dry_run:
                logger.info("[DRY RUN] Skipping state save, report archive, and Telegram send.")
                print(report_md)
                return report_md

            store.save(state)
            tg_notifier.archive_report("initial_baseline.md", report_md)
            if cfg.telegram.bot_token and cfg.telegram.chat_id:
                tg_notifier.send_message(report_md)
            else:
                logger.warning("TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set; skipping Telegram notification.")
            return report_md

        if mode == "daily":
            logger.info("Starting daily mode for channels: %s", cfg.telegram.channels)
            messages_by_channel = tg_scraper.fetch_daily(
                checkpoints=state.channel_checkpoints,
                channels=cfg.telegram.channels,
            )
            telemetry = compute_daily_telemetry(
                messages_by_channel=messages_by_channel,
                baselines=state.channel_baselines,
            )

            # Pass 1: filter, flag promo spam, and extract top speculative altcoin tickers
            pass1 = analyzer.run_daily_pass1(
                messages_by_channel=messages_by_channel,
                state=state,
                telemetry=telemetry,
            )

            # Fetch institutional market ground truth + Pass 1 altcoin metrics
            snapshot = mkt_fetcher.fetch_snapshot(altcoin_tickers=pass1.top_altcoin_tickers)

            # Compute deterministic 0-100 Bull-Ignition Scorecard before LLM reality check
            scorecard = compute_bull_ignition_scorecard(snapshot=snapshot, telemetry=telemetry)

            # Pass 2: reality check vs institutional flows, structure & playbook
            pass2 = analyzer.run_daily_pass2(
                pass1=pass1,
                market_snapshot=snapshot,
                state=state,
                scorecard=scorecard,
                telemetry=telemetry,
            )

            # Pass 3: final daily briefing & rolling state update
            today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            report = analyzer.run_daily_pass3(
                pass1=pass1,
                pass2=pass2,
                market_snapshot=snapshot,
                state=state,
                scorecard=scorecard,
                telemetry=telemetry,
                report_date_utc=today_utc,
            )
            report_md = format_daily_report_markdown(
                report=report,
                snapshot=snapshot,
                scorecard=scorecard,
                telemetry=telemetry,
            )

            store.update_checkpoints(state, _latest_ids_by_channel(messages_by_channel))
            store.merge_memory(
                state,
                macro_themes=report.rolling_state_update.macro_themes,
                active_altcoin_narratives=report.rolling_state_update.active_altcoin_narratives,
                recurring_shilled_tickers=report.rolling_state_update.recurring_shilled_tickers,
                channel_credibility_notes=report.rolling_state_update.channel_credibility_notes,
                replace=False,
            )
            store.append_verdict(
                state,
                VerdictRecord(
                    date=report.report_date_utc,
                    phase=report.macro_phase,
                    confidence=report.confidence_score,
                    bull_ignition_score=scorecard.total_score,
                    direct_answer=report.bull_run_direct_answer,
                ),
            )

            if dry_run:
                logger.info("[DRY RUN] Skipping state save, report archive, and Telegram send.")
                print(report_md)
                return report_md

            store.save(state)
            tg_notifier.archive_report(f"{report.report_date_utc}.md", report_md)
            if cfg.telegram.bot_token and cfg.telegram.chat_id:
                tg_notifier.send_message(report_md)
            else:
                logger.warning("TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set; skipping Telegram notification.")
            return report_md

        raise ValueError(f"Unsupported mode: {mode!r}. Expected 'backfill' or 'daily'.")
    finally:
        if owns_scraper:
            tg_scraper.close()
        if owns_market:
            mkt_fetcher.close()
        if owns_notifier:
            tg_notifier.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Automated Crypto Telegram & Institutional Market Analysis Pipeline")
    parser.add_argument(
        "--mode",
        choices=("backfill", "daily"),
        required=True,
        help="Pipeline mode: 'backfill' (60-day baseline) or 'daily' (incremental daily report).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run ingestion and LLM analysis without saving state, archiving reports, or sending Telegram messages.",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to YAML configuration file (default: config.yaml).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Logging verbosity (default: INFO).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    run_pipeline(mode=args.mode, dry_run=args.dry_run, config_path=args.config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
