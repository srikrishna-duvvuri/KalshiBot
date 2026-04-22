"""Unit tests for the Polymarket cross-market divergence signal."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from unittest.mock import MagicMock

import pytest

from src.signals.crossmarket import (
    CrossMarketResult,
    CrossMarketSignal,
    PolymarketClient,
    _extract_yes_price,
    _maybe_json,
)


class TestMaybeJson:
    def test_parses_json_string(self):
        assert _maybe_json('["Yes", "No"]') == ["Yes", "No"]

    def test_passes_through_list(self):
        assert _maybe_json(["Yes", "No"]) == ["Yes", "No"]

    def test_returns_none_for_none(self):
        assert _maybe_json(None) is None

    def test_returns_none_for_garbage(self):
        assert _maybe_json("not json{{") is None


class TestExtractYesPrice:
    def test_parses_gamma_style_strings(self):
        market = {
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.62", "0.38"]',
        }
        assert _extract_yes_price(market) == pytest.approx(0.62)

    def test_parses_list_form(self):
        market = {
            "outcomes": ["Yes", "No"],
            "outcomePrices": ["0.45", "0.55"],
        }
        assert _extract_yes_price(market) == pytest.approx(0.45)

    def test_returns_none_when_no_yes_outcome(self):
        market = {
            "outcomes": '["Red", "Blue"]',
            "outcomePrices": '["0.5", "0.5"]',
        }
        assert _extract_yes_price(market) is None

    def test_returns_none_on_mismatched_lengths(self):
        market = {"outcomes": '["Yes"]', "outcomePrices": '["0.5", "0.5"]'}
        assert _extract_yes_price(market) is None


class TestPolymarketClient:
    def _mock_client(self, payload, status=200):
        http = MagicMock()
        resp = MagicMock()
        resp.status_code = status
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value=payload)
        http.get = MagicMock(return_value=resp)
        return PolymarketClient(http_client=http), http

    def test_returns_yes_price_from_list_payload(self):
        payload = [{
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.71", "0.29"]',
            "active": True,
            "closed": False,
        }]
        client, _ = self._mock_client(payload)
        assert client.get_yes_price("some-slug") == pytest.approx(0.71)

    def test_returns_none_when_closed(self):
        payload = [{
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.71", "0.29"]',
            "closed": True,
        }]
        client, _ = self._mock_client(payload)
        assert client.get_yes_price("some-slug") is None

    def test_returns_none_on_empty_response(self):
        client, _ = self._mock_client([])
        assert client.get_yes_price("some-slug") is None

    def test_swallows_http_errors(self):
        http = MagicMock()
        http.get = MagicMock(side_effect=RuntimeError("network down"))
        client = PolymarketClient(http_client=http)
        assert client.get_yes_price("some-slug") is None

    def test_empty_slug_short_circuits(self):
        http = MagicMock()
        client = PolymarketClient(http_client=http)
        assert client.get_yes_price("") is None
        http.get.assert_not_called()


class TestCrossMarketSignal:
    def _signal(self, poly_price, mappings):
        poly = MagicMock()
        poly.get_yes_price = MagicMock(return_value=poly_price)
        return CrossMarketSignal(client=poly, mappings=mappings), poly

    def test_returns_none_when_ticker_not_mapped(self):
        sig, poly = self._signal(0.60, mappings={})
        assert sig.check("FED-CUT-JUN26", 0.40) is None
        poly.get_yes_price.assert_not_called()

    def test_returns_none_when_polymarket_unavailable(self):
        sig, _ = self._signal(None, mappings={"FED-CUT-JUN26": "fed-cut-june-2026"})
        assert sig.check("FED-CUT-JUN26", 0.40) is None

    def test_flags_divergence_above_threshold(self):
        sig, _ = self._signal(0.60, mappings={"FED-CUT-JUN26": "fed-cut-june-2026"})
        result = sig.check("FED-CUT-JUN26", 0.40)
        assert result is not None
        assert result.has_divergence is True
        assert result.divergence == pytest.approx(0.20)
        assert result.polymarket_slug == "fed-cut-june-2026"

    def test_no_divergence_when_prices_agree(self):
        sig, _ = self._signal(0.42, mappings={"FED-CUT-JUN26": "fed-cut-june-2026"})
        result = sig.check("FED-CUT-JUN26", 0.40)
        assert result is not None
        assert result.has_divergence is False

    def test_caches_polymarket_fetches(self):
        sig, poly = self._signal(0.60, mappings={"FED-CUT-JUN26": "fed-cut-june-2026"})
        sig.check("FED-CUT-JUN26", 0.40)
        sig.check("FED-CUT-JUN26", 0.45)
        assert poly.get_yes_price.call_count == 1

    def test_divergence_is_signed(self):
        sig, _ = self._signal(0.30, mappings={"X": "x-slug"})
        result = sig.check("X", 0.55)
        assert result.divergence == pytest.approx(-0.25)
