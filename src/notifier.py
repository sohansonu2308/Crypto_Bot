"""Markdown report formatting, archiving, and Telegram Bot API delivery with 4,096-char chunking."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from src.analyzer import BaselineSynthesis, DailyReport
from src.market_data import MarketSnapshot
from src.quant_scoring import BullIgnitionScorecard, compute_bull_ignition_scorecard
from src.telegram_ingest import TelegramTelemetrySummary

logger = logging.getLogger(__name__)

TELEGRAM_MAX_MESSAGE_LENGTH = 4096


def _fmt_usd(value: float | None) -> str:
    if value is None:
        return "N/A"
    abs_val = abs(value)
    sign = "-" if value < 0 else ""
    if abs_val >= 1e12:
        return f"{sign}${abs_val / 1e12:.2f}T"
    if abs_val >= 1e9:
        return f"{sign}${abs_val / 1e9:.2f}B"
    if abs_val >= 1e6:
        return f"{sign}${abs_val / 1e6:.2f}M"
    if abs_val >= 1_000:
        return f"{sign}${abs_val:,.2f}"
    return f"{sign}${abs_val:.4f}"


def _fmt_pct(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "N/A"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.{digits}f}%"


def _fmt_bps(value: float | None) -> str:
    if value is None:
        return "N/A"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.1f} bps"


def _format_scorecard_block(card: BullIgnitionScorecard) -> list[str]:
    flags: list[str] = []
    if card.disbelief_rally_flag:
        flags.append("DISBELIEF RALLY SETUP")
    if card.bull_trap_risk_flag:
        flags.append("BULL-TRAP / CROWDED LEVERAGE WARNING")
    flag_str = f" | Flags: `{', '.join(flags)}`" if flags else ""

    return [
        "### Deterministic Bull-Ignition Scorecard",
        f"- **Composite Score:** `{card.total_score} / 100` (Quant Regime: `{card.quant_suggested_phase}`){flag_str}",
        f"  - Fiat Liquidity & Macro: `{card.liquidity_pillar.score:.1f}/{card.liquidity_pillar.max_score:.0f}`",
        f"  - BTC Spot Structure & Demand: `{card.spot_structure_pillar.score:.1f}/{card.spot_structure_pillar.max_score:.0f}`",
        f"  - Derivatives Disbelief & Skew: `{card.derivatives_pillar.score:.1f}/{card.derivatives_pillar.max_score:.0f}`",
        f"  - Breadth & Capital Rotation: `{card.breadth_rotation_pillar.score:.1f}/{card.breadth_rotation_pillar.max_score:.0f}`",
        f"  - Contrarian Sentiment & Apathy: `{card.contrarian_sentiment_pillar.score:.1f}/{card.contrarian_sentiment_pillar.max_score:.0f}`",
    ]


def format_daily_report_markdown(
    report: DailyReport,
    snapshot: MarketSnapshot,
    scorecard: BullIgnitionScorecard | None = None,
    telemetry: TelegramTelemetrySummary | None = None,
) -> str:
    """Render the Whale-Grade DailyReport + Scorecard + MarketSnapshot into Markdown."""
    card = scorecard or compute_bull_ignition_scorecard(snapshot, telemetry)
    m = snapshot.macro
    st = snapshot.structure
    fl = snapshot.spot_flows
    lev = snapshot.leverage
    liq = snapshot.liquidity
    br = snapshot.breadth
    sent = snapshot.sentiment

    lines: list[str] = [
        f"# Daily Crypto Bull-Run Briefing — {report.report_date_utc}",
        "",
        "## 1. Macro Bull Run Verdict",
        f"- **Phase:** `{report.macro_phase}` (LLM Confidence: `{report.confidence_score}%`)",
        f"- **Is this the start of a bull run?** {report.bull_run_direct_answer}",
        "",
        *_format_scorecard_block(card),
        "",
        "### Institutional Market Ground Truth",
        f"- **BTC:** {_fmt_usd(m.btc.price_usd)} (24h: {_fmt_pct(m.btc.change_24h_pct)} | 7d: {_fmt_pct(m.btc.change_7d_pct)}) | **ETH:** {_fmt_usd(m.eth.price_usd)} (`ETH/BTC`: {st.eth_btc_ratio if st.eth_btc_ratio is not None else 'N/A'}, 14d: {_fmt_pct(st.eth_btc_14d_change_pct)})",
        f"- **BTC Trend Structure:** vs 20D SMA {_fmt_pct(st.btc_dist_sma20_pct)} | vs 50D SMA {_fmt_pct(st.btc_dist_sma50_pct)} | vs 200D SMA {_fmt_pct(st.btc_dist_sma200_pct)} | 20D Range Pct: {f'{st.btc_donchian_20d_pct:.0f}%' if st.btc_donchian_20d_pct is not None else 'N/A'} | Spot Vol: {f'{st.spot_volume_20d_ratio:.2f}x' if st.spot_volume_20d_ratio is not None else 'N/A'} 20D avg",
        f"- **US Institutional & Fiat Tells:** Coinbase Spot Premium `{_fmt_bps(fl.coinbase_premium_bps)}` | Kraken `USDT/USD` `{f'${fl.kraken_usdt_usd:.5f}' if fl.kraken_usdt_usd is not None else 'N/A'}` (`{_fmt_bps(fl.usdt_peg_deviation_bps)}`)",
        f"- **Perps & Options ({lev.btc.source}):** BTC 8h Funding `{_fmt_pct(lev.btc.funding_rate_8h_pct, 4)}` | Basis `{_fmt_bps(lev.btc.perp_basis_bps)}` | OI {_fmt_usd(lev.btc.open_interest_usd)} | Deribit DVOL `{lev.options.btc_dvol_index if lev.options.btc_dvol_index is not None else 'N/A'}` | Put/Call OI `{lev.options.btc_put_call_oi_ratio if lev.options.btc_put_call_oi_ratio is not None else 'N/A'}`",
        f"- **Pure Fiat Stables (USDT+USDC):** {_fmt_usd(liq.fiat_stablecoin_mcap_usd)} (7d: {_fmt_pct(liq.fiat_stablecoin_7d_change_pct)} | 30d: {_fmt_pct(liq.fiat_stablecoin_30d_change_pct)} | WoW Accel: {_fmt_pct(liq.fiat_stablecoin_7d_accel_pct)})",
        f"- **Macro & TVL:** US Fed Net Liquidity {f'${liq.fed_net_liquidity_usd_bn:,.0f}B' if liq.fed_net_liquidity_usd_bn is not None else 'N/A'} (4w: {_fmt_pct(liq.fed_net_liquidity_4w_change_pct)}) | DeFi TVL {_fmt_usd(liq.defi_tvl_usd)} (7d: {_fmt_pct(liq.defi_tvl_7d_change_pct)})",
        f"- **Majors Breadth & Sentiment:** BTC.D {_fmt_pct(m.btc_dominance_pct)} | Top-12 Majors Green 7d: `{f'{br.majors_positive_7d_pct:.0f}%' if br.majors_positive_7d_pct is not None else 'N/A'}` | Fear & Greed: `{sent.fear_greed_value if sent.fear_greed_value is not None else 'N/A'}` ({sent.fear_greed_label or 'N/A'}, 7d delta: `{f'{sent.fear_greed_7d_delta:+.0f}' if sent.fear_greed_7d_delta is not None else 'N/A'}`)",
    ]

    if telemetry is not None:
        apathy_str = (
            f"{telemetry.overall_views_apathy_ratio:.2f}x of 60d median"
            if telemetry.overall_views_apathy_ratio is not None
            else "N/A"
        )
        lines.append(
            f"- **Telegram Retail Telemetry:** {telemetry.total_posts} posts | Views Apathy `{apathy_str}` | Promo/Hindsight Spam `{telemetry.overall_promo_spam_ratio * 100:.0f}%`"
        )

    if report.what_must_flip_for_bull_run:
        lines.extend(["", "### What Must Flip Next for Bull Ignition"])
        lines.extend(f"- {item}" for item in report.what_must_flip_for_bull_run)

    lines.extend(
        [
            "",
            "## 2. Altcoin Rotation & Narrative Pulse",
            f"- **Rotation Stage:** {report.altcoin_rotation_stage}",
            f"- **Real Volume Sectors:** {', '.join(report.real_volume_sectors) if report.real_volume_sectors else 'None confirmed'}",
            f"- **Pure Telegram Hype Sectors:** {', '.join(report.pure_hype_sectors) if report.pure_hype_sectors else 'None flagged'}",
            f"- **Summary:** {report.altcoin_pulse_summary}",
            "",
            "## 3. Verified Signals",
        ]
    )
    if report.verified_signals:
        lines.extend(f"- {sig}" for sig in report.verified_signals)
    else:
        lines.append("- No major verified catalysts today.")

    lines.extend(["", "## 4. Manipulation & Fake News Watch"])
    if report.manipulation_watch:
        lines.extend(f"- {item}" for item in report.manipulation_watch)
    else:
        lines.append("- No coordinated manipulation or major unverified rumors flagged today.")

    lines.extend(["", "## 5. Channel Pulse (Narrative vs. Reality)"])
    if report.channel_pulse:
        for ch, summary in report.channel_pulse.items():
            lines.append(f"- **@{ch.lstrip('@')}:** {summary}")
    else:
        lines.append("- No new channel activity recorded.")

    return "\n".join(lines).strip() + "\n"


def format_baseline_report_markdown(
    baseline: BaselineSynthesis,
    snapshot: MarketSnapshot,
    backfill_days: int,
    message_counts: dict[str, int],
    scorecard: BullIgnitionScorecard | None = None,
) -> str:
    """Render the initial 60-day backfill baseline report into Markdown."""
    card = scorecard or compute_bull_ignition_scorecard(snapshot)
    total_msgs = sum(message_counts.values())
    counts_str = ", ".join(f"@{ch}: {cnt}" for ch, cnt in message_counts.items()) or "none"

    lines: list[str] = [
        f"# Initial {backfill_days}-Day Crypto Baseline Report (v2 Whale Edition)",
        "",
        f"- **Baseline Phase:** `{baseline.baseline_phase}`",
        f"- **Confidence:** `{baseline.confidence_score}%`",
        f"- **Messages Analyzed:** {total_msgs} ({counts_str})",
        f"- **BTC / ETH Reference:** {_fmt_usd(snapshot.macro.btc.price_usd)} / {_fmt_usd(snapshot.macro.eth.price_usd)} | BTC.D: {_fmt_pct(snapshot.macro.btc_dominance_pct)}",
        "",
        *_format_scorecard_block(card),
        "",
        "## Executive Summary",
        baseline.executive_summary,
        "",
        "## Dominant Macro Themes",
    ]
    lines.extend(f"- {t}" for t in (baseline.macro_themes or ["None recorded."]))

    lines.extend(["", "## Active Altcoin Narratives"])
    lines.extend(f"- {n}" for n in (baseline.active_altcoin_narratives or ["None recorded."]))

    lines.extend(["", "## Recurring Shilled / High-Noise Tickers"])
    lines.extend(f"- {t}" for t in (baseline.recurring_shilled_tickers or ["None flagged."]))

    lines.extend(["", "## Channel Credibility Notes"])
    if baseline.channel_credibility_notes:
        for ch, note in baseline.channel_credibility_notes.items():
            lines.append(f"- **@{ch.lstrip('@')}:** {note}")
    else:
        lines.append("- No channel notes yet.")

    lines.extend(["", "## Key Watchpoints Going Forward"])
    lines.extend(f"- {w}" for w in (baseline.key_watchpoints or ["Monitor spot volume & pure fiat stablecoin inflows."]))

    return "\n".join(lines).strip() + "\n"


def chunk_telegram_message(text: str, max_length: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> list[str]:
    """Split a long message into chunks strictly <= `max_length` on natural boundaries."""
    if max_length < 1:
        raise ValueError("max_length must be positive.")
    cleaned = text.strip()
    if not cleaned:
        return []
    if len(cleaned) <= max_length:
        return [cleaned]

    chunks: list[str] = []
    remaining = cleaned

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        window = remaining[:max_length]
        split_idx = window.rfind("\n\n")
        if split_idx < max_length // 3:
            split_idx = window.rfind("\n")
        if split_idx < max_length // 3:
            split_idx = window.rfind(" ")
        if split_idx <= 0:
            split_idx = max_length

        chunk = remaining[:split_idx].rstrip()
        if not chunk:
            chunk = remaining[:max_length]
            split_idx = max_length
        chunks.append(chunk)
        remaining = remaining[split_idx:].lstrip()

    return chunks


class TelegramNotifier:
    """Archives Markdown reports and delivers chunked notifications via Telegram Bot API."""

    def __init__(
        self,
        bot_token: str | None,
        chat_id: str | None,
        reports_dir: str | Path = "reports",
        http_client: httpx.Client | None = None,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.reports_dir = Path(reports_dir)
        self._owns_client = http_client is None
        self.http_client = http_client or httpx.Client(timeout=20.0)

    def close(self) -> None:
        if self._owns_client:
            self.http_client.close()

    def __enter__(self) -> TelegramNotifier:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def archive_report(self, filename: str, markdown_content: str) -> Path:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.reports_dir / filename
        out_path.write_text(markdown_content, encoding="utf-8")
        logger.info("Archived report to %s", out_path)
        return out_path

    def send_message(self, text: str) -> list[dict]:
        """Send text to Telegram in <=4,096-character chunks; fallback to plain text on Markdown 400."""
        if not self.bot_token or not self.chat_id:
            raise ValueError("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID are required to send Telegram messages.")

        chunks = chunk_telegram_message(text, max_length=TELEGRAM_MAX_MESSAGE_LENGTH)
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        responses: list[dict] = []

        for idx, chunk in enumerate(chunks, start=1):
            payload = {
                "chat_id": self.chat_id,
                "text": chunk,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }
            resp = self.http_client.post(url, json=payload)
            if resp.status_code == 400:
                logger.warning(
                    "Telegram rejected Markdown for chunk %d/%d; retrying as plain text.",
                    idx,
                    len(chunks),
                )
                payload.pop("parse_mode", None)
                resp = self.http_client.post(url, json=payload)

            resp.raise_for_status()
            responses.append(resp.json())

        logger.info("Sent %d Telegram message chunk(s).", len(responses))
        return responses
