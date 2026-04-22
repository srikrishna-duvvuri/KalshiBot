import json
import logging

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich import box

from config.settings import settings
from src.accounting.ledger import Ledger
from src.kalshi.markets import KalshiMarkets

logger = logging.getLogger("bot.executor")


class Executor:
    def __init__(self, ledger: Ledger, markets: KalshiMarkets):
        self._ledger = ledger
        self._markets = markets
        self._console = Console()

    def review_opportunities(self):
        opportunities = self._ledger.get_open_opportunities()
        if not opportunities:
            self._console.print("[yellow]No pending opportunities.[/yellow]")
            return

        self._console.print(f"\n[bold cyan]Found {len(opportunities)} pending opportunity(ies)[/bold cyan]\n")

        for opp in opportunities:
            self._display_opportunity(opp)
            choice = Prompt.ask(
                "[bold]Action[/bold]",
                choices=["a", "s", "r", "q"],
                default="s",
            ).lower()

            if choice == "q":
                self._console.print("[dim]Exiting review.[/dim]")
                break
            elif choice == "s":
                self._ledger.mark_opportunity_acted_on(opp["id"])
                self._console.print("[dim]Skipped.\n[/dim]")
            elif choice == "a":
                self._place_trade(opp, float(opp["bet_size_dollars"]))
            elif choice == "r":
                try:
                    new_size = float(Prompt.ask("New bet size ($)").replace("$", "").replace(",", ""))
                    if new_size > 0:
                        self._place_trade(opp, new_size)
                    else:
                        self._ledger.mark_opportunity_acted_on(opp["id"])
                except ValueError:
                    self._console.print("[red]Invalid size, skipping.[/red]")
                    self._ledger.mark_opportunity_acted_on(opp["id"])

    def _display_opportunity(self, opp: dict):
        edge = float(opp.get("edge", 0))
        direction = opp.get("direction", "yes").upper()
        confidence = opp.get("confidence", "low")
        edge_color = "bright_green" if edge >= 0.08 else "yellow"
        dir_color = "green" if direction == "YES" else "red"
        conf_color = {"high": "green", "medium": "yellow", "low": "red"}.get(confidence, "white")

        t = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        t.add_column("", style="dim", width=18)
        t.add_column("", min_width=45)

        t.add_row("Ticker", f"[bold]{opp.get('market_ticker', '')}[/bold]")
        t.add_row(
            "Probability",
            f"Claude: [bold]{float(opp.get('claude_probability', 0)) * 100:.0f}%[/bold]  "
            f"Market: {float(opp.get('market_price', 0)) * 100:.0f}%  "
            f"Edge: [{edge_color}]+{edge * 100:.1f}%[/{edge_color}]",
        )
        t.add_row("Direction", f"[{dir_color}][bold]{direction}[/bold][/{dir_color}]")
        t.add_row("Bet Size", f"[bold]${float(opp.get('bet_size_dollars', 0)):.2f}[/bold]")
        t.add_row("Confidence", f"[{conf_color}]{confidence}[/{conf_color}]")
        t.add_row("Reasoning", opp.get("reasoning", ""))

        for field_name, label in [("key_factors", "Key Factors"), ("data_gaps", "Data Gaps")]:
            raw = opp.get(field_name, "[]")
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except Exception:
                    raw = [raw]
            if raw:
                style = "[dim]" if field_name == "data_gaps" else ""
                end = "[/dim]" if field_name == "data_gaps" else ""
                t.add_row(
                    f"{style}{label}{end}",
                    "\n".join(f"{style}• {item}{end}" for item in raw),
                )

        self._console.print(Panel(
            t,
            title=f"[bold]{opp.get('market_title', opp.get('market_ticker', ''))}[/bold]",
            border_style=edge_color,
        ))
        self._console.print("[dim]  [A]ccept  [S]kip  [R]esize  [Q]uit[/dim]")

    def _place_trade(self, opp: dict, bet_dollars: float):
        ticker = opp.get("market_ticker", "")
        direction = opp.get("direction", "yes")
        recorded_price = float(opp.get("market_price", 0.5))

        try:
            live = self._markets.get_market(ticker)
            raw = live.get("yes_bid") or live.get("last_price") or recorded_price
            live_price = float(raw) / 100.0 if float(raw) > 1.0 else float(raw)
        except Exception:
            live_price = recorded_price

        if abs(live_price - recorded_price) > 0.02:
            self._console.print(
                f"[yellow]Price moved: {recorded_price * 100:.0f}% → {live_price * 100:.0f}%[/yellow]"
            )
            if not Confirm.ask("Proceed anyway?"):
                self._ledger.mark_opportunity_acted_on(opp["id"])
                self._console.print("[dim]Cancelled.\n[/dim]")
                return

        entry_price = live_price if direction == "yes" else (1.0 - live_price)
        contracts = max(1, int(bet_dollars / entry_price))
        price_cents = int(live_price * 100)

        self._console.print(
            f"\n[bold]Order:[/bold] {ticker} {direction.upper()} x{contracts} @ {price_cents}¢ (${bet_dollars:.2f})"
        )
        if not Confirm.ask("Confirm?"):
            self._ledger.mark_opportunity_acted_on(opp["id"])
            self._console.print("[dim]Cancelled.\n[/dim]")
            return

        try:
            self._markets.place_order(ticker, direction, contracts, price_cents)
            trade_id = self._ledger.record_trade(opp["id"], direction, contracts, entry_price)
            self._console.print(
                f"[bold green]Order placed | trade_id={trade_id} | "
                f"{contracts} contracts @ {entry_price * 100:.0f}¢[/bold green]\n"
            )
        except Exception as e:
            self._console.print(f"[bold red]Order failed: {e}[/bold red]\n")
            logger.error("Order failed for %s: %s", ticker, e)
