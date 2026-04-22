"""Unit tests for the accounting ledger."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import tempfile
from src.accounting.ledger import Ledger


@pytest.fixture
def ledger(tmp_path):
    db = str(tmp_path / "test.db")
    return Ledger(db)


def _record_opp(ledger, ticker="TEST-MARKET", direction="yes", claude_p=0.65, market_price=0.40):
    call_id = ledger.record_claude_call(
        model="claude-sonnet-4-6", markets_analyzed=1,
        input_tokens=100, output_tokens=50,
        cache_write_tokens=0, cache_read_tokens=0,
        cost_usd=0.001,
    )
    return ledger.record_opportunity(
        market_ticker=ticker, market_title="Test Market",
        market_price=market_price, claude_probability=claude_p,
        edge=abs(claude_p - market_price), kelly_fraction=0.15,
        bet_size_dollars=50.0, confidence="medium",
        direction=direction, reasoning="Test reasoning",
        key_factors=["factor1"], data_gaps=["gap1"],
        claude_call_id=call_id,
    )


class TestLedgerSchema:
    def test_schema_creates_without_error(self, ledger):
        # Ledger init creates schema — just verify it doesn't raise
        assert ledger is not None

    def test_empty_pnl_summary(self, ledger):
        summary = ledger.get_pnl_summary()
        assert summary["total_trades"] == 0
        assert summary["gross_pnl"] == 0.0
        assert summary["net_pnl"] == 0.0

    def test_empty_claude_cost_summary(self, ledger):
        summary = ledger.get_claude_cost_summary()
        assert summary["total_calls"] == 0
        assert summary["total_cost"] == 0.0


class TestClaudeCallRecording:
    def test_record_claude_call_returns_id(self, ledger):
        call_id = ledger.record_claude_call(
            model="claude-sonnet-4-6", markets_analyzed=3,
            input_tokens=500, output_tokens=200,
            cache_write_tokens=100, cache_read_tokens=50,
            cost_usd=0.0042,
        )
        assert isinstance(call_id, int)
        assert call_id > 0

    def test_daily_cost_accumulates(self, ledger):
        ledger.record_claude_call("m", 1, 100, 50, 0, 0, cost_usd=0.50)
        ledger.record_claude_call("m", 1, 100, 50, 0, 0, cost_usd=0.30)
        assert abs(ledger.get_daily_claude_cost() - 0.80) < 0.001

    def test_claude_cost_summary(self, ledger):
        ledger.record_claude_call("claude-sonnet-4-6", 5, 1000, 500, 200, 100, cost_usd=0.025)
        s = ledger.get_claude_cost_summary()
        assert s["total_calls"] == 1
        assert s["total_markets_analyzed"] == 5
        assert s["total_cost"] == pytest.approx(0.025)


class TestOpportunityRecording:
    def test_record_opportunity_returns_id(self, ledger):
        opp_id = _record_opp(ledger)
        assert isinstance(opp_id, int)
        assert opp_id > 0

    def test_open_opportunities_returned(self, ledger):
        _record_opp(ledger, ticker="MKT-A")
        _record_opp(ledger, ticker="MKT-B")
        opps = ledger.get_open_opportunities()
        assert len(opps) == 2
        tickers = {o["market_ticker"] for o in opps}
        assert "MKT-A" in tickers and "MKT-B" in tickers

    def test_opportunities_ordered_by_edge_desc(self, ledger):
        _record_opp(ledger, ticker="LOW-EDGE", claude_p=0.55, market_price=0.50)
        _record_opp(ledger, ticker="HIGH-EDGE", claude_p=0.80, market_price=0.40)
        opps = ledger.get_open_opportunities()
        assert opps[0]["market_ticker"] == "HIGH-EDGE"

    def test_mark_opportunity_acted_on(self, ledger):
        opp_id = _record_opp(ledger)
        ledger.mark_opportunity_acted_on(opp_id)
        opps = ledger.get_open_opportunities()
        assert len(opps) == 0


class TestTradeRecording:
    def test_record_trade_returns_id(self, ledger):
        opp_id = _record_opp(ledger)
        trade_id = ledger.record_trade(opp_id, "yes", contracts=10, entry_price=0.40)
        assert isinstance(trade_id, int)
        assert trade_id > 0

    def test_trade_marks_opportunity_acted_on(self, ledger):
        opp_id = _record_opp(ledger)
        ledger.record_trade(opp_id, "yes", contracts=10, entry_price=0.40)
        assert len(ledger.get_open_opportunities()) == 0

    def test_open_trades_returned(self, ledger):
        opp_id = _record_opp(ledger)
        ledger.record_trade(opp_id, "yes", contracts=5, entry_price=0.40)
        trades = ledger.get_open_trades()
        assert len(trades) == 1
        assert trades[0]["status"] == "open"


class TestTradeSettlement:
    def test_settle_yes_trade_win(self, ledger):
        opp_id = _record_opp(ledger, direction="yes")
        trade_id = ledger.record_trade(opp_id, "yes", contracts=10, entry_price=0.40)
        ledger.settle_trade(trade_id, resolved_yes=True, kalshi_fee=0.42)
        trades = ledger.get_open_trades()
        assert len(trades) == 0
        summary = ledger.get_pnl_summary()
        assert summary["settled_trades"] == 1
        assert summary["winning_trades"] == 1
        # gross = 10 * (1 - 0.40) = 6.00, net = 6.00 - 0.42 = 5.58
        assert summary["gross_pnl"] == pytest.approx(6.00, abs=0.01)

    def test_settle_yes_trade_loss(self, ledger):
        opp_id = _record_opp(ledger, direction="yes")
        trade_id = ledger.record_trade(opp_id, "yes", contracts=10, entry_price=0.40)
        ledger.settle_trade(trade_id, resolved_yes=False)
        summary = ledger.get_pnl_summary()
        assert summary["winning_trades"] == 0
        # gross = -10 * 0.40 = -4.00
        assert summary["gross_pnl"] == pytest.approx(-4.00, abs=0.01)

    def test_settle_no_trade_win(self, ledger):
        opp_id = _record_opp(ledger, direction="no", market_price=0.70)
        # Buying No at price (1 - 0.70) = 0.30
        trade_id = ledger.record_trade(opp_id, "no", contracts=10, entry_price=0.30)
        ledger.settle_trade(trade_id, resolved_yes=False)
        summary = ledger.get_pnl_summary()
        assert summary["winning_trades"] == 1
        # gross = 10 * (1 - 0.30) = 7.00
        assert summary["gross_pnl"] == pytest.approx(7.00, abs=0.01)

    def test_pnl_summary_includes_claude_costs(self, ledger):
        opp_id = _record_opp(ledger)
        trade_id = ledger.record_trade(opp_id, "yes", contracts=10, entry_price=0.40)
        ledger.settle_trade(trade_id, resolved_yes=True)
        summary = ledger.get_pnl_summary()
        assert summary["total_claude_costs"] > 0
        assert summary["net_pnl"] < summary["gross_pnl"]
