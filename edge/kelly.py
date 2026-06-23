"""
Kelly Criterion stake sizing.
"""
from __future__ import annotations


def kelly(prob: float, decimal_odds: float) -> float:
    """Full Kelly fraction f* = (b*p - q) / b."""
    b = decimal_odds - 1.0
    q = 1.0 - prob
    if b <= 0:
        return 0.0
    return max(0.0, (b * prob - q) / b)


def fractional_kelly(prob: float, decimal_odds: float,
                     fraction: float = 0.25) -> float:
    """Quarter-Kelly by default — appropriate for model uncertainty."""
    return kelly(prob, decimal_odds) * fraction


def stake_units(prob: float, decimal_odds: float,
                bankroll_units: float = 100.0,
                fraction: float = 0.25,
                max_units: float = 5.0) -> float:
    """Stake in betting units (capped at max_units)."""
    f = fractional_kelly(prob, decimal_odds, fraction)
    return min(f * bankroll_units, max_units)
