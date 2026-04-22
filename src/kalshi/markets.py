import logging
from datetime import datetime, timezone

from config.settings import settings
from src.kalshi.client import KalshiClient

logger = logging.getLogger("kalshi.markets")


def _parse_price(raw) -> float:
    if raw is None:
        return 0.5
    val = float(raw)
    return val / 100.0 if val > 1.0 else val


def _days_to_resolution(close_time_str: str) -> float:
    try:
        close_dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
        delta = close_dt - datetime.now(timezone.utc)
        return delta.total_seconds() / 86400
    except Exception:
        return 30.0


def is_watchlisted(ticker: str) -> bool:
    """True if ticker matches a scheduled-event prefix in settings.watchlist_ticker_prefixes."""
    if not ticker:
        return False
    return any(ticker.startswith(p) for p in settings.watchlist_ticker_prefixes)


class KalshiMarkets:
    def __init__(self, client: KalshiClient):
        self._client = client

    def get_active_markets(self, limit: int = 200) -> list[dict]:
        markets = []
        cursor = None

        while True:
            params = {"status": "open", "limit": min(limit, 200)}
            if cursor:
                params["cursor"] = cursor

            try:
                data = self._client.get("/trade-api/v2/markets", params=params)
            except Exception as e:
                logger.error("Failed to fetch markets: %s", e)
                break

            page_markets = data.get("markets", [])
            for m in page_markets:
                try:
                    yes_bid = _parse_price(m.get("yes_bid") or m.get("last_price") or 0.5)
                    volume = float(m.get("volume", 0) or 0)
                    days = _days_to_resolution(m.get("close_time", ""))
                    watchlisted = is_watchlisted(m.get("ticker", ""))

                    # Watchlisted tickers bypass the volume filter — pre-release
                    # scheduled markets may be thin until the event approaches.
                    if not watchlisted and volume < settings.min_market_volume:
                        continue
                    if not (settings.min_market_price <= yes_bid <= settings.max_market_price):
                        continue
                    if not (settings.min_days_to_resolution <= days <= settings.max_days_to_resolution):
                        continue

                    m["_yes_bid_normalized"] = yes_bid
                    m["_volume_dollars"] = volume
                    m["_days_to_resolution"] = days
                    m["_watchlisted"] = watchlisted
                    markets.append(m)
                except Exception as e:
                    logger.debug("Skipping market %s: %s", m.get("ticker", "?"), e)

            cursor = data.get("cursor")
            if not cursor or len(page_markets) < 200:
                break

        logger.info("Fetched %d qualifying markets", len(markets))
        return markets

    def get_market(self, ticker: str) -> dict:
        data = self._client.get(f"/trade-api/v2/markets/{ticker}")
        return data.get("market", data)

    def get_balance(self) -> float:
        try:
            data = self._client.get("/trade-api/v2/portfolio/balance")
            cents = data.get("balance", 0)
            return float(cents) / 100.0
        except Exception as e:
            logger.warning("Could not fetch balance: %s", e)
            return 0.0

    def get_bid_ask(self, ticker: str) -> tuple[float, float, float]:
        """Return (yes_bid, yes_ask, days_to_resolution) from a live market fetch."""
        market = self.get_market(ticker)
        last = market.get("last_price")
        yes_bid = _parse_price(market.get("yes_bid") or last or 0.5)
        yes_ask = _parse_price(market.get("yes_ask") or last or 0.5)
        days = _days_to_resolution(market.get("close_time", ""))
        return yes_bid, yes_ask, days

    def place_order(self, ticker: str, side: str, count: int, price_cents: int) -> dict:
        body = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "count": count,
            "type": "limit",
            "yes_price": price_cents if side == "yes" else (100 - price_cents),
        }
        return self._client.post("/trade-api/v2/portfolio/orders", body=body)
