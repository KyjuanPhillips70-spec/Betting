"""
Odds conversions and vig removal.
"""
from __future__ import annotations


def american_to_decimal(american: float) -> float:
    if american > 0:
        return 1.0 + american / 100.0
    return 1.0 + 100.0 / abs(american)


def decimal_to_american(decimal: float) -> float:
    if decimal >= 2.0:
        return (decimal - 1.0) * 100.0
    return -100.0 / (decimal - 1.0)


def american_to_implied(american: float) -> float:
    """Raw implied probability (includes vig)."""
    if american > 0:
        return 100.0 / (american + 100.0)
    return abs(american) / (abs(american) + 100.0)


def decimal_to_implied(decimal: float) -> float:
    return 1.0 / decimal


def devig_two_way(price_a: float, price_b: float,
                  fmt: str = "american") -> tuple[float, float]:
    """Remove vig from a two-outcome market. Returns (fair_prob_a, fair_prob_b)."""
    fn = american_to_implied if fmt == "american" else decimal_to_implied
    ra, rb = fn(price_a), fn(price_b)
    total = ra + rb
    return ra / total, rb / total


def devig_multi_way(prices: list[float], fmt: str = "american") -> list[float]:
    """Remove vig from a 3+ outcome market (e.g. 1X2)."""
    fn = american_to_implied if fmt == "american" else decimal_to_implied
    raws = [fn(p) for p in prices]
    total = sum(raws)
    return [r / total for r in raws]


def expected_value(model_prob: float, decimal_odds: float) -> float:
    """EV = p*(d-1) - (1-p). Positive = profitable in expectation."""
    return model_prob * (decimal_odds - 1.0) - (1.0 - model_prob)


def overround(prices: list[float], fmt: str = "american") -> float:
    """Book's overround = sum(implied probs) - 1."""
    fn = american_to_implied if fmt == "american" else decimal_to_implied
    return sum(fn(p) for p in prices) - 1.0


def best_line(prices: list[tuple[str, float]]) -> tuple[str, float]:
    """Pick the best American price from [(book, american_odds)] pairs."""
    return max(prices, key=lambda x: american_to_decimal(x[1]))
