"""Unit tests for Kelly criterion sizing."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.sizing.kelly import calculate_kelly, expected_value, KellyResult


class TestKellyDirection:
    def test_yes_when_claude_above_market(self):
        result = calculate_kelly(claude_p=0.65, market_price=0.40, bankroll=1000)
        assert result.direction == "yes"

    def test_no_when_claude_below_market(self):
        result = calculate_kelly(claude_p=0.30, market_price=0.60, bankroll=1000)
        assert result.direction == "no"

    def test_no_edge_when_equal(self):
        result = calculate_kelly(claude_p=0.50, market_price=0.50, bankroll=1000)
        assert result.bet_dollars == 0.0
        assert not result.is_worthwhile


class TestKellyEdgeCases:
    def test_extreme_yes_edge(self):
        result = calculate_kelly(claude_p=0.90, market_price=0.10, bankroll=1000)
        assert result.direction == "yes"
        assert result.edge > 0.5
        assert result.bet_dollars > 0

    def test_near_zero_market_price(self):
        result = calculate_kelly(claude_p=0.20, market_price=0.05, bankroll=1000)
        assert result.direction == "yes"
        assert result.bet_dollars >= 0

    def test_near_one_market_price(self):
        result = calculate_kelly(claude_p=0.70, market_price=0.95, bankroll=1000)
        assert result.direction == "no"

    def test_max_position_cap(self):
        result = calculate_kelly(
            claude_p=0.99, market_price=0.01, bankroll=1000,
            max_position_pct=0.05,
        )
        assert result.bet_dollars <= 1000 * 0.05 + 0.01

    def test_fee_reduces_bet_size(self):
        no_fee = calculate_kelly(claude_p=0.65, market_price=0.40, bankroll=1000, fee_rate=0.0)
        with_fee = calculate_kelly(claude_p=0.65, market_price=0.40, bankroll=1000, fee_rate=0.10)
        assert no_fee.bet_dollars >= with_fee.bet_dollars

    def test_is_worthwhile_threshold(self):
        small_edge = calculate_kelly(claude_p=0.51, market_price=0.50, bankroll=1000, min_edge=0.05)
        assert not small_edge.is_worthwhile

        big_edge = calculate_kelly(claude_p=0.65, market_price=0.40, bankroll=1000, min_edge=0.05)
        assert big_edge.is_worthwhile


class TestKellyMath:
    def test_half_kelly_is_half_of_full(self):
        result = calculate_kelly(claude_p=0.65, market_price=0.40, bankroll=1000)
        assert abs(result.half_kelly_fraction - result.kelly_fraction / 2) < 1e-9

    def test_edge_is_absolute_probability_difference(self):
        result = calculate_kelly(claude_p=0.65, market_price=0.40, bankroll=1000)
        assert abs(result.edge - abs(0.65 - 0.40)) < 1e-9

    def test_bet_dollars_positive_when_worthwhile(self):
        result = calculate_kelly(claude_p=0.65, market_price=0.40, bankroll=1000, min_edge=0.05)
        if result.is_worthwhile:
            assert result.bet_dollars > 0


class TestExpectedValue:
    def test_positive_ev_when_above_market(self):
        ev = expected_value(claude_p=0.65, market_price=0.40)
        assert ev > 0

    def test_negative_ev_when_below_market(self):
        ev = expected_value(claude_p=0.30, market_price=0.60)
        assert ev < 0

    def test_zero_ev_at_fair_price(self):
        # At market price with no fee, EV should be ~0
        ev = expected_value(claude_p=0.40, market_price=0.40, fee_rate=0.0)
        assert abs(ev) < 0.01
