# Automated Crypto Bull-Run Analysis Pipeline

An automated Python 3.11+ pipeline that scrapes public Telegram crypto channels without API credentials, cross-checks social narratives against objective market ground truth (spot volume, `BTC.D`, perp funding/open interest, stablecoin supply, DeFi TVL, Fear & Greed, and altcoin price/volume), and delivers a daily bull-run verdict to Telegram at **08:00 IST (`02:30 UTC`)**.

> **Migration Note:** The previous Market Radar V4.7 codebase is preserved in Git history under the `legacy-v1` tag.

---

## Architecture

- `config.yaml` — Default channels, 60-day backfill window, scraping delays, free market API endpoints, and LLM settings (overridden by environment variables).
- `knowledge/bull_run_playbook.md` — Historical bull-run playbook (2017, 2020–21, 2023–24) covering liquidity inflection points, capital rotation, spot vs. perp trap detection, and Telegram manipulation signatures.
- `src/telegram_ingest.py` — Zero-auth public scraper (`httpx` + `BeautifulSoup4`) against `https://t.me/s/{channel}` with `?before=` backfill pagination and incremental `last_message_id` daily fetching.
- `src/market_data.py` — Free, unauthenticated market ground truth via CoinGecko, Binance Spot & Futures, DefiLlama (stablecoins & TVL), and Alternative.me (Fear & Greed) with graceful per-source fallbacks.
- `src/llm_client.py` — OpenAI-compatible Chat Completions wrapper (`OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)`) with Pydantic schema validation, automatic `json_schema` fallback + markdown-fence stripping, and exponential backoff on 429/5xx errors.
- `src/analyzer.py` — Weekly backfill synthesis + 3-pass daily pipeline (Pass 1 fast filter → altcoin market lookup → Pass 2 reality check → Pass 3 bull-run verdict & rolling state update).
- `src/state_store.py` — Rolling 60-day JSON state persisted in `data/state.json`.
- `src/notifier.py` — Archives Markdown reports to `reports/YYYY-MM-DD.md` (and `reports/initial_baseline.md`) and delivers chunked Telegram messages under the 4,096-character limit.
- `src/main.py` — CLI supporting `--mode backfill`, `--mode daily`, and `--dry-run`.

---

## GitHub Actions Secrets Setup

### Reused Existing Secrets (No Action Needed)
These secrets were detected in the repository's previous workflow and are **already reused automatically** by both new workflows:
- `TELEGRAM_TOKEN`
- `TELEGRAM_CHAT_ID`

### New Secrets to Add
Add the following secrets under **Settings → Secrets and variables → Actions → New repository secret**:

| Secret Name | Required | Description |
|---|---|---|
| `LLM_BASE_URL` | Yes | Custom OpenAI-compatible API base URL (e.g., `https://api.provider.com/v1`) |
| `LLM_API_KEY` | Yes | API key for your custom LLM provider |
| `LLM_FAST_MODEL` | Yes | Model ID for 60-day weekly chunking & Pass 1 filtering |
| `LLM_REASONING_MODEL` | Yes | Model ID for Pass 2 reality check, Pass 3 verdict & baseline synthesis (can match `LLM_FAST_MODEL`) |
| `TELEGRAM_CHANNELS` | Yes | Comma-separated public Telegram channel handles (leading `@` is stripped automatically, e.g., `channel_1,channel_2,channel_3,channel_4`) |
| `LLM_EXTRA_HEADERS_JSON` | Optional | JSON object of extra HTTP headers for your LLM gateway (e.g., `{"HTTP-Referer": "https://example.com"}`) |

---

## Workflows

1. **First-time Baseline (`Backfill`):** Go to **Actions → Crypto Pipeline Backfill (60-Day Baseline) → Run workflow**. This scrapes the last 60 days, builds `data/state.json` and `reports/initial_baseline.md`, sends a summary to Telegram, and commits them to `main`.
2. **Daily Briefing (`Daily Analysis`):** Runs automatically every day at **08:00 IST (`30 2 * * *` UTC)** and supports manual `workflow_dispatch` (including a `dry_run` toggle).

---

## Local Development & Testing

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env       # Fill in your local env vars

# Dry-run daily analysis without writing state/reports or sending Telegram alerts
python -m src.main --mode daily --dry-run

# Run unit tests
pytest -v
```
