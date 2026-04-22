# API Reference

Every class and function in the codebase, documented with signatures, parameters,
return types, and behavioural notes. Organized by module.

---

## `config/settings.py`

### `Settings` (dataclass)

Single global config object. Instantiated as `settings = Settings()` at module load.
All fields read from environment variables via `os.getenv()` with defaults.

```python
settings = Settings()
```

**Fields:**

| Field | Type | Default | Source |
|-------|------|---------|--------|
| `kalshi_api_key_id` | str | `""` | `KALSHI_API_KEY_ID` |
| `kalshi_private_key_path` | str | `"kalshi-key.key"` | `KALSHI_PRIVATE_KEY_PATH` |
| `kalshi_use_demo` | bool | `True` | `KALSHI_USE_DEMO` |
| `min_market_volume` | float | `5000.0` | hardcoded |
| `min_days_to_resolution` | int | `1` | hardcoded |
| `max_days_to_resolution` | int | `90` | hardcoded |
| `min_market_price` | float | `0.05` | hardcoded |
| `max_market_price` | float | `0.95` | hardcoded |
| `max_position_pct` | float | `0.05` | hardcoded |
| `min_edge_to_surface` | float | `0.05` | hardcoded |
| `min_edge_to_execute` | float | `0.08` | hardcoded |
| `min_confidence` | str | `"medium"` | hardcoded |
| `price_move_threshold` | float | `0.05` | hardcoded |
| `volume_spike_multiplier` | float | `2.0` | hardcoded |
| `poll_interval_seconds` | int | `120` | hardcoded |
| `market_cooldown_seconds` | int | `1800` | hardcoded |
| `anthropic_api_key` | str | `""` | `ANTHROPIC_API_KEY` |
| `claude_model` | str | `"claude-sonnet-4-6"` | hardcoded |
| `max_markets_per_claude_call` | int | `5` | hardcoded |
| `claude_daily_budget_usd` | float | `2.00` | hardcoded |
| `news_api_key` | str | `""` | `NEWS_API_KEY` |
| `news_poll_interval_seconds` | int | `300` | hardcoded |
| `kalshi_fee_rate` | float | `0.07` | hardcoded |
| `autopilot_mode` | bool | `False` | `AUTOPILOT_MODE` |
| `db_path` | str | `"data/kalshi_bot.db"` | hardcoded |
| `log_dir` | str | `"logs"` | hardcoded |

**Property:**

```python
@property
def base_url(self) -> str
```
Returns `"https://demo-api.kalshi.co"` if `kalshi_use_demo` else `"https://trading-api.kalshi.com"`.

---

## `src/kalshi/client.py`

### `KalshiAuthError(Exception)`
Raised when the RSA private key is missing or auth fails (HTTP 401).

### `KalshiAPIError(Exception)`
```python
KalshiAPIError(status_code: int, message: str)
```
Raised for non-2xx, non-429, non-401 responses. `.status_code` attribute available.

### `KalshiClient`

Low-level Kalshi API client. Handles auth, signing, retry.

```python
client = KalshiClient()
```

#### `_load_key() -> None`
Lazy-loads the RSA private key from `settings.kalshi_private_key_path`.
Idempotent (no-op if already loaded). Raises `KalshiAuthError` if file not found.

#### `_sign_request(method: str, path: str) -> dict`
Generates the three Kalshi auth headers. `path` may include query string — it is
stripped internally before signing.

Returns:
```python
{
  "KALSHI-ACCESS-KEY": str,
  "KALSHI-ACCESS-SIGNATURE": str,   # base64(RSA-PSS-SHA256(msg))
  "KALSHI-ACCESS-TIMESTAMP": str,   # unix ms
  "Content-Type": "application/json",
}
```

#### `_request(method, path, params=None, body=None) -> dict`
Core HTTP method. Retries on 429 (rate limit) and network errors with exponential backoff
(start 1s, max 60s, 5 attempts). Re-signs on 429 retry (timestamp would be stale).

