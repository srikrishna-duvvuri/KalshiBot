"""Retrospective backtest simulator.

Replays recorded opportunities against their settled outcomes and computes
what P&L the strategy *would* have generated if every opportunity had been
traded at its analysis-time market price.

Key assumptions (intentional simplifications, all directionally optimistic):

- Fills at mid-price (opportunities.market_price). Real live trading pays the
  ask or the spread-aware limit price; the roadmap's spread-aware-Kelly item
  tightens this.
- Flat fee model (settings.kalshi_fee_rate × winning payout). Kalshi's actual
  fee is price-dependent (~7% × price × (1−price) × contracts), generally
  smaller at price extremes. Conservative on purpose.
- No slippage, no partial fills, no order rejection, no market closure between
  analysis and fill.
- Contract count uses the same rule as live execution: max(1, bet_dollars / entry_price).

The simulator is pure: pass in `OpportunityRow` iterables, get back a
`BacktestResult`. The DB-fetching helper `fetch_settled_opportunities` is
here for ergonomics but is separable.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Optional


CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}

EDGE_BUCKETS = [
    ("5-8%",   0.05, 0.08),
    ("8-12%",  0.08, 0.12),
    ("12-20%", 0.12, 0.20),
    ("20%+",   0.20, float("inf")),
]


@dataclass(frozen=True)
class OpportunityRow:
    opportunity_id: int
    ticker: str
    created_at: str
    market_price: float       # YES price at time of analysis (0..1)
    claude_probability: float
    edge: float               # |claude_probability − market_price|
    bet_size_dollars: float
    confidence: str           # "low"/"medium"/"high"
    direction: str            # "yes"/"no"
    resolved_yes: int         # 0 or 1 — caller must filter to settled rows


@dataclass(frozen=True)
class BacktestTrade:
    opportunity_id: int
    ticker: str
    direction: str
    claude_probability: float
    market_price: float
    edge: float
    confidence: str
    bet_dollars: float
    contracts: int
    entry_price: float        # side-specific
    resolved_yes: int
    won: bool
    gross_pnl: float
    fee: float
    net_pnl: float


@dataclass
class Bucket:
    count: int = 0
    wins: int = 0
    gross_pnl: float = 0.0
    fees: float = 0.0
    net_pnl: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.count if self.count else 0.0


@dataclass
class BacktestResult:
    trades: list[BacktestTrade] = field(default_factory=list)
    opportunities_considered: int = 0
    skipped_by_filter: int = 0
    skipped_no_bet: int = 0
    brier_score: Optional[float] = None
    by_confidence: dict[str, Bucket] = field(default_factory=dict)
    by_edge_bucket: dict[str, Bucket] = field(default_factory=dict)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.won)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if not t.won)

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total else 0.0

    @property
    def total_staked(self) -> float:
        return sum(t.bet_dollars for t in self.trades)

    @property
    def gross_pnl(self) -> float:
        return sum(t.gross_pnl for t in self.trades)

    @property
    def total_fees(self) -> float:
        return sum(t.fee for t in self.trades)

    @property
    def net_pnl(self) -> float:
        return sum(t.net_pnl for t in self.trades)

    @property
    def roi(self) -> float:
        return self.net_pnl / self.total_staked if self.total_staked else 0.0


def simulate(
    rows: Iterable[OpportunityRow],
    *,
    min_edge: float = 0.0,
    min_confidence: str = "low",
    fee_rate: float = 0.07,
) -> BacktestResult:
    """Replay opportunities and compute counterfactual P&L.

    `min_edge` and `min_confidence` filter which opportunities would have been
    traded. Filtered-out rows still contribute to `brier_score` (Claude's
    calibration is measured across all predictions, not just acted-on ones).
    """
    result = BacktestResult()
    result.by_confidence = {level: Bucket() for level in ("low", "medium", "high")}
    result.by_edge_bucket = {label: Bucket() for label, _, _ in EDGE_BUCKETS}

    min_conf_rank = CONFIDENCE_ORDER.get(min_confidence, 0)
    brier_sum = 0.0
    brier_n = 0

    for r in rows:
        result.opportunities_considered += 1
        brier_sum += (r.claude_probability - r.resolved_yes) ** 2
        brier_n += 1

        if r.edge < min_edge:
            result.skipped_by_filter += 1
            continue
        if CONFIDENCE_ORDER.get(r.confidence, -1) < min_conf_rank:
            result.skipped_by_filter += 1
            continue
        if r.bet_size_dollars <= 0:
            result.skipped_no_bet += 1
            continue

        entry_price = r.market_price if r.direction == "yes" else 1.0 - r.market_price
        if entry_price <= 0 or entry_price >= 1:
            result.skipped_no_bet += 1
            continue

        contracts = max(1, int(r.bet_size_dollars / entry_price))
        won = (r.direction == "yes" and r.resolved_yes == 1) or (
            r.direction == "no" and r.resolved_yes == 0
        )

        if won:
            gross = contracts * (1.0 - entry_price)
            fee = gross * fee_rate
        else:
            gross = -contracts * entry_price
            fee = 0.0
        net = gross - fee

        trade = BacktestTrade(
            opportunity_id=r.opportunity_id,
            ticker=r.ticker,
            direction=r.direction,
            claude_probability=r.claude_probability,
            market_price=r.market_price,
            edge=r.edge,
            confidence=r.confidence,
            bet_dollars=r.bet_size_dollars,
            contracts=contracts,
            entry_price=entry_price,
            resolved_yes=r.resolved_yes,
            won=won,
            gross_pnl=gross,
            fee=fee,
            net_pnl=net,
        )
        result.trades.append(trade)

        _update_bucket(result.by_confidence.get(r.confidence), trade)
        for label, lo, hi in EDGE_BUCKETS:
            if lo <= r.edge < hi:
                _update_bucket(result.by_edge_bucket[label], trade)
                break

    result.brier_score = brier_sum / brier_n if brier_n else None
    return result


def _update_bucket(bucket: Optional[Bucket], trade: BacktestTrade) -> None:
    if bucket is None:
        return
    bucket.count += 1
    if trade.won:
        bucket.wins += 1
    bucket.gross_pnl += trade.gross_pnl
    bucket.fees += trade.fee
    bucket.net_pnl += trade.net_pnl


def fetch_settled_opportunities(
    db_path: str,
    since_days: Optional[int] = None,
) -> list[OpportunityRow]:
    """Load opportunities that have a settled calibration row.

    Returns an empty list if the DB or expected tables don't exist — the CLI
    treats that case as "no data yet" rather than an error.
    """
    import os
    if not os.path.exists(db_path):
        return []

    where = "c.resolved_yes IS NOT NULL"
    params: list = []
    if since_days is not None:
        cutoff = (datetime.now() - timedelta(days=since_days)).isoformat()
        where += " AND o.created_at >= ?"
        params.append(cutoff)

    sql = f"""
        SELECT o.id, o.market_ticker, o.created_at, o.market_price,
               o.claude_probability, o.edge, o.bet_size_dollars,
               o.confidence, o.direction, c.resolved_yes
        FROM opportunities o
        JOIN calibration c ON c.opportunity_id = o.id
        WHERE {where}
        ORDER BY o.created_at ASC
    """
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []

    return [
        OpportunityRow(
            opportunity_id=int(r["id"]),
            ticker=str(r["market_ticker"]),
            created_at=str(r["created_at"]),
            market_price=float(r["market_price"]),
            claude_probability=float(r["claude_probability"]),
            edge=float(r["edge"]),
            bet_size_dollars=float(r["bet_size_dollars"]),
            confidence=str(r["confidence"]),
            direction=str(r["direction"]),
            resolved_yes=int(r["resolved_yes"]),
        )
        for r in rows
    ]
