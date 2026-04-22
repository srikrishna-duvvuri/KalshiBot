"""Unit tests for calibration tracker."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.accounting.ledger import Ledger
from src.calibration.tracker import CalibrationTracker


@pytest.fixture
def setup(tmp_path):
    db = str(tmp_path / "test.db")
    ledger = Ledger(db)
    tracker = CalibrationTracker(db)
    return ledger, tracker


def _add_prediction(ledger, ticker, predicted_p, market_price=0.40):
    call_id = ledger.record_claude_call("m", 1, 100, 50, 0, 0, 0.001)
    return ledger.record_opportunity(
        market_ticker=ticker, market_title="Test",
        market_price=market_price, claude_probability=predicted_p,
        edge=abs(predicted_p - market_price), kelly_fraction=0.1,
        bet_size_dollars=10.0, confidence="medium", direction="yes",
        reasoning="test", key_factors=[], data_gaps=[], claude_call_id=call_id,
    )


class TestCalibrationStats:
    def test_empty_calibration_stats(self, setup):
        _, tracker = setup
        stats = tracker.get_calibration_stats()
        assert stats.total_predictions == 0
        assert stats.settled_predictions == 0
        assert stats.brier_score == 0.25  # default

    def test_stats_after_adding_predictions(self, setup):
        ledger, tracker = setup
        _add_prediction(ledger, "MKT-A", 0.70)
        _add_prediction(ledger, "MKT-B", 0.60)
        stats = tracker.get_calibration_stats()
        assert stats.total_predictions == 2
        assert stats.settled_predictions == 0

    def test_brier_score_after_settlement(self, setup):
        ledger, tracker = setup
        _add_prediction(ledger, "MKT-A", 0.80)
        tracker.record_settlement("MKT-A", resolved_yes=True)
        stats = tracker.get_calibration_stats()
        assert stats.settled_predictions == 1
        # Brier = (0.80 - 1.0)^2 = 0.04
        assert stats.brier_score == pytest.approx(0.04, abs=0.001)

    def test_brier_score_perfect_prediction(self, setup):
        ledger, tracker = setup
        _add_prediction(ledger, "MKT-A", 1.0)
        tracker.record_settlement("MKT-A", resolved_yes=True)
        stats = tracker.get_calibration_stats()
        assert stats.brier_score == pytest.approx(0.0, abs=0.001)

    def test_brier_score_worst_prediction(self, setup):
        ledger, tracker = setup
        _add_prediction(ledger, "MKT-A", 1.0)
        tracker.record_settlement("MKT-A", resolved_yes=False)
        stats = tracker.get_calibration_stats()
        assert stats.brier_score == pytest.approx(1.0, abs=0.001)


class TestSettlement:
    def test_settlement_returns_row_count(self, setup):
        ledger, tracker = setup
        _add_prediction(ledger, "MKT-A", 0.70)
        _add_prediction(ledger, "MKT-A", 0.65)  # two predictions for same market
        count = tracker.record_settlement("MKT-A", resolved_yes=True)
        assert count == 2

    def test_settlement_of_unknown_ticker(self, setup):
        _, tracker = setup
        count = tracker.record_settlement("NONEXISTENT", resolved_yes=True)
        assert count == 0

    def test_double_settlement_no_duplicate(self, setup):
        ledger, tracker = setup
        _add_prediction(ledger, "MKT-A", 0.70)
        tracker.record_settlement("MKT-A", resolved_yes=True)
        count2 = tracker.record_settlement("MKT-A", resolved_yes=True)
        assert count2 == 0  # already settled, no new rows to update


class TestCorrectionFactor:
    def test_no_correction_with_few_predictions(self, setup):
        ledger, tracker = setup
        _add_prediction(ledger, "MKT-A", 0.70)
        result = tracker.get_correction_factor(0.70)
        assert result == 0.70  # unchanged, not enough data

    def test_correction_applied_with_enough_data(self, setup):
        ledger, tracker = setup
        # Add 12 predictions (above threshold of 10)
        for i in range(12):
            ticker = f"MKT-{i}"
            _add_prediction(ledger, ticker, 0.75)
            # All resolve Yes, so actual rate = 1.0 for 60-80% bucket
            tracker.record_settlement(ticker, resolved_yes=True)

        corrected = tracker.get_correction_factor(0.75)
        # Should blend toward actual rate (1.0): 0.7*0.75 + 0.3*1.0 = 0.825
        assert corrected > 0.75
