# Kalshi Bot

Algorithmic prediction market scanner. Uses Claude to analyze Kalshi markets, finds edges vs crowd pricing, and surfaces trade opportunities.

---

## Setup (first time only)

```bash
# 1. Create and activate the conda environment
conda env create -f environment.yml
conda activate kalshi-bot

# 2. Make the auth helper executable
chmod +x scripts/claude_auth_helper.sh

# 3. Verify your .env has the right values (already configured)
cat .env
```

Your `.env` should look like:
```
KALSHI_API_KEY_ID=<your key id>
KALSHI_PRIVATE_KEY_PATH=kalshi-key.key
KALSHI_USE_DEMO=true
USE_CLAUDE_CODE_CLI=true
CLAUDE_MODEL=claude-sonnet-4-6
AUTOPILOT_MODE=false
```

---

## Running the bot

Always activate the conda env first:
```bash
conda activate kalshi-bot
```

### Scan for opportunities (main command)
```bash
python scripts/scan.py
```

Fetches live markets, runs news, sends them to Claude, prints any opportunities found. Nothing is traded — `AUTOPILOT_MODE=false` by default.

**Useful flags:**
```bash
# Override your bankroll (default pulled from DB)
python scripts/scan.py --bankroll 500

# Force-analyze N random markets even if they have $0 volume (useful for demo account testing)
python scripts/scan.py --force-analyze 5

# Both
python scripts/scan.py --bankroll 500 --force-analyze 5
```

### View past opportunities and trades
```bash
python scripts/report.py
```

### Review pending opportunities (human-in-loop)
```bash
python scripts/review.py
```

### Settle resolved trades
```bash
python scripts/settle.py
```

---

## Demo vs Live

Controlled by `KALSHI_USE_DEMO` in `.env`:
- `true` — hits `demo-api.kalshi.co`, uses your demo account, no real money
- `false` — hits `trading-api.kalshi.com`, real trades

> **Note:** The demo API has ~84k markets but all show $0 volume. The bot's volume filter will skip everything unless you use `--force-analyze`.

---

## Claude costs

The bot calls Claude via your local Claude Code subscription (no separate API key needed). Model is set in `.env`:
- `claude-sonnet-4-6` — default, cheap (~$0.003/scan)
- `claude-opus-4-6` — smarter, used automatically when Sonnet is low-confidence on a high-edge market

Daily budget cap: `$2.00` (set in `config/settings.py` → `claude_daily_budget_usd`).

---

## Logs

All output is logged to `logs/`. Each run appends to the current day's log file. Check there if something looks wrong in the terminal.

---

## Tests

```bash
conda activate kalshi-bot
python -m pytest tests/ -v
```
