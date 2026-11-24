"""Zero-auth public Telegram channel scraper using https://t.me/s/{channel}."""

from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from src.config import TelegramConfig, normalize_channel_name

logger = logging.getLogger(__name__)

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


class TelegramMessage(BaseModel):
    channel: str
    message_id: int
    timestamp_utc: datetime
    text: str = ""
    forwarded_from: str | None = None
    views: int | None = None
    views_raw: str | None = None
    has_media: bool = False
    media_types: list[str] = Field(default_factory=list)


def parse_views_count(raw: str | None) -> int | None:
    """Convert Telegram view strings like '12.4K', '1.2M', '845' into an integer."""
    if not raw:
        return None
    cleaned = raw.strip().upper().replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMB])?", cleaned)
    if not match:
        return None
    value = float(match.group(1))
    suffix = match.group(2)
    multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suffix or "", 1)
    return int(round(value * multiplier))


def parse_channel_page(html: str, channel: str) -> list[TelegramMessage]:
    """Parse messages from a Telegram public web preview page (`t.me/s/{channel}`)."""
    soup = BeautifulSoup(html, "html.parser")
    messages: list[TelegramMessage] = []
    normalized_channel = normalize_channel_name(channel)

    for widget in soup.select("div.tgme_widget_message"):
        data_post = widget.get("data-post")
        if not data_post or "/" not in str(data_post):
            continue

        _, raw_id = str(data_post).rsplit("/", 1)
        if not raw_id.isdigit():
            continue
        message_id = int(raw_id)

        time_tag = widget.select_one("time[datetime]")
        if not time_tag or not time_tag.get("datetime"):
            continue
        try:
            dt = datetime.fromisoformat(str(time_tag["datetime"]).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
        except ValueError:
            continue

        text_el = widget.select_one("div.tgme_widget_message_text")
        if text_el is not None:
            for br in text_el.find_all("br"):
                br.replace_with("\n")
            text = text_el.get_text(separator="").strip()
        else:
            text = ""

        fwd_el = widget.select_one("a.tgme_widget_message_forwarded_from_name, span.tgme_widget_message_forwarded_from_name")
        forwarded_from = fwd_el.get_text(strip=True) if fwd_el else None

        views_el = widget.select_one("span.tgme_widget_message_views")
        views_raw = views_el.get_text(strip=True) if views_el else None
        views = parse_views_count(views_raw)

        media_types: list[str] = []
        media_selectors = {
            "photo": ".tgme_widget_message_photo_wrap",
            "video": ".tgme_widget_message_video_player, .tgme_widget_message_roundvideo_player",
            "document": ".tgme_widget_message_document",
            "voice": ".tgme_widget_message_voice_player",
            "sticker": ".tgme_widget_message_sticker",
            "poll": ".tgme_widget_message_poll",
            "link_preview": ".tgme_widget_message_link_preview",
        }
        for media_name, selector in media_selectors.items():
            if widget.select_one(selector) is not None:
                media_types.append(media_name)

        messages.append(
            TelegramMessage(
                channel=normalized_channel,
                message_id=message_id,
                timestamp_utc=dt,
                text=text,
                forwarded_from=forwarded_from,
                views=views,
                views_raw=views_raw,
                has_media=bool(media_types),
                media_types=media_types,
            )
        )

    messages.sort(key=lambda m: m.message_id)
    return messages


class TelegramScraper:
    """Scrapes public Telegram channels without API credentials via `t.me/s/{channel}`."""

    def __init__(
        self,
        config: TelegramConfig,
        http_client: httpx.Client | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._owns_client = http_client is None
        self.http_client = http_client or httpx.Client(
            headers=_DEFAULT_HEADERS,
            timeout=config.request_timeout_seconds,
            follow_redirects=True,
        )
        self._sleep = sleep_fn

    def close(self) -> None:
        if self._owns_client:
            self.http_client.close()

    def __enter__(self) -> TelegramScraper:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _polite_delay(self) -> None:
        low = max(0.0, self.config.request_delay_min_seconds)
        high = max(low, self.config.request_delay_max_seconds)
        if high > 0:
            self._sleep(random.uniform(low, high))

    def _fetch_page(self, channel: str, before_id: int | None = None) -> list[TelegramMessage]:
        url = f"https://t.me/s/{normalize_channel_name(channel)}"
        params = {"before": str(before_id)} if before_id is not None else None

        for attempt in range(3):
            try:
                resp = self.http_client.get(url, params=params)
                if resp.status_code == 200:
                    return parse_channel_page(resp.text, channel)
                if resp.status_code in (429, 500, 502, 503, 504):
                    self._sleep(2.0 * (attempt + 1))
                    continue
                logger.warning("Unexpected HTTP %s fetching %s", resp.status_code, url)
                return []
            except httpx.HTTPError as exc:
                logger.warning("HTTP error fetching %s (attempt %d): %s", url, attempt + 1, exc)
                self._sleep(1.5 * (attempt + 1))
        return []

    def fetch_backfill(
        self,
        channels: list[str] | None = None,
        days: int | None = None,
        now_utc: datetime | None = None,
    ) -> dict[str, list[TelegramMessage]]:
        """Paginate backward via `?before={oldest_id}` until `days` of history are collected."""
        target_channels = channels if channels is not None else self.config.channels
        lookback_days = days if days is not None else self.config.backfill_days
        reference_now = now_utc or datetime.now(timezone.utc)
        cutoff = reference_now - timedelta(days=lookback_days)

        results: dict[str, list[TelegramMessage]] = {}
        for idx, raw_ch in enumerate(target_channels):
            channel = normalize_channel_name(raw_ch)
            if idx > 0:
                self._polite_delay()

            collected: dict[int, TelegramMessage] = {}
            before_id: int | None = None

            for page_num in range(self.config.max_pages_per_channel):
                if page_num > 0:
                    self._polite_delay()

                page_messages = self._fetch_page(channel, before_id=before_id)
                if not page_messages:
                    break

                oldest_on_page = page_messages[0].message_id
                new_added = 0
                reached_cutoff = False

                for msg in page_messages:
                    if msg.timestamp_utc < cutoff:
                        reached_cutoff = True
                        continue
                    if msg.message_id not in collected:
                        collected[msg.message_id] = msg
                        new_added += 1

                if reached_cutoff or oldest_on_page <= 1:
                    break
                if before_id is not None and oldest_on_page >= before_id:
                    break
                if new_added == 0 and page_messages[0].timestamp_utc < cutoff:
                    break

                before_id = oldest_on_page

            ordered = sorted(collected.values(), key=lambda m: m.message_id)
            results[channel] = ordered
            logger.info("Backfill '%s': collected %d messages since %s", channel, len(ordered), cutoff.date())

        return results

    def fetch_daily(
        self,
        checkpoints: dict[str, int],
        channels: list[str] | None = None,
        fallback_hours: int = 24,
        now_utc: datetime | None = None,
    ) -> dict[str, list[TelegramMessage]]:
        """Fetch only messages with `message_id > last_message_id` per channel."""
        target_channels = channels if channels is not None else self.config.channels
        reference_now = now_utc or datetime.now(timezone.utc)
        fallback_cutoff = reference_now - timedelta(hours=fallback_hours)

        results: dict[str, list[TelegramMessage]] = {}
        for idx, raw_ch in enumerate(target_channels):
            channel = normalize_channel_name(raw_ch)
            if idx > 0:
                self._polite_delay()

            last_id = checkpoints.get(channel) or checkpoints.get(channel.lower()) or 0
            collected: dict[int, TelegramMessage] = {}
            before_id: int | None = None

            for page_num in range(self.config.max_pages_per_channel):
                if page_num > 0:
                    self._polite_delay()

                page_messages = self._fetch_page(channel, before_id=before_id)
                if not page_messages:
                    break

                oldest_on_page = page_messages[0].message_id
                hit_checkpoint = False

                for msg in page_messages:
                    if last_id > 0:
                        if msg.message_id <= last_id:
                            hit_checkpoint = True
                            continue
                    else:
                        if msg.timestamp_utc < fallback_cutoff:
                            hit_checkpoint = True
                            continue
                    collected[msg.message_id] = msg

                if hit_checkpoint or oldest_on_page <= 1:
                    break
                if before_id is not None and oldest_on_page >= before_id:
                    break
                # If no checkpoint existed yet, limit initial daily catch-up to 3 pages
                if last_id == 0 and page_num >= 2:
                    break

                before_id = oldest_on_page

            ordered = sorted(collected.values(), key=lambda m: m.message_id)
            results[channel] = ordered
            logger.info(
                "Daily '%s': fetched %d new messages (last_message_id=%d)",
                channel,
                len(ordered),
                last_id,
            )

        return results
