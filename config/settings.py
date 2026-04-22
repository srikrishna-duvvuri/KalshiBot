import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    # Kalshi auth
    kalshi_api_key_id: str = field(default_factory=lambda: os.getenv("KALSHI_API_KEY_ID", ""))
    kalshi_private_key_path: str = field(default_factory=lambda: os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi-key.key"))
    kalshi_use_demo: bool = field(default_factory=lambda: os.getenv("KALSHI_USE_DEMO", "true").lower() == "true")

    # Market filters
    min_market_volume: float = 5000.0
    min_days_to_resolution: int = 1
    max_days_to_resolution: int = 90
    min_market_price: float = 0.05
    max_market_price: float = 0.95

    # Trading parameters
    max_position_pct: float = 0.05
    min_edge_to_surface: float = 0.05
    min_edge_to_execute: float = 0.08
    min_confidence: str = "medium"

    # Movement detection
    price_move_threshold: float = 0.05
    volume_spike_multiplier: float = 2.0
    poll_interval_seconds: int = 120
    market_cooldown_seconds: int = 1800

    # Claude
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    # use_claude_code_cli=True  → call `claude --print --bare` subprocess (no API key needed)
    # use_claude_code_cli=False → call Anthropic SDK directly (requires ANTHROPIC_API_KEY)
    use_claude_code_cli: bool = field(default_factory=lambda: os.getenv("USE_CLAUDE_CODE_CLI", "true").lower() == "true")
    # Supported: claude-sonnet-4-6, claude-opus-4-6  (avoid claude-opus-4-7 — too expensive)
    claude_model: str = field(default_factory=lambda: os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"))
    max_markets_per_claude_call: int = 5
    claude_daily_budget_usd: float = 2.00
    # Escalate low-confidence markets to Opus when raw edge exceeds this threshold
    min_edge_to_escalate: float = field(default_factory=lambda: float(os.getenv("MIN_EDGE_TO_ESCALATE", "0.10")))

    # News
    news_api_key: str = field(default_factory=lambda: os.getenv("NEWS_API_KEY", ""))
    news_poll_interval_seconds: int = 300

    # Order placement
    # Go aggressive (pay ask) when spread is this tight or market closes this soon
    aggressive_spread_threshold: float = 0.02   # ≤ 2¢ spread → just cross it
    aggressive_within_days: float = 1.0         # < 1 day to close → can't wait for passive fill

    # Scheduled-event watchlist
    # Tickers matching these prefixes bypass the volume filter and are analyzed every
    # cycle regardless of movement (cooldown still applies). Use for Fed decisions,
    # economic releases, SCOTUS, scheduled elections — events where release timing
    # is known in advance and Claude's reasoning has room to breathe.
    watchlist_ticker_prefixes: tuple = (
        "FED-",        # Federal Reserve rate decisions
        "FEDFUNDS-",   # Fed funds target
        "CPI-",        # Consumer Price Index
        "JOBS-",       # Non-farm payrolls
        "GDP-",        # GDP releases
        "SCOTUS-",     # Supreme Court opinions
        "ELECTION-",   # Scheduled elections
        "PRES-",       # Presidential
    )

    # Cross-market arbitrage
    # Map Kalshi tickers → Polymarket slugs for markets resolving on the same event.
    # Curated manually — verify resolution criteria match before adding.
    polymarket_mappings: dict = field(default_factory=dict)
    polymarket_divergence_threshold: float = 0.05   # ≥ 5 pp gap → flag to Claude
    polymarket_cache_seconds: int = 60              # cache fetches this long

    # Fees (verify current schedule at kalshi.com)
    kalshi_fee_rate: float = 0.07

    # Autopilot
    autopilot_mode: bool = field(default_factory=lambda: os.getenv("AUTOPILOT_MODE", "false").lower() == "true")

    # Paths
    db_path: str = "data/kalshi_bot.db"
    log_dir: str = "logs"

    @property
    def base_url(self) -> str:
        return "https://demo-api.kalshi.co" if self.kalshi_use_demo else "https://trading-api.kalshi.com"


settings = Settings()
