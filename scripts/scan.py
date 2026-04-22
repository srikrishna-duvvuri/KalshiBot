#!/usr/bin/env python3
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse


def main():
    parser = argparse.ArgumentParser(description="Kalshi bot scanner")
    parser.add_argument("--demo", action="store_true", help="Force demo API mode")
    parser.add_argument("--once", action="store_true", help="Run one scan cycle and exit")
    parser.add_argument("--autopilot", action="store_true", help="Enable autopilot mode")
    parser.add_argument("--bankroll", type=float, default=None,
                        help="Override bankroll (skips API balance fetch and interactive prompt)")
    parser.add_argument("--force-analyze", type=int, default=None, metavar="N",
                        help="Bypass movement detection: pull N raw markets and send straight to Claude")
    args = parser.parse_args()

    if args.demo:
        os.environ["KALSHI_USE_DEMO"] = "true"
    if args.autopilot:
        os.environ["AUTOPILOT_MODE"] = "true"

    from src.bot.scanner import Scanner
    scanner = Scanner(bankroll_override=args.bankroll)

    if args.force_analyze:
        scanner.force_analyze(n=args.force_analyze)
    elif args.once:
        scanner._scan_cycle()
    else:
        scanner.run()


if __name__ == "__main__":
    main()
