"""Markdown report formatting, archiving, and Telegram Bot API delivery with 4,096-char chunking."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from src.analyzer import BaselineSynthesis, DailyReport
from src.market_data import MarketSnapshot

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


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "N/A"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}%"


def format_daily_report_markdown(report: DailyReport, snapshot: MarketSnapshot) -> str:
    """Render the DailyReport + MarketSnapshot into a clean Markdown briefing."""
    m = snapshot.macro
    lev = snapshot.leverage
    liq = snapshot.liquidity
    sent = snapshot.sentiment

    lines: list[str] = [
        f"# Daily Crypto Bull-Run Briefing — {report.report_date_utc}",
        "",
        "## 1. Macro Bull Run Verdict",
        f"- **Phase:** `{report.macro_phase}`",
        f"- **Confidence:** `{report.confidence_score}%`",
        f"- **Is this the start of a bull run?** {report.bull_run_direct_answer}",
        "",
        "### Market Ground Truth",
        f"- **BTC:** {_fmt_usd(m.btc.price_usd)} (24h: {_fmt_pct(m.btc.change_24h_pct)} | 7d: {_fmt_pct(m.btc.change_7d_pct)} | Vol: {_fmt_usd(m.btc.volume_24h_usd)})",
        f"- **ETH:** {_fmt_usd(m.eth.price_usd)} (24h: {_fmt_pct(m.eth.change_24h_pct)} | 7d: {_fmt_pct(m.eth.change_7d_pct)} | Vol: {_fmt_usd(m.eth.volume_24h_usd)})",
        f"- **Total MCap / BTC.D:** {_fmt_usd(m.total_market_cap_usd)} ({_fmt_pct(m.total_market_cap_change_24h_pct)} 24h) | BTC.D: {_fmt_pct(m.btc_dominance_pct)}",
        f"- **Perp Funding (8h) & OI:** BTC {_fmt_pct(lev.btc.funding_rate_8h_pct)} (OI {_fmt_usd(lev.btc.open_interest_usd)}) | ETH {_fmt_pct(lev.eth.funding_rate_8h_pct)} (OI {_fmt_usd(lev.eth.open_interest_usd)})",
        f"- **Liquidity (7d):** Stablecoins {_fmt_usd(liq.total_stablecoin_mcap_usd)} ({_fmt_pct(liq.stablecoin_7d_change_pct)}) | DeFi TVL {_fmt_usd(liq.defi_tvl_usd)} ({_fmt_pct(liq.defi_tvl_7d_change_pct)})",
        f"- **Fear & Greed:** {sent.fear_greed_value if sent.fear_greed_value is not None else 'N/A'} ({sent.fear_greed_label or 'N/A'}, 7d avg: {sent.fear_greed_7d_avg if sent.fear_greed_7d_avg is not None else 'N/A'})",
        "",
        "## 2. Altcoin Rotation & Narrative Pulse",
        f"- **Rotation Stage:** {report.altcoin_rotation_stage}",
        f"- **Real Volume Sectors:** {', '.join(report.real_volume_sectors) if report.real_volume_sectors else 'None confirmed'}",
        f"- **Pure Telegram Hype Sectors:** {', '.join(report.pure_hype_sectors) if report.pure_hype_sectors else 'None flagged'}",
        f"- **Summary:** {report.altcoin_pulse_summary}",
        "",
        "## 3. Verified Signals",
    ]
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
) -> str:
    """Render the initial 60-day backfill baseline report into Markdown."""
    total_msgs = sum(message_counts.values())
    counts_str = ", ".join(f"@{ch}: {cnt}" for ch, cnt in message_counts.items()) or "none"

    lines: list[str] = [
        f"# Initial {backfill_days}-Day Crypto Baseline Report",
        "",
        f"- **Baseline Phase:** `{baseline.baseline_phase}`",
        f"- **Confidence:** `{baseline.confidence_score}%`",
        f"- **Messages Analyzed:** {total_msgs} ({counts_str})",
        f"- **BTC / ETH Reference:** {_fmt_usd(snapshot.macro.btc.price_usd)} / {_fmt_usd(snapshot.macro.eth.price_usd)} | BTC.D: {_fmt_pct(snapshot.macro.btc_dominance_pct)}",
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
    lines.extend(f"- {w}" for w in (baseline.key_watchpoints or ["Monitor spot volume & stablecoin inflows."]))

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
