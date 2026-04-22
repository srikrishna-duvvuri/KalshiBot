# Roadmap — Viability Audit and Strategic Pivot

This document captures the findings of a top-to-bottom audit of KalshiBot and the
resulting change in direction. Read `CLAUDE.md` first for the current architecture,
then this doc for what's changing and why.

---

## 1. Audit summary

### What's already good

- **Trading math is correct.** Fee-adjusted half-Kelly in `src/sizing/kelly.py:32-50`
  handles YES and NO directions, negative edge, and zero-payout edge cases.
- **The historical NO-`entry_price` bug is fixed.** `src/accounting/ledger.py:144-148`
  stores NO price directly and settles with `contracts × (1 − entry_price)` on a win.
  Covered by `test_settle_no_trade_win`.
- **API plumbing is right.** RSA-PSS signing strips query strings
  (`src/kalshi/client.py:50`), retries re-sign on 429, price normalization is applied
  at ingress, NO orders correctly convert to `yes_price = 100 - no_price_cents`.
- **Budget enforcement is crash-safe.** Dual gate: in-memory counter + ledger read
  on restart (`src/claude/analyzer.py:89`, `src/bot/scanner.py:174`).
- **Prompt caching is real.** `cache_control: ephemeral` is on the system prompt
  and cost tracking reads `cache_creation_input_tokens` / `cache_read_input_tokens`.
- **Model escalation Option A is implemented.** Low-confidence + high-edge results
  re-analyze on Opus (`src/claude/analyzer.py:114-131`).
- **81 tests, end-to-end lifecycle covered** in `test_smoke.py`. Zero TODO/FIXME
  in `src/` or `scripts/`.

### Real gaps

1. **Calibration is orphaned.** `CalibrationTracker` is instantiated in
   `src/bot/scanner.py:45` and never called. `get_correction_factor()` exists but
   is not wired into `ClaudeAnalyzer._build_opportunity()`. Systematic miscalibration
   compounds with no correction.
2. **No backtest harness.** Zero historical simulation exists. The bot ships live
   without ever having seen what its edge looks like on past markets. This is the
   biggest engineering gap.
