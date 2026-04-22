import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from config.settings import settings
from src.kalshi.client import KalshiClient
from src.kalshi.markets import KalshiMarkets, is_watchlisted
from src.signals.crossmarket import CrossMarketSignal
from src.signals.movement import MovementDetector
from src.signals.news import NewsFetcher
from src.claude.analyzer import ClaudeAnalyzer
from src.claude.prompts import MarketContext
from src.calibration.tracker import CalibrationTracker
from src.accounting.ledger import Ledger


def _setup_logging(log_dir: str):
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler(f"{log_dir}/kalshi_bot.log"),
            logging.StreamHandler(sys.stdout),
        ],
    )


class Scanner:
    def __init__(self, bankroll_override: float = None):
        _setup_logging(settings.log_dir)
        self._logger = logging.getLogger("bot.scanner")

        self._client = KalshiClient()
        self._markets = KalshiMarkets(self._client)
        self._bankroll_override = bankroll_override
        self._bankroll = bankroll_override if bankroll_override else self._get_bankroll()

        self._movement = MovementDetector()
        self._news = NewsFetcher()
        self._crossmarket = CrossMarketSignal()
        self._analyzer = ClaudeAnalyzer(self._bankroll)
        self._ledger = Ledger(settings.db_path)
        self._calibration = CalibrationTracker(settings.db_path)

        self._logger.info(
            "Scanner initialized | bankroll=$%.2f | demo=%s | autopilot=%s",
            self._bankroll, settings.kalshi_use_demo, settings.autopilot_mode,
        )

    def _get_bankroll(self) -> float:
        balance = self._markets.get_balance()
        if balance > 0:
            self._logger.info("Bankroll from Kalshi API: $%.2f", balance)
            return balance

        print("\nCould not fetch balance from Kalshi API.")
        while True:
            try:
                raw = input("Enter your current bankroll ($): ").strip().replace("$", "").replace(",", "")
                val = float(raw)
                if val > 0:
                    return val
                print("Please enter a positive amount.")
            except ValueError:
                print("Invalid number. Try again.")

    def run(self):
        self._logger.info("=== Kalshi Bot Scanner Starting ===")
        while True:
            try:
                self._scan_cycle()
            except KeyboardInterrupt:
                self._logger.info("Scanner stopped by user")
                break
            except Exception as e:
                self._logger.error("Scan cycle error: %s", e, exc_info=True)
                self._logger.info("Sleeping 60s before retry...")
                time.sleep(60)
                continue

            self._logger.info("Next scan in %ds...", settings.poll_interval_seconds)
            time.sleep(settings.poll_interval_seconds)

    def force_analyze(self, n: int = 3):
        """Bypass movement detection and volume filters — send the first N raw API markets
        straight to Claude. Used for end-to-end pipeline testing."""
        self._logger.info("=== FORCE ANALYZE: pulling %d raw markets from API ===", n)
        data = self._client.get("/trade-api/v2/markets", params={"status": "open", "limit": n})
        raw_markets = data.get("markets", [])[:n]
        if not raw_markets:
            self._logger.warning("No markets returned from API")
            return

        # Normalize fields the same way get_active_markets() does
        from src.kalshi.markets import _parse_price, _days_to_resolution
        markets = []
        for m in raw_markets:
            m["_yes_bid_normalized"] = _parse_price(m.get("yes_bid") or m.get("last_price") or 0.5)
            m["_volume_dollars"] = float(m.get("volume", 0) or 0)
            m["_days_to_resolution"] = _days_to_resolution(m.get("close_time", ""))
            markets.append(m)

        self._logger.info("Markets: %s", [m["ticker"] for m in markets])
        self._news.refresh_cache()
        contexts = [self._build_market_context(m) for m in markets]
        opportunities, usage = self._analyzer.analyze_markets(contexts)

        if not usage:
            self._logger.warning("No Claude usage returned (budget hit or empty batch)")
            return

        claude_call_id = self._ledger.record_claude_call(
            model=settings.claude_model,
            markets_analyzed=usage.get("markets_analyzed", len(markets)),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_write_tokens=usage.get("cache_write_tokens", 0),
            cache_read_tokens=usage.get("cache_read_tokens", 0),
            cost_usd=usage.get("cost_usd", 0.0),
        )

        self._logger.info(
            "Claude call done: %d markets → %d opportunities | cost=$%.4f",
            len(markets), len(opportunities), usage.get("cost_usd", 0),
        )

        for opp in opportunities:
            opp.claude_call_id = claude_call_id
            opp_id = self._ledger.record_opportunity(
                market_ticker=opp.ticker, market_title=opp.title,
                market_price=opp.market_price, claude_probability=opp.claude_probability,
                edge=opp.edge, kelly_fraction=opp.kelly_result.half_kelly_fraction,
                bet_size_dollars=opp.kelly_result.bet_dollars,
                confidence=opp.confidence, direction=opp.direction,
                reasoning=opp.reasoning, key_factors=opp.key_factors,
                data_gaps=opp.data_gaps, claude_call_id=claude_call_id,
            )
            if settings.autopilot_mode:
                self._auto_execute(opp, opp_id)
            else:
                self._prompt_and_execute(opp, opp_id)

    def _scan_cycle(self):
        cycle_start = datetime.now()
        self._logger.info("=== Scan cycle @ %s ===", cycle_start.strftime("%H:%M:%S"))

        if not self._bankroll_override:
            try:
                live_balance = self._markets.get_balance()
                if live_balance > 0 and abs(live_balance - self._bankroll) / max(self._bankroll, 1) > 0.01:
                    self._bankroll = live_balance
                    self._analyzer.update_bankroll(live_balance)
                    self._logger.info("Bankroll updated: $%.2f", live_balance)
            except Exception:
                pass

        all_markets = self._markets.get_active_markets()
        if not all_markets:
            self._logger.warning("No qualifying markets returned")
            return

        for m in all_markets:
            self._movement.update_market(m)

        flagged = self._movement.get_flagged_markets(all_markets)

        # Always-flag watchlisted scheduled-event markets (cooldown still respected).
        # These are analyzed every cycle regardless of movement — release timing is
        # known and the edge comes from reasoning, not reacting to price drift.
        flagged_tickers = {m.get("ticker") for m in flagged}
        for m in all_markets:
            ticker = m.get("ticker", "")
            if not is_watchlisted(ticker):
                continue
            if ticker in flagged_tickers:
                continue
            if self._movement.is_in_cooldown(ticker):
                continue
            self._logger.info("Watchlist flag: %s (scheduled-event bypass)", ticker)
            flagged.append(m)
            flagged_tickers.add(ticker)

        if not flagged:
            self._logger.info("No flagged markets (tracking %d total)", self._movement.market_count())
            return

        self._logger.info("%d markets flagged", len(flagged))

        daily_cost = self._ledger.get_daily_claude_cost()
        if daily_cost >= settings.claude_daily_budget_usd:
            self._logger.warning(
                "Daily Claude budget $%.2f reached ($%.4f used). Skipping.",
                settings.claude_daily_budget_usd, daily_cost,
            )
            return

        self._news.refresh_cache()
        contexts = [self._build_market_context(m) for m in flagged]
        opportunities, usage = self._analyzer.analyze_markets(contexts)

        if not usage:
            return

        claude_call_id = self._ledger.record_claude_call(
            model=settings.claude_model,
            markets_analyzed=usage.get("markets_analyzed", len(flagged)),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_write_tokens=usage.get("cache_write_tokens", 0),
            cache_read_tokens=usage.get("cache_read_tokens", 0),
            cost_usd=usage.get("cost_usd", 0.0),
        )

        for m in flagged:
            self._movement.mark_analyzed(m.get("ticker", ""))

        for opp in opportunities:
            opp.claude_call_id = claude_call_id
            opp_id = self._ledger.record_opportunity(
                market_ticker=opp.ticker,
                market_title=opp.title,
                market_price=opp.market_price,
                claude_probability=opp.claude_probability,
                edge=opp.edge,
                kelly_fraction=opp.kelly_result.half_kelly_fraction,
                bet_size_dollars=opp.kelly_result.bet_dollars,
                confidence=opp.confidence,
                direction=opp.direction,
                reasoning=opp.reasoning,
                key_factors=opp.key_factors,
                data_gaps=opp.data_gaps,
                claude_call_id=claude_call_id,
            )

            if settings.autopilot_mode:
                self._auto_execute(opp, opp_id)
            else:
                self._prompt_and_execute(opp, opp_id)

        elapsed = (datetime.now() - cycle_start).total_seconds()
        self._logger.info(
            "Cycle done: %d markets → %d flagged → %d opportunities | "
            "cost=$%.4f daily=$%.4f elapsed=%.1fs",
            len(all_markets), len(flagged), len(opportunities),
            usage.get("cost_usd", 0), daily_cost + usage.get("cost_usd", 0), elapsed,
        )

    def _build_market_context(self, market: dict) -> MarketContext:
        ticker = market.get("ticker", "")
        title = market.get("title", ticker)
        resolution = market.get("resolution_rules", market.get("subtitle", "Resolves per contract terms"))
        price = float(market.get("_yes_bid_normalized", 0.5))
        volume = float(market.get("_volume_dollars", 0))
        days = float(market.get("_days_to_resolution", 30))
        news = self._news.get_relevant_news(title, max_items=4)

        cross_yes = None
        cross_venue = None
        try:
            cross = self._crossmarket.check(ticker, price)
            if cross is not None:
                cross_yes = cross.polymarket_yes
                cross_venue = "Polymarket"
        except Exception as e:
            self._logger.debug("Cross-market check failed for %s: %s", ticker, e)

        return MarketContext(
            ticker=ticker, title=title, resolution_criteria=resolution,
            market_price=price, volume=volume, days_to_resolution=days, news_items=news,
            cross_market_yes=cross_yes, cross_market_venue=cross_venue,
        )

    def _compute_order_price(self, opp, yes_bid: float, yes_ask: float, days: float) -> tuple[float, float, bool]:
        """Return (limit_price, real_edge, is_aggressive) for the given opportunity."""
        spread = yes_ask - yes_bid

        if opp.direction == "yes":
            side_ask = yes_ask
            max_price = opp.claude_probability - settings.min_edge_to_execute
        else:
            side_ask = 1.0 - yes_bid   # no_ask
            max_price = (1.0 - opp.claude_probability) - settings.min_edge_to_execute

        go_aggressive = (
            days < settings.aggressive_within_days
            or spread <= settings.aggressive_spread_threshold
        )

        if go_aggressive:
            limit_price = side_ask
        else:
            # Passive: best price we'd pay, capped one cent below ask, expressed in whole cents
            passive_cents = min(int(max_price * 100), int(side_ask * 100) - 1)
            limit_price = passive_cents / 100.0

        if opp.direction == "yes":
            real_edge = opp.claude_probability - limit_price
        else:
            real_edge = (1.0 - opp.claude_probability) - limit_price

        return limit_price, real_edge, go_aggressive

    def _prompt_and_execute(self, opp, opportunity_id: int):
        days = None
        try:
            yes_bid, yes_ask, days = self._markets.get_bid_ask(opp.ticker)
            limit_price, real_edge, is_aggressive = self._compute_order_price(opp, yes_bid, yes_ask, days)
            order_type = "aggressive/ask" if is_aggressive else "passive limit"
            fill_line = f"${limit_price:.2f} ({order_type})  real edge={real_edge * 100:.1f}%  spread={int((yes_ask - yes_bid) * 100)}¢"
        except Exception:
            limit_price = opp.market_price
            real_edge = opp.edge
            fill_line = f"${limit_price:.2f} (stale scan price)"

        sep = "─" * 60
        print(f"\n{sep}")
        if days is not None and days <= 0:
            print(f"  WARNING: Market may already be closed (days_to_resolution={days:.2f})")
        print(f"  OPPORTUNITY: {opp.ticker}")
        print(f"  Direction  : {opp.direction.upper()}")
        print(f"  Claude p   : {opp.claude_probability * 100:.0f}%  vs  scan mid={opp.market_price * 100:.0f}%")
        print(f"  Order      : {fill_line}")
        print(f"  Bet size   : ${opp.kelly_result.bet_dollars:.0f}  (half-Kelly)")
        print(f"  Confidence : {opp.confidence}")
        print(f"  Reasoning  : {opp.reasoning}")
        if opp.key_factors:
            print(f"  Key factors: {', '.join(opp.key_factors)}")
        if opp.data_gaps:
            print(f"  Data gaps  : {', '.join(opp.data_gaps)}")
        print(sep)

        try:
            answer = input("  Place this trade? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Skipped.")
            return

        if answer == "y":
            self._execute(opp, opportunity_id, limit_price, real_edge)
        else:
            self._logger.info("Trade skipped by user: %s %s", opp.ticker, opp.direction.upper())
            print("  Skipped.\n")

    def _auto_execute(self, opp, opportunity_id: int):
        if opp.confidence == "low" or opp.kelly_result.bet_dollars <= 0:
            return
        try:
            yes_bid, yes_ask, days = self._markets.get_bid_ask(opp.ticker)
            limit_price, real_edge, is_aggressive = self._compute_order_price(opp, yes_bid, yes_ask, days)
        except Exception as e:
            self._logger.error("Could not fetch live prices for %s: %s", opp.ticker, e)
            return

        if days <= 0:
            self._logger.warning("Skipping %s — market already closed (days=%.2f)", opp.ticker, days)
            return

        if real_edge < settings.min_edge_to_execute:
            self._logger.info(
                "Spread killed edge on %s %s: limit=%.2f real_edge=%.1f%% (need %.1f%%)",
                opp.ticker, opp.direction.upper(),
                limit_price, real_edge * 100, settings.min_edge_to_execute * 100,
            )
            return

        self._execute(opp, opportunity_id, limit_price, real_edge)

    def _execute(self, opp, opportunity_id: int, limit_price: float, real_edge: float):
        from src.kalshi.client import KalshiAPIError
        try:
            contracts = max(1, int(opp.kelly_result.bet_dollars / limit_price))
            price_cents = round(limit_price * 100)
            self._markets.place_order(opp.ticker, opp.direction, contracts, price_cents)
            trade_id = self._ledger.record_trade(opportunity_id, opp.direction, contracts, limit_price)
            self._logger.info(
                "TRADE: %s %s x%d @ $%.2f real_edge=%.1f%% trade_id=%d",
                opp.ticker, opp.direction.upper(), contracts, limit_price, real_edge * 100, trade_id,
            )
            print(f"  Order placed: {contracts} contract(s) @ ${limit_price:.2f}\n")
        except KalshiAPIError as e:
            if e.status_code == 409 and "market_closed" in str(e):
                self._logger.warning("Market closed before order could be placed: %s", opp.ticker)
                print("  Skipped — market closed before order landed.\n")
            else:
                self._logger.error("Order placement failed for %s: %s", opp.ticker, e)
                print(f"  Order failed: {e}\n")
        except Exception as e:
            self._logger.error("Order placement failed for %s: %s", opp.ticker, e)