#### `get(path, params=None) -> dict`
#### `post(path, body=None) -> dict`
#### `delete(path) -> dict`
Thin wrappers around `_request()`.

---

## `src/kalshi/markets.py`

### `_parse_price(raw) -> float`
Normalizes Kalshi price fields which may be 0-99 int or 0.0-1.0 float.
Returns 0.5 if `raw` is None. `val / 100 if val > 1.0 else val`.

### `_days_to_resolution(close_time_str: str) -> float`
Parses ISO8601 string (handling `Z` suffix), computes days from now.
Returns 30.0 on parse failure.

### `KalshiMarkets`

```python
markets = KalshiMarkets(client: KalshiClient)
```

#### `get_active_markets(limit: int = 200) -> list[dict]`
Paginates through all open markets. Applies filters:
- `volume >= settings.min_market_volume`
- `min_market_price <= yes_bid <= max_market_price`
- `min_days_to_resolution <= days <= max_days_to_resolution`

Adds three normalized fields to each passing market:
- `_yes_bid_normalized: float` — 0-1 range
- `_volume_dollars: float`
- `_days_to_resolution: float`

Price field priority: `yes_bid → last_price → 0.5`.

#### `get_market(ticker: str) -> dict`
Returns single market data. Unwraps `{"market": {...}}` envelope if present.

#### `get_balance() -> float`
Returns balance in dollars (API returns cents). Returns 0.0 on error (non-raising).

#### `place_order(ticker, side, count, price_cents) -> dict`
Places a limit buy order. `side` is `"yes"` or `"no"`. `price_cents` is 0-100.
For NO orders, sets `yes_price = 100 - price_cents`.

---

## `src/signals/movement.py`

### `PriceSnapshot` (dataclass)
```python
PriceSnapshot(price: float, volume: float, timestamp: datetime)
```

### `MovementDetector`

In-memory state. Resets on process restart.

```python
detector = MovementDetector()
```

#### `update_market(market: dict) -> None`
Appends a `PriceSnapshot` to the market's ring buffer (`deque(maxlen=12)`).
Creates buffer on first call for that ticker. Reads `_yes_bid_normalized` and
`_volume_dollars` from market dict (falls back to `yes_bid`/`volume`).

#### `get_flagged_markets(all_markets: list[dict]) -> list[dict]`
Checks each tracked ticker for movement since the oldest snapshot:
- Price change: `|current - oldest| / oldest >= settings.price_move_threshold`
- Volume spike: `current_volume / avg_prior_volume >= settings.volume_spike_multiplier`
  (requires ≥4 snapshots to compute avg)

Returns only markets that are in `all_markets` (by ticker) and NOT in cooldown.
Returns subset of the passed `all_markets` list (not synthetic objects).

#### `mark_analyzed(ticker: str) -> None`
Records current timestamp as the cooldown start for `ticker`.

#### `is_in_cooldown(ticker: str) -> bool`
Returns True if `mark_analyzed` was called within `settings.market_cooldown_seconds` ago.

#### `market_count() -> int`
Number of tickers currently in the ring buffer history.

---

## `src/signals/news.py`

### `RSS_FEEDS: list[tuple[str, str]]`
9 named feeds: Reuters Top, Reuters Business, BBC, NYT, NPR, Politico, CNBC, ESPN, Yahoo Finance.

### `STOP_WORDS: set[str]`
~50 common English stop words filtered during keyword extraction.

### `NewsItem` (dataclass)
```python
NewsItem(title: str, summary: str, published: datetime, source: str, url: str)
```
`published` should be timezone-aware (UTC).

### `NewsFetcher`

```python
fetcher = NewsFetcher()
```

