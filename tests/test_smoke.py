"""
Smoke tests — verify end-to-end wiring without external API calls.
These simulate a full scan → analyze → record → settle → report cycle
using mocked external services (Kalshi API, Claude API).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from src.accounting.ledger import Ledger
from src.calibration.tracker import CalibrationTracker
from src.sizing.kelly import calculate_kelly
from src.signals.movement import MovementDetector
from src.claude.prompts import MarketContext, build_analysis_prompt


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "smoke_test.db")


@pytest.fixture
def ledger(db_path):
    return Ledger(db_path)


@pytest.fixture
def calibration(db_path):
    return CalibrationTracker(db_path)


class TestFullTradeLifecycle:
    """Simulate: opportunity → trade → settlement → calibration → report."""

    def test_complete_winning_trade_lifecycle(self, ledger, calibration):
        # 1. Record a Claude call
        call_id = ledger.record_claude_call(
            model="claude-sonnet-4-6", markets_analyzed=3,
            input_tokens=1500, output_tokens=400,
            cache_write_tokens=200, cache_read_tokens=800,
            cost_usd=0.0085,
        )

        # 2. Record opportunity from Claude analysis
        opp_id = ledger.record_opportunity(
            market_ticker="FED-CUT-JUN26",
            market_title="Will Fed cut rates in June 2026?",
            market_price=0.38,
            claude_probability=0.62,
            edge=0.24,
            kelly_fraction=0.18,
            bet_size_dollars=90.0,
            confidence="medium",
            direction="yes",
            reasoning="Labor market cooling faster than Fed projects",
            key_factors=["Unemployment rising", "CPI below target"],
            data_gaps=["May jobs report not yet released"],
            claude_call_id=call_id,
        )
        assert opp_id > 0

        # 3. Verify opportunity appears in open list
        opps = ledger.get_open_opportunities()
        assert len(opps) == 1
        assert opps[0]["market_ticker"] == "FED-CUT-JUN26"
        assert opps[0]["acted_on"] == 0

        # 4. Execute trade
        trade_id = ledger.record_trade(
            opportunity_id=opp_id,
            direction="yes",
            contracts=225,  # $90 / $0.38/contract ≈ 237 → 225
            entry_price=0.38,
        )
        assert trade_id > 0

        # 5. Verify trade shows as open
        open_trades = ledger.get_open_trades()
        assert len(open_trades) == 1
        assert open_trades[0]["status"] == "open"

        # 6. Verify opportunity is now acted on
        opps = ledger.get_open_opportunities()
        assert len(opps) == 0

        # 7. Settle the trade (market resolved Yes — we won)
        kalshi_fee = 6.30  # ~7% of gross profit
        ledger.settle_trade(trade_id, resolved_yes=True, kalshi_fee=kalshi_fee)

        # 8. Verify P&L: gross = 225 * (1 - 0.38) = 139.50
        summary = ledger.get_pnl_summary()
        assert summary["settled_trades"] == 1
        assert summary["winning_trades"] == 1
        assert summary["gross_pnl"] == pytest.approx(225 * 0.62, abs=0.01)
        assert summary["total_kalshi_fees"] == pytest.approx(kalshi_fee, abs=0.01)

        # 9. Net = gross - kalshi_fee - claude_cost
        net = summary["net_pnl"]
        expected_net = (225 * 0.62) - kalshi_fee - 0.0085
        assert net == pytest.approx(expected_net, abs=0.10)
        assert net > 0  # Should be profitable

        # 10. Settle calibration
        cal_count = calibration.record_settlement("FED-CUT-JUN26", resolved_yes=True)
        assert cal_count == 1

        # 11. Verify calibration stats
        stats = calibration.get_calibration_stats()
        assert stats.settled_predictions == 1
        # Brier = (0.62 - 1.0)^2 = 0.1444
        assert stats.brier_score == pytest.approx((0.62 - 1.0) ** 2, abs=0.001)

    def test_complete_losing_trade_lifecycle(self, ledger, calibration):
        call_id = ledger.record_claude_call("m", 1, 100, 50, 0, 0, 0.001)
        opp_id = ledger.record_opportunity(
            market_ticker="SPORTS-CELTICS-WIN",
            market_title="Will Celtics win Game 5?",
            market_price=0.45,
            claude_probability=0.65,
            edge=0.20, kelly_fraction=0.14, bet_size_dollars=50.0,
            confidence="medium", direction="yes",
            reasoning="Home court advantage and rest differential",
            key_factors=["Home court"], data_gaps=["Injury report"],
            claude_call_id=call_id,
        )
        trade_id = ledger.record_trade(opp_id, "yes", contracts=100, entry_price=0.45)
        ledger.settle_trade(trade_id, resolved_yes=False, kalshi_fee=0.0)

        summary = ledger.get_pnl_summary()
        assert summary["winning_trades"] == 0
        assert summary["gross_pnl"] == pytest.approx(-100 * 0.45, abs=0.01)
        assert summary["net_pnl"] < 0


class TestKellyWiringWithLedger:
    """Verify Kelly output feeds correctly into ledger sizing."""

    def test_kelly_result_matches_ledger_bet_size(self, ledger):
        bankroll = 1000.0
        kelly = calculate_kelly(
            claude_p=0.70, market_price=0.40,
            bankroll=bankroll, fee_rate=0.07, max_position_pct=0.05,
        )
        assert kelly.is_worthwhile
        assert kelly.bet_dollars > 0
        assert kelly.bet_dollars <= bankroll * 0.05

        call_id = ledger.record_claude_call("m", 1, 100, 50, 0, 0, 0.001)
        opp_id = ledger.record_opportunity(
            market_ticker="TEST", market_title="Test",
            market_price=0.40, claude_probability=0.70,
            edge=kelly.edge, kelly_fraction=kelly.half_kelly_fraction,
            bet_size_dollars=kelly.bet_dollars,
            confidence="high", direction=kelly.direction,
            reasoning="test", key_factors=[], data_gaps=[],
            claude_call_id=call_id,
        )
        opp = ledger.get_open_opportunities()[0]
        assert abs(opp["bet_size_dollars"] - kelly.bet_dollars) < 0.01
        assert opp["direction"] == "yes"


class TestMovementToAnalysisPipeline:
    """Verify movement detection correctly routes markets for analysis."""

    def test_movement_flags_feed_into_market_context(self):
        detector = MovementDetector()

        markets = [
            {"ticker": "MKT-STABLE", "title": "Stable market",
             "_yes_bid_normalized": 0.50, "_volume_dollars": 10000},
            {"ticker": "MKT-MOVING", "title": "Moving market",
             "_yes_bid_normalized": 0.50, "_volume_dollars": 10000},
        ]

        # Feed 6 snapshots for both markets
        for _ in range(6):
            for m in markets:
                detector.update_market(m)

        # Simulate a big move on MKT-MOVING only
        markets[1] = {**markets[1], "_yes_bid_normalized": 0.65}
        detector.update_market(markets[1])

        flagged = detector.get_flagged_markets(markets)
        assert len(flagged) == 1
        assert flagged[0]["ticker"] == "MKT-MOVING"

        # Build MarketContext for flagged market
        ctx = MarketContext(
            ticker=flagged[0]["ticker"],
            title=flagged[0]["title"],
            resolution_criteria="Test resolution",
            market_price=0.65,
            volume=10000.0,
            days_to_resolution=14.0,
            news_items=[],
        )
        prompt = build_analysis_prompt([ctx])
        assert "MKT-MOVING" in prompt
        assert "65" in prompt  # price reflected


class TestClaudeCostBudgetEnforcement:
    """Verify daily budget logic works correctly in ledger."""

    def test_budget_tracking_across_multiple_calls(self, ledger):
        # Record 3 calls totaling $1.95
        for cost in [0.75, 0.80, 0.40]:
            ledger.record_claude_call("m", 5, 1000, 400, 200, 500, cost)

        daily = ledger.get_daily_claude_cost()
        assert daily == pytest.approx(1.95, abs=0.001)

    def test_cost_summary_cache_hit_rate(self, ledger):
        # 1000 total input tokens, 400 are cache reads = 40% hit rate
        ledger.record_claude_call("m", 5, 500, 400, 100, 400, 0.01)
        summary = ledger.get_claude_cost_summary()
        total_input = 500 + 100 + 400  # input + cache_write + cache_read
        expected_rate = 400 / total_input
        assert summary["cache_hit_rate"] == pytest.approx(expected_rate, abs=0.01)


class TestReportDataIntegrity:
    """Verify report metrics compute consistently across multiple trades."""

    def test_win_rate_calculation(self, ledger):
        call_id = ledger.record_claude_call("m", 1, 100, 50, 0, 0, 0.001)

        def add_trade(ticker, win):
            opp_id = ledger.record_opportunity(
                market_ticker=ticker, market_title=ticker,
                market_price=0.40, claude_probability=0.65,
                edge=0.25, kelly_fraction=0.15, bet_size_dollars=50.0,
                confidence="medium", direction="yes", reasoning="test",
                key_factors=[], data_gaps=[], claude_call_id=call_id,
            )
            trade_id = ledger.record_trade(opp_id, "yes", contracts=100, entry_price=0.40)
            ledger.settle_trade(trade_id, resolved_yes=win)

        add_trade("WIN-1", True)
        add_trade("WIN-2", True)
        add_trade("WIN-3", True)
        add_trade("LOSE-1", False)

        summary = ledger.get_pnl_summary()
        assert summary["settled_trades"] == 4
        assert summary["winning_trades"] == 3
        assert summary["win_rate"] == pytest.approx(0.75, abs=0.01)

    def test_period_filter_days(self, ledger):
        """Record trades and verify period filter works."""
        call_id = ledger.record_claude_call("m", 1, 100, 50, 0, 0, 0.001)
        opp_id = ledger.record_opportunity(
            market_ticker="MKT-A", market_title="A",
            market_price=0.40, claude_probability=0.65,
            edge=0.25, kelly_fraction=0.15, bet_size_dollars=50.0,
            confidence="medium", direction="yes", reasoning="test",
            key_factors=[], data_gaps=[], claude_call_id=call_id,
        )
        trade_id = ledger.record_trade(opp_id, "yes", contracts=10, entry_price=0.40)
        ledger.settle_trade(trade_id, resolved_yes=True)

        # All time should show 1 trade
        all_time = ledger.get_pnl_summary(days=None)
        assert all_time["total_trades"] == 1

        # Last 30 days should also show 1 trade (just created)
        last_30 = ledger.get_pnl_summary(days=30)
        assert last_30["total_trades"] == 1
