"""Unit tests for movement detection."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from datetime import datetime, timedelta
from src.signals.movement import MovementDetector


def _make_market(ticker, price, volume=10000.0):
    return {
        "ticker": ticker,
        "title": f"Test {ticker}",
        "_yes_bid_normalized": price,
        "_volume_dollars": volume,
    }


class TestMovementDetectorBasics:
    def test_no_flags_with_single_snapshot(self):
        d = MovementDetector()
        m = _make_market("MKT-A", 0.40)
        d.update_market(m)
        assert d.get_flagged_markets([m]) == []

    def test_no_flags_with_stable_price(self):
        d = MovementDetector()
        m = _make_market("MKT-A", 0.40)
        for _ in range(5):
            d.update_market(m)
        assert d.get_flagged_markets([m]) == []

    def test_flags_large_price_move(self):
        d = MovementDetector()
        for _ in range(6):
            d.update_market(_make_market("MKT-A", 0.40))
        # Simulate a big move
        d.update_market(_make_market("MKT-A", 0.50))
        flagged = d.get_flagged_markets([_make_market("MKT-A", 0.50)])
        assert len(flagged) == 1
        assert flagged[0]["ticker"] == "MKT-A"

    def test_no_flag_for_small_move(self):
        d = MovementDetector()
        for _ in range(6):
            d.update_market(_make_market("MKT-A", 0.40))
        d.update_market(_make_market("MKT-A", 0.401))  # 0.25% move — below 5% threshold
        flagged = d.get_flagged_markets([_make_market("MKT-A", 0.401)])
        assert flagged == []


class TestCooldown:
    def test_market_not_flagged_during_cooldown(self):
        d = MovementDetector()
        for _ in range(6):
            d.update_market(_make_market("MKT-A", 0.40))
        d.update_market(_make_market("MKT-A", 0.50))
        d.mark_analyzed("MKT-A")
        flagged = d.get_flagged_markets([_make_market("MKT-A", 0.50)])
        assert flagged == []

    def test_is_in_cooldown_true_after_mark(self):
        d = MovementDetector()
        d.mark_analyzed("MKT-A")
        assert d.is_in_cooldown("MKT-A") is True

    def test_is_in_cooldown_false_before_mark(self):
        d = MovementDetector()
        assert d.is_in_cooldown("MKT-NEW") is False

    def test_market_count(self):
        d = MovementDetector()
        d.update_market(_make_market("MKT-A", 0.40))
        d.update_market(_make_market("MKT-B", 0.60))
        assert d.market_count() == 2

    def test_only_tracked_markets_can_be_flagged(self):
        d = MovementDetector()
        for _ in range(6):
            d.update_market(_make_market("MKT-A", 0.40))
        d.update_market(_make_market("MKT-A", 0.55))
        unrelated = _make_market("MKT-OTHER", 0.50)
        flagged = d.get_flagged_markets([unrelated])
        assert flagged == []