#### `get_relevant_news(market_title: str, max_items: int = 5) -> list[NewsItem]`
Main public method. Refreshes cache if stale (> `news_poll_interval_seconds` old).
Scores cached items by keyword overlap with `market_title`. Filters items older than
48 hours. Returns top `max_items` by score. Items with score 0 are included if
they're within the 48h window (score=-1 means older than 48h, excluded).

#### `refresh_cache() -> None`
Fetches all RSS feeds and optionally NewsAPI. Deduplicates by URL. Sorts by publish
time descending. Updates `_cache` and `_last_refresh`.

#### `_fetch_rss() -> list[NewsItem]`
Parses all `RSS_FEEDS` via `feedparser`. Takes up to 20 entries per feed. Non-fatal
on failure (logs warning, continues).

#### `_fetch_newsapi() -> list[NewsItem]`
Optional. Only called if `settings.news_api_key` is set. Fetches top 50 headlines.
Non-fatal on failure.

#### `_extract_keywords(market_title: str) -> list[str]`
Lowercases, strips `?` and `,`, splits on whitespace. Removes STOP_WORDS and
words shorter than 3 characters.

#### `_parse_time(t) -> datetime`
Converts `time.struct_time` (from feedparser) to timezone-aware UTC datetime.

---

## `src/claude/prompts.py`

### `SYSTEM_PROMPT: str`
~700-word calibration-focused system prompt. Sets the tone as "calibrated analyst"
not optimist/pessimist. Specifies JSON output schema. Lists domain-specific guidance
for sports, politics/macro, and economics categories. Requires valid JSON always.

### `MarketContext` (dataclass)
```python
MarketContext(
    ticker: str,
    title: str,
    resolution_criteria: str,
    market_price: float,           # 0-1 range
    volume: float,                 # dollars
    days_to_resolution: float,
    news_items: list,              # list[NewsItem] or list[dict]
)
```

### `build_analysis_prompt(markets: list[MarketContext]) -> str`
Builds the user-turn prompt for a batch of markets. Format:
```
Analyze these N prediction market(s) and return a JSON array:

--- Market 1 ---
Ticker: ...
Title: ...
Resolution: ...
Current market price (Yes): 0.42 (42%)
Days to resolution: 14.0
Volume: $50,000
  Relevant news:
  - [Reuters] Article title: summary...

--- Market 2 ---
...

Return a JSON array with exactly N objects.
```

News items are taken from `news_items[:4]`. If `news_items` is empty, writes
"Relevant news: None found".

---

## `src/claude/analyzer.py`

### `CLAUDE_PRICING: dict`
Pricing per token (in USD) for each model:
```python
{
  "claude-sonnet-4-6": {
    "input": 3e-6, "output": 15e-6,
    "cache_write": 3.75e-6, "cache_read": 0.30e-6,
  }
}
```

### `Opportunity` (dataclass)
```python
Opportunity(
    ticker: str,
    title: str,
    market_price: float,
    claude_probability: float,
    confidence: str,               # "high"|"medium"|"low"
    direction: str,                # "yes"|"no"
    edge: float,                   # abs(claude_p - market_price)
    kelly_result: KellyResult,
    reasoning: str,
    key_factors: list[str],
    data_gaps: list[str],
    claude_call_id: int = 0,       # set after ledger.record_claude_call()
)
```

### `ClaudeAnalyzer`

```python
analyzer = ClaudeAnalyzer(bankroll: float)
```

#### `update_bankroll(bankroll: float) -> None`
Updates the bankroll used in Kelly sizing. Called each scan cycle when balance refreshes.

#### `daily_cost: float` (property)
In-memory accumulator for today's Claude spend. Auto-resets at midnight.

#### `analyze_markets(markets: list[MarketContext]) -> tuple[list[Opportunity], dict]`
Main public method. Returns `([], {})` if no markets or daily budget exceeded.

Processes markets in batches of `settings.max_markets_per_claude_call` (default 5).
Aggregates usage across batches. On batch failure: logs error, continues to next batch.

