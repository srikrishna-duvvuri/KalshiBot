#!/usr/bin/env python3
"""Retrospective backtest — what would the strategy have earned?

Replays all settled opportunities from the ledger. Opportunities are filtered
by min-edge and confidence floor; Brier score is computed across all settled
predictions (filtering changes what you'd have *traded*, not what Claude
*predicted*).

Usage:
  python scripts/backtest.py
  python scripts/backtest.py --days 30
  python scripts/backtest.py --min-edge 0.10
  python scripts/backtest.py --min-confidence medium
  python scripts/backtest.py --fee-rate 0.02    # model Kalshi's real fee curve at mid

Reminder: fills are simulated at opportunities.market_price (mid). Real live
trading pays the ask or a spread-aware limit. Treat these numbers as an
upper bound, not a forecast.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from config.settings import settings
from src.backtest.simulator import (
    EDGE_BUCKETS,
    fetch_settled_opportunities,
    simulate,
)


def main():
    parser = argparse.ArgumentParser(description="Retrospective backtest of recorded opportunities")
    parser.add_argument("--days", type=int, default=None,
                        help="Only consider opportunities from the last N days")
    parser.add_argument("--min-edge", type=float, default=settings.min_edge_to_surface,
                        help=f"Edge floor for simulated trades (default {settings.min_edge_to_surface})")
    parser.add_argument("--min-confidence", choices=["low", "medium", "high"], default="low",
                        help="Confidence floor (default: low — accept all)")
    parser.add_argument("--fee-rate", type=float, default=settings.kalshi_fee_rate,
                        help=f"Winning-side fee rate (default {settings.kalshi_fee_rate})")
    args = parser.parse_args()

    console = Console()

    rows = fetch_settled_opportunities(settings.db_path, since_days=args.days)
    if not rows:
        console.print()
        console.print(Panel(
            "[yellow]No settled opportunities in the ledger yet.[/yellow]\n\n"
            "Opportunities are created automatically when the scanner runs. They "
            "become [i]settled[/i] once their markets resolve and you've run\n"
            "[cyan]python scripts/settle.py --ticker <TICKER> --result yes|no[/cyan].\n\n"
            "Until a few markets have settled, there is nothing to backtest.",
            title="NO DATA",
            border_style="yellow",
        ))
        return

    result = simulate(
        rows,
        min_edge=args.min_edge,
        min_confidence=args.min_confidence,
        fee_rate=args.fee_rate,
    )

    period = f"Last {args.days} days" if args.days else "All time"
    console.print()
    console.rule(f"[bold cyan]BACKTEST — {period.upper()}[/bold cyan]")

    # Filters panel
    t_filters = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    t_filters.add_column("", style="dim"); t_filters.add_column("", justify="right")
    t_filters.add_row("Min edge", f"{args.min_edge * 100:.1f}%")
    t_filters.add_row("Min confidence", args.min_confidence)
    t_filters.add_row("Fee rate", f"{args.fee_rate * 100:.2f}%")
    t_filters.add_row("Opportunities seen", str(result.opportunities_considered))
    t_filters.add_row("Filtered out (edge/conf)", str(result.skipped_by_filter))
    t_filters.add_row("Skipped (no bet)", str(result.skipped_no_bet))
    t_filters.add_row("Trades simulated", str(len(result.trades)))
    console.print(Panel(t_filters, title="FILTERS", border_style="blue"))

    # Top-line P&L
    net = result.net_pnl
    net_color = "bright_green" if net > 0 else "bright_red" if net < 0 else "white"
    wr = result.win_rate
    wr_color = "green" if wr > 0.55 else "yellow" if wr >= 0.45 else "red"
    roi = result.roi

    t_pnl = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    t_pnl.add_column("", style="dim"); t_pnl.add_column("", justify="right")
    t_pnl.add_row("Wins / Losses", f"{result.wins} / {result.losses}")
    t_pnl.add_row("Win rate", f"[{wr_color}]{wr * 100:.1f}%[/{wr_color}]")
    t_pnl.add_row("Total staked", f"${result.total_staked:,.2f}")
    t_pnl.add_row("Gross P&L", f"{'+' if result.gross_pnl >= 0 else ''}${result.gross_pnl:,.2f}")
    t_pnl.add_row("Fees", f"-${result.total_fees:,.2f}")
    t_pnl.add_row("─" * 22, "─" * 12)
    t_pnl.add_row("[bold]Net P&L[/bold]",
                  f"[bold {net_color}]{'+' if net >= 0 else ''}${net:,.2f}[/bold {net_color}]")
    t_pnl.add_row("ROI on staked", f"[{net_color}]{roi * 100:+.1f}%[/{net_color}]")
    console.print(Panel(t_pnl, title="SIMULATED P&L", border_style=net_color))

    # Calibration
    if result.brier_score is not None:
        brier = result.brier_score
        brier_color = "green" if brier < 0.20 else "yellow" if brier <= 0.25 else "red"
        t_cal = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
        t_cal.add_column("", style="dim"); t_cal.add_column("", justify="right")
        t_cal.add_row("Brier score (all predictions)",
                      f"[{brier_color}]{brier:.3f}[/{brier_color}] [dim](random=0.250)[/dim]")
        console.print(Panel(t_cal, title="CALIBRATION", border_style=brier_color))

    # By confidence
    t_conf = Table(box=box.SIMPLE, padding=(0, 2))
    t_conf.add_column("Confidence", style="dim")
    t_conf.add_column("Trades", justify="right")
    t_conf.add_column("Win rate", justify="right")
    t_conf.add_column("Net P&L", justify="right")
    for level in ("high", "medium", "low"):
        b = result.by_confidence[level]
        if b.count == 0:
            continue
        pnl_color = "bright_green" if b.net_pnl > 0 else "bright_red" if b.net_pnl < 0 else "white"
        t_conf.add_row(
            level,
            str(b.count),
            f"{b.win_rate * 100:.1f}%",
            f"[{pnl_color}]{'+' if b.net_pnl >= 0 else ''}${b.net_pnl:,.2f}[/{pnl_color}]",
        )
    console.print(Panel(t_conf, title="BY CONFIDENCE", border_style="magenta"))

    # By edge bucket
    t_edge = Table(box=box.SIMPLE, padding=(0, 2))
    t_edge.add_column("Edge", style="dim")
    t_edge.add_column("Trades", justify="right")
    t_edge.add_column("Win rate", justify="right")
    t_edge.add_column("Net P&L", justify="right")
    for label, _, _ in EDGE_BUCKETS:
        b = result.by_edge_bucket[label]
        if b.count == 0:
            continue
        pnl_color = "bright_green" if b.net_pnl > 0 else "bright_red" if b.net_pnl < 0 else "white"
        t_edge.add_row(
            label,
            str(b.count),
            f"{b.win_rate * 100:.1f}%",
            f"[{pnl_color}]{'+' if b.net_pnl >= 0 else ''}${b.net_pnl:,.2f}[/{pnl_color}]",
        )
    console.print(Panel(t_edge, title="BY EDGE", border_style="cyan"))

    console.print()
    console.print(
        "[dim]Note: fills simulated at market mid. Real execution pays the ask; "
        "treat net as an upper bound.[/dim]"
    )
    console.print()


if __name__ == "__main__":
    main()
