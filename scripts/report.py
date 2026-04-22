#!/usr/bin/env python3
"""Accounting report for the Kalshi bot.

Usage:
  python scripts/report.py           # all time
  python scripts/report.py --days 30
  python scripts/report.py --days 7
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich import box

from config.settings import settings
from src.accounting.ledger import Ledger
from src.calibration.tracker import CalibrationTracker


def main():
    parser = argparse.ArgumentParser(description="Kalshi bot accounting report")
    parser.add_argument("--days", type=int, default=None)
    args = parser.parse_args()

    console = Console()
    ledger = Ledger(settings.db_path)
    calibration = CalibrationTracker(settings.db_path)

    period = f"Last {args.days} days" if args.days else "All time"
    pnl = ledger.get_pnl_summary(days=args.days)
    claude = ledger.get_claude_cost_summary(days=args.days)
    cal = calibration.get_calibration_stats()

    console.print()
    console.rule(f"[bold cyan]KALSHI BOT — {period.upper()}[/bold cyan]")

    # Trades
    wr = pnl.get("win_rate", 0)
    wr_color = "green" if wr > 0.55 else "yellow" if wr >= 0.45 else "red"
    t1 = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    t1.add_column("", style="dim"); t1.add_column("", justify="right")
    t1.add_row("Total trades", str(pnl["total_trades"]))
    t1.add_row("Settled", str(pnl["settled_trades"]))
    t1.add_row("Open", str(pnl["open_trades"]))
    t1.add_row("Win rate", f"[{wr_color}]{wr * 100:.1f}%[/{wr_color}]")
    t1.add_row("Avg edge", f"{pnl['avg_edge'] * 100:.1f}%")
    t1.add_row("Avg bet", f"${pnl['avg_bet_size']:.2f}")
    console.print(Panel(t1, title="TRADES", border_style="blue"))

    # P&L
    gross = pnl["gross_pnl"]
    fees = pnl["total_kalshi_fees"]
    claude_cost = claude["total_cost"]
    net = pnl["net_pnl"]
    net_color = "bright_green" if net > 0 else "bright_red"

    t2 = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    t2.add_column("", style="dim"); t2.add_column("", justify="right")
    t2.add_row("Gross P&L", f"{'+'if gross>=0 else''}${gross:.2f}")
    t2.add_row("Kalshi fees", f"-${abs(fees):.2f}")
    t2.add_row("Claude API", f"-${claude_cost:.4f}")
    t2.add_row("─" * 22, "─" * 12)
    t2.add_row("[bold]Net profit[/bold]", f"[bold {net_color}]{'+'if net>=0 else''}${net:.2f}[/bold {net_color}]")
    console.print(Panel(t2, title="P&L SUMMARY", border_style=net_color))

    # Claude usage
    t3 = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    t3.add_column("", style="dim"); t3.add_column("", justify="right")
    t3.add_row("Calls", str(claude["total_calls"]))
    t3.add_row("Markets analyzed", str(claude["total_markets_analyzed"]))
    t3.add_row("Total tokens", f"{claude['total_tokens']:,}")
    t3.add_row("Cache hit rate", f"{claude['cache_hit_rate'] * 100:.1f}%")
    t3.add_row("Input cost", f"${claude['input_cost']:.4f}")
    t3.add_row("Output cost", f"${claude['output_cost']:.4f}")
    t3.add_row("Cache write", f"${claude['cache_write_cost']:.4f}")
    t3.add_row("Cache read", f"${claude['cache_read_cost']:.4f}")
    t3.add_row("[bold]Total[/bold]", f"[bold]${claude_cost:.4f}[/bold]")
    console.print(Panel(t3, title="CLAUDE USAGE", border_style="magenta"))

    # Calibration
    brier = cal.brier_score
    brier_color = "green" if brier < 0.20 else "yellow" if brier <= 0.25 else "red"
    t4 = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    t4.add_column("", style="dim"); t4.add_column("", justify="right")
    t4.add_row("Total predictions", str(cal.total_predictions))
    t4.add_row("Settled", str(cal.settled_predictions))
    t4.add_row("Brier score", f"[{brier_color}]{brier:.3f}[/{brier_color}] [dim](random=0.250)[/dim]")
    if cal.accuracy_by_bucket:
        t4.add_row("", "")
        for label, data in sorted(cal.accuracy_by_bucket.items()):
            t4.add_row(label, f"{data['predicted_avg']*100:.0f}% → {data['actual_rate']*100:.0f}% (n={data['count']})")
    console.print(Panel(t4, title="CALIBRATION", border_style=brier_color))
    console.print()


if __name__ == "__main__":
    main()