Returns:
- `list[Opportunity]` — only worthwhile opportunities (filtered by confidence and Kelly)
- `dict` with keys: `input_tokens`, `output_tokens`, `cache_write_tokens`,
  `cache_read_tokens`, `cost_usd`, `markets_analyzed`

#### `_call_claude(batch: list[MarketContext]) -> tuple[list[dict], dict]`
Single Claude API call with prompt caching on `SYSTEM_PROMPT`. Strips ` ``` ` fences
from response before JSON parsing. Handles both `dict` (single market) and `list` responses.
Returns raw parsed dicts (not Opportunity objects) and usage dict.

#### `_calculate_cost(usage) -> float`
Computes USD cost from API usage object using `CLAUDE_PRICING`.
Uses `getattr(usage, field, 0)` to safely handle missing fields.

#### `_build_opportunity(analysis: dict, market: MarketContext) -> Opportunity | None`
Builds an `Opportunity` from one Claude analysis dict and its source MarketContext.
Returns `None` if:
- confidence is below `settings.min_confidence`
- `kelly_result.is_worthwhile` is False

Uses `analysis["direction"]` if present, falls back to `kelly_result.direction`.

---

## `src/sizing/kelly.py`

### `KellyResult` (dataclass)
```python
KellyResult(
    direction: str,           # "yes"|"no"
    edge: float,              # abs(claude_p - market_price)
    kelly_fraction: float,    # full Kelly fraction
    half_kelly_fraction: float,
    bet_dollars: float,       # half_kelly_fraction × bankroll, capped at max_position_pct × bankroll
    is_worthwhile: bool,      # half_kelly_fraction >= min_edge
)
```

### `calculate_kelly(claude_p, market_price, bankroll, fee_rate=0.07, max_position_pct=0.05, min_edge=None) -> KellyResult`

Fee-adjusted half-Kelly for Kalshi binary contracts.

```python
# YES direction (claude_p > market_price):
b = (1 - market_price) * (1 - fee_rate) / market_price
p = claude_p

# NO direction (claude_p < market_price):
b = market_price * (1 - fee_rate) / (1 - market_price)
p = 1 - claude_p

