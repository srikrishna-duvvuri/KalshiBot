from collections import deque
from dataclasses import dataclass
from datetime import datetime
import logging

logger = logging.getLogger("signals.movement")


@dataclass
class PriceSnapshot:
    price: float
    volume: float
    timestamp: datetime


class MovementDetector:
    """
    Maintains a 12-snapshot ring buffer per market (1 hour at 2-min polling).
    Flags markets with significant price movement or volume spikes.
    Enforces per-market cooldown after analysis to prevent Claude spam.
    """

    def __init__(self):
        self._history: dict[str, deque] = {}
        self._cooldowns: dict[str, datetime] = {}

    def update_market(self, market: dict) -> None:
        ticker = market.get("ticker")
        if not ticker:
            return
        price = float(market.get("_yes_bid_normalized", market.get("yes_bid", 0.5)))
        volume = float(market.get("_volume_dollars", market.get("volume", 0)))

        if ticker not in self._history:
            self._history[ticker] = deque(maxlen=12)

        self._history[ticker].append(PriceSnapshot(
            price=price,
            volume=volume,
            timestamp=datetime.now(),
        ))

    def get_flagged_markets(self, all_markets: list[dict]) -> list[dict]:
        from config.settings import settings
        market_by_ticker = {m.get("ticker"): m for m in all_markets}
        flagged = []

        for ticker, snapshots in self._history.items():
            if self.is_in_cooldown(ticker):
                continue
            if len(snapshots) < 2:
                continue
            if ticker not in market_by_ticker:
                continue

            current = snapshots[-1]
            oldest = snapshots[0]

            price_change_pct = abs(current.price - oldest.price) / oldest.price if oldest.price > 0 else 0.0

            if len(snapshots) >= 4:
                avg_volume = sum(s.volume for s in list(snapshots)[:-1]) / (len(snapshots) - 1)
                volume_ratio = current.volume / avg_volume if avg_volume > 0 else 1.0
            else:
                volume_ratio = 1.0

            price_flagged = price_change_pct >= settings.price_move_threshold
            volume_flagged = volume_ratio >= settings.volume_spike_multiplier

            if price_flagged or volume_flagged:
                reason = (
                    "both" if (price_flagged and volume_flagged)
                    else "price_move" if price_flagged
                    else "volume_spike"
                )
                logger.info(
                    "Flagged %s: price_chg=%.1f%% vol_ratio=%.1f reason=%s",
                    ticker, price_change_pct * 100, volume_ratio, reason,
                )
                flagged.append(market_by_ticker[ticker])

        return flagged

    def mark_analyzed(self, ticker: str) -> None:
        self._cooldowns[ticker] = datetime.now()

    def is_in_cooldown(self, ticker: str) -> bool:
        from config.settings import settings
        if ticker not in self._cooldowns:
            return False
        elapsed = (datetime.now() - self._cooldowns[ticker]).total_seconds()
        return elapsed < settings.market_cooldown_seconds

    def market_count(self) -> int:
        return len(self._history)
