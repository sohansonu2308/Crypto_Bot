"""Tests for configuration loading, env overrides, and Telegram HTML scraping/pagination."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from src.config import TelegramConfig, load_config, normalize_channel_name
from src.telegram_ingest import TelegramScraper, parse_channel_page, parse_views_count


def _build_tg_html(channel: str, posts: list[dict]) -> str:
    blocks = []
    for p in posts:
        fwd_html = (
            f'<a class="tgme_widget_message_forwarded_from_name">{p["fwd"]}</a>'
            if p.get("fwd")
            else ""
        )
        media_html = '<a class="tgme_widget_message_photo_wrap"></a>' if p.get("photo") else ""
        views_html = (
            f'<span class="tgme_widget_message_views">{p["views"]}</span>'
            if p.get("views")
            else ""
        )
        blocks.append(
            f"""
            <div class="tgme_widget_message" data-post="{channel}/{p['id']}">
              {fwd_html}
              {media_html}
              <div class="tgme_widget_message_text">{p['text']}</div>
              {views_html}
              <time datetime="{p['ts']}"></time>
            </div>
            """
        )
    return "<html><body>" + "\n".join(blocks) + "</body></html>"


def test_config_env_overrides_and_channel_normalization(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "telegram:\n  channels:\n    - default_ch\n  backfill_days: 60\nllm:\n  fast_model: yaml-fast\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TELEGRAM_TOKEN", "tok-123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "99999")
    monkeypatch.setenv("TELEGRAM_CHANNELS", " @AlphaOne, https://t.me/s/BetaTwo , alphaone, @Gamma_3 ")
    monkeypatch.setenv("LLM_BASE_URL", "https://custom.gateway.example/v1")
    monkeypatch.setenv("LLM_API_KEY", "sk-custom-key")
    monkeypatch.setenv("LLM_REASONING_MODEL", "gpt-6")
    monkeypatch.delenv("LLM_FAST_MODEL", raising=False)
    monkeypatch.setenv("LLM_EXTRA_HEADERS_JSON", '{"X-Custom-Header": "abc"}')

    cfg = load_config(cfg_file, load_dotenv_file=False)
    assert cfg.telegram.bot_token == "tok-123"
    assert cfg.telegram.chat_id == "99999"
    assert cfg.telegram.channels == ["AlphaOne", "BetaTwo", "Gamma_3"]
    assert cfg.llm.base_url == "https://custom.gateway.example/v1"
    assert cfg.llm.api_key == "sk-custom-key"
    assert cfg.llm.reasoning_model == "gpt-6"
    assert cfg.llm.fast_model == "gpt-6"
    assert cfg.llm.extra_headers == {"X-Custom-Header": "abc"}


def test_parse_views_and_channel_html():
    assert parse_views_count("12.4K") == 12_400
    assert parse_views_count("1.5M") == 1_500_000
    assert parse_views_count("845") == 845
    assert normalize_channel_name("@my_channel") == "my_channel"

    html = _build_tg_html(
        "crypto_alpha",
        [
            {
                "id": 102,
                "ts": "2026-11-24T01:00:00+00:00",
                "text": "BTC breakout!<br/>Check $SOL volume.",
                "fwd": "WhaleAlert",
                "views": "15.2K",
                "photo": True,
            },
            {
                "id": 101,
                "ts": "2026-11-23T22:00:00Z",
                "text": "Earlier post",
                "views": "900",
            },
        ],
    )
    msgs = parse_channel_page(html, "@crypto_alpha")
    assert [m.message_id for m in msgs] == [101, 102]
    assert msgs[1].forwarded_from == "WhaleAlert"
    assert msgs[1].views == 15_200
    assert msgs[1].has_media is True
    assert "photo" in msgs[1].media_types
    assert "BTC breakout!\nCheck $SOL volume." == msgs[1].text


def test_telegram_backfill_pagination_and_daily_checkpoints():
    page_latest = _build_tg_html(
        "alpha",
        [
            {"id": 203, "ts": "2026-11-24T01:00:00+00:00", "text": "Latest msg"},
            {"id": 202, "ts": "2026-11-20T01:00:00+00:00", "text": "4 days ago"},
        ],
    )
    page_before_202 = _build_tg_html(
        "alpha",
        [
            {"id": 201, "ts": "2026-10-15T01:00:00+00:00", "text": "Inside 60d window"},
            {"id": 200, "ts": "2026-08-01T01:00:00+00:00", "text": "Older than 60d cutoff"},
        ],
    )

    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if request.url.params.get("before") == "202":
            return httpx.Response(200, text=page_before_202)
        return httpx.Response(200, text=page_latest)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    scraper = TelegramScraper(
        TelegramConfig(channels=["alpha"], backfill_days=60, request_delay_min_seconds=0, request_delay_max_seconds=0),
        http_client=client,
        sleep_fn=lambda _: None,
    )

    now = datetime(2026, 11, 24, 2, 0, tzinfo=timezone.utc)
    backfill = scraper.fetch_backfill(now_utc=now)
    assert [m.message_id for m in backfill["alpha"]] == [201, 202, 203]
    assert any("before=202" in u for u in seen_urls)

    # Daily mode with checkpoint 202 should only return message 203
    daily = scraper.fetch_daily(checkpoints={"alpha": 202}, now_utc=now)
    assert [m.message_id for m in daily["alpha"]] == [203]