kelly_f = p - (1 - p) / b
half_kelly_f = kelly_f / 2
bet_dollars = min(half_kelly_f × bankroll, max_position_pct × bankroll)
is_worthwhile = half_kelly_f >= min_edge
```

`min_edge` defaults to `settings.min_edge_to_surface` (0.05).

Returns `KellyResult("yes", 0, 0, 0, 0, False)` when `claude_p == market_price`.
Returns `KellyResult(direction, 0, 0, 0, 0, False)` when `net_loss <= 0` or `net_payout_win <= 0`.

### `expected_value(claude_p, market_price, fee_rate=0.07) -> float`
Simple EV calculation (YES direction only):
```python
win_payout = (1 - market_price) * (1 - fee_rate)
return claude_p * win_payout - (1 - claude_p) * market_price
```

---

## `src/calibration/tracker.py`

### `CalibrationStats` (dataclass)
```python
CalibrationStats(
    total_predictions: int,
    settled_predictions: int,
    brier_score: float,             # default 0.25 (random) when no settled predictions
    random_brier: float = 0.25,
    accuracy_by_bucket: dict = {},  # populated when settled > 0
)
```

`accuracy_by_bucket` structure:
```python
{
    "60-80%": {
        "predicted_avg": 0.72,   # mean predicted probability in this bucket
        "actual_rate": 0.80,     # actual YES resolution rate
        "count": 15,
    },
    ...
}
```
Buckets: `"0-20%"`, `"20-40%"`, `"40-60%"`, `"60-80%"`, `"80-100%"`. Only populated
if the bucket has at least 1 settled prediction.

### `CalibrationTracker`

Does NOT create database schema. Requires `Ledger` to have been initialized first.

```python
tracker = CalibrationTracker(db_path: str)
```

#### `record_settlement(market_ticker: str, resolved_yes: bool) -> int`
Updates all unsettled calibration rows for `market_ticker` (WHERE `resolved_yes IS NULL`).
Computes `brier_contribution = (predicted_probability - outcome)²` per row.
Returns count of rows updated (0 if all already settled or ticker not found).

**This is the only place that writes to the calibration table** (other than the initial
INSERT in `ledger.record_opportunity()`).

#### `get_calibration_stats() -> CalibrationStats`
Reads all calibration rows. Computes Brier score as `AVG(brier_contribution)` across
settled rows. Builds `accuracy_by_bucket` from settled predictions.
Returns `CalibrationStats(brier_score=0.25)` when no settled predictions.

#### `get_correction_factor(raw_probability: float) -> float`
Applies bucket-level correction blending:
```python
corrected = 0.7 × raw_probability + 0.3 × bucket_actual_rate
```
Returns `raw_probability` unchanged if:
- Fewer than 10 settled predictions total
- No `accuracy_by_bucket` data
- Relevant bucket has fewer than 3 samples

---

## `src/accounting/ledger.py`

### `Ledger`

Creates the SQLite database and schema on init if not present. WAL mode enabled.

```python
ledger = Ledger(db_path: str)
```

#### `record_claude_call(model, markets_analyzed, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, cost_usd) -> int`
Inserts into `claude_calls`. Computes `running_daily_cost` by querying current day's sum.
Returns `id` of inserted row.

#### `record_opportunity(market_ticker, market_title, market_price, claude_probability, edge, kelly_fraction, bet_size_dollars, confidence, direction, reasoning, key_factors, data_gaps, claude_call_id) -> int`
Inserts into `opportunities` AND `calibration` in a single transaction.
`key_factors` and `data_gaps` are JSON-serialized lists.
Returns `id` of inserted opportunity row.

#### `record_trade(opportunity_id, direction, contracts, entry_price) -> int`
Inserts into `trades`. Copies `market_ticker` from opportunities via `SELECT`.
Sets `status='open'`. Sets `acted_on=1` on the opportunity.
Returns `id` of inserted trade row.

#### `settle_trade(trade_id, resolved_yes, kalshi_fee=0.0) -> None`
Computes `gross_pnl` based on direction and outcome:
```python
# YES direction:
gross = contracts × (1 - entry_price)   if resolved_yes else  -contracts × entry_price

