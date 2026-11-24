# Automated Crypto Bull-Run Analysis Pipeline (v2 — Whale Edition)

An institutional-grade Python 3.11+ crypto analysis pipeline that computes a **deterministic `0–100` Bull-Ignition Scorecard** from multi-timeframe spot structure, pure fiat liquidity velocity (`USDT + USDC`), Coinbase Spot Premium, `USDT/USD` peg flows, geo-block-free perp/options positioning (Hyperliquid + Deribit), and contrarian Telegram retail apathy—delivering a daily briefing to Telegram at **08:00 IST (`02:30 UTC`)**.

> **Migration Note:** The previous Market Radar V4.7 codebase is preserved in Git history under the `legacy-v1` tag.

---

## v2 Institutional Architecture

- `src/market_data.py` — Zero-auth, US-GitHub-Actions-compatible ground truth:
  - **200D Spot Structure:** Kraken + Coinbase daily OHLCV (`20D / 50D / 200D SMA`, `200D` slope, `20D` Donchian breakout percentile, Higher-Low check, Spot Volume vs. `20D` average, `ETH/BTC` 14d/30d RS).
  - **Institutional Fiat Tells:** **Coinbase Spot Premium** (`Coinbase BTC-USD` vs. offshore `BTC-USDT` in bps) + **Kraken `USDT/USD` Peg Deviation** (real-time fiat redemption vs. minting thermometer).
  - **Derivatives & Options (No US 451 Geo-Block):** **Hyperliquid** (`funding`, `openInterest`, `perp-to-oracle basis` across 150+ perps) + **Deribit** (`DVOL` implied vol & BTC Options Put/Call OI ratio).
  - **Pure Fiat Liquidity & Macro:** DefiLlama **`USDT + USDC` pure fiat supply** (7d, 30d, and **2nd-derivative WoW acceleration**, stripped of `USDe` noise) + **US Fed Net Liquidity (`WALCL - TGA - RRP`)** via St. Louis Fed FRED CSVs.
  - **Benchmark Breadth:** Fixed 12-asset Liquid Majors Basket (`SOL, BNB, XRP, ADA, DOGE, AVAX, LINK, SUI, TON, AAVE, TAO, ONDO`) evaluated independently of Telegram micro-cap shills.
- `src/telegram_ingest.py` — Zero-auth `t.me/s/{channel}` scraper + quantitative **Retail Views Apathy Ratio** (`median views/post` vs. 60d baseline) and **VIP/Hindsight Promo Spam** detection.
- `src/quant_scoring.py` — Deterministic `0–100` **Bull-Ignition Scorecard** across 5 weighted pillars (`25 + 25 + 20 + 15 + 15`) and 6 cycle regimes (`Markdown / Liquidity Drain`, `Capitulation / Bottoming Watch`, `Stealth Accumulation`, `Early Bull Ignition`, `Confirmed Bull Trend`, `Bull Trap / Distribution`).
- `src/analyzer.py` — Weekly backfill synthesis + 3-pass daily LLM pipeline anchored to the deterministic scorecard and `knowledge/bull_run_playbook.md`.
- `src/state_store.py` & `src/notifier.py` — Atomic `data/state.json` persistence (excluding majors from shill lists) and 4,096-char chunked Telegram delivery.

---

## GitHub Actions Secrets Setup

### Reused Existing Secrets (No Action Needed)
- `TELEGRAM_TOKEN`
- `TELEGRAM_CHAT_ID`

### New Secrets
| Secret Name | Required | Description |
|---|---|---|
| `LLM_BASE_URL` | Yes | Custom OpenAI-compatible API base URL (e.g., `https://api.provider.com/v1`) |
| `LLM_API_KEY` | Yes | API key for your custom LLM provider |
| `LLM_FAST_MODEL` | Yes | Model ID for 60-day weekly chunking & Pass 1 filtering |
| `LLM_REASONING_MODEL` | Yes | Model ID for Pass 2 reality check, Pass 3 verdict & baseline synthesis |
| `TELEGRAM_CHANNELS` | Yes | Comma-separated public Telegram channel handles |
| `LLM_EXTRA_HEADERS_JSON` | Optional | JSON object of extra HTTP headers for your LLM gateway |

---

## Local Development & Testing

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Run unit tests
pytest -v
```
