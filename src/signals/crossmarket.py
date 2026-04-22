"""Cross-market divergence signal.

Fetches YES-side mid prices from Polymarket for Kalshi tickers that have a
manually curated mapping, and reports divergence. Edge source is market
structure (same event priced differently on two venues), not prediction speed.

Failures are swallowed — a Polymarket outage must never block the Kalshi scan
cycle. Missing mappings return None silently.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from config.settings import settings

logger = logging.getLogger("signals.crossmarket")

GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"
REQUEST_TIMEOUT_SECONDS = 5.0


@dataclass
class CrossMarketResult:
    kalshi_ticker: str
    polymarket_slug: str
    kalshi_yes: float
    polymarket_yes: float
    divergence: float   # signed: polymarket_yes - kalshi_yes
    has_divergence: bool


class PolymarketClient:
    """Thin read-only client for Polymarket's public Gamma API."""

    def __init__(self, http_client: Optional[httpx.Client] = None):
        self._http = http_client or httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)

    def get_yes_price(self, slug: str) -> Optional[float]:
        """Return the YES mid price as 0.0–1.0, or None if unavailable."""
        if not slug:
            return None
        try:
            resp = self._http.get(GAMMA_API_URL, params={"slug": slug})
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            logger.warning("Polymarket fetch failed for slug=%s: %s", slug, e)
            return None

        # Gamma returns a list of markets matching the slug
        markets = payload if isinstance(payload, list) else payload.get("markets", [])
        if not markets:
            logger.debug("Polymarket returned no markets for slug=%s", slug)
            return None

        market = markets[0]
        if market.get("closed") or not market.get("active", True):
            logger.debug("Polymarket market %s is closed or inactive", slug)
            return None

        return _extract_yes_price(market)


def _extract_yes_price(market: dict) -> Optional[float]:
    """Parse YES price from a Polymarket market object.

    `outcomes` and `outcomePrices` come back as JSON-encoded strings in the
    Gamma API (e.g. '["Yes", "No"]' and '["0.62", "0.38"]'). Handle both
    that and already-parsed list forms.
    """
    outcomes = _maybe_json(market.get("outcomes"))
    prices = _maybe_json(market.get("outcomePrices"))
    if not outcomes or not prices or len(outcomes) != len(prices):
        return None

    for name, price in zip(outcomes, prices):
        if str(name).strip().lower() in ("yes", "y"):
            try:
                return float(price)
            except (TypeError, ValueError):
                return None
    return None


def _maybe_json(raw):
    if raw is None:
        return None
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return None


class CrossMarketSignal:
    """Reports divergence between a Kalshi YES price and the Polymarket equivalent.

    Uses `settings.polymarket_mappings` (kalshi_ticker → polymarket_slug) as the
    source of truth for cross-venue identity. Results are cached per-slug for
    `settings.polymarket_cache_seconds` to avoid hammering the API.
    """

    def __init__(
        self,
        client: Optional[PolymarketClient] = None,
        mappings: Optional[dict] = None,
    ):
        self._client = client or PolymarketClient()
        self._mappings = mappings if mappings is not None else settings.polymarket_mappings
        self._cache: dict[str, tuple[float, float]] = {}   # slug → (price, fetched_at)

    def check(self, kalshi_ticker: str, kalshi_yes_price: float) -> Optional[CrossMarketResult]:
        slug = self._mappings.get(kalshi_ticker)
        if not slug:
            return None

        poly_yes = self._get_cached(slug)
        if poly_yes is None:
            return None

        divergence = poly_yes - kalshi_yes_price
        has_divergence = abs(divergence) >= settings.polymarket_divergence_threshold

        if has_divergence:
            logger.info(
                "Cross-market divergence %s vs %s: kalshi=%.2f poly=%.2f Δ=%+.1fpp",
                kalshi_ticker, slug, kalshi_yes_price, poly_yes, divergence * 100,
            )

        return CrossMarketResult(
            kalshi_ticker=kalshi_ticker,
            polymarket_slug=slug,
            kalshi_yes=kalshi_yes_price,
            polymarket_yes=poly_yes,
            divergence=divergence,
            has_divergence=has_divergence,
        )

    def _get_cached(self, slug: str) -> Optional[float]:
        now = time.time()
        cached = self._cache.get(slug)
        if cached and (now - cached[1]) < settings.polymarket_cache_seconds:
            return cached[0]

        price = self._client.get_yes_price(slug)
        if price is not None:
            self._cache[slug] = (price, now)
        return price
