#!/usr/bin/env python3
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings
from src.accounting.ledger import Ledger
from src.kalshi.client import KalshiClient
from src.kalshi.markets import KalshiMarkets
from src.bot.executor import Executor


def main():
    ledger = Ledger(settings.db_path)
    client = KalshiClient()
    markets = KalshiMarkets(client)
    executor = Executor(ledger, markets)
    executor.review_opportunities()


if __name__ == "__main__":
    main()
