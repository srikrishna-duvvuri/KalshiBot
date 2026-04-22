# Kalshi Bot — System Design

This document records the original design rationale, architectural decisions, and
trade-offs made. It is a reference for understanding *why* the system is built the
way it is, not just *what* it does.

---

## Problem Statement

Kalshi is a prediction market exchange where participants trade contracts that resolve
to $1 (YES) or $0 (NO) based on real-world outcomes. The market price of a YES contract
represents the crowd's probability estimate for that outcome.

**The core thesis**: If Claude can estimate the true probability of an event more
accurately than the market consensus, there is positive expected value in placing bets
in that direction. The profitability equation is:

```
Net Profit = Gross P&L − Kalshi Fees − Claude API Costs
```

For the bot to be viable, Claude's edge must reliably exceed these two cost layers.

---

## Architecture Decisions

### Decision 1: Claude as the Sole Analyst

**Chosen**: Use Claude Sonnet as the probability estimator, receiving market metadata
and recent news, returning calibrated probability estimates.

**Why**: Claude already has extensive world knowledge, understands base rates across
politics/economics/sports domains, and can synthesize news signals. Training a custom
model would require substantial historical data and ongoing maintenance.

**Trade-off**: Claude has a knowledge cutoff. For very recent events, it relies entirely
on the news we inject. This is acceptable — the news fetcher provides recency.

### Decision 2: Movement Detection Before Analysis

**Chosen**: Don't analyze every market every cycle. Only analyze markets that show
meaningful price or volume movement.

**Why**: Analyzing all open markets every 2 minutes would cost ~$50+/day in Claude API
fees (at scale). Movement is a proxy for "new information has arrived" — exactly when
there's something for Claude to add.

**How**: A 12-snapshot ring buffer (~1 hour of history at 2-min polling) per market.
Flag on ≥5% price change or ≥2× volume spike. 30-minute cooldown per market after analysis
to prevent re-analyzing the same move repeatedly.

### Decision 3: Half-Kelly with Fee Adjustment

**Chosen**: Half-Kelly criterion where `b` (net odds) already accounts for the ~7%
Kalshi fee on winning trades. Position is capped at 5% of bankroll.

**Why**: Full Kelly is theoretically optimal for log-wealth maximization but requires
perfectly calibrated probabilities. Since Claude's calibration is imperfect (especially
early on), half-Kelly provides a safety margin. The fee adjustment ensures we aren't
sizing as if we collect the full payout when we actually collect 93%.

**Formula**: See `docs/api_reference.md` → `calculate_kelly()`.

### Decision 4: Human-in-Loop First, Autopilot Second

**Chosen**: Default `AUTOPILOT_MODE=false`. Human reviews opportunities via `review.py`,
which fetches live price before placing to catch drift.

**Why**: Trust must be earned. Autopilot risks runaway losses if Claude has a bad
calibration period or if there's a bug in the sizing logic. Once live performance data
confirms positive EV, autopilot becomes safe to enable.

**Transition criteria**: Enable autopilot when the 30-day Brier score is below 0.20
and win rate is above 55% on at least 20 settled trades.

### Decision 5: SQLite with WAL Mode

**Chosen**: SQLite in WAL (Write-Ahead Logging) mode, not a client-server database.

**Why**: This is a single-machine bot. SQLite WAL handles concurrent readers during
writes cleanly. No server to maintain. Data is a single `.db` file that's easy to
inspect, back up, and version. For this scale (hundreds of trades/year) SQLite is
appropriate.

### Decision 6: Separate Anthropic API Key

**Chosen**: Bot uses its own `ANTHROPIC_API_KEY` from `.env`, not the Claude Code session.

**Why**: Claude Code session costs are billed differently and the key may not be available
when running the bot unattended. The $2/day budget is enforced at the application layer
against this separate key.

### Decision 7: RSS Feeds as Primary News Source

**Chosen**: 9 RSS feeds (Reuters, BBC, NYT, NPR, Politico, CNBC, ESPN, Yahoo Finance)
as primary. NewsAPI as optional enhancement.

**Why**: RSS is free, reliable, and doesn't require an API key. NewsAPI adds breadth
but costs API calls. The keyword scoring approach (word overlap between market title and
article text) is simple and good enough — Claude handles the actual relevance assessment.

**Feeds chosen**: Mix of general news (Reuters, BBC, NPR), finance (CNBC, Yahoo), politics
(Politico, NYT), and sports (ESPN) to cover Kalshi's main market categories.

---

## Data Flow

```
[Kalshi API]                                 [News Sources]
     │                                            │
     ▼                                            ▼
get_active_markets()                        refresh_cache()
  • volume filter (>$5k)                    • 9 RSS feeds
  • price filter (5-95%)                    • optional NewsAPI
  • days filter (1-90 days)                 • 48h window
  • normalize price 0→1                     • deduplicate by URL
     │                                      • sort by publish time
     ▼
update_market() × N
  • 12-snapshot ring buffer
  • price + volume per tick
     │
     ▼
get_flagged_markets()
  • ≥5% price change OR
  • ≥2× volume spike
  • skip if in 30-min cooldown
     │
     ▼ (flagged markets)
build_market_context()                      get_relevant_news(market_title)
  • gather metadata                           • keyword match against cache
  • attach news items                         • return top 4 by score
     │
     ▼
build_analysis_prompt()
  • batch of up to 5 markets
  • structured text format
  • prompts for JSON output
     │
     ▼
Claude API (Sonnet + cached SYSTEM_PROMPT)
  • returns JSON array
  • one object per market
  • {probability, confidence, direction, reasoning, key_factors, data_gaps}
     │
     ▼
_build_opportunity()
  • filter low confidence
  • calculate_kelly() → KellyResult
  • filter is_worthwhile
     │
     ▼
ledger.record_opportunity()
  • INSERT opportunities row
  • INSERT calibration row (predicted_probability, NULL outcome)
     │
     ┌──────────────────┐
     ▼                  ▼
[autopilot=false]  [autopilot=true]
scan logs only     _auto_execute():
                   • edge ≥ 0.08
                   • confidence != low
                   • place_order() → Kalshi API
                   • record_trade()
     │
     ▼ (later, human runs review.py)
Executor.review_opportunities()
  • display Rich UI panel
  • fetch live price
  • warn if >2% drift
  • [A]ccept → place_order + record_trade
  • [S]kip → mark_opportunity_acted_on
  • [R]esize → custom size → place_order
```

