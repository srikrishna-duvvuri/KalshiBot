#!/usr/bin/env python3
"""Settle a resolved Kalshi market and update calibration.

Usage:
  python scripts/settle.py --ticker FED-25BPS-MAY26 --result yes
  python scripts/settle.py --ticker FED-25BPS-MAY26 --result no --fee 12.50
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
from rich.console import Console
from rich.table import Table
from rich.prompt import Confirm
from rich import box

from config.settings import settings
from src.accounting.ledger import Ledger
from src.calibration.tracker import CalibrationTracker


def main():
    parser = argparse.ArgumentParser(description="Settle a resolved Kalshi market")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--result", required=True, choices=["yes", "no"])
    parser.add_argument("--fee", type=float, default=0.0, help="Total Kalshi fee charged ($)")
    args = parser.parse_args()

    console = Console()
    ledger = Ledger(settings.db_path)
    calibration = CalibrationTracker(settings.db_path)

    resolved_yes = args.result == "yes"
    open_trades = [t for t in ledger.get_open_trades() if t["market_ticker"] == args.ticker]
    cal_count = calibration.record_settlement(args.ticker, resolved_yes)

    if not open_trades:
        console.print(f"[yellow]No open trades for {args.ticker}[/yellow]")
        console.print(f"[dim]Calibration: {cal_count} prediction(s) updated[/dim]")
        return

    table = Table(title=f"Settling {args.ticker} → {args.result.upper()}", box=box.ROUNDED)
    table.add_column("Trade ID")
    table.add_column("Direction")
    table.add_column("Contracts")
    table.add_column("Entry")
    table.add_column("Projected P&L")

    for trade in open_trades:
        direction = trade["direction"]
        contracts = float(trade["contracts"])
        entry = float(trade["entry_price"])
        if direction == "yes":
            pnl = contracts * (1.0 - entry) if resolved_yes else -contracts * entry
        else:
            pnl = contracts * (1.0 - entry) if not resolved_yes else -contracts * entry
        color = "green" if pnl > 0 else "red"
        table.add_row(
            str(trade["id"]), direction.upper(),
            f"{contracts:.0f}", f"{entry * 100:.0f}¢",
            f"[{color}]${pnl:.2f}[/{color}]",
        )

    console.print(table)

    if not Confirm.ask(f"Settle {len(open_trades)} trade(s)?"):
        console.print("[dim]Cancelled.[/dim]")
        return

    fee_per_trade = args.fee / len(open_trades)
    total_net = 0.0

    for trade in open_trades:
        ledger.settle_trade(trade["id"], resolved_yes, kalshi_fee=fee_per_trade)
        direction = trade["direction"]
        contracts = float(trade["contracts"])
        entry = float(trade["entry_price"])
        if direction == "yes":
            gross = contracts * (1.0 - entry) if resolved_yes else -contracts * entry
        else:
            gross = contracts * (1.0 - entry) if not resolved_yes else -contracts * entry
        total_net += gross - fee_per_trade

    color = "green" if total_net > 0 else "red"
    console.print(f"\n[bold]Settlement complete[/bold]")
    console.print(f"  Trades settled:  {len(open_trades)}")
    console.print(f"  Kalshi fees:     ${args.fee:.2f}")
    console.print(f"  Net P&L:         [{color}][bold]${total_net:.2f}[/bold][/{color}]")
    console.print(f"  Calibration:     {cal_count} prediction(s) updated")


if __name__ == "__main__":
    main()