# NO direction (entry_price IS the NO price):
gross = contracts × (1 - entry_price)   if not resolved_yes else  -contracts × entry_price
```
Sets `net_pnl = gross_pnl - kalshi_fee`. Sets `status='settled'`, `exit_price=float(resolved_yes)`.
Does NOT update calibration (that's CalibrationTracker's responsibility).

#### `get_daily_claude_cost(for_date=None) -> float`
Sum of `cost_usd` for all claude_calls on `for_date` (defaults to today).

#### `get_pnl_summary(days=None) -> dict`
```python
{
    "total_trades": int,
    "settled_trades": int,
    "winning_trades": int,         # count where gross_pnl > 0
    "open_trades": int,
    "win_rate": float,             # winning_trades / settled_trades
    "gross_pnl": float,
    "total_kalshi_fees": float,
    "total_claude_costs": float,   # from get_claude_cost_summary()
    "net_pnl": float,              # sum(net_pnl) - total_claude_costs
    "avg_edge": float,
    "avg_bet_size": float,
}
```
`days=None` returns all-time. `days=N` filters to trades opened in last N days.

**Winner detection**: `gross_pnl > 0` (not `resolved_yes == 1`). A winning NO trade
has `resolved_yes=0` but positive gross_pnl.

#### `get_claude_cost_summary(days=None) -> dict`
```python
{
    "total_calls": int,
    "total_markets_analyzed": int,
    "total_tokens": int,
    "cache_hit_rate": float,       # cache_read_tokens / (input + cache_write + cache_read)
    "input_cost": float,
    "output_cost": float,
    "cache_write_cost": float,
    "cache_read_cost": float,
    "total_cost": float,
}
```

#### `get_open_opportunities() -> list[dict]`
Returns all rows WHERE `acted_on=0`, ordered by `edge DESC`.

#### `get_open_trades() -> list[dict]`
Returns all rows WHERE `status='open'`.

#### `mark_opportunity_acted_on(opportunity_id) -> None`
Sets `acted_on=1`. Used when human skips an opportunity in review.

#### `get_net_profit(days=None) -> float`
Convenience wrapper: `get_pnl_summary(days)["net_pnl"]`.

---

## `src/bot/scanner.py`

### `_setup_logging(log_dir: str) -> None`
Configures `basicConfig` with both FileHandler and StreamHandler. Creates log directory.

### `Scanner`

```python
scanner = Scanner()
```
Constructor initializes all dependencies, fetches initial bankroll (API first, then
interactive prompt), logs startup info.

#### `run() -> None`
Main loop. Calls `_scan_cycle()` every `poll_interval_seconds`. Catches and logs
exceptions (sleeps 60s on error). Exits cleanly on `KeyboardInterrupt`.

#### `_scan_cycle() -> None`
Full pipeline (see data flow in `docs/design.md`):
1. Refresh bankroll (best-effort, only if >1% changed)
2. Fetch active markets
3. Update movement detector
4. Get flagged markets
5. Check daily Claude budget
6. Refresh news cache
7. Build MarketContexts with news
8. Analyze with Claude
9. Record claude_call in ledger
10. Mark analyzed markets as in cooldown
11. Record each opportunity in ledger
12. Either auto-execute (autopilot) or log for human review

#### `_build_market_context(market: dict) -> MarketContext`
Reads `_yes_bid_normalized`, `_volume_dollars`, `_days_to_resolution` from normalized
market dict. Fetches relevant news. Falls back gracefully on missing fields.

#### `_auto_execute(opp: Opportunity, opportunity_id: int) -> None`
Autopilot trade execution. Guards:
- `opp.edge >= settings.min_edge_to_execute` (0.08)
- `opp.confidence != "low"`
- `opp.kelly_result.bet_dollars > 0`

Computes contracts: `max(1, int(bet_dollars / market_price))`.
Converts to cents: `int(market_price * 100)`.
Logs on success, logs error on failure (non-raising).

---

## `src/bot/executor.py`

### `Executor`

```python
executor = Executor(ledger: Ledger, markets: KalshiMarkets)
```

#### `review_opportunities() -> None`
Fetches all open (unacted) opportunities. For each, displays Rich panel and prompts:
- `[A]` — accept at suggested size → `_place_trade(opp, bet_dollars)`
- `[S]` — skip → `mark_opportunity_acted_on()`
- `[R]` — resize → prompts for new size → `_place_trade(opp, new_size)`
- `[Q]` — quit loop

#### `_display_opportunity(opp: dict) -> None`
Renders a Rich `Panel` containing a two-column table with: ticker, probability
comparison, direction (color-coded green=YES, red=NO), bet size, confidence,
reasoning, key factors (bullets), data gaps (dim bullets).

Panel border color: `bright_green` if edge ≥ 8%, `yellow` otherwise.

#### `_place_trade(opp: dict, bet_dollars: float) -> None`
1. Fetches live market price via `get_market(ticker)`
2. Warns if live price has drifted >2% from recorded price; asks to confirm
3. Computes `entry_price`: YES uses `live_price`; NO uses `1.0 - live_price`
4. Computes `contracts = max(1, int(bet_dollars / entry_price))`
5. Displays order summary; requires explicit confirmation
6. Calls `place_order()` then `record_trade()`
7. Prints trade_id on success; prints error on failure (non-raising)

---

## `scripts/scan.py`

Entrypoint. Adds project root to `sys.path`. CLI flags:
- `--demo` → sets `KALSHI_USE_DEMO=true`
- `--autopilot` → sets `AUTOPILOT_MODE=true`
- `--once` → calls `scanner._scan_cycle()` instead of `scanner.run()`

Imports `Scanner` after env overrides so settings pick up the new values.

---

## `scripts/review.py`

Creates `Ledger`, `KalshiClient`, `KalshiMarkets`, `Executor`.
Calls `executor.review_opportunities()`. No CLI args.

---

## `scripts/settle.py`

CLI args: `--ticker` (required), `--result yes|no` (required), `--fee` (float, default 0.0).

Order of operations:
1. `calibration.record_settlement(ticker, resolved_yes)` — always run first
2. Fetch open trades for ticker
3. Display table of trades with projected P&L
4. Confirm → for each trade: `ledger.settle_trade(trade_id, resolved_yes, fee_per_trade)`

Fee is split evenly across all open trades for that ticker.

---

## `scripts/report.py`

CLI args: `--days` (int, optional, default=None for all-time).

Renders four Rich panels:
1. **TRADES** — total/settled/open counts, win rate (green >55%, yellow 45-55%, red <45%), avg edge, avg bet
2. **P&L SUMMARY** — gross, kalshi fees, claude API, net (green if positive, red if negative)
3. **CLAUDE USAGE** — calls, markets, tokens, cache hit rate, cost breakdown by type
4. **CALIBRATION** — prediction counts, Brier score (green <0.20, yellow ≤0.25, red >0.25), bucket table

---

## Database Schema

```sql
CREATE TABLE opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    market_title TEXT NOT NULL,
    market_price REAL NOT NULL,
    claude_probability REAL NOT NULL,
    edge REAL NOT NULL,
    kelly_fraction REAL NOT NULL,
    bet_size_dollars REAL NOT NULL,
    confidence TEXT NOT NULL,
    direction TEXT NOT NULL,
    reasoning TEXT,
    key_factors TEXT,       -- JSON array
    data_gaps TEXT,         -- JSON array
    acted_on INTEGER DEFAULT 0,
    claude_call_id INTEGER,
    FOREIGN KEY (claude_call_id) REFERENCES claude_calls(id)
);

CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    market_ticker TEXT NOT NULL,
    direction TEXT NOT NULL,
    contracts REAL NOT NULL,
    entry_price REAL NOT NULL,  -- NO trades: this is the NO price directly
    exit_price REAL,
    gross_pnl REAL,
    kalshi_fee REAL,
    net_pnl REAL,
    status TEXT DEFAULT 'open',
    resolved_yes INTEGER,        -- market outcome (1=YES, 0=NO), NOT whether we won
    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id)
);

CREATE TABLE claude_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    called_at TEXT NOT NULL,
    model TEXT NOT NULL,
    markets_analyzed INTEGER NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cost_usd REAL NOT NULL,
    running_daily_cost REAL NOT NULL
);

CREATE TABLE calibration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER NOT NULL,
    market_ticker TEXT NOT NULL,
    predicted_probability REAL NOT NULL,
    market_price_at_analysis REAL NOT NULL,
    resolved_yes INTEGER,          -- NULL until settled
    resolution_date TEXT,
    brier_contribution REAL,       -- NULL until settled; (predicted_p - outcome)^2
    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id)
);

-- Indexes
CREATE INDEX idx_opp_ticker   ON opportunities(market_ticker);
CREATE INDEX idx_opp_acted    ON opportunities(acted_on);
CREATE INDEX idx_trade_ticker ON trades(market_ticker);
CREATE INDEX idx_trade_status ON trades(status);
CREATE INDEX idx_cal_ticker   ON calibration(market_ticker);
CREATE INDEX idx_claude_date  ON claude_calls(called_at);
```