---

## Settlement Flow

When a Kalshi market resolves:

```
Human runs:
python scripts/settle.py --ticker MKT-XYZ --result yes --fee 12.50

→ calibration.record_settlement(ticker, resolved_yes=True)
    • finds calibration rows WHERE market_ticker=? AND resolved_yes IS NULL
    • computes brier_contribution = (predicted_p - 1.0)² for each
    • updates: resolved_yes, resolution_date, brier_contribution

→ for each open trade on that ticker:
    ledger.settle_trade(trade_id, resolved_yes=True, kalshi_fee=fee_per_trade)
    • computes gross_pnl based on direction + outcome
    • net_pnl = gross_pnl - kalshi_fee
    • status → 'settled'
```

**Ownership boundary**: `ledger.settle_trade()` owns financial P&L only.
`CalibrationTracker.record_settlement()` owns calibration. This separation is intentional
and was a bug fix — the original code tried to update calibration in both places.

---

## P&L Accounting

```
Net Profit = Σ(gross_pnl across settled trades)
           − Σ(kalshi_fee across settled trades)
           − Σ(cost_usd across claude_calls)
```

**YES trade P&L:**
- Win (resolved_yes=True):  `contracts × (1 - entry_price)`
- Loss (resolved_yes=False): `-contracts × entry_price`

**NO trade P&L** (entry_price is the NO price, e.g. 0.30 for a NO at 30¢):
- Win (resolved_yes=False): `contracts × (1 - entry_price)`
- Loss (resolved_yes=True):  `-contracts × entry_price`

Note: YES and NO formulas are structurally identical because `entry_price` is always
the price you paid per contract. For YES it's the YES price; for NO it's the NO price.

---

## Cost Model

**Kalshi fees**: ~7% of gross winnings on the winning side. Applied at settlement.
Losers pay no fee (they just lose their stake). Fee rate is configured in `settings.py`
and used in Kelly sizing to adjust expected payout.

**Claude API pricing** (as of 2025, claude-sonnet-4-6):
| Token type | Price |
|------------|-------|
| Input | $3.00/MTok |
| Output | $15.00/MTok |
| Cache write | $3.75/MTok |
| Cache read | $0.30/MTok |

With prompt caching, a typical 5-market batch call costs roughly:
- Cache write (first call): SYSTEM_PROMPT (~800 tokens) × $3.75/MTok ≈ $0.003
- Cache read (subsequent): 800 tokens × $0.30/MTok ≈ $0.00024
- Output: ~400 tokens × $15.00/MTok ≈ $0.006
- **Effective per-call cost**: ~$0.006–$0.009 depending on cache status

At $2/day budget and ~$0.008/call, the bot can make ~250 calls/day before hitting the cap.
In practice, flagged markets are sparse, so typical daily usage is $0.10–$0.50.

---

## Calibration Design

The calibration system tracks Claude's historical accuracy to:
1. Report Brier scores for transparency
2. Apply bucket-level correction factors to future probability estimates

**Brier score mechanics:**
- Every opportunity recorded creates a calibration row with `predicted_probability`
- When the market settles, `brier_contribution = (predicted_p - outcome)²` is stored
- Mean brier_contribution across settled rows = brier_score

**Bucket correction (future enhancement, not yet wired into scan loop):**
```python
# After ≥10 settled predictions, blend toward observed accuracy
corrected = 0.7 × raw_p + 0.3 × bucket_actual_rate
```
Example: If Claude says 75% probability and in the 60-80% bucket, the actual YES
resolution rate is 90%, the corrected probability is `0.7×0.75 + 0.3×0.90 = 0.795`.

---

## Security Notes

- RSA private key lives on disk; path configurable via `KALSHI_PRIVATE_KEY_PATH`
- `.gitignore` excludes `.env`, `*.key`, `*.pem`, `data/kalshi_bot.db`, `logs/`
- Demo mode (`KALSHI_USE_DEMO=true`) uses a separate Kalshi API endpoint with fake money
- Never commit real keys or the database file

---

## Known Limitations

1. **No position tracking against existing Kalshi positions.** The bot places orders
   without knowing what's already open on Kalshi. Risk: could double-position on the
   same market if scan finds the same opportunity twice.

2. **No partial fill handling.** If a limit order is partially filled, the bot records
   the full requested size, not the actual fill.

3. **No market category awareness.** Kelly sizing and edge thresholds are uniform across
   sports/politics/economics. Different categories may warrant different parameters.

4. **Movement detection is in-memory.** Restarting the scanner resets history. Markets
   that moved before the last restart won't be flagged until they move again.

5. **NewsAPI rate limits.** Free tier = 100 requests/day. At 5-minute refresh, that's
   288 requests/day. Use with NewsAPI only for supplemental data, not as primary source.
