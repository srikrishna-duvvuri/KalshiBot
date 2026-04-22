# Kalshi Trading Bot — Developer Reference

This file is the primary context document for Claude Code sessions working on this project.
Read it in full before making any changes. Cross-reference with `docs/design.md` for
architectural rationale and `docs/api_reference.md` for detailed signatures.

---

## What This Is

An algorithmic trading bot for [Kalshi](https://kalshi.com) prediction markets.
Claude Sonnet is the AI analyst — it receives flagged markets with recent news and
returns calibrated probability estimates. Human-in-loop mode is the default; autopilot
is opt-in via env var or CLI flag.

**Revenue model**: edge × position size, net of Kalshi fees (~7% of winnings) and Claude API costs.

---

## Environment Setup

```bash
conda activate kalshi-bot          # Python 3.11, all deps
conda env create -f environment.yml  # first-time setup

cp .env.example .env               # fill in secrets
```

Required `.env` keys:
```
KALSHI_API_KEY_ID=<from Kalshi account → API Keys>
KALSHI_PRIVATE_KEY_PATH=kalshi-key.key   # RSA-2048 PEM file
ANTHROPIC_API_KEY=<separate key — not this Claude Code session>
KALSHI_USE_DEMO=true               # start here, switch to false for live
AUTOPILOT_MODE=false               # always start false
NEWS_API_KEY=<optional, RSS feeds work without it>
```

---

## File Map

```
kalshi-bot/
│
├── config/settings.py          ← ALL tunable parameters. Single source of truth.
│
├── src/
│   ├── kalshi/
│   │   ├── client.py           ← HTTP + RSA-PSS auth + retry/backoff
│   │   └── markets.py          ← Market list, balance, order placement
│   │
│   ├── signals/
│   │   ├── movement.py         ← Ring-buffer detector; flags price/volume moves
│   │   └── news.py             ← RSS + optional NewsAPI; keyword scoring
│   │
│   ├── claude/
│   │   ├── prompts.py          ← SYSTEM_PROMPT, MarketContext, build_analysis_prompt()
│   │   └── analyzer.py         ← Batched Claude calls, cost tracking, Opportunity builder
│   │
│   ├── sizing/
│   │   └── kelly.py            ← Fee-adjusted half-Kelly formula → KellyResult
│   │
│   ├── calibration/
│   │   └── tracker.py          ← Brier score, bucket accuracy, probability correction
│   │
│   ├── accounting/
│   │   └── ledger.py           ← SQLite (WAL) — opportunities, trades, calls, calibration
│   │
│   └── bot/
│       ├── scanner.py          ← Main loop: poll → detect → analyze → record → [auto-execute]
│       └── executor.py         ← Rich UI: human review → live price check → confirm → order
│
├── scripts/
│   ├── scan.py                 ← Entrypoint. Run this.
│   ├── review.py               ← Human review of pending opportunities
│   ├── settle.py               ← Mark resolved markets; updates P&L + calibration
│   └── report.py               ← Rich terminal dashboard
│
├── tests/                      ← 81 tests, all passing
│   ├── test_kelly.py
│   ├── test_ledger.py
│   ├── test_movement.py
│   ├── test_calibration.py
│   ├── test_prompts.py
│   ├── test_news.py
│   └── test_smoke.py           ← End-to-end lifecycle tests (most important)
│
├── data/kalshi_bot.db          ← SQLite database (gitignored)
├── logs/kalshi_bot.log         ← Rotating log (gitignored)
├── docs/design.md              ← Architecture decisions and rationale
└── docs/api_reference.md       ← Every class and function documented
```

---

## Running the Bot

```bash
# Standard start (human-in-loop, demo API)
python scripts/scan.py

# Force flags
python scripts/scan.py --demo              # force demo.kalshi.co
python scripts/scan.py --once              # one scan cycle then exit
python scripts/scan.py --autopilot         # override AUTOPILOT_MODE env
python scripts/scan.py --bankroll 1000     # skip API balance fetch + interactive prompt

# Review pending opportunities
python scripts/review.py

# Settle a resolved market (required for P&L and calibration)
python scripts/settle.py --ticker FED-25BPS-MAY26 --result yes
python scripts/settle.py --ticker CELTICS-G5 --result no --fee 12.50

# Performance report
python scripts/report.py            # all time
python scripts/report.py --days 30
```

---

## Scan Loop (what happens every 2 minutes)

```
get_active_markets()               # filtered: vol>$5k, price 5-95%, 1-90 days
    ↓
update_market() × N                # feed each market into ring buffer
    ↓
get_flagged_markets()              # ≥5% price move or 2× volume spike
    ↓  (nothing flagged → sleep)
[budget check]                     # skip if daily Claude spend ≥ $2.00
    ↓
news.refresh_cache()               # RSS + NewsAPI, 48h window, deduplicated
    ↓
build_analysis_prompt()            # 1 prompt per batch of 5 markets
    ↓
Claude API call                    # SYSTEM_PROMPT is cached; cost ≈ cache_read + output
    ↓
calculate_kelly()                  # fee-adjusted half-Kelly per opportunity
    ↓
ledger.record_opportunity()        # also inserts into calibration table
    ↓
[autopilot=false] → log only       # human runs review.py separately
[autopilot=true]  → place_order() + record_trade()
```

---

## Data Model (SQLite)

### `opportunities`
| column | type | notes |
|--------|------|-------|
| id | INTEGER PK | |
| market_ticker | TEXT | e.g. `FED-25BPS-MAY26` |
| market_price | REAL | normalized 0–1 |
| claude_probability | REAL | Claude's estimate |
| edge | REAL | `abs(claude_p - market_price)` |
| kelly_fraction | REAL | half-Kelly fraction |
| bet_size_dollars | REAL | kelly_fraction × bankroll, capped at 5% |
| confidence | TEXT | `high`/`medium`/`low` |
| direction | TEXT | `yes`/`no` |
| acted_on | INTEGER | 0 = pending, 1 = traded or skipped |
| claude_call_id | INTEGER | FK → claude_calls |

### `trades`
| column | type | notes |
|--------|------|-------|
| id | INTEGER PK | |
| opportunity_id | INTEGER | FK → opportunities |
| market_ticker | TEXT | copied at record time (denormalized) |
| direction | TEXT | `yes`/`no` |
| contracts | REAL | number of contracts bought |
| entry_price | REAL | NO trades: entry_price IS the NO price (e.g. 0.30, not 0.70) |
| gross_pnl | REAL | set on settlement |
| kalshi_fee | REAL | set on settlement |
| net_pnl | REAL | gross_pnl − kalshi_fee |
| status | TEXT | `open`/`settled` |
| resolved_yes | INTEGER | market outcome: 1=YES resolved, 0=NO resolved |

**CRITICAL**: `entry_price` for NO trades is the NO price directly. A NO trade at 30¢/contract
has `entry_price=0.30`. Win gross: `contracts × (1 − 0.30)`. This is NOT the complement of YES price.

**CRITICAL**: `winning_trades` count uses `gross_pnl > 0`, NOT `resolved_yes == 1`.
A winning NO trade has `resolved_yes=0` but positive `gross_pnl`.

### `calibration`
| column | type | notes |
|--------|------|-------|
| opportunity_id | INTEGER | FK → opportunities (1:1) |
| market_ticker | TEXT | for lookup by settle.py |
| predicted_probability | REAL | Claude's estimate |
| resolved_yes | INTEGER | NULL until market settles |
| brier_contribution | REAL | `(predicted_p − outcome)²` |

**IMPORTANT**: Calibration rows are written by `ledger.record_opportunity()`.
Calibration rows are updated by `CalibrationTracker.record_settlement()`.
`ledger.settle_trade()` does NOT update calibration — that's CalibrationTracker's job.

### `claude_calls`
| column | type | notes |
|--------|------|-------|
| called_at | TEXT | ISO timestamp |
| markets_analyzed | INTEGER | number of markets in batch |
| input/output/cache_write/cache_read tokens | INTEGER | from usage object |
| cost_usd | REAL | computed at call time |
| running_daily_cost | REAL | cumulative for that day |

---

## Kelly Sizing Formula

```python
# YES bet (claude_p > market_price):
b = (1 - market_price) * (1 - fee_rate) / market_price
p = claude_p

# NO bet (claude_p < market_price):
b = market_price * (1 - fee_rate) / (1 - market_price)
p = 1 - claude_p

kelly_f = p - (1 - p) / b
half_kelly_f = kelly_f / 2
bet_dollars = min(half_kelly_f × bankroll, 0.05 × bankroll)
is_worthwhile = half_kelly_f >= min_edge_to_surface (default 0.05)
```

---

## Kalshi API

Base URLs:
- Demo: `https://demo-api.kalshi.co`
- Live: `https://trading-api.kalshi.com`

Auth headers (re-signed per request, or after 429):
```
KALSHI-ACCESS-KEY: <api_key_id>
KALSHI-ACCESS-TIMESTAMP: <unix_ms>
KALSHI-ACCESS-SIGNATURE: base64(RSA-PSS-SHA256("{timestamp_ms}{METHOD}{path_no_query}"))
```

**Signing rule**: path MUST NOT include query string. Strip with `urlparse(path).path`.

Rate limits: 20 reads/sec, 10 writes/sec (leaky bucket, most requests cost 10 tokens).
Retry strategy: exponential backoff starting at 1s, cap 60s, max 5 attempts.

Key endpoints:
- `GET /trade-api/v2/markets?status=open&limit=200` — paginated market list
- `GET /trade-api/v2/markets/{ticker}` — single market
- `GET /trade-api/v2/portfolio/balance` — returns cents, divide by 100
- `POST /trade-api/v2/portfolio/orders` — place limit order

Price fields from API may be integers (0–99) or floats (0.0–1.0). Always pass through
`_parse_price()`: `val / 100 if val > 1.0 else val`.

---

## Claude API Integration

Uses **prompt caching** on the system prompt:
```python
system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
```
Cache TTL = 5 minutes. Within a session, repeated calls pay cache_read ($0.30/MTok) not input ($3.00/MTok).

Usage object fields:
- `usage.input_tokens` — regular input
- `usage.output_tokens` — output
- `usage.cache_creation_input_tokens` — first write to cache
- `usage.cache_read_input_tokens` — cache hits

Cost calculation in `analyzer.py:_calculate_cost()`.

Daily budget enforcement: checked in both `ClaudeAnalyzer.analyze_markets()` (in-memory counter)
and `scanner._scan_cycle()` (from ledger, survives restarts).

---

## Calibration System

**Brier score** = mean((predicted_p − outcome)²) across all settled predictions.
- 0.0 = perfect
- 0.25 = random (coin flip)
- 1.0 = maximally wrong

**Bucket correction** (applied after ≥10 settled predictions):
```python
corrected = 0.7 * raw_probability + 0.3 * bucket_actual_rate
```
Buckets: 0-20%, 20-40%, 40-60%, 60-80%, 80-100%.
Only applied if bucket has ≥3 samples.

Called via `CalibrationTracker.get_correction_factor(prob)` — not yet wired into the main
scan loop (planned future enhancement).

---

## Key Configuration (`config/settings.py`)

| Parameter | Default | Notes |
|-----------|---------|-------|
| `poll_interval_seconds` | 120 | 2-minute market polling |
| `news_poll_interval_seconds` | 300 | 5-minute news refresh |
| `claude_daily_budget_usd` | 2.00 | Hard cap |
| `max_markets_per_claude_call` | 5 | Batch size |
| `claude_model` | `claude-sonnet-4-6` | Update here when upgrading |
| `max_position_pct` | 0.05 | 5% bankroll cap |
| `kalshi_fee_rate` | 0.07 | ~7% on winning side. Verify at kalshi.com |
| `min_edge_to_surface` | 0.05 | Show in review |
| `min_edge_to_execute` | 0.08 | Auto-execute threshold |
| `price_move_threshold` | 0.05 | 5% move to flag market |
| `volume_spike_multiplier` | 2.0 | 2× avg volume to flag |
| `market_cooldown_seconds` | 1800 | 30-min per-market cooldown after analysis |
| `min_market_volume` | 5000 | Filter thin markets |
| `min_days_to_resolution` | 1 | Skip expiring today |
| `max_days_to_resolution` | 90 | Skip far-future contracts |

---

## Testing

```bash
conda run -n kalshi-bot python -m pytest tests/ -v
```

81 tests, 0 failures. Run before committing any change.

Test organization:
- `test_kelly.py` — Kelly math, direction, edge cases (15 tests)
- `test_ledger.py` — Schema, CRUD, P&L math, settlement (13 tests)
- `test_movement.py` — Detector, cooldown, flag logic (9 tests)
- `test_calibration.py` — Brier, settlement, correction (10 tests)
- `test_prompts.py` — System prompt, prompt building (12 tests)
- `test_news.py` — Keyword extraction, news scoring (10 tests)
- `test_smoke.py` — End-to-end lifecycle (12 tests) ← most important

When adding features, add tests to the relevant file and an end-to-end test in `test_smoke.py`.

---

## Known Gotchas and Decisions

1. **NO trade entry_price is the NO price, not the YES price.** If YES is at 70¢, buying NO
   costs 30¢/contract. `entry_price=0.30`. Win gross = `contracts × (1 − 0.30)`. This was
   a bug in initial code that is now fixed and tested.

2. **Winner detection uses gross_pnl > 0, not resolved_yes == 1.** A winning NO trade
   has `resolved_yes=0` (market resolved NO) but positive gross_pnl.

3. **Calibration is updated by CalibrationTracker, not settle_trade.** The Ledger's
   `settle_trade()` handles financial P&L only. `CalibrationTracker.record_settlement()`
   updates the calibration table. Doing both would double-settle.

4. **Price normalization**: Kalshi API returns prices as 0-99 int or 0.0-1.0 float.
   Always pass through `_parse_price()` before using.

5. **RSA signing path**: Query string must be stripped before signing.
   Use `urlparse(path).path`, not the full path string.

6. **Prompt cache TTL is 5 minutes.** If the scanner sleeps 2 minutes between cycles,
   the cache stays warm. If you pause longer than 5 minutes, the next call pays cache_write cost.

7. **sys.path.insert(0, ...)** is at the top of every script. This is intentional — scripts
   live in `scripts/` but imports are from the project root.

8. **Balance from API is in cents.** `get_balance()` divides by 100. Don't do it again.

9. **CalibrationTracker does not create DB schema** — it reads from the `calibration` table
   that `Ledger._init_schema()` creates. Always instantiate `Ledger` before `CalibrationTracker`.

---

## Planned Next Steps (not yet implemented)

### Model Escalation — Option A (implemented)

Sonnet analyzes all markets. When a result comes back `confidence="low"` AND edge ≥
`min_edge_to_escalate` (default 0.10), that market is re-analyzed with Opus 4.6. The Opus
result replaces the Sonnet result. Cost hit is per-escalation only; typical sessions escalate
0–3 markets.

### Model Escalation (future: Option B — two-pass screening, not yet implemented)

Sonnet screens all flagged markets cheaply and surfaces opportunities. Any opportunity above a
higher edge threshold (e.g. ≥ 15%) gets a second **independent** analysis from Opus before the
trade is recorded. The two probabilities are then averaged or the conservative (lower edge) one
is taken.

Implementation sketch:
- After `analyze_markets()` produces `all_opportunities`, loop over the list
- For any opportunity where `opp.edge >= settings.opus_verification_threshold` (new config param,
  suggested default 0.15), re-run `_call_claude()` with model forced to `claude-opus-4-6`
- Build a second `Opportunity` from the Opus result; compare `claude_probability` values
- Use the conservative reading: if they diverge by > 5 pp, take the probability closer to the
  market price (lower edge); otherwise average them
- Log both probabilities in the `reasoning` field for audit trail

Difference from Option A: Option A escalates on *low confidence + high edge*, acting as a
quality check. Option B escalates on *high edge regardless of confidence*, acting as a second
opinion to reduce false positives at the most significant trade sizes. The two could coexist.

---

- Wire `get_correction_factor()` into `ClaudeAnalyzer._build_opportunity()` so calibration
  actually adjusts probabilities at analysis time
- Add `scripts/backfill.py` to mark historical opportunities with known outcomes
- Add market category tagging to opportunities so calibration can be segmented by domain
- Implement partial fills / split bet across multiple contracts
- Add Telegram or email notification when high-confidence opportunity is found
- Rate limiting self-imposed on the market fetch (currently no delay between page fetches)

---

## Dependencies

Key packages and why:
- `anthropic>=0.40.0` — Prompt caching support requires this version
- `httpx>=0.27.0` — Async-capable HTTP; used synchronously here
- `cryptography>=42.0.0` — RSA-PSS signing
- `feedparser` — RSS parsing for news feeds
- `rich` — Terminal UI (panels, tables, prompts) for review/report scripts
- `python-dotenv` — `.env` file loading
- `newsapi-python` — Optional NewsAPI integration
