"""Tests for the scheduled-event watchlist helpers."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from config.settings import settings
from src.kalshi.markets import is_watchlisted


class TestIsWatchlisted:
    def test_fed_ticker_is_watchlisted(self):
        assert is_watchlisted("FED-CUT-JUN26") is True

    def test_cpi_ticker_is_watchlisted(self):
        assert is_watchlisted("CPI-MAY26-CORE") is True

    def test_random_ticker_is_not_watchlisted(self):
        assert is_watchlisted("CELTICS-G5") is False

    def test_empty_ticker_is_not_watchlisted(self):
        assert is_watchlisted("") is False

    def test_none_is_safe(self):
        assert is_watchlisted(None) is False

    def test_prefix_match_is_not_substring_match(self):
        # "FED-" shouldn't match a ticker that only contains "FED" mid-string
        assert is_watchlisted("NOTAFED-XYZ") is False

    def test_all_configured_prefixes_match(self):
        for prefix in settings.watchlist_ticker_prefixes:
            assert is_watchlisted(prefix + "SOMETHING") is True
