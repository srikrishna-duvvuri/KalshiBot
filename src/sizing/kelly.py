from dataclasses import dataclass
from config.settings import settings


@dataclass
class KellyResult:
    direction: str
    edge: float
    kelly_fraction: float
    half_kelly_fraction: float
    bet_dollars: float
    is_worthwhile: bool


def calculate_kelly(
    claude_p: float,
    market_price: float,
    bankroll: float,
    fee_rate: float = 0.07,
    max_position_pct: float = 0.05,
    min_edge: float = None,
) -> KellyResult:
    """
    Fee-adjusted half-Kelly sizing for Kalshi binary contracts.

    Fees reduce the net payout on winning trades, so we compute b
    (net odds) after subtracting the fee before applying Kelly.
    """
    if min_edge is None:
        min_edge = settings.min_edge_to_surface

    if claude_p > market_price:
        direction = "yes"
        p = claude_p
        net_payout_win = (1.0 - market_price) * (1.0 - fee_rate)
        net_loss = market_price
    elif claude_p < market_price:
        direction = "no"
        p = 1.0 - claude_p
        net_payout_win = market_price * (1.0 - fee_rate)
        net_loss = 1.0 - market_price
    else:
        return KellyResult("yes", 0.0, 0.0, 0.0, 0.0, False)

    if net_loss <= 0 or net_payout_win <= 0:
        return KellyResult(direction, 0.0, 0.0, 0.0, 0.0, False)

    b = net_payout_win / net_loss
    kelly_f = p - (1.0 - p) / b
    half_kelly_f = kelly_f / 2.0
    edge = abs(claude_p - market_price)
    is_worthwhile = half_kelly_f >= min_edge

    bet_dollars = 0.0
    if half_kelly_f > 0:
        bet_dollars = min(half_kelly_f * bankroll, max_position_pct * bankroll)

    return KellyResult(
        direction=direction,
        edge=edge,
        kelly_fraction=kelly_f,
        half_kelly_fraction=half_kelly_f,
        bet_dollars=bet_dollars,
        is_worthwhile=is_worthwhile,
    )


def expected_value(claude_p: float, market_price: float, fee_rate: float = 0.07) -> float:
    win_payout = (1.0 - market_price) * (1.0 - fee_rate)
    return claude_p * win_payout - (1.0 - claude_p) * market_price
