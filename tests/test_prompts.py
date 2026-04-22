"""Unit tests for Claude prompt building."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import pytest
from src.claude.prompts import SYSTEM_PROMPT, MarketContext, build_analysis_prompt
from src.signals.news import NewsItem
from datetime import datetime, timezone


def _make_context(ticker="FED-MAY26", price=0.40, days=14.0):
    return MarketContext(
        ticker=ticker,
        title=f"Test market {ticker}",
        resolution_criteria="Resolves Yes if event occurs",
        market_price=price,
        volume=50000.0,
        days_to_resolution=days,
        news_items=[],
    )


class TestSystemPrompt:
    def test_system_prompt_is_non_empty(self):
        assert len(SYSTEM_PROMPT) > 100

    def test_system_prompt_mentions_calibration(self):
        assert "calibrat" in SYSTEM_PROMPT.lower()

    def test_system_prompt_mentions_json(self):
        assert "JSON" in SYSTEM_PROMPT

    def test_system_prompt_mentions_confidence_levels(self):
        assert "high" in SYSTEM_PROMPT
        assert "medium" in SYSTEM_PROMPT
        assert "low" in SYSTEM_PROMPT


class TestBuildAnalysisPrompt:
    def test_single_market_prompt_contains_ticker(self):
        ctx = _make_context(ticker="TEST-TICKER")
        prompt = build_analysis_prompt([ctx])
        assert "TEST-TICKER" in prompt

    def test_prompt_contains_market_price(self):
        ctx = _make_context(price=0.42)
        prompt = build_analysis_prompt([ctx])
        assert "42" in prompt

    def test_prompt_contains_days_to_resolution(self):
        ctx = _make_context(days=21.0)
        prompt = build_analysis_prompt([ctx])
        assert "21" in prompt

    def test_multiple_markets_all_present(self):
        contexts = [_make_context(ticker=f"MKT-{i}") for i in range(3)]
        prompt = build_analysis_prompt(contexts)
        for i in range(3):
            assert f"MKT-{i}" in prompt

    def test_prompt_mentions_json_count(self):
        contexts = [_make_context(ticker=f"MKT-{i}") for i in range(3)]
        prompt = build_analysis_prompt(contexts)
        assert "3" in prompt

    def test_prompt_with_news_items(self):
        ctx = _make_context()
        ctx.news_items = [
            NewsItem(
                title="Fed raises rates",
                summary="Federal Reserve raises interest rates by 25bps",
                published=datetime.now(timezone.utc),
                source="Reuters",
                url="https://reuters.com/test",
            )
        ]
        prompt = build_analysis_prompt([ctx])
        assert "Reuters" in prompt
        assert "Fed raises rates" in prompt

    def test_prompt_no_news_says_none(self):
        ctx = _make_context()
        prompt = build_analysis_prompt([ctx])
        assert "None" in prompt

    def test_prompt_includes_volume(self):
        ctx = _make_context()
        prompt = build_analysis_prompt([ctx])
        assert "50,000" in prompt or "50000" in prompt
