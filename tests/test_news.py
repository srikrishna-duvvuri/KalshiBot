"""Unit tests for news fetcher."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from src.signals.news import NewsFetcher, NewsItem, STOP_WORDS


def _make_item(title, summary="", hours_ago=1, source="Test"):
    return NewsItem(
        title=title,
        summary=summary,
        published=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
        source=source,
        url=f"https://example.com/{title.replace(' ', '-')}",
    )


class TestKeywordExtraction:
    def setup_method(self):
        self.fetcher = NewsFetcher()

    def test_removes_stop_words(self):
        keywords = self.fetcher._extract_keywords("Will the Fed raise rates in May?")
        assert "the" not in keywords
        assert "will" not in keywords
        assert "in" not in keywords

    def test_keeps_meaningful_words(self):
        keywords = self.fetcher._extract_keywords("Federal Reserve interest rates policy")
        assert "federal" in keywords
        assert "reserve" in keywords
        assert "interest" in keywords
        assert "rates" in keywords

    def test_filters_short_words(self):
        keywords = self.fetcher._extract_keywords("Is the Fed OK?")
        assert "ok" not in keywords  # len < 3

    def test_lowercases_output(self):
        keywords = self.fetcher._extract_keywords("Federal Reserve")
        assert all(k == k.lower() for k in keywords)


class TestRelevantNewsScoring:
    def setup_method(self):
        self.fetcher = NewsFetcher()
        self.fetcher._cache = [
            _make_item("Fed raises interest rates", "Federal Reserve decision"),
            _make_item("Sports news today", "Basketball game results"),
            _make_item("Federal Reserve meeting minutes", "Policy discussion"),
            _make_item("Old news", "Very old article", hours_ago=50),  # beyond 48h cutoff
        ]
        self.fetcher._last_refresh = datetime.now()  # prevent re-fetch

    def test_returns_relevant_items(self):
        items = self.fetcher.get_relevant_news("Will Fed raise rates in May?", max_items=3)
        titles = [i.title for i in items]
        assert any("Fed" in t or "Federal" in t for t in titles)

    def test_filters_old_items(self):
        items = self.fetcher.get_relevant_news("Old news", max_items=5)
        for item in items:
            age = (datetime.now(timezone.utc) - item.published).total_seconds() / 3600
            assert age <= 48

    def test_respects_max_items(self):
        items = self.fetcher.get_relevant_news("Federal Reserve rates", max_items=2)
        assert len(items) <= 2

    def test_empty_cache_returns_empty(self):
        self.fetcher._cache = []
        items = self.fetcher.get_relevant_news("Fed rates", max_items=5)
        assert items == []


class TestKeywordExtraction2:
    def setup_method(self):
        self.fetcher = NewsFetcher()

    def test_handles_special_chars(self):
        kws = self.fetcher._extract_keywords("Will GDP growth exceed 3%?")
        assert "3%" not in kws or len([k for k in kws if "%" in k]) == 0

    def test_handles_empty_title(self):
        kws = self.fetcher._extract_keywords("")
        assert kws == []
