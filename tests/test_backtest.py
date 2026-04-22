"""Tests for the retrospective backtest simulator."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.accounting.ledger import Ledger
from src.backtest.simulator import (
    OpportunityRow,
    fetch_settled_opportunities,
    simulate,
)
from src.calibration.tracker import CalibrationTracker


def _row(
    opp_id=1, ticker="TEST-1", price=0.40, claude_p=0.60, edge=0.20,
    bet=30.0, conf="medium", direction="yes", resolved_yes=1,
):
    return OpportunityRow(
        opportunity_id=opp_id, ticker=ticker, created_at="2026-04-01T00:00:00",
        market_price=price, claude_probability=claude_p, edge=edge,
        bet_size_dollars=bet, confidence=conf, direction=direction,
        resolved_yes=resolved_yes,
    )


class TestSimulateBasics:
    def test_empty_input_returns_zero_pnl(self):
        result = simulate([])
        assert result.net_pnl == 0.0
        assert result.wins == 0 and result.losses == 0
        assert result.brier_score is None

    def test_winning_yes_trade(self):
        # YES at 0.40, bet $30 → ~75 contracts. Win: 75 * (1-0.40) = $45 gross.
        rows = [_row(price=0.40, direction="yes", resolved_yes=1, bet=30.0)]
        result = simulate(rows, fee_rate=0.0)
        assert len(result.trades) == 1
        t = result.trades[0]
        assert t.won is True
        assert t.contracts == 75
        assert t.gross_pnl == pytest.approx(75 * 0.60)
        assert t.fee == 0.0
        assert t.net_pnl == pytest.approx(75 * 0.60)

    def test_losing_yes_trade(self):
        rows = [_row(price=0.40, direction="yes", resolved_yes=0, bet=30.0)]
        result = simulate(rows, fee_rate=0.07)
        t = result.trades[0]
        assert t.won is False
        assert t.gross_pnl == pytest.approx(-75 * 0.40)
        assert t.fee == 0.0    # fee only on winners
        assert t.net_pnl == pytest.approx(-75 * 0.40)

    def test_winning_no_trade(self):
        # Market YES=0.75 → entry=0.25 (clean floats). Bet $30 → 120 contracts.
        # Resolved NO → win. Gross = 120 * (1 - 0.25) = $90.
        rows = [_row(price=0.75, direction="no", resolved_yes=0, bet=30.0)]
        result = simulate(rows, fee_rate=0.0)
        t = result.trades[0]
        assert t.won is True
        assert t.contracts == 120
        assert t.entry_price == pytest.approx(0.25)
        assert t.gross_pnl == pytest.approx(120 * 0.75)

    def test_losing_no_trade(self):
        # Same setup, resolved YES → NO bet loses entry_price per contract.
        rows = [_row(price=0.75, direction="no", resolved_yes=1, bet=30.0)]
        result = simulate(rows, fee_rate=0.07)
        t = result.trades[0]
        assert t.won is False
        assert t.gross_pnl == pytest.approx(-120 * 0.25)
        assert t.fee == 0.0

    def test_fee_applied_to_winners_only(self):
        rows = [
            _row(opp_id=1, price=0.40, direction="yes", resolved_yes=1, bet=100.0),
            _row(opp_id=2, price=0.40, direction="yes", resolved_yes=0, bet=100.0),
        ]
        result = simulate(rows, fee_rate=0.07)
        winners = [t for t in result.trades if t.won]
        losers = [t for t in result.trades if not t.won]
        assert len(winners) == 1 and len(losers) == 1
        assert winners[0].fee > 0
        assert losers[0].fee == 0


class TestSimulateFilters:
    def test_min_edge_filters_but_still_counts_for_brier(self):
        rows = [
            _row(opp_id=1, edge=0.03, claude_p=0.50, resolved_yes=1),  # below filter
            _row(opp_id=2, edge=0.20, claude_p=0.70, resolved_yes=1),  # passes
        ]
        result = simulate(rows, min_edge=0.05)
        assert len(result.trades) == 1
        assert result.trades[0].opportunity_id == 2
        assert result.skipped_by_filter == 1
        assert result.brier_score is not None   # both predictions contribute

    def test_min_confidence_floor_filters_low(self):
        rows = [
            _row(opp_id=1, conf="low"),
            _row(opp_id=2, conf="medium"),
            _row(opp_id=3, conf="high"),
        ]
        result = simulate(rows, min_confidence="medium")
        assert len(result.trades) == 2
        assert result.skipped_by_filter == 1

    def test_zero_bet_skipped(self):
        rows = [_row(bet=0.0)]
        result = simulate(rows)
        assert len(result.trades) == 0
        assert result.skipped_no_bet == 1


class TestSimulateAggregates:
    def test_win_rate_aggregate(self):
        rows = [
            _row(opp_id=1, resolved_yes=1),
            _row(opp_id=2, resolved_yes=1),
            _row(opp_id=3, resolved_yes=0),
        ]
        result = simulate(rows, fee_rate=0.0)
        assert result.wins == 2
        assert result.losses == 1
        assert result.win_rate == pytest.approx(2 / 3)

    def test_brier_score_perfect_prediction(self):
        rows = [_row(claude_p=1.0, resolved_yes=1), _row(claude_p=0.0, resolved_yes=0, opp_id=2)]
        result = simulate(rows)
        assert result.brier_score == pytest.approx(0.0)

    def test_brier_score_worst_prediction(self):
        rows = [_row(claude_p=0.0, resolved_yes=1), _row(claude_p=1.0, resolved_yes=0, opp_id=2)]
        result = simulate(rows)
        assert result.brier_score == pytest.approx(1.0)

    def test_by_confidence_splits_pnl(self):
        rows = [
            _row(opp_id=1, conf="high",   resolved_yes=1),
            _row(opp_id=2, conf="medium", resolved_yes=0),
            _row(opp_id=3, conf="low",    resolved_yes=1),
        ]
        result = simulate(rows, fee_rate=0.0)
        assert result.by_confidence["high"].count == 1
        assert result.by_confidence["medium"].count == 1
        assert result.by_confidence["low"].count == 1

    def test_by_edge_bucket_assignment(self):
        rows = [
            _row(opp_id=1, edge=0.06),   # 5-8%
            _row(opp_id=2, edge=0.10),   # 8-12%
            _row(opp_id=3, edge=0.15),   # 12-20%
            _row(opp_id=4, edge=0.25),   # 20%+
        ]
        result = simulate(rows)
        assert result.by_edge_bucket["5-8%"].count == 1
        assert result.by_edge_bucket["8-12%"].count == 1
        assert result.by_edge_bucket["12-20%"].count == 1
        assert result.by_edge_bucket["20%+"].count == 1


class TestFetchSettledOpportunities:
    """End-to-end: write opportunities + settle, fetch, simulate."""

    def test_fetches_only_settled_rows(self, tmp_path):
        db_path = str(tmp_path / "backtest_smoke.db")
        ledger = Ledger(db_path)
        calibration = CalibrationTracker(db_path)

        call_id = ledger.record_claude_call(
            model="claude-sonnet-4-6", markets_analyzed=2,
            input_tokens=100, output_tokens=50,
            cache_write_tokens=0, cache_read_tokens=0, cost_usd=0.01,
        )

        opp_id_settled = ledger.record_opportunity(
            market_ticker="FED-CUT-JUN26", market_title="t",
            market_price=0.40, claude_probability=0.60, edge=0.20,
            kelly_fraction=0.1, bet_size_dollars=30.0,
            confidence="medium", direction="yes",
            reasoning="", key_factors=[], data_gaps=[], claude_call_id=call_id,
        )
        # Unsettled — should not come back from fetch
        ledger.record_opportunity(
            market_ticker="OTHER-TICKER", market_title="t",
            market_price=0.40, claude_probability=0.60, edge=0.20,
            kelly_fraction=0.1, bet_size_dollars=30.0,
            confidence="medium", direction="yes",
            reasoning="", key_factors=[], data_gaps=[], claude_call_id=call_id,
        )

        calibration.record_settlement("FED-CUT-JUN26", resolved_yes=True)

        rows = fetch_settled_opportunities(db_path)
        assert len(rows) == 1
        assert rows[0].opportunity_id == opp_id_settled
        assert rows[0].resolved_yes == 1

        result = simulate(rows, fee_rate=0.0)
        assert len(result.trades) == 1
        assert result.trades[0].won is True