3. **Fee model is directionally right but shape-wrong.** Flat 7% × payout overestimates
   the fee at price extremes (Kalshi's actual formula is ~7% × price × (1-price) × contracts).
   Kelly sizes are 2–5× smaller than warranted. Safe, but leaves edge on the table.
4. **Bid-ask spread is not modeled in sizing.** Order placement has spread awareness
   (`_compute_order_price` in `scanner.py:246`), but Kelly sizing assumes mid-price
   fills. On 2–4¢ spreads that's 2–4% of edge unaccounted for.
5. **Minor DRY:** `src/bot/executor.py:112` re-implements `_parse_price` instead of
   importing it.

---

## 2. Profitability assessment

### Unit economics

- Claude budget $2/day at Sonnet-4-6 prices with caching ≈ 1000+ market analyses/day,
  so API cost is not the constraint.
- Kalshi fees (modeled 7%, actual 1–2% mid-price) + bid-ask spread (2–4% round-trip)
  ≈ 3–6% total cost floor.
- `min_edge_to_execute = 0.08` gives 2–5% net edge after costs — tight.
- On a $1000 bankroll with 4 trades/day at claimed 4% true edge, expected net
  ≈ $13–16/day. At 1–2% true edge, expected net ≈ break-even or slightly negative.

### Concept-level critique

The current edge thesis — "price moves 5%+ or volume spikes 2×, Claude reads
recent news, Claude estimates probability better than the crowd" — has a structural
problem: **those are exactly the markets where informed traders have already acted.**
Claude arrives 0–60 minutes late via RSS (typical feed lag 15–30 min) and reasons
over news the market has already seen. This is adverse selection almost by construction.

Polling RSS faster helps only up to the floor set by the publisher's cadence; it's
hygiene, not strategy. The edge source needs to change, not the polling interval.

---

## 3. Priority backlog (engineering — pre-existing gaps)

Status tags: **[shipped]** = done on this branch. **[open]** = not yet done.

1. **[open] Wire `get_correction_factor()`** into `ClaudeAnalyzer._build_opportunity()`.
   One-liner; reduces systematic overconfidence. `CalibrationTracker` is instantiated
   in `scanner.py:46` but never called during analysis.
2. **[shipped] Backtest harness.** `src/backtest/simulator.py` + `scripts/backtest.py`.
   Retrospective replay of every settled opportunity, filtered by min-edge and
   confidence floor, reports gross/net P&L, win rate, ROI on staked, Brier score,
   and breakouts by confidence and edge bucket. No paper-trade table needed —
   the existing `opportunities` ⋈ `calibration` join provides the data once
   markets are settled via `scripts/settle.py`. See §6 for usage.
3. **[open] Model bid-ask spread in Kelly sizing**, not just in order placement.
   Use `get_bid_ask()` to compute the real fill price before sizing. The backtest
   simulator currently fills at mid — this over-estimates P&L by roughly the
   average spread (2–4% on typical contracts).
4. **[open] Correct the fee model** or keep it as an intentional safety margin.
   Current flat 7% × payout overestimates at price extremes; Kalshi's actual fee
   is ~7% × price × (1−price) × contracts. If keeping flat, document in `CLAUDE.md`.
5. **[open] Fix `executor.py` DRY issue** — import `_parse_price` instead of
   re-implementing.

---

## 4. Strategic pivot — change the edge source

Polling faster doesn't create edge against market makers with direct wire feeds.
To actually have edge, change *what* the bot bets on, not how fast it reacts.
In order of leverage:

### 4.1. Scheduled-event watchlist (#1) — **shipped in this branch**

Curate a list of ticker prefixes for scheduled events (Fed decisions, CPI/jobs
releases, SCOTUS opinion days, scheduled elections). These:

- **Bypass the volume filter** in `get_active_markets()` so thin pre-release
  markets are tracked.
- **Bypass the movement/volume-spike gate** in the scanner so they are analyzed
  every cycle (subject to cooldown).
- **Have a known release time**, so Claude can reason over the full information
  set rather than racing to parse breaking news.

Implementation: `watchlist_ticker_prefixes` in `config/settings.py`; honored by
`KalshiMarkets.get_active_markets()` and `Scanner._scan_cycle()`.

### 4.2. Cross-market arbitrage with Polymarket (#3) — **shipped in this branch**

Polymarket hosts many of the same events (2024/2026 elections, Fed decisions,
sports, geopolitics). Its Gamma API is public and unauthenticated for reads.

- Maintain `polymarket_mappings: dict[kalshi_ticker → polymarket_slug]` in config.
- `src/signals/crossmarket.py` fetches Polymarket mid-prices and computes divergence.
- If |kalshi_yes - polymarket_yes| ≥ threshold, surface the divergence to Claude
  in the prompt. Claude's job becomes "are these really the same resolution?"
  which it's good at — not "what's the probability?" which is harder.
- This creates edge from market structure, not from information speed.

Implementation: `src/signals/crossmarket.py`, mapping in `config/settings.py`,
divergence injected into `MarketContext.cross_market_prices` and rendered in
`build_analysis_prompt`.

### 4.3. Future sources (not in this branch)

- **Twitter/X firehose on a narrow watchlist** (official Fed, BLS, major campaigns,
  sports leagues). Real-time on sources that move these markets, volume low enough
  to reason over live.
- **Pre-committed analysis for scheduled events.** Before a Fed announcement,
  cache "if rates up 25bps → X, if hold → Y, if cut → Z". React in milliseconds.
- **Trade the calibration, not the thesis.** Many Kalshi markets sit at round
  numbers (50¢, 75¢) by convention. Identify systematic pricing anomalies at
  boundaries — edge from market structure, not prediction.

---

## 5. Still out of scope for this branch

- Calibration wiring (tracked in §3, open).
- Spread-aware Kelly sizing (tracked in §3, open).
- Fee-curve correction (tracked in §3, open).
- Twitter/X integration (§4.3).
- Pre-committed scheduled-event templates (§4.3).

---

## 6. Using the backtest harness

The harness replays every opportunity that has a settled calibration row.
It does not modify the ledger; it reads and simulates.

```bash
# All-time, default filters (min_edge = 0.05, accept all confidences)
python scripts/backtest.py

# Last 30 days only
python scripts/backtest.py --days 30

# Only trades where Claude was at least medium-confidence
python scripts/backtest.py --min-confidence medium

# Only high-edge opportunities
python scripts/backtest.py --min-edge 0.10

# Model the Kalshi-mid-price fee (≈ 0.07 × 0.5 × 0.5 ≈ 0.02)
python scripts/backtest.py --fee-rate 0.02
```

### How to interpret the output

- **ROI on staked** is the one number that matters. If it's negative across a
  reasonable sample (≥ 50 settled opportunities), the current strategy is
  losing money even before real-world spread is accounted for.
- **Brier score** reports Claude's calibration across *all* predictions, not
  just filtered trades. Below 0.20 is good; above 0.25 is worse than a coin flip.
- **By-confidence** breakdown tells you whether Claude's `high` predictions
  earn their keep. If `high` and `medium` both lose, the confidence signal is
  noise.
- **By-edge bucket** tells you where in the edge distribution the P&L lives.
  If only the 20%+ bucket is profitable, the bot is earning on outliers and
  `min_edge_to_execute` should be raised.

### Known optimistic biases in the output

1. **Fills at mid.** Real execution pays the ask. Expect 2–4% less P&L per
   round-trip in live trading than the backtest reports.
2. **No rejected orders or market-closed events.** Every opportunity becomes
   a trade in the sim.
3. **Fee model is flat 7%.** Kalshi's real fee is price-dependent and smaller
   at extremes — use `--fee-rate` to experiment with alternate assumptions.

Treat simulated net P&L as an **upper bound**, not a forecast.
